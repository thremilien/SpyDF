"""FastAPI app: routes for opening, rendering, flattening and downloading PDFs."""

import html
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
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles

from src.auth import (
    COOKIE_NAME,
    check_credentials,
    is_enabled,
    is_locked,
    issue_token,
    note_failure,
    note_success,
    token_is_valid,
)
from src.config import (
    AUTH_TTL,
    EXPORT_DPI,
    LOG_FILENAMES_VAR,
    MAX_SESSIONS,
    MAX_UPLOAD_BYTES,
    MAX_ZOOM,
    MIN_ZOOM,
    RENDER_ZOOM,
    SESSION_TTL,
    SID_LOG_LEN,
    UA_MAX_LEN,
    WATERMARK_DIAGONAL_RATIO,
    WATERMARK_FONT,
    WATERMARK_MAX_LEN,
    WATERMARK_MIN_SIZE,
    env_flag,
)
from src.flatten import flatten
from src.logs import log_event

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
# sid -> {"done", "total", "phase"} while an export of that session runs; each
# update swaps in a fresh dict, so a reader never sees one half-written.
EXPORTS: dict[str, dict] = {}


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


# ---------- login ----------
# Everything below is inert while no password is configured (see src.auth).

# Reachable without a cookie: the login page itself, what it needs to render,
# and the icon a browser asks for before anything else.
PUBLIC_PATHS = frozenset({"/login", "/logout", "/favicon.ico"})

LOGIN_ERRORS = {
    "bad": "Wrong user name or password.",
    "locked": "Too many attempts. Wait a few minutes before trying again.",
}


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Gate every route behind the login cookie.

    A page asks to be redirected to the login form; anything else — the client's
    own fetches — gets a 401 it can act on, because answering a redirect to an
    XHR would hand it a login page where it expected JSON.

    Args:
        request: The incoming request.
        call_next: The rest of the stack.

    Returns:
        The downstream response, or the redirect/401 that replaces it.
    """
    path = request.url.path
    if not is_enabled() or path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    if token_is_valid(request.cookies.get(COOKIE_NAME)):
        return await call_next(request)
    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/login", status_code=303)
    return JSONResponse({"detail": "not signed in"}, status_code=401)


def _login_page(error: str = "", user: str = "") -> str:
    """Render the login form.

    Args:
        error: A key of `LOGIN_ERRORS`, or empty for no banner.
        user: The user name to put back in the field. It comes from the form,
            so it is escaped rather than trusted.

    Returns:
        The page, as HTML.
    """
    page = (PACKAGE_DIR / "templates" / "login.html").read_text(encoding="utf-8")
    message = LOGIN_ERRORS.get(error, "")
    banner = (
        '<p class="login-error" role="alert">'
        '<svg viewBox="0 0 18 18" fill="none" aria-hidden="true">'
        '<circle cx="9" cy="9" r="6.5" stroke="currentColor" stroke-width="1.4"/>'
        '<line x1="9" y1="5.5" x2="9" y2="9.5" stroke="currentColor" stroke-width="1.5" '
        'stroke-linecap="round"/>'
        '<circle cx="9" cy="12.2" r=".9" fill="currentColor"/></svg>'
        f"<span>{html.escape(message)}</span></p>"
        if message
        else ""
    )
    return page.replace("{{error}}", banner).replace("{{user}}", html.escape(user, quote=True))


# A cookie is only sent back over TLS when the request itself came over TLS;
# behind Traefik that shows in the forwarded header, not in the URL.
def _is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return forwarded == "https" or request.url.scheme == "https"


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    """Show the login page, or send an already signed-in browser to the app."""
    if not is_enabled() or token_is_valid(request.cookies.get(COOKIE_NAME)):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(_login_page())


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(""), password: str = Form("")):
    """Check a submitted pair and hand out the session cookie.

    Neither the submitted name nor the password ever reaches the log: a password
    typed into the name field would land there in clear. Only the outcome and
    the address are recorded.

    Args:
        request: The incoming request, read for its address fields.
        username: The submitted user name.
        password: The submitted password.

    Returns:
        A redirect to the app, or the form again with an error banner.
    """
    if not is_enabled():
        return RedirectResponse("/", status_code=303)
    ip_fields = _log_ip_fields(request)
    ip = ip_fields["ip"]

    if is_locked(ip):
        log_event("login_rejected", level=logging.WARNING, reason="locked_out", **ip_fields)
        return HTMLResponse(_login_page("locked", username), status_code=429)

    if not check_credentials(username, password):
        tries = note_failure(ip)
        log_event(
            "login_rejected",
            level=logging.WARNING,
            reason="bad_credentials",
            tries=tries,
            **ip_fields,
        )
        return HTMLResponse(_login_page("bad", username), status_code=401)

    note_success(ip)
    log_event("login", **ip_fields)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        issue_token(),
        max_age=AUTH_TTL,
        httponly=True,  # the cookie is the session: JS has no business reading it
        samesite="lax",
        secure=_is_https(request),
        path="/",
    )
    return response


@app.post("/logout")
def logout(request: Request):
    """Drop the cookie and go back to the login page."""
    log_event("logout", **_log_ip_fields(request))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


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


# A rectangular zone has nothing to cut up: its outline *is* its bounding box.
def _is_box(points, rect) -> bool:
    return len(points) == 4 and all(
        (abs(p.x - rect.x0) < 0.01 or abs(p.x - rect.x1) < 0.01)
        and (abs(p.y - rect.y0) < 0.01 or abs(p.y - rect.y1) < 0.01)
        for p in points
    )


def _zone_color(raw) -> tuple[float, float, float]:
    """Read the colour a zone is filled with, sent as three 0-255 channels.

    On a coloured scan a white patch is itself a mark — it says where something
    was. The client samples the paper along the drawn outline and sends that.

    Args:
        raw: The value received, of any type.

    Returns:
        The three channels as 0-1 floats; white for anything unusable, which is
        what the fill was before it had a colour.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        return WHITE
    out = []
    for c in raw:
        if isinstance(c, bool) or not isinstance(c, (int, float)):
            return WHITE
        out.append(min(RGB_MAX, max(0.0, float(c))) / RGB_MAX)
    return (out[0], out[1], out[2])


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


@app.post("/api/export")
async def api_export(request: Request, payload: dict):
    """Flatten the session document, paint its zones into the pixels, hand it back.

    Zones arrive as {"3": [{"points": [[x, y], ...], "mode": "delete",
    "color": [r, g, b]}, ...]} in PDF coordinates, "points" being the outline of
    the zone (a rectangle is 4 corners, but a polygon or freehand stroke works
    too). The outline is followed exactly: `src.flatten` fills it row by row in
    the page bitmap, and nothing outside it is ever touched.

    Args:
        request: The incoming request.
        payload: {"sid", "zones", "deleted_pages", "watermark"}.

    Returns:
        {"download", "filename", "pages", "indexed", "fragments", "ocr"} — the
        last four say how much of the export the recognition managed to index.

    Raises:
        HTTPException: 404 unknown session, 400 when every page would be deleted.
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
                    "mode": "pixelate" if z.get("mode") == "pixelate" else "delete",
                    "color": _zone_color(z.get("color")),
                }
            )
        if parsed:
            zones_by_page[int(k)] = parsed

    deleted_pages = {int(p) for p in (payload.get("deleted_pages") or [])}
    # nothing to paint on a page that is about to disappear entirely
    zones_by_page = {p: zs for p, zs in zones_by_page.items() if p not in deleted_pages}

    watermark = _normalize_watermark(payload.get("watermark"))

    # An export with no zone at all is not a no-op any more: flattening alone
    # strips the file of everything that is not on the page.
    doc = fitz.open(stream=entry["bytes"], filetype="pdf")
    page_count = doc.page_count
    doc.close()
    if len(deleted_pages) >= page_count:
        log_event(
            "export_rejected", level=logging.WARNING, reason="all_pages_deleted", sid=sid_prefix
        )
        raise HTTPException(400, "cannot delete every page")

    sid = payload["sid"]
    total = sum(1 for n in range(page_count) if n not in deleted_pages)

    # Called from the threadpool thread after each page.
    def progress(done: int, total: int) -> None:
        EXPORTS[sid] = {"done": done, "total": total, "phase": "pages"}

    EXPORTS[sid] = {"done": 0, "total": total, "phase": "starting"}
    try:
        # Rendering and recognition are both slow and both CPU-bound: off the
        # event loop, or one export freezes every other session.
        out, report = await run_in_threadpool(
            flatten, entry["bytes"], zones_by_page, deleted_pages, progress
        )
        EXPORTS[sid] = {"done": total, "total": total, "phase": "finishing"}

        # The watermark is stamped last, on the flattened pages: it is the one
        # thing in the exported file that is real text rather than pixels, and it
        # has to stay out of the bitmap the recognition was given.
        if watermark:
            out = _apply_watermark(out, watermark)

        base = os.path.splitext(entry["name"])[0] or "document"
        key = _put(f"{base}_redacted.pdf", out)
    finally:
        EXPORTS.pop(sid, None)

    # Privacy rule: the watermark is free text typed by the operator and the
    # text layer is the document's own content. Only counts are logged, never a
    # word of either — do not "improve" this by adding the text.
    log_event(
        "export",
        sid=sid_prefix,
        zones=sum(len(zs) for zs in zones_by_page.values()),
        pages_deleted=len(deleted_pages),
        watermark=bool(watermark),
        dpi=EXPORT_DPI,
        ocr=report["ocr"],
        fragments=report["fragments"],
        out_bytes=len(out),
        ms=round((time.perf_counter() - start) * 1000),
    )

    return JSONResponse(
        {
            "download": f"/api/download/{key}",
            "filename": f"{base}_redacted.pdf",
            **report,
        }
    )


@app.get("/api/export/progress/{sid}")
def api_export_progress(sid: str):
    """Report how far the running export of a session has got.

    `POST /api/export` answers only once the whole document is built, which on a
    long scan takes minutes; the client polls this meanwhile to draw a bar.

    Args:
        sid: Session id.

    Returns:
        {"active", "done", "total", "phase"}: `phase` is "starting", "pages" or
        "finishing" during an export, "idle" (with zeros) when none runs.

    Raises:
        HTTPException: 404 for an unknown or expired session.
    """
    _get(sid)
    state = EXPORTS.get(sid)
    if state is None:
        return {"active": False, "done": 0, "total": 0, "phase": "idle"}
    return {"active": True, **state}


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


# The sign-out control, or nothing at all when there is nothing to sign out of.
SIGNOUT_HTML = (
    '<div class="sep"></div>'  # it sits next to Export: a misclick must not be one key away
    '<form class="signout" method="post" action="/logout">'
    '<button type="submit" class="btn icon-only" title="Sign out" aria-label="Sign out">'
    '<svg viewBox="0 0 18 18" fill="none">'
    '<path d="M11 5.5V4a1 1 0 00-1-1H4.5a1 1 0 00-1 1v10a1 1 0 001 1H10a1 1 0 001-1v-1.5" '
    'stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>'
    '<path d="M12 6.5L14.5 9 12 11.5" stroke="currentColor" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round"/>'
    '<line x1="7.5" y1="9" x2="14" y2="9" stroke="currentColor" stroke-width="1.5" '
    'stroke-linecap="round"/></svg></button></form>'
)


# The single page of the app; logs one connection event.
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    ua = request.headers.get("user-agent", "")[:UA_MAX_LEN]
    log_event("connect", **_log_ip_fields(request), ua=ua)
    page = (PACKAGE_DIR / "templates" / "index.html").read_text(encoding="utf-8")
    return page.replace("{{signout}}", SIGNOUT_HTML if is_enabled() else "")
