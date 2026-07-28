"""Read-only: extracts what a PDF carries without showing it, for the inspector."""

import fitz

MAX_SPANS = 30_000  # guard rail on a very long document
SNIPPET = 2000  # a whole script is never returned

# An opaque rectangle painted over an image. Below these sizes it is decoration
# (a rule, a bullet), not something dropped on a scan to hide a name.
COVER_MIN_SIDE = 4.0  # PDF points
COVER_MIN_AREA_RATIO = 0.0005  # of the page area
COVER_TOLERANCE = 1.0  # a cover flush with the image edge still counts as inside

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


def _images(page) -> list[dict]:
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
        for rect in rects or [None]:
            out.append(
                {
                    "w": im[2],
                    "h": im[3],
                    "name": im[7] or f"image {xref}",
                    "rect": _r(rect) if rect is not None else None,
                }
            )
    return out


def _covers(page, image_rects) -> list[dict]:
    """Find the opaque rectangles painted over an image.

    A white box dropped on a scan removes nothing: the image still carries, byte
    for byte, what the box hides. Every renderer paints the box, so the area
    looks blank — including in this app, where the user then has no reason to
    draw a zone there — while anything reading the image instead of the composed
    page (OCR, "extract images", an editor deleting the overlay) gets the
    original back. On an exam header that is the student's name.

    A rectangle containing the whole image is the page background, not a cover,
    so only fills lying inside an image count.

    Args:
        page: The page to read.
        image_rects: The rectangles the page's images occupy.

    Returns:
        One entry per cover, with its rectangle and fill colour.
    """
    if not image_rects:
        return []
    try:
        drawings = page.get_drawings()
    except Exception:
        return []
    page_area = abs(page.rect.width * page.rect.height) or 1.0
    pad = (-COVER_TOLERANCE, -COVER_TOLERANCE, COVER_TOLERANCE, COVER_TOLERANCE)
    grown = [(r + pad, r) for r in image_rects]
    out, seen = [], set()
    for d in drawings:
        if "f" not in (d.get("type") or "") or d.get("fill") is None:
            continue
        if (d.get("fill_opacity") if d.get("fill_opacity") is not None else 1) < 0.99:
            continue
        r = d["rect"]
        if r.width < COVER_MIN_SIDE or r.height < COVER_MIN_SIDE:
            continue
        if abs(r.width * r.height) < page_area * COVER_MIN_AREA_RATIO:
            continue
        if not any(g.contains(r) and not r.contains(im) for g, im in grown):
            continue
        key = tuple(_r(r))
        if key in seen:
            continue
        seen.add(key)
        out.append({"rect": list(key), "color": [round(float(c), 3) for c in d["fill"]]})
    return out


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
        for n, page in enumerate(doc):
            try:
                drawings = len(page.get_drawings())
            except Exception:
                drawings = 0
            images = _images(page)
            image_rects = [fitz.Rect(im["rect"]) for im in images if im["rect"]]
            pages.append(
                {
                    "n": n,
                    "blocks": _text_blocks(page, budget),
                    "annots": _annots(page),
                    "widgets": _widgets(page),
                    "links": _links(page),
                    "images": images,
                    "covers": _covers(page, image_rects),
                    "struct": structs.get(n, []),
                    "drawings": drawings,
                }
            )
    finally:
        doc.close()
    return {"doc": info, "pages": pages, "truncated": budget[0] <= 0}
