"""OCR of a flattened page: read its bitmap, lay the text back as invisible ink.

An export renders every page flat, so the document it produces holds pixels and
nothing else — no text layer, no annotation, no structure tree. That is the
point, and it is also what would make the result unsearchable and unindexable.
This module gives it back: the very bitmap that was written into the page is
handed to the recognition engine, and each fragment it returns is drawn over the
page in invisible ink, at the place it was read.

The bitmap read here is the one the zones have already been painted into, so the
text layer can only ever carry what a reader of the file can see. There is no
way for it to describe something a zone hides — the pixels were gone before the
engine saw them.

The engine is optional: without it every function here returns empty and the
export goes out as an image, which the report says.
"""

import logging

import fitz

from src.config import OCR_MAX_SIDE, OCR_MIN_SCORE, OCR_SNIPPET
from src.logs import log_event

_engine = None  # built on first use: loading the models costs a second
_engine_broken = False

MIN_SIDE = 32  # a bitmap smaller than this carries no readable text
MIN_FONTSIZE = 1.0  # below this the fragment is not worth a text object


def _get_engine():
    """The OCR engine, or None if it cannot be used.

    Returns:
        The engine instance, or None when the package is missing or its models
        fail to load. Failure is remembered, so a broken install costs one
        attempt and not one per page.
    """
    global _engine, _engine_broken
    if _engine is not None or _engine_broken:
        return _engine
    try:
        from rapidocr_onnxruntime import RapidOCR

        _engine = RapidOCR()
    except Exception as e:
        _engine_broken = True
        # The export degrades to a plain image and says so, but only here can it
        # say *why*: without this line an operator sees a document come back
        # with no text layer and has nothing to go on. It names the exception,
        # never a document — the engine is loaded before any file is read.
        log_event("ocr_unavailable", level=logging.WARNING, error=f"{type(e).__name__}: {e}")
    return _engine


def available() -> bool:
    """Whether OCR can run at all in this install."""
    return _get_engine() is not None


def _flat(pm) -> bool:
    """Whether a pixmap holds one single colour — a blank page has nothing to read."""
    s = pm.samples
    return not s or min(s) == max(s)


def _readable(pm):
    """The bitmap the engine is given: RGB, no alpha, shrunk to an OCR-sized side.

    Args:
        pm: The page bitmap, as rendered for the export.

    Returns:
        A pixmap ready for the engine, or None when it is too small to carry
        text or holds one flat colour. The original is never modified.
    """
    try:
        if pm.alpha:
            pm = fitz.Pixmap(pm, 0)
        if pm.colorspace is None or pm.colorspace.n not in (1, 3):
            pm = fitz.Pixmap(fitz.csRGB, pm)
        if max(pm.width, pm.height) > OCR_MAX_SIDE:
            # shrink(1) halves both sides: an integer downscale, no resampling
            # cost. On a copy, since the caller writes this very bitmap into the
            # page and must keep it at its full size.
            pm = fitz.Pixmap(pm)
            while max(pm.width, pm.height) > OCR_MAX_SIDE:
                pm.shrink(1)
    except Exception:
        return None
    if pm.width < MIN_SIDE or pm.height < MIN_SIDE:
        return None
    return None if _flat(pm) else pm


def bitmap_words(pm) -> list[dict]:
    """Read one page bitmap.

    Args:
        pm: The bitmap of the page, zones already painted into it.

    Returns:
        One entry per recognised fragment: {"text", "score", "rect"}, the
        rectangle normalised to 0-1 so it survives the downscale and can be
        placed on a page of any size.
    """
    engine = _get_engine()
    if engine is None:
        return []
    small = _readable(pm)
    if small is None:
        return []
    try:
        result, _ = engine(small.tobytes("png"))
    except Exception:
        return []
    out = []
    for box, text, score in result or []:
        if not text.strip() or score < OCR_MIN_SCORE:
            continue
        xs = [p[0] / small.width for p in box]
        ys = [p[1] / small.height for p in box]
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


def _fits(text: str) -> bool:
    """Whether the base-14 font can really write this fragment.

    Helvetica only encodes Latin-1; anything else would be written as
    substitution glyphs, and a line of question marks in the index is worse
    than a gap in it.
    """
    try:
        text.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


def write_layer(page, words) -> int:
    """Draw the recognised fragments over the page, invisibly.

    `render_mode=3` writes the glyphs into the content stream without painting
    them: nothing changes on screen, and the words are selectable, searchable
    and indexable exactly where they are drawn on the image underneath.

    Args:
        page: The page of the exported document, already carrying its bitmap.
        words: The fragments of `bitmap_words`, rectangles normalised to 0-1.

    Returns:
        How many fragments were actually written.
    """
    rect = page.rect
    written = 0
    for w in words:
        text = w["text"]
        if not _fits(text):
            continue
        u0, v0, u1, v1 = w["rect"]
        x0, x1 = rect.x0 + u0 * rect.width, rect.x0 + u1 * rect.width
        y0, y1 = rect.y0 + v0 * rect.height, rect.y0 + v1 * rect.height
        box_w, box_h = x1 - x0, y1 - y0
        if box_w <= 0 or box_h <= 0:
            continue
        # The glyphs are invisible, so their size only decides what a selection
        # dragged across the line covers: start from the height of the box the
        # engine read, then narrow until the string spans its width rather than
        # overrunning the next one.
        size = box_h * 0.8
        try:
            length = fitz.get_text_length(text, fontsize=size)
        except Exception:
            continue
        if length > box_w and length > 0:
            size *= box_w / length
        if size < MIN_FONTSIZE:
            continue
        try:
            # baseline, not the top of the box: the descenders sit below it.
            page.insert_text(fitz.Point(x0, y1 - box_h * 0.2), text, fontsize=size, render_mode=3)
        except Exception:
            continue
        written += 1
    return written
