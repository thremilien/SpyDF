"""Tests for the export watermark, whose ordering against `_verify` is the whole point."""

import math

import fitz
import pytest
from fastapi.testclient import TestClient

from src.app import _apply_watermark, _verify, app
from tests.test_redaction import (
    ZONE,
    _doc_with_survivors,
    build_pdf,
    every_byte,
    open_doc,
    rect_points,
)

WATERMARK = "COPIE CONFIDENTIELLE"


@pytest.fixture
def client():
    return TestClient(app)


def export(client, sid, zones=None, deleted_pages=(), strip_meta=True, watermark=None):
    """Variant of tests/test_redaction.py::export that passes a watermark."""
    r = client.post(
        "/api/export",
        json={
            "sid": sid,
            "zones": zones or {},
            "deleted_pages": list(deleted_pages),
            "strip_meta": strip_meta,
            "watermark": watermark,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    dl = client.get(body["download"])
    assert dl.status_code == 200
    return body, dl.content


# ---------------------------------------------------------------- presence


def test_watermark_appears_on_every_page(client):
    doc = fitz.open()
    for _ in range(3):
        doc.new_page(width=595, height=842)
    data = doc.tobytes()
    doc.close()

    sid = open_doc(client, data)
    zones = {"0": [{"type": "rect", "points": rect_points((50, 80, 200, 120)), "mode": "delete"}]}
    _, out = export(client, sid, zones, watermark=WATERMARK)

    chk = fitz.open(stream=out, filetype="pdf")
    try:
        for page in chk:
            assert WATERMARK in page.get_text()
    finally:
        chk.close()


@pytest.mark.parametrize("raw", [None, "", "   ", "\n\t "])
def test_no_watermark_text_when_field_is_absent_or_blank(client, raw):
    sid = open_doc(client, build_pdf())
    zones = {"0": [{"type": "rect", "points": rect_points(ZONE), "mode": "delete"}]}
    _, out = export(client, sid, zones, watermark=raw)

    chk = fitz.open(stream=out, filetype="pdf")
    try:
        for page in chk:
            # no diagonal text inserted: the page carries only what it already
            # carried.
            assert "COPIE" not in page.get_text()
    finally:
        chk.close()


# ---------------------------------------------------------------- verify/watermark order


def test_watermark_crossing_a_zone_is_not_reported_as_a_leak(client):
    """The regression for the verify-then-watermark order: the watermark crosses
    the zone diagonally and must still raise no leak."""
    doc = fitz.open()
    doc.new_page(width=595, height=842)
    data = doc.tobytes()
    doc.close()

    sid = open_doc(client, data)
    # a zone large and central enough that the watermark diagonal must cross it
    # (the watermark runs bottom-left to top-right).
    zones = {"0": [{"type": "rect", "points": rect_points((150, 300, 450, 550)), "mode": "delete"}]}
    body, out = export(client, sid, zones, watermark=WATERMARK)

    assert body["leak_count"] == 0, body["leaks"]
    assert not any(WATERMARK in leak["text"] for leak in body["leaks"])

    # the watermark is there despite no leak being reported
    chk = fitz.open(stream=out, filetype="pdf")
    try:
        assert WATERMARK in chk[0].get_text()
    finally:
        chk.close()


def test_real_leak_is_still_reported_with_a_watermark(client):
    """The watermark must not mask a real leak.

    Takes the booby-trapped document from test_redaction.py (text, annotation
    and field all left intact inside the zone) and runs it through
    `_apply_watermark` *before* `_verify`, proving the watermark ink does not
    make detected leaks vanish. Were the order reversed, this test would catch
    it.
    """
    zone = {"rect": fitz.Rect(50, 80, 300, 140), "rects": [fitz.Rect(50, 80, 300, 140)]}
    watermarked = _apply_watermark(_doc_with_survivors(), WATERMARK)

    leaks = _verify(watermarked, {0: [zone]}, {0: 0})
    kinds = {lk["kind"] for lk in leaks}
    assert kinds == {"text", "annotation", "field"}, leaks
    texts = " ".join(lk["text"] for lk in leaks)
    assert "STILLHERE" in texts


# ---------------------------------------------------------------- geometry


def test_watermark_ink_stays_inside_the_page_rect(client):
    doc = fitz.open()
    doc.new_page(width=595, height=842)
    doc.new_page(width=300, height=800)  # page etroite: cas limite pour la taille de police
    data = doc.tobytes()
    doc.close()

    sid = open_doc(client, data)
    _, out = export(client, sid, watermark=WATERMARK)

    chk = fitz.open(stream=out, filetype="pdf")
    try:
        for page in chk:
            hits = page.search_for(WATERMARK)
            assert hits, "le filigrane devrait etre localisable par recherche de texte"
            for hit in hits:
                assert page.rect.contains(hit), (page.rect, hit)
    finally:
        chk.close()


# ---------------------------------------------------------------- empty guard


def test_watermark_only_export_succeeds(client):
    sid = open_doc(client, build_pdf())
    body, out = export(client, sid, watermark=WATERMARK)
    assert body["leak_count"] == 0

    chk = fitz.open(stream=out, filetype="pdf")
    try:
        assert WATERMARK in chk[0].get_text()
    finally:
        chk.close()


def test_export_with_nothing_at_all_is_still_refused(client):
    sid = open_doc(client, build_pdf())
    r = client.post(
        "/api/export", json={"sid": sid, "zones": {}, "deleted_pages": [], "watermark": ""}
    )
    assert r.status_code == 400


# ---------------------------------------------------------------- deleted pages


def test_watermark_applies_to_surviving_pages_after_deletion(client):
    doc = fitz.open()
    for i in range(3):
        p = doc.new_page(width=595, height=842)
        p.insert_text((72, 100), f"PAGEMARKER{i}", fontsize=14)
    data = doc.tobytes()
    doc.close()

    sid = open_doc(client, data)
    body, out = export(client, sid, deleted_pages=[0], watermark=WATERMARK)

    chk = fitz.open(stream=out, filetype="pdf")
    try:
        assert chk.page_count == 2
        for page in chk:
            assert WATERMARK in page.get_text()
    finally:
        chk.close()


# ---------------------------------------------------------------- existing guarantees


def test_redaction_guarantees_hold_with_a_watermark(client):
    sid = open_doc(client, build_pdf())
    zones = {"0": [{"type": "rect", "points": rect_points(ZONE), "mode": "delete"}]}
    body, out = export(client, sid, zones, watermark=WATERMARK)

    assert b"SECRETNAMEALPHA" not in every_byte(out)
    assert body["leak_count"] == 0, body["leaks"]


# ---------------------------------------------------------------- encoding


def test_typographic_characters_are_folded_to_latin1(client):
    """Base-14 Helvetica is Latin-1: an em dash passed through as-is becomes a
    stray glyph on the page. It must be folded, not rendered."""
    sid = open_doc(client, build_pdf())
    zones = {"0": [{"type": "rect", "points": rect_points(ZONE), "mode": "delete"}]}
    _, out = export(client, sid, zones, watermark="COPIE — NE PAS DIFFUSER")

    chk = fitz.open(stream=out, filetype="pdf")
    try:
        text = chk[0].get_text()
    finally:
        chk.close()
    assert "COPIE - NE PAS DIFFUSER" in text
    assert "·" not in text and "—" not in text


def test_watermark_size_does_not_depend_on_the_page_scale(client):
    """Same page, same watermark, two MediaBox scales.

    A scan whose box is in pixels (2480x3508) must get the same watermark, in
    proportion, as an A4 in points (595x842). An absolute font-size cap dropped
    the second one to 14% of the diagonal.
    """

    def span(width, height):
        doc = fitz.open()
        doc.new_page(width=width, height=height)
        data = doc.tobytes()
        doc.close()
        sid = open_doc(client, data)
        _, out = export(client, sid, watermark="COPIE")
        chk = fitz.open(stream=out, filetype="pdf")
        try:
            page = chk[0]
            hits = page.search_for("COPIE")
            assert hits, "filigrane absent"
            box = hits[0]
            # the rotated text's diagonal, relative to the page's
            return math.hypot(box.width, box.height) / math.hypot(width, height)
        finally:
            chk.close()

    a4 = span(595, 842)
    scan = span(2480, 3508)
    assert a4 == pytest.approx(scan, rel=0.02), f"A4 {a4:.3f} vs scan {scan:.3f}"
