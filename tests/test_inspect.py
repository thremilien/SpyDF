"""Tests for the read-only inspection of a document's invisible payload."""

import fitz
import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.probe import inspect_document
from tests.test_redaction import build_pdf, open_doc


@pytest.fixture
def client():
    return TestClient(app)


def test_inspect_surfaces_every_hidden_carrier(client):
    """Every family of trace must show up in the report: that is the whole point
    of the pane, showing what a PDF reader does not."""
    sid = open_doc(client, build_pdf())
    r = client.get(f"/api/inspect/{sid}")
    assert r.status_code == 200
    d = r.json()

    doc, page = d["doc"], d["pages"][0]
    assert {m["value"] for m in doc["metadata"]} >= {"METAAUTHORLAMBDA", "METATITLEMU"}
    assert "XMPMARKERNU" in doc["xmp"]
    assert [t["title"] for t in doc["toc"]] == ["TOCNAMEETA"]
    assert [a["name"] for a in doc["attachments"]] == ["ATTACHNAMETHETA"]
    assert [lk["name"] for lk in doc["layers"]] == ["LAYERNAMEKAPPA"]
    assert doc["fonts"]

    assert any(
        a["author"] == "ANNOTAUTHORDELTA" and a["content"] == "ANNOTBODYGAMMA"
        for a in page["annots"]
    )
    assert any(
        w["name"] == "FIELDNAMEEPSILON" and w["value"] == "FIELDVALUEZETA" for w in page["widgets"]
    )

    spans = [s for b in page["blocks"] for ln in b["lines"] for s in ln["spans"]]
    assert "SECRETNAMEALPHA" in {s["text"] for s in spans}
    assert all(len(s["rect"]) == 4 for s in spans)  # positionnable dans le panneau


def test_inspect_reports_invisible_text():
    """An OCR layer, or deliberately hidden text, is indexed and copyable while
    never being displayed: the most interesting case."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "VISIBLE", fontsize=12)
    page.insert_text((72, 130), "INVISIBLE", fontsize=12, render_mode=3)
    data = doc.tobytes()
    doc.close()

    spans = [
        s
        for b in inspect_document(data)["pages"][0]["blocks"]
        for ln in b["lines"]
        for s in ln["spans"]
    ]
    by_text = {s["text"]: s for s in spans}
    assert by_text["INVISIBLE"]["hidden"] is True
    assert by_text["VISIBLE"]["hidden"] is False


def test_inspect_reports_links_and_images():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_link(
        {
            "kind": fitz.LINK_URI,
            "from": fitz.Rect(10, 10, 60, 30),
            "uri": "https://exemple.test/eleve",
        }
    )
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8), False)
    pix.clear_with(120)
    page.insert_image(fitz.Rect(100, 100, 200, 200), pixmap=pix)
    data = doc.tobytes()
    doc.close()

    p = inspect_document(data)["pages"][0]
    assert any(lk["kind"] == "url" and "exemple.test" in lk["target"] for lk in p["links"])
    assert p["images"] and p["images"][0]["rect"]


def test_inspect_reports_javascript():
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    doc = fitz.open(stream=data, filetype="pdf")
    xref = doc.get_new_xref()
    doc.update_object(xref, "<< /S /JavaScript /JS (app.alert\\('trace'\\);) >>")
    data = doc.tobytes()
    doc.close()

    js = inspect_document(data)["doc"]["javascript"]
    assert js and "app.alert" in js[0]["code"]


def test_inspect_is_read_only(client):
    """The pane must change nothing: the session document stays the original."""
    original = build_pdf()
    sid = open_doc(client, original)
    client.get(f"/api/inspect/{sid}")
    from src.app import DOCS

    assert DOCS[sid]["bytes"] == original


def test_inspect_unknown_session(client):
    assert client.get("/api/inspect/deadbeef").status_code == 404


def test_inspect_survives_a_bare_document(client):
    """A PDF free of any trace must not make the read fail."""
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    sid = open_doc(client, data)
    d = client.get(f"/api/inspect/{sid}").json()
    assert d["doc"]["page_count"] == 1
    assert d["doc"]["toc"] == [] and d["doc"]["attachments"] == []
    assert d["pages"][0]["blocks"] == []
    assert d["truncated"] is False
