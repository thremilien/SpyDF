"""Regression tests for the redaction path: every trace here once survived export."""

import contextlib

import fitz
import pytest
from fastapi.testclient import TestClient

from src.app import _shape_mask, _verify, app

# Zone as drawn by the user, in PDF coordinates.
ZONE = (50, 80, 260, 175)

# Markers that must be gone, and where they sit in the source PDF.
SECRETS = [
    "SECRETNAMEALPHA",  # texte
    "ANNOTBODYGAMMA",  # corps d'une annotation
    "ANNOTAUTHORDELTA",  # annotation author (/T)
    "FIELDNAMEEPSILON",  # nom d'un champ de formulaire
    "FIELDVALUEZETA",  # valeur du champ
    "TOCNAMEETA",  # bookmark ("... 's copy")
    "ATTACHNAMETHETA",  # attachment
    "ATTACHDATAIOTA",
    "LAYERNAMEKAPPA",  # layer name in /OCProperties
    "METAAUTHORLAMBDA",  # metadata
    "METATITLEMU",
    "XMPMARKERNU",  # XMP
]

PUBLIC = "PUBLICTEXTBETA"  # outside the zone: must survive


@pytest.fixture
def client():
    return TestClient(app)


def build_pdf() -> bytes:
    """A booby-trapped PDF: every class of identifying trace is present."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)

    page.insert_text((72, 100), "SECRETNAMEALPHA", fontsize=14)
    page.insert_text((72, 400), PUBLIC, fontsize=14)

    # line art running past the zone: the "signature over the edge" case.
    page.draw_line(fitz.Point(60, 150), fitz.Point(320, 150), width=2)
    page.draw_line(fitz.Point(60, 160), fitz.Point(320, 160), width=2)

    annot = page.add_text_annot(fitz.Point(100, 120), "ANNOTBODYGAMMA")
    annot.set_info(title="ANNOTAUTHORDELTA", content="ANNOTBODYGAMMA")
    annot.update()

    widget = fitz.Widget()
    widget.rect = fitz.Rect(80, 105, 240, 130)
    widget.field_name = "FIELDNAMEEPSILON"
    widget.field_value = "FIELDVALUEZETA"
    widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    page.add_widget(widget)

    ocg = doc.add_ocg("LAYERNAMEKAPPA")
    page.insert_text((72, 500), "texte sur calque", fontsize=11, oc=ocg)

    doc.set_toc([[1, "TOCNAMEETA", 1]])
    doc.embfile_add("ATTACHNAMETHETA", b"ATTACHDATAIOTA")
    doc.set_metadata({"author": "METAAUTHORLAMBDA", "title": "METATITLEMU"})
    doc.set_xml_metadata(
        '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        "<rdf:Description>XMPMARKERNU</rdf:Description>"
        '</rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
    )

    out = doc.tobytes()
    doc.close()
    return out


def every_byte(pdf: bytes) -> bytes:
    """Raw bytes plus every object and decompressed stream.

    A marker hidden in a compressed stream does not show up in the file as-is.
    """
    chunks = [pdf]
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        for xref in range(1, doc.xref_length()):
            with contextlib.suppress(Exception):
                chunks.append(doc.xref_object(xref, compressed=False).encode("utf-8", "replace"))
            try:
                if doc.xref_is_stream(xref):
                    chunks.append(doc.xref_stream(xref))
            except Exception:
                pass
    finally:
        doc.close()
    return b"\n".join(chunks)


def open_doc(client, data: bytes) -> str:
    r = client.post("/api/open", files={"file": ("exam.pdf", data, "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()["sid"]


def rect_points(rect):
    x0, y0, x1, y1 = rect
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def export(client, sid, zones, deleted_pages=(), strip_meta=True):
    r = client.post(
        "/api/export",
        json={
            "sid": sid,
            "zones": zones,
            "deleted_pages": list(deleted_pages),
            "strip_meta": strip_meta,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    dl = client.get(body["download"])
    assert dl.status_code == 200
    return body, dl.content


# ---------------------------------------------------------------- leaks


def test_no_identifying_trace_survives_export(client):
    sid = open_doc(client, build_pdf())
    zones = {"0": [{"type": "rect", "points": rect_points(ZONE), "mode": "delete"}]}
    body, out = export(client, sid, zones)

    haystack = every_byte(out)
    survivors = [m for m in SECRETS if m.encode() in haystack]
    assert not survivors, f"traces encore presentes dans le PDF exporte: {survivors}"


def test_export_reports_no_leak(client):
    sid = open_doc(client, build_pdf())
    zones = {"0": [{"type": "rect", "points": rect_points(ZONE), "mode": "delete"}]}
    body, _ = export(client, sid, zones)
    assert body["leak_count"] == 0, body["leaks"]


def test_content_outside_the_zone_is_kept(client):
    sid = open_doc(client, build_pdf())
    zones = {"0": [{"type": "rect", "points": rect_points(ZONE), "mode": "delete"}]}
    _, out = export(client, sid, zones)

    doc = fitz.open(stream=out, filetype="pdf")
    try:
        assert PUBLIC in doc[0].get_text()
    finally:
        doc.close()


def test_line_art_crossing_the_zone_edge_is_removed(client):
    """PyMuPDF's default (REMOVE_IF_COVERED) left any stroke running past the
    zone intact, whole under the white cover."""
    sid = open_doc(client, build_pdf())
    zones = {"0": [{"type": "rect", "points": rect_points(ZONE), "mode": "delete"}]}
    _, out = export(client, sid, zones)

    doc = fitz.open(stream=out, filetype="pdf")
    try:
        # right of the zone, at the height of the lines: no drawing left
        beyond = fitz.Rect(ZONE[2] + 5, 140, 400, 170)
        strays = [d for d in doc[0].get_drawings() if d["rect"].intersects(beyond)]
        assert not strays, f"trace vectoriel survivant: {strays}"
    finally:
        doc.close()


def test_pixelate_destroys_the_source_text(client):
    """The mosaic is a real downsample: the original text must not survive under
    the image."""
    sid = open_doc(client, build_pdf())
    zones = {"0": [{"type": "rect", "points": rect_points(ZONE), "mode": "pixelate"}]}
    body, out = export(client, sid, zones)

    assert b"SECRETNAMEALPHA" not in every_byte(out)
    assert body["leak_count"] == 0, body["leaks"]


# A triangle, wide at the top and pointed at the bottom: its bounding box juts
# out well to either side, at the heights where text sits.
TRIANGLE = [[60, 60], [400, 60], [230, 140]]


def test_non_rectangular_zone_follows_its_outline(client):
    """What disappears follows the stroke, not its bounding box: a word inside
    the box but outside the outline survives."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((175, 100), "INSIDETRIANGLE", fontsize=12)
    page.insert_text((62, 100), "OUTSIDE", fontsize=12)
    data = doc.tobytes()
    doc.close()

    sid = open_doc(client, data)
    zones = {"0": [{"type": "polygon", "points": TRIANGLE, "mode": "delete"}]}
    body, out = export(client, sid, zones)

    haystack = every_byte(out)
    assert b"INSIDETRIANGLE" not in haystack
    assert b"OUTSIDE" in haystack
    assert body["leak_count"] == 0, body["leaks"]


def test_polygon_on_a_scan_does_not_whiten_its_bounding_box(client):
    """The case that made the bounding box unacceptable: on an image page,
    PDF_REDACT_IMAGE_PIXELS whitened the whole box under a cover that itself
    followed the outline."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    grey = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 300, 400), False)
    grey.clear_with(90)
    page.insert_image(page.rect, pixmap=grey)
    data = doc.tobytes()
    doc.close()

    sid = open_doc(client, data)
    zones = {"0": [{"type": "polygon", "points": TRIANGLE, "mode": "delete"}]}
    _, out = export(client, sid, zones)

    chk = fitz.open(stream=out, filetype="pdf")
    try:
        pm = chk[0].get_pixmap()  # page A4 rendue a l'echelle 1
        # (70, 130): inside the bounding box, outside the triangle
        assert pm.pixel(70, 130) == (90, 90, 90)
        # (230, 80): squarely inside the triangle
        assert pm.pixel(230, 80) == (255, 255, 255)
    finally:
        chk.close()


def test_pixelated_polygon_mosaic_stays_inside_the_outline():
    """The mosaic is captured over the bounding box: its alpha mask is what keeps
    it from covering the edges."""
    points = [fitz.Point(*p) for p in TRIANGLE]
    rect = fitz.Rect(60, 60, 400, 140)
    mask = _shape_mask(points, rect)

    def at(x, y):
        i = int((x - rect.x0) / rect.width * mask.width)
        j = int((y - rect.y0) / rect.height * mask.height)
        return mask.pixel(i, j)[0]

    assert at(230, 70) == 255  # dans le triangle: la mosaique s'affiche
    assert at(70, 130) == 0  # dans le rectangle, hors du triangle: transparent
    assert at(390, 130) == 0


def test_pixelated_polygon_on_a_scan_keeps_the_outside_intact(client):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    grey = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 300, 400), False)
    grey.clear_with(90)
    page.insert_image(page.rect, pixmap=grey)
    page.insert_text((175, 100), "SECRETNAMEALPHA", fontsize=12)
    data = doc.tobytes()
    doc.close()

    sid = open_doc(client, data)
    zones = {"0": [{"type": "polygon", "points": TRIANGLE, "mode": "pixelate"}]}
    _, out = export(client, sid, zones)

    assert b"SECRETNAMEALPHA" not in every_byte(out)
    chk = fitz.open(stream=out, filetype="pdf")
    try:
        assert chk[0].get_pixmap().pixel(70, 130) == (90, 90, 90)
    finally:
        chk.close()


# ---------------------------------------------------------------- pages


def test_deleted_page_is_gone_and_zones_still_map(client):
    doc = fitz.open()
    for i in range(3):
        p = doc.new_page(width=595, height=842)
        p.insert_text((72, 100), f"PAGEMARKER{i}", fontsize=14)
    data = doc.tobytes()
    doc.close()

    sid = open_doc(client, data)
    zones = {"2": [{"type": "rect", "points": rect_points((50, 80, 300, 120)), "mode": "delete"}]}
    body, out = export(client, sid, zones, deleted_pages=[0])

    assert body["leak_count"] == 0, body["leaks"]
    haystack = every_byte(out)
    assert b"PAGEMARKER0" not in haystack  # page supprimee
    assert b"PAGEMARKER2" not in haystack  # page 2 redigee (devenue page 1)
    assert b"PAGEMARKER1" in haystack  # page intacte

    chk = fitz.open(stream=out, filetype="pdf")
    try:
        assert chk.page_count == 2
    finally:
        chk.close()


def test_cannot_delete_every_page(client):
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    sid = open_doc(client, data)
    r = client.post("/api/export", json={"sid": sid, "zones": {}, "deleted_pages": [0]})
    assert r.status_code == 400


# ---------------------------------------------------------------- verification


def _zone(rect):
    """A zone as /api/export prepares it: its outline, and the rectangles really
    redacted — one here, the zone being rectangular."""
    return {"rect": rect, "rects": [rect]}


def _doc_with_survivors() -> bytes:
    """A PDF where text, annotation and field all sit in the zone: exactly what a
    failed export would produce."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "STILLHERE", fontsize=14)
    annot = page.add_text_annot(fitz.Point(100, 110), "reste")
    annot.set_info(title="AUTEURRESTANT", content="reste")
    annot.update()
    widget = fitz.Widget()
    widget.rect = fitz.Rect(150, 90, 250, 115)
    widget.field_name = "CHAMPRESTANT"
    widget.field_value = "v"
    widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    page.add_widget(widget)
    out = doc.tobytes()
    doc.close()
    return out


def test_verify_catches_text_annotations_and_fields():
    """The check runs on the exported bytes and reports all three families of
    residue, not just text."""
    zone = _zone(fitz.Rect(50, 80, 300, 140))
    leaks = _verify(_doc_with_survivors(), {0: [zone]}, {0: 0})

    kinds = {lk["kind"] for lk in leaks}
    assert kinds == {"text", "annotation", "field"}, leaks
    # get_text("words") can weld the word to the neighbouring field value
    texts = " ".join(lk["text"] for lk in leaks)
    assert "STILLHERE" in texts
    assert "AUTEURRESTANT" in texts
    assert "CHAMPRESTANT" in texts
    assert all(lk["page"] == 1 for lk in leaks)


def test_verify_reports_the_page_number_of_the_exported_file():
    """Zones are indexed on the source document; after deleting pages, a leak
    must be reported at its number in the produced file."""
    zone = _zone(fitz.Rect(50, 80, 300, 140))
    # zone on original page 4, which becomes page 1 (index 0) in the export
    leaks = _verify(_doc_with_survivors(), {3: [zone]}, {3: 0})
    assert leaks and all(lk["page"] == 1 for lk in leaks)

    # page gone from the exported document: nothing to check, and no crash
    assert _verify(_doc_with_survivors(), {3: [zone]}, {}) == []


# ---------------------------------------------------------------- web


def test_rejects_non_pdf(client):
    r = client.post("/api/open", files={"file": ("x.pdf", b"not a pdf", "application/pdf")})
    assert r.status_code == 400


def test_rejects_password_protected_pdf(client):
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    doc.close()
    r = client.post("/api/open", files={"file": ("x.pdf", data, "application/pdf")})
    assert r.status_code == 400
    assert "password" in r.text


def test_unknown_session(client):
    r = client.post("/api/export", json={"sid": "deadbeef", "zones": {}, "deleted_pages": [0]})
    assert r.status_code == 404


def test_export_without_zones_is_refused(client):
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    sid = open_doc(client, data)
    r = client.post("/api/export", json={"sid": sid, "zones": {}, "deleted_pages": []})
    assert r.status_code == 400


def test_download_headers_are_safe(client):
    sid = open_doc(client, build_pdf())
    zones = {"0": [{"type": "rect", "points": rect_points(ZONE), "mode": "delete"}]}
    body, _ = export(client, sid, zones)
    r = client.get(body["download"])
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert "\n" not in r.headers["content-disposition"]


def test_filename_is_sanitised(client):
    data = build_pdf()
    r = client.post(
        "/api/open",
        files={
            "file": ('../../etc/pa"ss\r\nwd.pdf', data, "application/pdf"),
        },
    )
    assert r.status_code == 200
    name = r.json()["name"]
    assert "/" not in name and '"' not in name and "\r" not in name and "\n" not in name
