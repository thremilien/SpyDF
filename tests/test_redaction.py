"""Regression tests for the redaction path: every trace here once survived export.

The export flattens every page to a bitmap and re-indexes it with OCR, so a
class of leak that used to need a dedicated carrier (an annotation's author, a
form field's value, the structure tree, metadata...) simply has nowhere left to
be: rasterising the page is what removes it, for every export, whether or not a
zone was drawn.
"""

import contextlib

import fitz
import pytest
from fastapi.testclient import TestClient

from src.app import app

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

PUBLIC = "PUBLICTEXTBETA"  # outside the zone: must survive, on the page


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


def export(client, sid, zones=None, deleted_pages=()):
    r = client.post(
        "/api/export",
        json={"sid": sid, "zones": zones or {}, "deleted_pages": list(deleted_pages)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    dl = client.get(body["download"])
    assert dl.status_code == 200
    return body, dl.content


# ---------------------------------------------------------------- leaks


def test_no_identifying_trace_survives_export(client):
    """Flattening strips every trace, not only the one under a drawn zone."""
    sid = open_doc(client, build_pdf())
    zones = {"0": [{"type": "rect", "points": rect_points(ZONE), "mode": "delete"}]}
    body, out = export(client, sid, zones)

    page_text = fitz.open(stream=out, filetype="pdf")[0].get_text()
    survivors_text = [m for m in SECRETS if m in page_text]
    haystack = every_byte(out)
    survivors_bytes = [m for m in SECRETS if m.encode() in haystack]
    assert not survivors_text, f"traces still in the text layer: {survivors_text}"
    assert not survivors_bytes, f"traces still in the raw file: {survivors_bytes}"
    assert body["ocr"] == "unavailable"  # the default fixture keeps recognition off


def test_no_identifying_trace_survives_export_with_no_zone_at_all(client):
    """Flattening alone strips the file: nothing needs to be drawn."""
    sid = open_doc(client, build_pdf())
    _, out = export(client, sid)

    haystack = every_byte(out)
    survivors = [m for m in SECRETS if m.encode() in haystack]
    assert not survivors, f"traces still in the file with no zone drawn: {survivors}"


def test_pixels_outside_the_zone_are_intact(client):
    """The real guarantee with recognition off: nothing but the zone is painted."""
    sid = open_doc(client, build_pdf())
    zones = {"0": [{"type": "rect", "points": rect_points(ZONE), "mode": "delete"}]}
    _, out = export(client, sid, zones)

    # PUBLIC sits at (72, 400), well below the zone (which ends at y=175): its
    # rendered pixels must be unaffected, even though it left no text layer of
    # its own without OCR.
    doc = fitz.open(stream=out, filetype="pdf")
    try:
        pm = doc[0].get_pixmap(clip=fitz.Rect(60, 385, 300, 415))
    finally:
        doc.close()
    assert min(pm.samples) != max(pm.samples), "text outside the zone should still be visible"


def test_public_text_survives_export_and_reads_back_with_ocr(client, real_ocr):
    """With recognition on, text outside the zone comes back in the text layer."""
    sid = open_doc(client, build_pdf())
    zones = {"0": [{"type": "rect", "points": rect_points(ZONE), "mode": "delete"}]}
    body, out = export(client, sid, zones)
    assert body["ocr"] in ("ok", "partial")

    doc = fitz.open(stream=out, filetype="pdf")
    try:
        text = doc[0].get_text().upper()
    finally:
        doc.close()
    # robust to OCR noise: a close reading of PUBLIC is enough
    assert "PUBLICTEXTBET" in text or "UBLICTEXTBETA" in text


def test_line_art_crossing_the_zone_edge_is_partly_removed(client):
    """The stroke running past the zone edge survives outside it and disappears
    inside it: flattening paints the zone straight into the pixels it covers."""
    sid = open_doc(client, build_pdf())
    zones = {"0": [{"type": "rect", "points": rect_points(ZONE), "mode": "delete"}]}
    _, out = export(client, sid, zones)

    doc = fitz.open(stream=out, filetype="pdf")
    try:
        # right of the zone, at the height of the lines: still drawn
        beyond = doc[0].get_pixmap(clip=fitz.Rect(ZONE[2] + 5, 145, 400, 165))
        # inside the zone, at the same height: gone (painted over)
        inside = doc[0].get_pixmap(clip=fitz.Rect(ZONE[0] + 5, 145, ZONE[2] - 5, 165))
    finally:
        doc.close()
    assert min(beyond.samples) != max(beyond.samples), "line art beyond the zone should survive"
    assert min(inside.samples) == max(inside.samples), "line art inside the zone should be gone"


def test_pixelate_destroys_the_source_text(client):
    """The mosaic is a real downsample: the original text must not survive under
    the image, in text layer or in the raw bytes."""
    sid = open_doc(client, build_pdf())
    zones = {"0": [{"type": "rect", "points": rect_points(ZONE), "mode": "pixelate"}]}
    _, out = export(client, sid, zones)

    assert b"SECRETNAMEALPHA" not in every_byte(out)


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
    _, out = export(client, sid, zones)

    haystack = every_byte(out)
    assert b"INSIDETRIANGLE" not in haystack


def test_polygon_on_a_scan_does_not_whiten_its_bounding_box(client):
    """The case that made the bounding box unacceptable: a cover must follow the
    outline, not square off over the whole zone."""
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
        pm = chk[0].get_pixmap()
        # (70, 130): inside the bounding box, outside the triangle -- close to
        # the source grey, allowing for JPEG re-encoding
        px = pm.pixel(70, 130)
        assert all(abs(c - 90) < 20 for c in px), px
        # (230, 80): squarely inside the triangle -- painted over (default white)
        px = pm.pixel(230, 80)
        assert all(c > 230 for c in px), px
    finally:
        chk.close()


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
        px = chk[0].get_pixmap().pixel(70, 130)
        assert all(abs(c - 90) < 20 for c in px), px
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

    haystack = every_byte(out)
    assert b"PAGEMARKER0" not in haystack  # page supprimee
    assert b"PAGEMARKER2" not in haystack  # page 2 redigee (devenue page 1)

    chk = fitz.open(stream=out, filetype="pdf")
    try:
        assert chk.page_count == 2
    finally:
        chk.close()
    assert body["pages"] == 2


def test_cannot_delete_every_page(client):
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    sid = open_doc(client, data)
    r = client.post("/api/export", json={"sid": sid, "zones": {}, "deleted_pages": [0]})
    assert r.status_code == 400


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


def test_a_live_session_can_be_picked_up_again(client):
    """What a reloaded page asks for: the document is still there, and its geometry."""
    data = build_pdf()
    opened = client.post("/api/open", files={"file": ("exam.pdf", data, "application/pdf")}).json()

    r = client.get(f"/api/session/{opened['sid']}")
    assert r.status_code == 200
    again = r.json()
    assert again["sid"] == opened["sid"]
    assert again["name"] == opened["name"]
    # same geometry as on opening: the zones are laid back on it unchanged
    assert again["pages"] == opened["pages"]


def test_an_expired_session_cannot_be_picked_up(client):
    """A marking whose document is gone must be dropped, not drawn over nothing."""
    assert client.get("/api/session/deadbeef").status_code == 404


def test_export_with_no_zone_no_deletion_and_no_watermark_now_succeeds(client):
    """Flattening alone strips the file: this is no longer a no-op refused."""
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    sid = open_doc(client, data)
    r = client.post("/api/export", json={"sid": sid, "zones": {}, "deleted_pages": []})
    assert r.status_code == 200
    body = r.json()
    assert body["pages"] == 1


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


def colored_page() -> bytes:
    """A page of coloured paper with a name on it."""
    doc = fitz.open()
    page = doc.new_page(width=300, height=300)
    page.draw_rect(page.rect, color=None, fill=(0.9, 0.96, 0.78))
    page.insert_text((60, 120), "SECRETNAMEALPHA", fontsize=14)
    out = doc.tobytes()
    doc.close()
    return out


def cover_pixel(data: bytes, point=(150, 110)) -> tuple:
    """The colour of the exported page where the zone was."""
    doc = fitz.open(stream=data, filetype="pdf")
    pm = doc[0].get_pixmap(dpi=72)
    px = pm.pixel(*point)
    doc.close()
    return px


def assert_close(actual: tuple, expected: tuple, tol: int = 3) -> None:
    """Compare a sampled pixel to an expected colour, allowing for JPEG re-encoding."""
    assert all(abs(a - e) <= tol for a, e in zip(actual, expected, strict=True)), (
        actual,
        expected,
    )


def export_zone(client, data: bytes, color=None) -> bytes:
    zone = {"type": "rect", "points": rect_points((40, 100, 260, 130))}
    if color is not None:
        zone["color"] = color
    sid = open_doc(client, data)
    r = client.post(
        "/api/export",
        json={"sid": sid, "zones": {"0": [zone]}, "deleted_pages": []},
    )
    assert r.status_code == 200, r.text
    return client.get(r.json()["download"]).content


def test_zone_cover_takes_the_colour_it_is_given(client):
    """A white patch on coloured paper says "something was here": the cover is
    painted in the colour the client sampled from the page."""
    out = export_zone(client, colored_page(), color=[229, 244, 198])
    assert_close(cover_pixel(out), (229, 244, 198))
    assert b"SECRETNAMEALPHA" not in every_byte(out)


def test_zone_cover_stays_white_without_a_colour(client):
    """Older clients, and anything unusable, keep the original behaviour."""
    data = colored_page()
    assert_close(cover_pixel(export_zone(client, data)), (255, 255, 255))
    assert_close(cover_pixel(export_zone(client, data, color="green")), (255, 255, 255))
    assert_close(cover_pixel(export_zone(client, data, color=[1, 2])), (255, 255, 255))


def test_zone_colour_channels_are_clamped(client):
    """Out-of-range channels must not raise, nor wrap around."""
    out = export_zone(client, colored_page(), color=[-40, 300, 128])
    assert_close(cover_pixel(out), (0, 255, 128))
