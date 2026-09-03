"""Read-only: extracts what a PDF carries without showing it, for the inspector."""

import fitz

from src.config import COVER_INK_DELTA, COVER_INK_RATIO, COVER_PROBE_PX
from src.imagemeta import image_traces

MAX_SPANS = 30_000  # guard rail on a very long document
SNIPPET = 2000  # a whole script is never returned

# An opaque rectangle painted over an image. Below these sizes it is decoration
# (a rule, a bullet), not something dropped on a scan to hide a name.
COVER_MIN_SIDE = 4.0  # PDF points
COVER_MIN_AREA_RATIO = 0.0005  # of the page area
COVER_MASK_SOLID = 0.9  # of a soft mask that must be opaque before it hides

META_LABELS = [
    ("title", "Title"),
    ("author", "Author"),
    ("subject", "Subject"),
    ("keywords", "Keywords"),
    ("creator", "Originating application"),
    ("producer", "PDF producer"),
    ("creationDate", "Creation date"),
    ("modDate", "Modification date"),
    ("format", "Format"),
    ("encryption", "Encryption"),
]

# Text a tagged PDF carries in its structure tree instead of in the page: the
# description of a figure, the real characters behind a glyph run, an
# abbreviation's expansion. Shared with app.py, which strips these same keys.
STRUCT_TEXT_KEYS = [
    ("Alt", "alternative text"),
    ("ActualText", "actual text"),
    ("E", "expansion"),
]

LINK_KINDS = {
    fitz.LINK_GOTO: "page",
    fitz.LINK_URI: "url",
    fitz.LINK_LAUNCH: "file",
    fitz.LINK_GOTOR: "external document",
    fitz.LINK_NAMED: "action",
}


# A rectangle as four rounded floats, ready for JSON.
def _r(rect) -> list[float]:
    return [round(float(v), 2) for v in (rect.x0, rect.y0, rect.x1, rect.y1)]


def _metadata(doc) -> list[dict]:
    md = doc.metadata or {}
    return [
        {"key": k, "label": label, "value": str(md[k]).strip()}
        for k, label in META_LABELS
        if md.get(k) and str(md[k]).strip()
    ]


def _toc(doc) -> list[dict]:
    try:
        return [
            {"level": lvl, "title": title, "page": page}
            for lvl, title, page in doc.get_toc(simple=True)
        ]
    except Exception:
        return []


def _attachments(doc) -> list[dict]:
    out = []
    try:
        names = doc.embfile_names()
    except Exception:
        return out
    for name in names:
        try:
            info = doc.embfile_info(name)
        except Exception:
            info = {}
        out.append(
            {
                "name": name,
                "filename": info.get("filename") or "",
                "desc": info.get("desc") or "",
                "size": info.get("size") or 0,
            }
        )
    return out


def _layers(doc) -> list[dict]:
    try:
        ocgs = doc.get_ocgs() or {}
    except Exception:
        return []
    return [
        {"name": v.get("name") or f"layer {xref}", "on": bool(v.get("on", True))}
        for xref, v in ocgs.items()
    ]


def _javascript(doc) -> list[dict]:
    """List the scripts a PDF embeds, which fire on opening or on an action.

    PyMuPDF exposes no API for these, so the objects are walked by hand.

    Args:
        doc: An open document.

    Returns:
        One entry per script, its code truncated to SNIPPET characters.
    """
    out = []
    for xref in range(1, doc.xref_length()):
        try:
            if doc.xref_get_key(xref, "S")[1] != "/JavaScript":
                continue
            kind, val = doc.xref_get_key(xref, "JS")
        except Exception:
            continue
        code = ""
        try:
            if kind == "string":
                code = val.strip("()")
            elif kind == "xref":
                code = doc.xref_stream(int(val.split()[0])).decode("utf-8", "replace")
        except Exception:
            pass
        out.append({"name": f"action {xref}", "code": code[:SNIPPET]})
    return out


def _fonts(doc) -> list[dict]:
    """List the fonts used, deduplicated by base name.

    A subset font name ("ABCDEF+Calibri") and the font list as a whole give away
    the machine and the application the file came from.

    Args:
        doc: An open document.

    Returns:
        One entry per distinct font, flagged as embedded or merely referenced.
    """
    seen, out = set(), []
    for page in doc:
        try:
            fonts = page.get_fonts(full=False)
        except Exception:
            continue
        for f in fonts:
            ext, ftype, basefont = f[1], f[2], f[3]
            if basefont in seen:
                continue
            seen.add(basefont)
            out.append({"name": basefont, "type": ftype, "embedded": ext != "n/a"})
    return out


def _struct_text(doc) -> dict[int, list[dict]]:
    """Collect the text the structure tree carries for each page.

    Readers show it, copy it and read it out, indexers index it — and redaction
    never reaches it, because it is not page content: it hangs off the
    structure tree. On a scanned page it is often the only text there is, which
    is exactly when the page looks empty of text and is not.

    PyMuPDF exposes no API for the structure tree, so the objects are walked by
    hand, as for the JavaScript.

    Args:
        doc: An open document.

    Returns:
        Page number -> its entries, each {"kind", "text"}.
    """
    try:
        page_of = {page.xref: page.number for page in doc}
    except Exception:
        return {}
    out: dict[int, list[dict]] = {}
    for xref in range(1, doc.xref_length()):
        try:
            if doc.xref_get_key(xref, "Type")[1] != "/StructElem":
                continue
            kind, val = doc.xref_get_key(xref, "Pg")
            n = page_of.get(int(val.split()[0])) if kind == "xref" else None
            if n is None:
                continue
            for key, label in STRUCT_TEXT_KEYS:
                k, text = doc.xref_get_key(xref, key)
                if k == "string" and text.strip():
                    out.setdefault(n, []).append({"kind": label, "text": text[:SNIPPET]})
        except Exception:
            continue
    return out


def _text_blocks(page, budget: list[int]) -> list[dict]:
    """Extract the text layer as stored, in blocks and lines.

    Every fragment keeps its rectangle, which is what later tells whether it
    falls inside a drawn zone.

    Args:
        page: The page to read.
        budget: Single-element list holding the number of spans left for the
            whole document; decremented in place and stops the walk at zero.

    Returns:
        The blocks, each holding lines of spans.
    """
    try:
        raw = page.get_text("dict")
    except Exception:
        return []
    blocks = []
    for b in raw.get("blocks", []):
        if b.get("type") != 0:
            continue
        lines = []
        for line in b.get("lines", []):
            spans = []
            for sp in line.get("spans", []):
                if not sp.get("text", "").strip():
                    continue
                if budget[0] <= 0:
                    return blocks
                budget[0] -= 1
                # alpha 0 = invisible text: an OCR layer, or a deliberately
                # hidden trace. It is indexed and copyable all the same.
                spans.append(
                    {
                        "text": sp["text"],
                        "rect": [round(v, 2) for v in sp["bbox"]],
                        "font": sp.get("font", ""),
                        "size": round(sp.get("size", 0), 1),
                        "hidden": sp.get("alpha", 255) == 0,
                    }
                )
            if spans:
                lines.append({"spans": spans})
        if lines:
            blocks.append({"rect": [round(v, 2) for v in b["bbox"]], "lines": lines})
    return blocks


def _annots(page) -> list[dict]:
    out = []
    try:
        annots = list(page.annots())
    except Exception:
        return out
    for a in annots:
        info = a.info or {}
        out.append(
            {
                "type": a.type[1] if len(a.type) > 1 else str(a.type[0]),
                "author": info.get("title") or "",
                "content": (info.get("content") or "")[:SNIPPET],
                "subject": info.get("subject") or "",
                "date": info.get("modDate") or info.get("creationDate") or "",
                "rect": _r(a.rect),
            }
        )
    return out


def _widgets(page) -> list[dict]:
    out = []
    try:
        widgets = list(page.widgets())
    except Exception:
        return out
    for w in widgets:
        out.append(
            {
                "name": w.field_name or "",
                "label": w.field_label or "",
                "value": str(w.field_value if w.field_value is not None else "")[:SNIPPET],
                "type": w.field_type_string or "",
                "rect": _r(w.rect),
            }
        )
    return out


def _links(page) -> list[dict]:
    out = []
    try:
        links = page.get_links()
    except Exception:
        return out
    for lk in links:
        target = lk.get("uri") or lk.get("file") or lk.get("name") or ""
        if not target and lk.get("kind") == fitz.LINK_GOTO:
            target = f"page {lk.get('page', 0) + 1}"
        out.append(
            {
                "kind": LINK_KINDS.get(lk.get("kind"), "other"),
                "target": str(target)[:SNIPPET],
                "rect": _r(fitz.Rect(lk["from"])),
            }
        )
    return out


def _images(page, traces: dict) -> list[dict]:
    """List the images drawn on a page, with what each one carries besides pixels.

    Args:
        page: The page to read.
        traces: xref -> its metadata, filled as images are met. An image used on
            several pages is read once, and a scan's stream is not small.

    Returns:
        One entry per placement of an image on the page.
    """
    out = []
    try:
        imgs = page.get_images(full=True)
    except Exception:
        return out
    for im in imgs:
        xref = im[0]
        try:
            rects = page.get_image_rects(xref)
        except Exception:
            rects = []
        if xref not in traces:
            traces[xref] = image_traces(page.parent, xref)
        for rect in rects or [None]:
            out.append(
                {
                    "w": im[2],
                    "h": im[3],
                    "name": im[7] or f"image {xref}",
                    "rect": _r(rect) if rect is not None else None,
                    "meta": traces[xref],
                }
            )
    return out


def _paint_log(page):
    """Every paint operation of the page, in the order it happens.

    `get_bboxlog()` is the only view that keeps that order, and order is what
    makes a cover a cover: paint lying over nothing is a background, the same
    paint over an earlier one hides it. `seqno` on a drawing is this list's
    index, and the k-th image entry is the k-th placement of `get_image_info()`.

    Args:
        page: The page to read.

    Returns:
        (entries, images): entries are {"kind", "rect"}; images maps an entry
        index to its `get_image_info` record.
    """
    try:
        log = page.get_bboxlog()
        infos = page.get_image_info(xrefs=True)
    except Exception:
        return [], {}
    entries, images, k = [], {}, 0
    for i, item in enumerate(log):
        kind, box = item[0], item[1]
        entries.append({"kind": kind, "rect": fitz.Rect(box)})
        if kind == "fill-image":
            if k < len(infos):
                images[i] = infos[k]
            k += 1
    return entries, images


def _image_subrect(rect, info, page):
    """Where a page rectangle falls inside the image drawn under it, 0-1.

    The inverse of `src.ocr.place`, and it has to stay so: one says where the
    image's content lands on the page, the other which part of the image a cover
    sits on.

    Args:
        rect: The rectangle in page coordinates.
        info: One entry of `page.get_image_info()`.
        page: The page it is drawn on.

    Returns:
        (u0, v0, u1, v1) clipped to the image, or None when they do not meet.
    """
    try:
        mat = ~fitz.Matrix(info["transform"])
    except Exception:
        return None
    pts = []
    for x in (rect.x0, rect.x1):
        for y in (rect.y0, rect.y1):
            p = fitz.Point(x, page.rect.y1 - y) * mat
            pts.append((p.x, 1 - p.y))
    us = [max(0.0, min(1.0, p[0])) for p in pts]
    vs = [max(0.0, min(1.0, p[1])) for p in pts]
    u0, u1, v0, v1 = min(us), max(us), min(vs), max(vs)
    if u1 - u0 < 1e-4 or v1 - v0 < 1e-4:
        return None
    return (round(u0, 4), round(v0, 4), round(u1, 4), round(v1, 4))


def _crop_pixmap(doc, xref, sub):
    """Render a normalised part of an image, small, for a look at what is there.

    Args:
        doc: The open document.
        xref: The image's xref.
        sub: (u0, v0, u1, v1), normalised to the image.

    Returns:
        A small pixmap of that part, or None.
    """
    try:
        img = doc.extract_image(xref)
        idoc = fitz.open(stream=img["image"], filetype=img["ext"])
        page = idoc[0]
        r = page.rect
        clip = fitz.Rect(
            r.x0 + sub[0] * r.width,
            r.y0 + sub[1] * r.height,
            r.x0 + sub[2] * r.width,
            r.y0 + sub[3] * r.height,
        )
        scale = min(COVER_PROBE_PX / max(clip.width, clip.height, 1e-6), 4.0)
        pm = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
        idoc.close()
    except Exception:
        return None
    return pm if pm.width and pm.height else None


def _has_ink(pm) -> bool:
    """Whether a pixmap holds anything but an even tone.

    A scan's blank paper is never one exact value — it has grain — so a plain
    min/max spread would call every empty margin content. What separates ink
    from grain is that a few percent of the pixels sit far from the average.

    Args:
        pm: The pixmap to weigh.

    Returns:
        Whether it carries something worth hiding.
    """
    s = pm.samples
    if not s:
        return False
    mean = sum(s) / len(s)
    far = sum(1 for v in s if abs(v - mean) > COVER_INK_DELTA)
    return far > len(s) * COVER_INK_RATIO


def _mean_color(pm) -> list[float]:
    """The average colour of a pixmap, as 0-1 channels, padded to RGB."""
    s, n = pm.samples, pm.n
    if not s or not n:
        return [1.0, 1.0, 1.0]
    chans = [sum(s[i::n]) / (len(s) / n) / 255.0 for i in range(min(n, 3))]
    while len(chans) < 3:
        chans.append(chans[0])
    return [round(c, 3) for c in chans]


def _opaque_paint(entry, index, drawings, images, doc):
    """Whether one paint operation hides whatever it is laid over, and in what colour.

    Args:
        entry: The paint log entry.
        index: Its index in the log.
        drawings: seqno -> the `get_drawings()` record.
        images: log index -> the `get_image_info()` record.
        doc: The open document.

    Returns:
        (opaque, colour) — colour as 0-1 RGB, used to repaint the area when the
        cover becomes a zone. `(False, None)` for anything see-through.
    """
    if entry["kind"] == "fill-path":
        d = drawings.get(index)
        if d is None or d.get("fill") is None:
            return False, None
        if (d.get("fill_opacity") if d.get("fill_opacity") is not None else 1) < 0.99:
            return False, None
        return True, [round(float(c), 3) for c in d["fill"]]
    if entry["kind"] == "fill-image":
        info = images.get(index)
        if info is None:
            return False, None
        # A stencil or soft mask can be see-through anywhere; what matters is
        # whether this placement actually hides, so the alpha is measured rather
        # than assumed. An image without one is opaque by definition.
        xref = info.get("xref") or 0
        pm = _crop_pixmap(doc, xref, (0.0, 0.0, 1.0, 1.0))
        if pm is None:
            return False, None
        if info.get("has-mask") and not _mask_is_solid(doc, xref):
            return False, None
        return True, _mean_color(pm)
    return False, None


def _mask_is_solid(doc, xref) -> bool:
    """Whether an image's transparency actually hides, over most of its area.

    A phone's markup tool feathers the edges of the patch it lays down: the mask
    dips below full opacity all round the rim and hides perfectly everywhere
    else. Refusing such a patch the status of a cover would leave the name under
    it unreported, so the rim is allowed to be soft and the middle is not.

    Args:
        doc: The open document.
        xref: The image's xref.

    Returns:
        Whether it is opaque over nearly all of itself.
    """
    try:
        info = doc.extract_image(xref)
        smask = info.get("smask") or 0
        if not smask:
            return True
        pm = _crop_pixmap(doc, smask, (0.0, 0.0, 1.0, 1.0))
    except Exception:
        return True
    if pm is None:
        return True
    s = pm.samples
    if not s:
        return True
    solid = sum(1 for v in s if v >= 250)
    return solid > len(s) * COVER_MASK_SOLID


def _covers(page) -> list[dict]:
    """Find what the page paints over something else, hiding it without removing it.

    A white box dropped on a scan removes nothing: the image still carries, byte
    for byte, what the box hides. Every renderer paints the box, so the area
    looks blank — including in this app, where the user then has no reason to
    draw a zone there — while anything reading the image instead of the composed
    page gets the original back. Chrome does exactly that, on every scanned PDF
    it opens (see `src.ocr`). On an exam header that is the student's name.

    The test is paint order, not shape: opaque paint over an earlier paint. That
    is what catches an image used as a cover — which is what a phone's markup
    tool produces, and what a rule looking only at vector fills misses entirely.
    It also makes the page background a non-event rather than a special case,
    since nothing is painted under it.

    Opaque paint over *blank* paper hides nothing, so what is underneath is
    looked at before the cover is reported: without that, every solid shape on
    every designed page would cry wolf.

    Args:
        page: The page to read.

    Returns:
        One entry per cover: its rectangle, the colour to repaint it in, what it
        hides, and which image it sits on, so the pane can show what is there.
    """
    doc = page.parent
    entries, images = _paint_log(page)
    if not entries:
        return []
    drawings = {}
    try:
        for d in page.get_drawings():
            drawings[d.get("seqno")] = d
    except Exception:
        pass
    page_area = abs(page.rect.width * page.rect.height) or 1.0
    out, seen = [], set()
    for i, entry in enumerate(entries):
        r = entry["rect"]
        if r.width < COVER_MIN_SIDE or r.height < COVER_MIN_SIDE:
            continue
        if abs(r.width * r.height) < page_area * COVER_MIN_AREA_RATIO:
            continue
        opaque, color = _opaque_paint(entry, i, drawings, images, doc)
        if not opaque:
            continue
        hides, under = _hidden_under(entries[:i], images, r, doc, page, page_area)
        if not hides:
            continue
        key = tuple(_r(r))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "rect": list(key),
                "color": color,
                "hides": sorted(hides),
                "under": under,
                "kind": "image" if entry["kind"] == "fill-image" else "fill",
            }
        )
    return out


def _hidden_under(earlier, images, rect, doc, page, page_area):
    """What an opaque rectangle actually hides among everything painted before it.

    Args:
        earlier: The paint log entries preceding the cover.
        images: log index -> `get_image_info()` record.
        rect: The cover's rectangle.
        doc: The open document.
        page: The page being read.
        page_area: Its area, to tell a background fill from a drawn shape.

    Returns:
        (kinds, under): what is hidden, and {"xref", "sub"} for the image it
        sits on, so the hidden area can be shown as it is.
    """
    kinds, under = set(), None
    for j, prev in enumerate(earlier):
        if not (prev["rect"] & rect).is_valid or (prev["rect"] & rect).is_empty:
            continue
        if prev["kind"].endswith("text"):
            kinds.add("text")
        elif prev["kind"] == "fill-image":
            info = images.get(j)
            if info is None:
                continue
            sub = _image_subrect(rect, info, page)
            if sub is None:
                continue
            pm = _crop_pixmap(doc, info.get("xref") or 0, sub)
            if pm is not None and _has_ink(pm):
                kinds.add("image")
                if under is None:
                    under = {"xref": info.get("xref") or 0, "sub": list(sub)}
        # a page-sized fill is the paper, not something being hidden
        elif prev["kind"].endswith("path") and (
            abs(prev["rect"].width * prev["rect"].height) < page_area * 0.5
        ):
            kinds.add("drawing")
    return kinds, under


def inspect_document(data: bytes) -> dict:
    """Read everything the document carries without displaying it.

    Args:
        data: The PDF bytes.

    Returns:
        {"doc": document-level traces, "pages": per-page traces, "truncated":
        whether the span budget ran out}.
    """
    doc = fitz.open(stream=data, filetype="pdf")
    budget = [MAX_SPANS]
    try:
        info = {
            "page_count": doc.page_count,
            "encrypted": bool(doc.is_encrypted),
            "metadata": _metadata(doc),
            "xmp": (doc.get_xml_metadata() or "").strip() or None,
            "toc": _toc(doc),
            "attachments": _attachments(doc),
            "layers": _layers(doc),
            "javascript": _javascript(doc),
            "fonts": _fonts(doc),
        }
        structs = _struct_text(doc)
        pages = []
        traces: dict[int, list[dict]] = {}
        for n, page in enumerate(doc):
            try:
                drawings = len(page.get_drawings())
            except Exception:
                drawings = 0
            images = _images(page, traces)
            pages.append(
                {
                    "n": n,
                    "blocks": _text_blocks(page, budget),
                    "annots": _annots(page),
                    "widgets": _widgets(page),
                    "links": _links(page),
                    "images": images,
                    "covers": _covers(page),
                    "struct": structs.get(n, []),
                    "drawings": drawings,
                }
            )
    finally:
        doc.close()
    return {"doc": info, "pages": pages, "truncated": budget[0] <= 0}
