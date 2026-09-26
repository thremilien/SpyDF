"""Tests for the export watermark, stamped after flattening on real vector text."""

import math

import fitz
import pytest
from fastapi.testclient import TestClient

from src.app import _apply_watermark, app
from tests.test_redaction import build_pdf, every_byte, open_doc

WATERMARK = "COPIE CONFIDENTIELLE"


@pytest.fixture
def client():
    return TestClient(app)


def export(client, sid, zones=None, deleted_pages=(), watermark=None):
    """Variant of tests/test_redaction.py::export that passes a watermark."""
    r = client.post(
        "/api/export",
        json={
            "sid": sid,
            "zones": zones or {},
            "deleted_pages": list(deleted_pages),
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
    _, out = export(client, sid, watermark=WATERMARK)

    chk = fitz.open(stream=out, filetype="pdf")
    try:
        for page in chk:
            assert WATERMARK in page.get_text()
    finally:
        chk.close()


@pytest.mark.parametrize("raw", [None, "", "   ", "\n\t "])
def test_no_watermark_text_when_field_is_absent_or_blank(client, raw):
    sid = open_doc(client, build_pdf())
    _, out = export(client, sid, watermark=raw)

    chk = fitz.open(stream=out, filetype="pdf")
    try:
        for page in chk:
            # no diagonal text inserted: the page carries only its own bitmap
            assert "COPIE" not in page.get_text()
    finally:
        chk.close()


# ---------------------------------------------------------------- ordering


def test_watermark_is_stamped_after_flattening_as_real_vector_text(client):
    """The regression this file exists for: the watermark must not be baked
    into the bitmap (it would then cross every zone as a leak of its own)."""
    doc = fitz.open()
    doc.new_page(width=595, height=842)
    data = doc.tobytes()
    doc.close()

    sid = open_doc(client, data)
    _, out = export(client, sid, watermark=WATERMARK)

    chk = fitz.open(stream=out, filetype="pdf")
    try:
        page = chk[0]
        assert WATERMARK in page.get_text()
        # exactly one image (the flattened page) plus the vector watermark text
        assert len(page.get_images(full=True)) == 1
        assert page.get_drawings() == []
    finally:
        chk.close()


def test_apply_watermark_adds_only_text_no_new_image_or_drawing():
    """`_apply_watermark` runs on already-flattened bytes: it must add a text
    object and nothing else — no new image, no vector shape."""
    doc = fitz.open()
    doc.new_page(width=595, height=842)
    data = doc.tobytes()
    doc.close()

    before = fitz.open(stream=data, filetype="pdf")[0]
    before_images = len(before.get_images(full=True))

    stamped = _apply_watermark(data, WATERMARK)
    after = fitz.open(stream=stamped, filetype="pdf")[0]

    assert WATERMARK in after.get_text()
    assert len(after.get_images(full=True)) == before_images
    assert after.get_drawings() == []


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
    _, out = export(client, sid, watermark=WATERMARK)

    chk = fitz.open(stream=out, filetype="pdf")
    try:
        assert WATERMARK in chk[0].get_text()
    finally:
        chk.close()


def test_export_with_nothing_at_all_now_succeeds(client):
    """Flattening alone strips the file: no zone, no deletion, no watermark is
    no longer refused."""
    sid = open_doc(client, build_pdf())
    r = client.post(
        "/api/export", json={"sid": sid, "zones": {}, "deleted_pages": [], "watermark": ""}
    )
    assert r.status_code == 200


# ---------------------------------------------------------------- deleted pages


def test_watermark_applies_to_surviving_pages_after_deletion(client):
    doc = fitz.open()
    for i in range(3):
        p = doc.new_page(width=595, height=842)
        p.insert_text((72, 100), f"PAGEMARKER{i}", fontsize=14)
    data = doc.tobytes()
    doc.close()

    sid = open_doc(client, data)
    _, out = export(client, sid, deleted_pages=[0], watermark=WATERMARK)

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
    zones = {
        "0": [
            {
                "type": "rect",
                "points": [[50, 80], [260, 80], [260, 175], [50, 175]],
                "mode": "delete",
            }
        ]
    }
    _, out = export(client, sid, zones, watermark=WATERMARK)
    assert b"SECRETNAMEALPHA" not in every_byte(out)


# ---------------------------------------------------------------- encoding


def test_typographic_characters_are_folded_to_latin1(client):
    """Base-14 Helvetica is Latin-1: an em dash passed through as-is becomes a
    stray glyph on the page. It must be folded, not rendered."""
    sid = open_doc(client, build_pdf())
    _, out = export(client, sid, watermark="COPIE — NE PAS DIFFUSER")

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
            # the rotated text's diagonal, relative to the exported page's: a
            # page larger than A4 comes out shrunk to fit it
            rect = page.rect
            return math.hypot(box.width, box.height) / math.hypot(rect.width, rect.height)
        finally:
            chk.close()

    a4 = span(595, 842)
    scan = span(2480, 3508)
    assert a4 == pytest.approx(scan, rel=0.02), f"A4 {a4:.3f} vs scan {scan:.3f}"
