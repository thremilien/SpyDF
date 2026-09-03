"""Tests for src.ocr: reading a bitmap, and laying the result back as invisible ink."""

import fitz
import pytest

import src.ocr as ocr


class _FakeEngine:
    """A stand-in for RapidOCR: returns canned (box, text, score) triples."""

    def __init__(self, results):
        self.results = results
        self.calls = 0

    def __call__(self, _data):
        self.calls += 1
        return self.results, None


def _not_flat_pixmap(width=100, height=50):
    """A pixmap that is not one single colour, so `_readable` lets it through."""
    pm = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, width, height), False)
    pm.clear_with(255)
    pm.set_rect(fitz.IRect(0, 0, 10, 10), (0, 0, 0))
    return pm


# ---------------------------------------------------------------- available()


def test_available_is_false_without_an_engine():
    """The default fixture (conftest._no_ocr) keeps recognition off."""
    assert ocr.available() is False


def test_available_reflects_a_real_engine(real_ocr):
    assert ocr.available() is True


# ---------------------------------------------------------------- bitmap_words()


def test_bitmap_words_normalises_rects_and_filters_by_score(monkeypatch):
    pm = _not_flat_pixmap()
    results = [
        ([[10, 10], [50, 10], [50, 30], [10, 30]], "hello", 0.9),
        ([[60, 10], [90, 10], [90, 30], [60, 30]], "noise", 0.1),  # below OCR_MIN_SCORE
    ]
    monkeypatch.setattr(ocr, "_get_engine", lambda: _FakeEngine(results))

    words = ocr.bitmap_words(pm)

    assert len(words) == 1
    w = words[0]
    assert w["text"] == "hello"
    assert w["score"] == 0.9
    assert w["rect"] == pytest.approx([0.1, 0.2, 0.5, 0.6], abs=0.01)


def test_a_flat_bitmap_reads_as_nothing(monkeypatch):
    """A blank patch has nothing to recognise: recognition is never even run."""
    pm = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 50), False)
    pm.clear_with(128)
    fake = _FakeEngine([([[0, 0], [10, 0], [10, 10], [0, 10]], "should not be read", 0.99)])
    monkeypatch.setattr(ocr, "_get_engine", lambda: fake)

    assert ocr.bitmap_words(pm) == []
    assert fake.calls == 0


def test_bitmap_words_reads_real_text(real_ocr):
    """The one test where recognition itself is under test."""
    doc = fitz.open()
    page = doc.new_page(width=400, height=200)
    page.insert_text((20, 120), "SECRETNAME", fontsize=40)
    pm = page.get_pixmap(dpi=150, alpha=False)
    doc.close()

    words = ocr.bitmap_words(pm)
    text = " ".join(w["text"] for w in words).upper()
    assert "SECRETNAM" in text


# ---------------------------------------------------------------- write_layer()


def test_write_layer_places_fragments_where_they_were_read():
    doc = fitz.open()
    page = doc.new_page(width=200, height=100)
    words = [{"text": "hello", "score": 0.9, "rect": [0.1, 0.2, 0.5, 0.6]}]

    written = ocr.write_layer(page, words)

    assert written == 1
    found = [w for w in page.get_text("words") if w[4] == "hello"]
    assert found
    hit_rect = fitz.Rect(found[0][:4])
    expected = fitz.Rect(0.1 * 200, 0.2 * 100, 0.5 * 200, 0.6 * 100)
    assert hit_rect.intersects(expected)


def test_a_fragment_outside_latin1_is_skipped_not_substituted():
    """Helvetica only encodes Latin-1: a CJK fragment must be dropped, not
    written as a line of substitution glyphs."""
    doc = fitz.open()
    page = doc.new_page(width=200, height=100)
    words = [{"text": "中文", "score": 0.9, "rect": [0.1, 0.2, 0.5, 0.6]}]

    written = ocr.write_layer(page, words)

    assert written == 0
    assert page.get_text("words") == []


def test_write_layer_is_invisible():
    doc = fitz.open()
    page = doc.new_page(width=200, height=100)
    before = page.get_pixmap().samples

    ocr.write_layer(page, [{"text": "hello", "score": 0.9, "rect": [0.1, 0.2, 0.5, 0.6]}])

    after = page.get_pixmap().samples
    assert before == after
