"""Tests for the read-only inspection of a document's invisible payload."""

import fitz
import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.probe import inspect_document
from tests.test_redaction import build_pdf, open_doc, rect_points


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


def build_tagged_scan() -> bytes:
    """A scanned page whose only text hangs off the structure tree.

    That is the shape a graded-exam export takes: an image of the copy, and an
    /Alt describing it. No zone can reach that text — it is not page content —
    so the pane has to name it and the scrubbing has to remove it.
    """
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.draw_rect(fitz.Rect(50, 50, 545, 792), color=(0, 0, 0))

    elem = doc.get_new_xref()
    doc.update_object(
        elem,
        "<</Type/StructElem/S/Figure/Alt(STRUCTALTOMICRON)"
        f"/ActualText(STRUCTACTUALPI)/K[0]/Pg {page.xref} 0 R>>",
    )
    root = doc.get_new_xref()
    doc.update_object(root, f"<</Type/StructTreeRoot/K[{elem} 0 R]>>")
    doc.xref_set_key(doc.pdf_catalog(), "StructTreeRoot", f"{root} 0 R")

    out = doc.tobytes()
    doc.close()
    return out


def test_inspect_reports_text_carried_by_the_structure_tree():
    """A page can read as "no text" and still carry some: the pane must say so."""
    page = inspect_document(build_tagged_scan())["pages"][0]
    assert page["blocks"] == []  # nothing in the page content
    assert [e["text"] for e in page["struct"]] == ["STRUCTALTOMICRON", "STRUCTACTUALPI"]
    assert [e["kind"] for e in page["struct"]] == ["alternative text", "actual text"]


def test_structure_tree_text_is_scrubbed_on_export(client):
    """It survives redaction by construction, so only the scrubbing can take it
    out — and it used to survive that too."""
    data = build_tagged_scan()
    sid = open_doc(client, data)
    r = client.post(
        "/api/export",
        json={
            "sid": sid,
            "zones": {"0": [{"type": "rect", "points": rect_points((10, 10, 30, 30))}]},
            "strip_meta": True,
            "deleted_pages": [],
        },
    )
    assert r.status_code == 200, r.text
    out = client.get(r.json()["download"]).content
    assert b"STRUCTALTOMICRON" not in out
    assert b"STRUCTACTUALPI" not in out
    assert not inspect_document(out)["pages"][0]["struct"]


def test_structure_tree_text_is_kept_without_the_checkbox(client):
    """Unchecked, the box promises nothing is stripped: it must not lie either."""
    sid = open_doc(client, build_tagged_scan())
    r = client.post(
        "/api/export",
        json={
            "sid": sid,
            "zones": {"0": [{"type": "rect", "points": rect_points((10, 10, 30, 30))}]},
            "strip_meta": False,
            "deleted_pages": [],
        },
    )
    assert r.status_code == 200, r.text
    out = client.get(r.json()["download"]).content
    assert [e["text"] for e in inspect_document(out)["pages"][0]["struct"]] == [
        "STRUCTALTOMICRON",
        "STRUCTACTUALPI",
    ]


def build_covered_scan() -> bytes:
    """A scan with a red block, hidden under an opaque white rectangle.

    The shape ilovepdf-style "redaction" takes, and the one the pane exists for:
    the page reads blank there, the image still holds every pixel.
    """
    scan = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 400, 400), False)
    scan.set_rect(scan.irect, (255, 255, 255))
    scan.set_rect(fitz.IRect(40, 40, 200, 90), (255, 0, 0))  # the "name"

    doc = fitz.open()
    page = doc.new_page(width=300, height=300)
    page.insert_image(fitz.Rect(20, 20, 280, 280), pixmap=scan)
    page.draw_rect(COVERED_RECT, color=None, fill=(1, 1, 1))

    out = doc.tobytes()
    doc.close()
    return out


COVERED_RECT = fitz.Rect(45, 45, 150, 80)  # over the red block, inside the image


def red_pixels(data: bytes) -> int:
    """How many red pixels the page's embedded image still holds."""
    doc = fitz.open(stream=data, filetype="pdf")
    page = doc[0]
    xref = page.get_images(full=True)[0][0]
    pm = fitz.Pixmap(doc.extract_image(xref)["image"])
    n = sum(
        1
        for y in range(pm.height)
        for x in range(pm.width)
        if pm.pixel(x, y)[0] > 200 and pm.pixel(x, y)[1] < 80 and pm.pixel(x, y)[2] < 80
    )
    doc.close()
    return n


def test_inspect_reports_an_opaque_cover_over_a_scan():
    """A white box is not an erasure: the pane must say so, since the page shows
    nothing there and nothing invites a zone."""
    page = inspect_document(build_covered_scan())["pages"][0]
    assert len(page["covers"]) == 1
    cov = page["covers"][0]
    assert cov["color"] == [1.0, 1.0, 1.0]
    assert fitz.Rect(cov["rect"]).intersects(COVERED_RECT)


def test_a_page_background_is_not_reported_as_a_cover():
    """The white rectangle a page is painted on contains the image: flagging it
    would cry wolf on every scan."""
    doc = fitz.open()
    page = doc.new_page(width=300, height=300)
    page.draw_rect(page.rect, color=None, fill=(1, 1, 1))
    pm = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100), False)
    pm.set_rect(pm.irect, (200, 200, 200))
    page.insert_image(fitz.Rect(50, 50, 250, 250), pixmap=pm)
    data = doc.tobytes()
    doc.close()

    assert inspect_document(data)["pages"][0]["covers"] == []


def test_a_zone_over_a_cover_destroys_the_pixels_underneath(client):
    """What the pane promises, the export must deliver: redacting a covered area
    has to reach the image, not just repaint the box."""
    data = build_covered_scan()
    assert red_pixels(data) > 100  # the name is there, under the cover

    sid = open_doc(client, data)
    r = client.post(
        "/api/export",
        json={
            "sid": sid,
            "zones": {"0": [{"type": "rect", "points": rect_points(tuple(COVERED_RECT))}]},
            "strip_meta": True,
            "deleted_pages": [],
        },
    )
    assert r.status_code == 200, r.text
    out = client.get(r.json()["download"]).content
    assert red_pixels(out) == 0
