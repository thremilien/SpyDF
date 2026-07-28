"""FastAPI app: routes for opening, rendering, redacting and downloading PDFs."""

import contextlib
import logging
import math
import mimetypes
import os
import re
import time
import unicodedata
import uuid
from pathlib import Path

import fitz
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from src.config import (
    LEAK_COVERAGE,
    LOG_FILENAMES_VAR,
    MASK_MAX_PX,
    MAX_SESSIONS,
    MAX_STRIPS,
    MAX_UPLOAD_BYTES,
    MAX_ZOOM,
    MIN_ZOOM,
    MOSAIC_BLOCKS,
    RECOMPRESS_QUALITY,
    RENDER_ZOOM,
    SESSION_TTL,
    SID_LOG_LEN,
    STRIP_HEIGHT,
    UA_MAX_LEN,
    WATERMARK_DIAGONAL_RATIO,
    WATERMARK_FONT,
    WATERMARK_MAX_LEN,
    WATERMARK_MIN_SIZE,
    env_flag,
)
from src.imagemeta import strip_image_metadata
from src.logs import log_event
from src.probe import STRUCT_TEXT_KEYS, inspect_document

PACKAGE_DIR = Path(__file__).parent

RGB_MAX = 255.0  # the client sends 0-255 channels, PyMuPDF wants 0-1
WHITE = (1.0, 1.0, 1.0)

# On some systems .js is guessed as application/javascript, which gets no
# charset, and the JS accents then reach the UI broken.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

app = FastAPI()
app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

DOCS: dict[str, dict] = {}  # sid -> {"bytes": ..., "name": ..., "ts": ...}


def _sweep():
    """Drop expired and surplus sessions.

    Nothing is persisted, but a held PDF keeps in RAM everything just removed
    from it, so forgotten sessions are not left lying around.
    """
    now = time.time()
    for k in [k for k, v in DOCS.items() if now - v["ts"] > SESSION_TTL]:
        DOCS.pop(k, None)
    while len(DOCS) > MAX_SESSIONS:
        DOCS.pop(min(DOCS, key=lambda k: DOCS[k]["ts"]), None)


# Stores a document under a fresh session id and returns it.
def _put(name: str, data: bytes) -> str:
    _sweep()
    key = uuid.uuid4().hex
    DOCS[key] = {"bytes": data, "name": name, "ts": time.time()}
    return key


# Fetches a live session, or raises 404.
def _get(key: str) -> dict:
    _sweep()
    entry = DOCS.get(key)
    if not entry:
        raise HTTPException(404, "unknown or expired session")
    return entry


def _safe_filename(name: str) -> str:
    """Sanitise an uploaded file name.

    It reaches us from the dropped file, so it must neither break the
    Content-Disposition header nor carry a path.

    Args:
        name: The name as sent by the browser.

    Returns:
        An ASCII name, at most 100 characters, never empty.
    """
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", os.path.basename(name)).strip(" .")
    return name[:100] or "document.pdf"


# The only part of a session id that may reach a log line.
def _sid_prefix(sid: str) -> str:
    return (sid or "")[:SID_LOG_LEN]


def _log_ip_fields(request: Request) -> dict:
    """Build the address fields of a log line.

    `request.client.host` is the real TCP peer. X-Forwarded-For is supplied by
    the client (or by the Dokploy/Traefik reverse proxy in front) and is
    therefore untrusted on its own: it is logged as a separate field, never
    substituted for the peer address.

    Args:
        request: The incoming request.

    Returns:
        {"ip": ...} plus {"xff": ...} when the header is present.
    """
    fields = {"ip": request.client.host if request.client else "?"}
    xff = request.headers.get("x-forwarded-for")
    if xff:
        fields["xff"] = xff.split(",")[0].strip()
    return fields


def _page_geometry(doc) -> list[dict]:
    """The rectangle of every page, which is what the client lays its zones on."""
    return [{"w": p.rect.width, "h": p.rect.height, "x0": p.rect.x0, "y0": p.rect.y0} for p in doc]


@app.get("/api/session/{sid}")
def api_session(sid: str):
    """Report a still-open session, so a reloaded page can pick it up again.

    A browser reload loses the session id and the page geometry, but not the
    document: that is held here until it expires. The client keeps only the
    marking it drew and asks this route whether the document behind it is still
    there — an expired or unknown session answers 404, and the marking goes with
    it rather than being drawn over nothing.

    Args:
        sid: Session id.

    Returns:
        The same shape as `/api/open`: {"sid", "name", "pages"}.

    Raises:
        HTTPException: 404 for an unknown or expired session.
    """
    entry = _get(sid)
    doc = fitz.open(stream=entry["bytes"], filetype="pdf")
    pages = _page_geometry(doc)
    doc.close()
    return {"sid": sid, "name": entry["name"], "pages": pages}


@app.post("/api/open")
async def api_open(request: Request, file: UploadFile = File(...)):
    """Open a PDF and hold it in memory under a fresh session id.

    Args:
        request: The incoming request, read for its address fields.
        file: The uploaded PDF.

    Returns:
        {"sid": session id, "name": sanitised name, "pages": page geometry}.

    Raises:
        HTTPException: 400 empty, unreadable or password-protected, 413 too big.
    """
    start = time.perf_counter()
    ip_fields = _log_ip_fields(request)
    data = await file.read()
    if not data:
        log_event("import_rejected", level=logging.WARNING, reason="empty", **ip_fields)
        raise HTTPException(400, "empty file")
    if len(data) > MAX_UPLOAD_BYTES:
        log_event(
            "import_rejected",
            level=logging.WARNING,
            reason="too_large",
            size=len(data),
            **ip_fields,
        )
        raise HTTPException(413, "file too large (200 MB maximum)")
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        # An encrypted PDF opens, but its pages are unreadable: without the
        # password nothing could be rendered or redacted.
        if doc.needs_pass:
            doc.close()
            log_event(
                "import_rejected", level=logging.WARNING, reason="password_protected", **ip_fields
            )
            raise HTTPException(400, "password-protected PDF")
        pages = _page_geometry(doc)
        doc.close()
    except HTTPException:
        raise
    except Exception as e:
        # The exception text can in principle quote file content, so it is not
        # logged — and neither is the file name.
        log_event("import_rejected", level=logging.WARNING, reason="unreadable", **ip_fields)
        raise HTTPException(400, f"unreadable PDF: {e}") from e

    name = _safe_filename(file.filename or "document.pdf")
    sid = _put(name, data)

    # Privacy rule: an uploaded file name is potentially identifying
    # ("copie_jean_dupont.pdf"), so it is logged only when the operator asked
    # for it with SPYDF_LOG_FILENAMES=1. Do not "improve" this by dropping the
    # condition.
    fields = {
        "sid": _sid_prefix(sid),
        "size": len(data),
        "pages": len(pages),
        **ip_fields,
        "ms": round((time.perf_counter() - start) * 1000),
    }
    if env_flag(LOG_FILENAMES_VAR):
        fields["filename"] = name
    log_event("import", **fields)

    return {"sid": sid, "name": name, "pages": pages}


@app.get("/api/page/{sid}/{n}")
def api_page(sid: str, n: int, w: int = 0):
    """Render one page to PNG at the resolution the screen actually shows.

    A PDF is vector art: there is no "native quality", a resolution has to be
    picked. Rendering exactly what the screen displays beats a fixed zoom, which
    would be either blurry or wasteful.

    Args:
        sid: Session id.
        n: Zero-based page number.
        w: Wanted width in real screen pixels (CSS x devicePixelRatio). Zero
            falls back to RENDER_ZOOM.

    Returns:
        The PNG, marked no-store.

    Raises:
        HTTPException: 404 for an unknown session or an out-of-range page.
    """
    entry = _get(sid)
    doc = fitz.open(stream=entry["bytes"], filetype="pdf")
    if not 0 <= n < len(doc):
        doc.close()
        raise HTTPException(404, "page out of range")
    page = doc[n]
    if w > 0 and page.rect.width:
        zoom = min(max(w / page.rect.width, MIN_ZOOM), MAX_ZOOM)
    else:
        zoom = RENDER_ZOOM
    pm = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    png = pm.tobytes("png")
    doc.close()
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/inspect/{sid}")
def api_inspect(sid: str):
    """Report everything the document carries without displaying it.

    Text layer, metadata, bookmarks, annotations, fields, attachments, layers,
    links, JavaScript. Read-only; the export decides what actually disappears.

    Args:
        sid: Session id.

    Returns:
        The `src.probe.inspect_document` payload, as JSON.
    """
    entry = _get(sid)
    return JSONResponse(inspect_document(entry["bytes"]))


# A rectangular zone has nothing to cut up: its outline *is* its bounding box.
def _is_box(points, rect) -> bool:
    return len(points) == 4 and all(
        (abs(p.x - rect.x0) < 0.01 or abs(p.x - rect.x1) < 0.01)
        and (abs(p.y - rect.y0) < 0.01 or abs(p.y - rect.y1) < 0.01)
        for p in points
    )


def _spans(points, y):
    """Horizontal intervals lying inside the outline at ordinate y.

    Uses the non-zero winding rule (not even-odd), which is what the white cover
    laid down by PyMuPDF and the browser's SVG preview already apply. A freehand
    stroke crossing itself is therefore solid here as it is on screen.

    Args:
        points: The outline vertices.
        y: The scan line.

    Returns:
        (start, end) pairs, left to right.
    """
    xs = []
    for i, a in enumerate(points):
        b = points[(i + 1) % len(points)]
        if a.y == b.y:
            continue
        top, bot = (a, b) if a.y < b.y else (b, a)
        if not top.y <= y < bot.y:
            continue
        xs.append((a.x + (y - a.y) * (b.x - a.x) / (b.y - a.y), 1 if b.y > a.y else -1))
    xs.sort()
    out, wind, start = [], 0, 0.0
    for x, direction in xs:
        if wind == 0:
            start = x
        wind += direction
        if wind == 0 and x > start:
            out.append((start, x))
    return out


# Merges overlapping intervals into the fewest disjoint ones.
def _merge(spans):
    out = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _zone_rects(points, rect):
    """Cut a zone into the rectangles to redact.

    PyMuPDF can only redact rectangles. Redacting a polygon's bounding box does
    not merely delete too much text: on a scanned exam the page is an image, so
    PDF_REDACT_IMAGE_PIXELS destroys every pixel of that box and the whole
    rectangle turns white under a cover that did follow the outline. The zone is
    therefore cut into horizontal strips hugging the stroke.

    Strips overrun the outline slightly (the union of each strip's three scan
    lines): over-erasing a little is acceptable, leaving content alive inside the
    stroke is not.

    Args:
        points: The outline vertices.
        rect: Their bounding box.

    Returns:
        The rectangles to redact; the bounding box itself for a box zone, or for
        a degenerate outline with no area.
    """
    if _is_box(points, rect):
        return [rect]
    n = max(1, min(MAX_STRIPS, int(rect.height / STRIP_HEIGHT) + 1))
    h = rect.height / n
    out = []
    for i in range(n):
        y0 = rect.y0 + i * h
        y1 = y0 + h
        sampled = [s for y in (y0, y0 + h / 2, min(y1, rect.y1 - 1e-6)) for s in _spans(points, y)]
        for a, b in _merge(sampled):
            if b - a > 0.05:
                out.append(fitz.Rect(a, y0, b, y1))
    # degenerate outline (no area): its rectangle beats nothing at all
    return out or [rect]


def _shape_mask(points, rect):
    """Build the alpha mask of an outline, sized to its bounding box.

    Laid over the mosaic, it keeps the mosaic from spilling outside the stroke.

    Args:
        points: The outline vertices.
        rect: Their bounding box.

    Returns:
        A greyscale pixmap, or None for an empty rectangle.
    """
    if rect.width <= 0 or rect.height <= 0:
        return None
    s = min(MASK_MAX_PX / max(rect.width, rect.height), 4.0)
    w = max(1, min(MASK_MAX_PX, round(rect.width * s)))
    h = max(1, min(MASK_MAX_PX, round(rect.height * s)))
    buf = bytearray(w * h)
    for j in range(h):
        y = rect.y0 + (j + 0.5) * rect.height / h
        for a, b in _spans(points, y):
            i0 = max(0, int((a - rect.x0) / rect.width * w))
            i1 = min(w, int((b - rect.x0) / rect.width * w) + 1)
            if i1 > i0:
                buf[j * w + i0 : j * w + i1] = b"\xff" * (i1 - i0)
    return fitz.Pixmap(fitz.csGRAY, w, h, bytes(buf), False)


def _mosaic_pixmap(page, rect):
    """Render a zone very small, to be laid back at full size.

    Downsampled that hard, the result is an unreadable mosaic of the original
    content.

    Args:
        page: The page to sample, before any redaction.
        rect: The zone's bounding box.

    Returns:
        The tiny pixmap, or None if it could not be rendered.
    """
    w, h = max(rect.width, 1.0), max(rect.height, 1.0)
    s = MOSAIC_BLOCKS / max(w, h)
    try:
        pm = page.get_pixmap(matrix=fitz.Matrix(s, s), clip=rect, alpha=False)
    except Exception:
        return None
    return pm if pm.width and pm.height else None


def _recompress_images(doc):
    """Re-encode as JPEG the images `apply_redactions` rewrote losslessly.

    PDF_REDACT_IMAGE_PIXELS decodes every image a zone touches, blanks the
    covered pixels and writes it back Flate-compressed. On a scan that turns a
    JPEG page into a lossless bitmap, five to six times heavier — a 7 MB set of
    scans exports at 40 MB. Doing it after the fact is safe: those pixels are
    already destroyed, so the lossy pass has nothing left to give back.

    Args:
        doc: The document, after redaction and before saving.
    """
    if RECOMPRESS_QUALITY <= 0:
        return
    # Suppressed: on an older PyMuPDF, or an image MuPDF declines to re-encode,
    # a heavy export beats no export.
    with contextlib.suppress(Exception):
        # lossy=False leaves an untouched JPEG on its original bytes rather
        # than costing it a second generation; bitonal=False keeps a 1-bit fax
        # scan bitonal, which JPEG would both grey and inflate. dpi_threshold
        # stays unset: this recompresses, it never downsamples.
        doc.rewrite_images(quality=RECOMPRESS_QUALITY, lossy=False, bitonal=False)


def _zone_color(raw) -> tuple[float, float, float]:
    """Read the colour a zone's cover is painted in, sent as three 0-255 channels.

    On a coloured scan a white patch is itself a mark — it says where something
    was. The client samples the paper along the drawn outline and sends that.

    Args:
        raw: The value received, of any type.

    Returns:
        The three channels as 0-1 floats; white for anything unusable, which is
        what the cover was before it had a colour.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        return WHITE
    out = []
    for c in raw:
        if isinstance(c, bool) or not isinstance(c, (int, float)):
            return WHITE
        out.append(min(RGB_MAX, max(0.0, float(c))) / RGB_MAX)
    return (out[0], out[1], out[2])


def _purge_annots(page, rects):
    """Delete every annotation and form field touching one of the rects.

    apply_redactions leaves both alone: a marker's note keeps its author name
    and a field keeps its value even when a zone covers them entirely.

    Args:
        page: The page to purge.
        rects: The rectangles about to be redacted.
    """
    for a in list(page.annots()):
        if a.type[0] == fitz.PDF_ANNOT_REDACT:
            continue
        if any(a.rect.intersects(r) for r in rects):
            page.delete_annot(a)
    for w in list(page.widgets()):
        if any(w.rect.intersects(r) for r in rects):
            page.delete_widget(w)


# A layer name ("Jean Dupont's copy") survives in /OCProperties even once its
# content has been redacted.
def _rename_layers(doc):
    for i, xref in enumerate(doc.get_ocgs() or {}, 1):
        doc.xref_set_key(xref, "Name", fitz.get_pdf_str(f"layer {i}"))


# /Alt, /ActualText and /E hang off the structure tree, not off the page:
# redaction never reaches them, and on a scanned page they are often the only
# text the file carries. Emptied rather than deleted, so a tagged document stays
# structurally valid.
def _strip_struct_text(doc):
    for xref in range(1, doc.xref_length()):
        with contextlib.suppress(Exception):
            if doc.xref_get_key(xref, "Type")[1] != "/StructElem":
                continue
            for key, _ in STRUCT_TEXT_KEYS:
                if doc.xref_get_key(xref, key)[0] == "string":
                    doc.xref_set_key(xref, key, fitz.get_pdf_str(""))


def _scrub_document(doc):
    """Strip the identifying traces that do not live in the page content.

    Redaction leaves these untouched: metadata, XMP, bookmarks (often the
    student's name), attachments, JavaScript, links, form answers, the text
    the structure tree carries for a page, and the Exif an image carries inside
    its own stream. Each step is best-effort; one unsupported by the file must
    not fail the export.

    Args:
        doc: The document to scrub, in place.
    """
    doc.set_metadata({})
    for step in (
        lambda: doc.del_xml_metadata(),
        lambda: doc.set_toc([]),
        lambda: _rename_layers(doc),
        lambda: _strip_struct_text(doc),
        lambda: strip_image_metadata(doc),
        lambda: doc.scrub(redactions=False, clean_pages=False),
    ):
        with contextlib.suppress(Exception):
            step()


# Base-14 Helvetica only encodes Latin-1: an em dash or a typographic
# apostrophe comes out as a stray glyph. Accented characters are in Latin-1 and
# pass through untouched.
_WATERMARK_FOLD = str.maketrans(
    {
        "–": "-",
        "—": "-",
        "−": "-",
        "‑": "-",
        "‘": "'",
        "’": "'",
        "′": "'",
        "“": '"',
        "”": '"',
        "…": "...",
    }
)


def _normalize_watermark(raw) -> str:
    """Clean the watermark supplied by the client.

    No newline (it would break the single-line layout), no control character,
    bounded length, nothing outside Latin-1.

    Args:
        raw: The value received, of any type.

    Returns:
        The cleaned text; empty means "no watermark".
    """
    if not isinstance(raw, str):
        return ""
    text = "".join(c if c.isprintable() else " " for c in raw)
    text = text.translate(_WATERMARK_FOLD)
    # whatever is left outside Latin-1 (emoji, non-Latin scripts) has no glyph
    # in the font: fold it to ASCII, or drop it.
    text = "".join(
        c
        if c.isascii() or _in_latin1(c)
        else unicodedata.normalize("NFKD", c).encode("ascii", "ignore").decode()
        for c in text
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text[:WATERMARK_MAX_LEN]


# Whether a character has a glyph in the base-14 font.
def _in_latin1(c: str) -> bool:
    try:
        c.encode("latin-1")
        return True
    except UnicodeEncodeError:
        return False


_WATERMARK_FONT_METRICS = fitz.Font(WATERMARK_FONT)
# line height in multiples of the font size, ascender top to descender bottom.
_WATERMARK_LINE_HEIGHT = _WATERMARK_FONT_METRICS.ascender - _WATERMARK_FONT_METRICS.descender


def _watermark_fit_size(w0: float, rect) -> float:
    """Largest font size whose rotated text box still fits inside the page.

    The result is proportional to the page: doubling its dimensions doubles the
    font size, so the rendering is identical at any scale. This is the only cap
    on the watermark size — an absolute one in points would make the same
    watermark span 60% of an A4's diagonal and 14% of a scan whose MediaBox is
    in pixels.

    Args:
        w0: Width of the text at font size 1.
        rect: The page rectangle.

    Returns:
        The maximum font size, in points.
    """
    denom_w = w0 + _WATERMARK_LINE_HEIGHT * rect.height / rect.width
    denom_h = w0 + _WATERMARK_LINE_HEIGHT * rect.width / rect.height
    return math.hypot(rect.width, rect.height) / max(denom_w, denom_h) * 0.97


def _apply_watermark(data: bytes, text: str) -> bytes:
    """Stamp `text` diagonally across every page, bottom-left to top-right.

    Args:
        data: The exported PDF bytes.
        text: The normalised watermark.

    Returns:
        The stamped PDF, or `data` unchanged on any failure — an export without
        its watermark beats an export the user never gets.
    """
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        try:
            for page in doc:
                rect = page.rect
                if rect.width <= 0 or rect.height <= 0:
                    continue
                center = fitz.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
                diag = math.hypot(rect.width, rect.height)
                angle = math.degrees(math.atan2(rect.height, rect.width))

                stamp = text
                try:
                    w0 = fitz.get_text_length(stamp, fontname=WATERMARK_FONT, fontsize=1)
                except Exception:
                    # character outside Latin-1: fall back to an ASCII version
                    # rather than give up on the watermark.
                    stamp = unicodedata.normalize("NFKD", stamp).encode("ascii", "ignore").decode()
                    if not stamp:
                        continue
                    w0 = fitz.get_text_length(stamp, fontname=WATERMARK_FONT, fontsize=1)
                if w0 <= 0:
                    continue

                fontsize = diag * WATERMARK_DIAGONAL_RATIO / w0

                # the text has a typographic height too, so its rotated box can
                # overrun the page even when its width does not.
                fit_cap = _watermark_fit_size(w0, rect)
                fontsize = min(fontsize, fit_cap)

                # the floor must not reopen the door to overflow: on a tiny
                # page, fitting inside it wins.
                fontsize = min(max(fontsize, WATERMARK_MIN_SIZE), fit_cap)

                tl = fitz.get_text_length(stamp, fontname=WATERMARK_FONT, fontsize=fontsize)
                # centre the text on the pivot: after rotating about that same
                # point it stays centred on the page at any angle.
                vert_off = (
                    (_WATERMARK_FONT_METRICS.ascender + _WATERMARK_FONT_METRICS.descender)
                    / 2
                    * fontsize
                )
                origin = fitz.Point(center.x - tl / 2, center.y + vert_off)

                try:
                    page.insert_text(
                        origin,
                        stamp,
                        fontsize=fontsize,
                        fontname=WATERMARK_FONT,
                        color=(0.5, 0.5, 0.5),
                        fill_opacity=0.2,
                        morph=(center, fitz.Matrix(angle)),
                        overlay=True,
                    )
                except Exception:
                    continue
            out = doc.tobytes(garbage=4, deflate=True, clean=True)
        finally:
            doc.close()
        return out
    except Exception:
        return data


# Share of `rect` covered by the redacted strips. Strips never overlap (one per
# scan line), so their areas simply add up.
def _covered_fraction(rect, rects) -> float:
    area = rect.get_area()
    if area <= 0:
        return 0.0
    return sum((rect & r).get_area() for r in rects) / area


def _verify(out: bytes, zones_by_page, page_map):
    """Re-read the PDF actually produced and look for survivors inside the zones.

    Reads the output bytes, not the in-memory document. A word is reported only
    when the zone really bit into it: now that redaction follows the drawn
    outline rather than the bounding box, a word grazing the stroke by tenths of
    a point is routine, and reporting it would drown real leaks in noise.

    Args:
        out: The exported PDF bytes.
        zones_by_page: Parsed zones, keyed by original page number.
        page_map: Original page number -> page number in the export.

    Returns:
        One entry per survivor: {"page", "kind", "text"}.
    """
    leaks = []
    chk = fitz.open(stream=out, filetype="pdf")
    try:
        for pno, zs in zones_by_page.items():
            new_no = page_map.get(pno)
            if new_no is None or not 0 <= new_no < chk.page_count:
                continue
            page = chk[new_no]
            # les memes bandes que celles redigees: confronter le texte au
            # rectangle englobant signalerait comme fuite ce qui est reste
            # volontairement intact hors du trace.
            rects = [r for z in zs for r in z["rects"]]
            for w in page.get_text("words"):
                if _covered_fraction(fitz.Rect(w[:4]), rects) >= LEAK_COVERAGE:
                    leaks.append({"page": new_no + 1, "kind": "text", "text": w[4]})
            for a in page.annots():
                if any(a.rect.intersects(r) for r in rects):
                    leaks.append(
                        {
                            "page": new_no + 1,
                            "kind": "annotation",
                            "text": a.info.get("title") or a.type[1],
                        }
                    )
            for w in page.widgets():
                if any(w.rect.intersects(r) for r in rects):
                    leaks.append({"page": new_no + 1, "kind": "field", "text": w.field_name or "?"})
    finally:
        chk.close()
    return leaks


@app.post("/api/export")
async def api_export(request: Request, payload: dict):
    """Redact the session document and hold the result for download.

    Zones arrive as {"3": [{"points": [[x, y], ...], "mode": "delete",
    "color": [r, g, b]}, ...]} in
    PDF coordinates, "points" being the outline of the zone (a rectangle is 4
    corners, but a polygon or freehand stroke works too). Everything follows that
    outline: redaction through the strips of `_zone_rects`, the white cover, and
    the mosaic through its mask. Nothing outside the stroke is ever erased or
    covered.

    Args:
        request: The incoming request.
        payload: {"sid", "zones", "deleted_pages", "strip_meta", "watermark"}.

    Returns:
        {"download", "filename", "leaks", "leak_count"} — `leaks` is capped at 20
        entries, `leak_count` is the real total.

    Raises:
        HTTPException: 404 unknown session, 400 with nothing to do or when every
            page would be deleted.
    """
    start = time.perf_counter()
    sid_prefix = _sid_prefix(payload.get("sid") or "")
    try:
        entry = _get(payload.get("sid") or "")
    except HTTPException:
        log_event(
            "export_rejected", level=logging.WARNING, reason="unknown_session", sid=sid_prefix
        )
        raise

    raw = payload.get("zones") or {}
    zones_by_page: dict[int, list[dict]] = {}
    for k, v in raw.items():
        if not v:
            continue
        parsed = []
        for z in v:
            pts = z.get("points") or []
            if len(pts) < 2:
                continue
            points = [fitz.Point(x, y) for x, y in pts]
            rect = fitz.Rect(points[0], points[0])
            for p in points:
                rect.include_point(p)
            parsed.append(
                {
                    "points": points,
                    "rect": rect,
                    "box": _is_box(points, rect),
                    "rects": _zone_rects(points, rect),
                    "mode": "pixelate" if z.get("mode") == "pixelate" else "delete",
                    "color": _zone_color(z.get("color")),
                }
            )
        if parsed:
            zones_by_page[int(k)] = parsed

    deleted_pages = {int(p) for p in (payload.get("deleted_pages") or [])}
    # no need to redact a page that is about to disappear entirely
    zones_by_page = {p: zs for p, zs in zones_by_page.items() if p not in deleted_pages}

    watermark = _normalize_watermark(payload.get("watermark"))

    if not zones_by_page and not deleted_pages and not watermark:
        log_event("export_rejected", level=logging.WARNING, reason="nothing_to_do", sid=sid_prefix)
        raise HTTPException(400, "no zone, deleted page or watermark")

    strip_meta = bool(payload.get("strip_meta", True))

    doc = fitz.open(stream=entry["bytes"], filetype="pdf")

    if len(deleted_pages) >= doc.page_count:
        doc.close()
        log_event(
            "export_rejected", level=logging.WARNING, reason="all_pages_deleted", sid=sid_prefix
        )
        raise HTTPException(400, "cannot delete every page")

    for pno, zs in zones_by_page.items():
        page = doc[pno]
        delete_zs = [z for z in zs if z["mode"] == "delete"]
        pixel_zs = [z for z in zs if z["mode"] == "pixelate"]

        # 1. for "pixelate" zones, first capture a heavily downsampled version
        # of the original content, before any redaction: that is the mosaic laid
        # back down in step 4.
        mosaics = []
        for z in pixel_zs:
            pm = _mosaic_pixmap(page, z["rect"])
            if pm:
                mosaics.append((z, pm))

        # 2. real redaction of EVERY zone: text is deleted, covered image
        # pixels are destroyed rather than masked, and line art touching a zone
        # is removed. LINE_ART_REMOVE_IF_TOUCHED is essential: by default
        # PyMuPDF only drops a stroke *entirely* inside the zone, so a signature
        # running past the edge survived intact under the white cover.
        rects = [r for z in zs for r in z["rects"]]
        _purge_annots(page, rects)
        # each strip is filled in its own zone's colour: the cover of step 3
        # follows the outline exactly, the strips overshoot it a little, and a
        # white sliver around a coloured cover would show on coloured paper.
        for z in zs:
            for r in z["rects"]:
                page.add_redact_annot(r, fill=z["color"])
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_PIXELS,
            graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
            text=fitz.PDF_REDACT_TEXT_REMOVE,
        )

        # 3. "delete" zones: a cover following the exact outline, in the zone's
        # own colour — white unless the client sent one.
        if delete_zs:
            shape = page.new_shape()
            for z in delete_zs:
                shape.draw_polyline(z["points"])
                shape.finish(fill=z["color"], color=z["color"], closePath=True)
            shape.commit()

        # 4. "pixelate" zones: lay the mosaic back over the emptied area.
        # keep_proportion=False because the mosaic is a few pixels across, its
        # rounded proportions are not exactly the zone's, and a centred image
        # would leave two strips of original content showing.
        for z, pm in mosaics:
            rect = z["rect"]
            if z["box"]:
                page.insert_image(rect, pixmap=pm, keep_proportion=False)
                continue
            # non-rectangular zone: the mosaic is clipped to the outline, or it
            # would cover the whole bounding box.
            mask = _shape_mask(z["points"], rect)
            if mask is None:
                continue
            page.insert_image(
                rect, stream=pm.tobytes("png"), mask=mask.tobytes("png"), keep_proportion=False
            )

    # original page number -> page number in the exported document
    page_map = {p: p - sum(1 for d in deleted_pages if d < p) for p in zones_by_page}

    if deleted_pages:
        doc.delete_pages(sorted(deleted_pages))

    # only pages carrying a zone were rewritten, so there is nothing to
    # recompress otherwise — and nothing to lose a generation over.
    #
    # Before the scrubbing, not after: re-encoding writes fresh image streams,
    # and those are exactly what strip_image_metadata has to be the last to see.
    if zones_by_page:
        _recompress_images(doc)

    if strip_meta:
        _scrub_document(doc)

    # garbage=4 + clean: objects left orphaned (old images, replaced content
    # streams) are really removed from the file, not merely dereferenced as an
    # incremental save would do.
    out = doc.tobytes(garbage=4, deflate=True, clean=True)
    doc.close()

    # Verification runs on the PDF as exported, BEFORE the watermark: a diagonal
    # watermark necessarily crosses the redacted zones, and reporting it would
    # drown real leaks in guaranteed noise. Filtering leaks that merely match the
    # watermark text would be worse — a real leak saying the same thing would
    # slip through. So: verify first, stamp second.
    leaks = _verify(out, zones_by_page, page_map)
    if watermark:
        out = _apply_watermark(out, watermark)

    base = os.path.splitext(entry["name"])[0] or "document"
    key = _put(f"{base}_redacted.pdf", out)

    # Privacy rule: the leak entries from _verify() hold text taken literally
    # from the document (the very words the user was erasing), and the watermark
    # is free text typed by the operator. Only their count and presence are
    # logged, never their content — do not "improve" this by adding the text.
    log_event(
        "export",
        sid=sid_prefix,
        zones=sum(len(zs) for zs in zones_by_page.values()),
        pages_deleted=len(deleted_pages),
        watermark=bool(watermark),
        strip_meta=strip_meta,
        leaks=len(leaks),
        out_bytes=len(out),
        ms=round((time.perf_counter() - start) * 1000),
    )

    return JSONResponse(
        {
            "download": f"/api/download/{key}",
            "filename": f"{base}_redacted.pdf",
            "leaks": leaks[:20],
            "leak_count": len(leaks),
        }
    )


# Serves an exported PDF as an attachment.
@app.get("/api/download/{key}")
def api_download(key: str):
    entry = _get(key)
    return Response(
        entry["bytes"],
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{entry["name"]}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


# The single page of the app; logs one connection event.
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    ua = request.headers.get("user-agent", "")[:UA_MAX_LEN]
    log_event("connect", **_log_ip_fields(request), ua=ua)
    return (PACKAGE_DIR / "templates" / "index.html").read_text(encoding="utf-8")
