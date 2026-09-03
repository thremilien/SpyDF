"""Reading the images the way a reader does — cover or no cover."""

import fitz
import pytest

from src.app import _verify
from src.ocr import available, document_words, image_words, place
from src.probe import _image_subrect

pytestmark = pytest.mark.skipif(not available(), reason="no OCR engine installed")

SECRET = "SECRETNAME"
INK_AT = fitz.Rect(50, 380, 420, 450)


def scan_bytes(text: str = SECRET) -> bytes:
    """A picture of a page with something written on it, as a scan would be."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((INK_AT.x0 + 12, INK_AT.y1 - 22), text, fontsize=34)
    return page.get_pixmap(dpi=150, alpha=False).tobytes("png")


def blank_bytes() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    return page.get_pixmap(dpi=150, alpha=False).tobytes("png")


def covered_scan() -> bytes:
    """The scan, with a white patch laid over the writing — the exam's shape."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(page.rect, stream=scan_bytes())
    page.insert_image(INK_AT, stream=blank_bytes(), keep_proportion=False)
    return doc.tobytes()


def texts(payload) -> str:
    return " ".join(w["text"] for p in payload["pages"] for w in p["words"]).upper()


def test_reads_an_image_the_page_hides():
    """The whole point: the patch is a separate object, so the OCR never sees it.

    This is not a quirk being exploited — it is what Chrome does to every
    scanned PDF it opens, and therefore what the file really exposes.
    """
    assert SECRET in texts(document_words(covered_scan()))


def test_the_composed_page_shows_nothing_of_it():
    """What the eye gets, for contrast: the rendered page is blank there."""
    doc = fitz.open("pdf", covered_scan())
    pm = doc[0].get_pixmap(clip=INK_AT, alpha=False)
    assert min(pm.samples) == max(pm.samples)


def test_words_land_where_the_image_is_drawn():
    doc = fitz.open("pdf", covered_scan())
    page = doc[0]
    words = document_words(covered_scan())["pages"][0]["words"]
    hit = [w for w in words if SECRET in w["text"].upper()]
    assert hit
    assert fitz.Rect(hit[0]["rect"]).intersects(INK_AT)
    assert page.rect.contains(fitz.Rect(hit[0]["rect"]))


def test_placing_and_unplacing_a_rectangle_agree():
    """`ocr.place` and `probe._image_subrect` are inverses, and must stay so."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(fitz.Rect(100, 200, 400, 500), stream=scan_bytes())
    page = fitz.open("pdf", doc.tobytes())[0]
    info = page.get_image_info(xrefs=True)[0]

    norm = (0.25, 0.4, 0.75, 0.6)
    back = _image_subrect(fitz.Rect(place(norm, info, page)), info, page)
    assert back == pytest.approx(norm, abs=0.01)


def test_a_flat_image_is_not_read():
    """A blank patch has nothing to recognise: no engine call, no phantom word."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(page.rect, stream=blank_bytes())
    doc = fitz.open("pdf", doc.tobytes())
    assert image_words(doc, doc[0].get_images(full=True)[0][0]) == []


def test_export_check_catches_readable_pixels_left_in_a_zone():
    """The safety net itself: a scan whose zone did not bite is reported.

    Without this the text-based check would pass an image-only document in
    silence, having looked at a text layer that does not exist.
    """
    zones = {0: [{"rects": [INK_AT]}]}
    leaks = _verify(covered_scan(), zones, {0: 0})
    assert any(lk["kind"] == "image text" and SECRET in lk["text"].upper() for lk in leaks)


def test_export_check_ignores_what_the_zone_only_grazes():
    """A heading the zone's edge clips is not a leak, or every export cries wolf."""
    zones = {0: [{"rects": [fitz.Rect(INK_AT.x0, INK_AT.y1 - 6, INK_AT.x1, INK_AT.y1 + 60)]}]}
    leaks = _verify(covered_scan(), zones, {0: 0})
    assert not [lk for lk in leaks if lk["kind"] == "image text"]
