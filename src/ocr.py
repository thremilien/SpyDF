"""OCR of the images a page draws, one image at a time — the way Chrome reads a PDF.

Chrome's PDF viewer never runs its OCR on the composed page. `pdfium_ocr.cc`
walks the page objects, keeps the images, and calls
`FPDFImageObj_GetRenderedBitmap` on each one *alone*, then writes the recognised
text back over it as invisible ink so it can be selected. A white patch laid on
top of a scan is a separate image object, so it is simply not in the bitmap the
OCR is given: the name underneath comes back selectable, at the very position
the patch is meant to hide.

This module reproduces that on purpose. It reads image objects one by one and
never a rendered page, because reading the rendered page would show what the eye
shows and miss exactly what makes a cover dangerous. What it reports is
therefore not "what is written on the page" but "what a reader can extract from
the file" — the two differ precisely where someone tried to hide something.

The engine is optional: without it every function here returns empty and the
rest of the app is unaffected.
"""

import fitz

from src.config import OCR_MAX_IMAGES, OCR_MAX_SIDE, OCR_MIN_SCORE, OCR_SNIPPET

_engine = None  # built on first use: loading the models costs a second
_engine_broken = False


def _get_engine():
    """The OCR engine, or None if it cannot be used.

    Returns:
        The engine instance, or None when the package is missing or its models
        fail to load. Failure is remembered, so a broken install costs one
        attempt and not one per image.
    """
    global _engine, _engine_broken
    if _engine is not None or _engine_broken:
        return _engine
    try:
        from rapidocr_onnxruntime import RapidOCR

        _engine = RapidOCR()
    except Exception:
        _engine_broken = True
    return _engine


def available() -> bool:
    """Whether OCR can run at all in this install."""
    return _get_engine() is not None


def _flat(pm) -> bool:
    """Whether a pixmap holds one single colour — a blank cover has nothing to read."""
    s = pm.samples
    return not s or min(s) == max(s)


def _readable_pixmap(doc, xref):
    """The image's own pixels, RGB, no alpha, shrunk to something OCR-sized.

    The base image is used rather than the composed placement: that is the point
    of the module, and it is also what makes a soft-masked cover irrelevant here.

    Args:
        doc: The open document.
        xref: The image's xref.

    Returns:
        A pixmap ready for the engine, or None when the image cannot be decoded,
        is too small to carry text, or is one flat colour.
    """
    try:
        pm = fitz.Pixmap(doc, xref)
        if pm.alpha:
            pm = fitz.Pixmap(pm, 0)
        if pm.colorspace is None or pm.colorspace.n not in (1, 3):
            pm = fitz.Pixmap(fitz.csRGB, pm)
    except Exception:
        return None
    if pm.width < 32 or pm.height < 32:
        return None
    # shrink(1) halves both sides: an integer downscale, no resampling cost, and
    # a 2480 x 3507 scan drops to a size the engine handles in seconds. It comes
    # before the flatness test on purpose — reading `samples` pins a memoryview
    # that `shrink` then refuses to work around.
    while max(pm.width, pm.height) > OCR_MAX_SIDE:
        pm.shrink(1)
    return None if _flat(pm) else pm


def image_words(doc, xref) -> list[dict]:
    """Read one image object, in isolation.

    Args:
        doc: The open document.
        xref: The image's xref.

    Returns:
        One entry per recognised fragment: {"text", "score", "rect"}, the
        rectangle in the image's own space, normalised to 0-1 so it survives the
        downscale and can be placed under any of the image's placements.
    """
    engine = _get_engine()
    if engine is None:
        return []
    pm = _readable_pixmap(doc, xref)
    if pm is None:
        return []
    try:
        result, _ = engine(pm.tobytes("png"))
    except Exception:
        return []
    out = []
    for box, text, score in result or []:
        if not text.strip() or score < OCR_MIN_SCORE:
            continue
        xs = [p[0] / pm.width for p in box]
        ys = [p[1] / pm.height for p in box]
        out.append(
            {
                "text": text.strip()[:OCR_SNIPPET],
                "score": round(float(score), 2),
                "rect": [
                    round(min(xs), 4),
                    round(min(ys), 4),
                    round(max(xs), 4),
                    round(max(ys), 4),
                ],
            }
        )
    return out


def place(rect, info, page) -> list[float]:
    """Put a normalised image rectangle where its placement draws it on the page.

    The unit square is mapped through the placement matrix, which handles a
    rotated or mirrored placement as readily as the usual axis-aligned one. Two
    flips are needed: `v` counts downwards from the top of the image, and the
    matrix works in PDF coordinates whose y grows upwards.

    Args:
        rect: (u0, v0, u1, v1), normalised to the image.
        info: One entry of `page.get_image_info()`.
        page: The page it is drawn on.

    Returns:
        The rectangle in page coordinates.
    """
    u0, v0, u1, v1 = rect
    mat = fitz.Matrix(info["transform"])
    a = fitz.Point(u0, 1 - v1) * mat
    b = fitz.Point(u1, 1 - v0) * mat
    top, bottom = page.rect.y1 - a.y, page.rect.y1 - b.y
    r = fitz.Rect(min(a.x, b.x), min(top, bottom), max(a.x, b.x), max(top, bottom))
    return [round(float(v), 2) for v in r]


def page_words(page, cache: dict, budget: list[int]) -> list[dict]:
    """Everything a reader can extract from the images of one page.

    Args:
        page: The page to read.
        cache: xref -> its words, normalised. The same image placed twice, or
            reused on every page, is read once.
        budget: Single-element list holding the number of images left to read
            for the whole document; decremented in place.

    Returns:
        One entry per fragment and per placement: {"text", "score", "rect",
        "xref"}, rectangles in page coordinates.
    """
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:
        return []
    out = []
    for info in infos:
        xref = info.get("xref") or 0
        if xref not in cache:
            if budget[0] <= 0:
                continue
            budget[0] -= 1
            cache[xref] = image_words(page.parent, xref)
        for w in cache[xref]:
            out.append(
                {
                    "text": w["text"],
                    "score": w["score"],
                    "rect": place(w["rect"], info, page),
                    "xref": xref,
                }
            )
    return out


def document_words(data: bytes) -> dict:
    """Read every image of every page the way Chrome would.

    Args:
        data: The PDF bytes.

    Returns:
        {"available": whether the engine ran, "pages": [{"n", "words"}],
        "truncated": whether the image budget ran out}.
    """
    if not available():
        return {"available": False, "pages": [], "truncated": False}
    doc = fitz.open(stream=data, filetype="pdf")
    budget = [OCR_MAX_IMAGES]
    cache: dict[int, list[dict]] = {}
    try:
        pages = [{"n": n, "words": page_words(page, cache, budget)} for n, page in enumerate(doc)]
    finally:
        doc.close()
    return {"available": True, "pages": pages, "truncated": budget[0] <= 0}
