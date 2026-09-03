"""What a page paints over something else, and what it merely paints."""

import fitz
import pytest

from src.probe import _covers, inspect_document

# The picture fills the page, as a scan does, so a rectangle in page
# coordinates lands on the same spot of the image.
INK_AT = fitz.Rect(50, 390, 400, 440)  # where ink_image() writes


def ink_image(text: str) -> bytes:
    """A picture of a sheet of paper with something written on it."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((INK_AT.x0 + 10, INK_AT.y1 - 12), text, fontsize=28)
    return page.get_pixmap(alpha=False).tobytes("png")


def blank_image() -> bytes:
    """The same sheet, with nothing on it."""
    doc = fitz.open()
    page = doc.new_page()
    return page.get_pixmap(alpha=False).tobytes("png")


def reopen(doc) -> fitz.Document:
    """Round-trip through bytes: the paint log is read from the real file."""
    return fitz.open("pdf", doc.tobytes())


def test_image_over_image_is_a_cover():
    """The exam's case: a white picture pasted over the scan that carries the name."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(fitz.Rect(0, 0, 595, 842), stream=ink_image("SECRETNAME"))
    page.insert_image(INK_AT, stream=blank_image(), keep_proportion=False)

    covers = _covers(reopen(doc)[0])
    assert len(covers) == 1
    assert covers[0]["kind"] == "image"
    assert covers[0]["hides"] == ["image"]
    assert covers[0]["under"]["xref"]
    assert fitz.Rect(covers[0]["rect"]).intersects(INK_AT)


def test_fill_over_text_is_a_cover():
    """A white box over a paragraph, on a page carrying no image at all."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((60, 120), "SECRETNAME", fontsize=24)
    page.draw_rect(fitz.Rect(50, 90, 300, 135), fill=(1, 1, 1), color=None)

    covers = _covers(reopen(doc)[0])
    assert len(covers) == 1
    assert covers[0]["kind"] == "fill"
    assert "text" in covers[0]["hides"]


def test_background_is_not_a_cover():
    """The paper itself is painted first: nothing is under it, so it hides nothing."""
    doc = fitz.open()
    page = doc.new_page()
    page.draw_rect(page.rect, fill=(1, 1, 1), color=None)
    page.insert_text((60, 120), "PUBLICTEXT", fontsize=24)

    assert _covers(reopen(doc)[0]) == []


def test_opaque_shape_over_blank_paper_is_not_a_cover():
    """A design element hides nothing: over an empty margin there is nothing to report."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(fitz.Rect(0, 0, 595, 842), stream=blank_image())
    page.draw_rect(fitz.Rect(40, 300, 400, 380), fill=(0.2, 0.4, 0.9), color=None)

    assert _covers(reopen(doc)[0]) == []


def test_transparent_paint_is_not_a_cover():
    """A see-through box does not hide: it tints, and what is under stays readable."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((60, 120), "SECRETNAME", fontsize=24)
    page.draw_rect(fitz.Rect(50, 90, 300, 135), fill=(1, 1, 1), color=None, fill_opacity=0.4)

    assert _covers(reopen(doc)[0]) == []


def test_cover_colour_is_the_cover_s_own():
    """The zone made from a cover repaints it, so the colour has to come back."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((60, 120), "SECRETNAME", fontsize=24)
    page.draw_rect(fitz.Rect(50, 90, 300, 135), fill=(0.0, 0.0, 0.0), color=None)

    covers = _covers(reopen(doc)[0])
    assert covers and covers[0]["color"] == [0.0, 0.0, 0.0]


def test_covers_reach_the_inspector_payload():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(fitz.Rect(0, 0, 595, 842), stream=ink_image("SECRETNAME"))
    page.insert_image(INK_AT, stream=blank_image(), keep_proportion=False)

    data = inspect_document(doc.tobytes())
    assert len(data["pages"][0]["covers"]) == 1


@pytest.mark.parametrize("n", [1, 2])
def test_same_cover_drawn_twice_is_listed_once(n):
    """A phone's markup stacks a soft-edged patch on a solid one at the same spot."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(fitz.Rect(0, 0, 595, 842), stream=ink_image("SECRETNAME"))
    for _ in range(n):
        page.insert_image(INK_AT, stream=blank_image(), keep_proportion=False)

    assert len(_covers(reopen(doc)[0])) == 1
