"""Tests du filigrane ajoute a l'export.

Le point sensible est l'ordre des operations dans `/api/export`: la
verification des fuites (`_verify`) doit porter sur le PDF *avant* le
filigrane, sinon le trait diagonal du filigrane serait signale comme une
fuite sur chaque page. Voir le commentaire dans `src/app.py`.
"""

import fitz
import pytest
from fastapi.testclient import TestClient

from src.app import _apply_watermark, _verify, app
from tests.test_redaction import ZONE, _doc_with_survivors, build_pdf, every_byte, open_doc, rect_points

WATERMARK = "COPIE CONFIDENTIELLE"


@pytest.fixture
def client():
    return TestClient(app)


def export(client, sid, zones=None, deleted_pages=(), strip_meta=True, watermark=None):
    """Variante de tests/test_redaction.py::export qui transmet un filigrane."""
    r = client.post("/api/export", json={
        "sid": sid, "zones": zones or {},
        "deleted_pages": list(deleted_pages), "strip_meta": strip_meta,
        "watermark": watermark,
    })
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
            # aucune insertion de texte diagonal: la page ne porte que ce
            # qu'elle portait deja.
            assert "COPIE" not in page.get_text()
    finally:
        chk.close()


# ---------------------------------------------------------------- ordre verify/filigrane


def test_watermark_crossing_a_zone_is_not_reported_as_a_leak(client):
    """La regression pour l'ordre verify-puis-filigrane: le filigrane traverse
    la zone en diagonale, et ne doit pourtant declencher aucune fuite."""
    doc = fitz.open()
    doc.new_page(width=595, height=842)
    data = doc.tobytes()
    doc.close()

    sid = open_doc(client, data)
    # une zone assez grande, centree, pour que la diagonale du filigrane la
    # traverse forcement (le filigrane va du coin bas-gauche au coin haut-droit).
    zones = {"0": [{"type": "rect", "points": rect_points((150, 300, 450, 550)), "mode": "delete"}]}
    body, out = export(client, sid, zones, watermark=WATERMARK)

    assert body["leak_count"] == 0, body["leaks"]
    assert not any(WATERMARK in leak["text"] for leak in body["leaks"])

    # le filigrane est bien present malgre l'absence de fuite signalee
    chk = fitz.open(stream=out, filetype="pdf")
    try:
        assert WATERMARK in chk[0].get_text()
    finally:
        chk.close()


def test_real_leak_is_still_reported_with_a_watermark(client):
    """Le filigrane ne doit pas masquer une vraie fuite. On reprend le
    document piege de test_redaction.py (texte, annotation et champ tous
    laisses intacts dans la zone) et on le fait passer par
    `_apply_watermark` *avant* `_verify`, pour prouver que l'encre du
    filigrane ne fait pas disparaitre les fuites detectees: si l'ordre
    verify-puis-filigrane etait invers, ce test le detecterait."""
    zone = {"rect": fitz.Rect(50, 80, 300, 140), "rects": [fitz.Rect(50, 80, 300, 140)]}
    watermarked = _apply_watermark(_doc_with_survivors(), WATERMARK)

    leaks = _verify(watermarked, {0: [zone]}, {0: 0})
    kinds = {l["kind"] for l in leaks}
    assert kinds == {"texte", "annotation", "champ"}, leaks
    texts = " ".join(l["text"] for l in leaks)
    assert "STILLHERE" in texts


# ---------------------------------------------------------------- geometrie


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


# ---------------------------------------------------------------- garde vide


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
    r = client.post("/api/export", json={"sid": sid, "zones": {}, "deleted_pages": [], "watermark": ""})
    assert r.status_code == 400


# ---------------------------------------------------------------- pages supprimees


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


# ---------------------------------------------------------------- garanties existantes


def test_redaction_guarantees_hold_with_a_watermark(client):
    sid = open_doc(client, build_pdf())
    zones = {"0": [{"type": "rect", "points": rect_points(ZONE), "mode": "delete"}]}
    body, out = export(client, sid, zones, watermark=WATERMARK)

    assert b"SECRETNAMEALPHA" not in every_byte(out)
    assert body["leak_count"] == 0, body["leaks"]


# ---------------------------------------------------------------- encodage


def test_typographic_characters_are_folded_to_latin1(client):
    """Helvetica base-14 est du Latin-1: un tiret cadratin sorti tel quel
    devient un glyphe parasite sur la page. Il doit etre replie, pas rendu."""
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
