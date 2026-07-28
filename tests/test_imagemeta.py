"""Regression tests for the metadata carried inside an image, read and removed."""

import contextlib
import struct

import fitz
import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.imagemeta import _jpeg_segments, image_traces, strip_image_metadata, strip_jpeg
from src.probe import inspect_document

# Markers planted in the Exif block; none may survive a scrubbed export.
CAMERA_MAKE = "TestCamMAKE"
CAMERA_MODEL = "ModelMODEL"
ARTIST = "ArtistNAME"
SERIAL = "SN-SERIAL123"
COMMENT = b"COMMENTMARKER"
THUMBNAIL = b"\xff\xd8\xff\xd9THUMBNAILMARKER" + b"\x00" * 40


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------- fixtures


def _ifd(offset: int, tags: list, next_off: int = 0) -> tuple[bytes, int]:
    """Lay out one TIFF directory, pooling its ASCII values right after it.

    Args:
        offset: Where this directory starts, relative to the TIFF header.
        tags: (tag, type, bytes for ASCII | int for LONG) entries.
        next_off: Offset of the directory chained after this one.

    Returns:
        (the directory and its data pool, the offset just past them).
    """
    data_off = offset + 2 + len(tags) * 12 + 4
    body, blob = struct.pack("<H", len(tags)), b""
    for tag, typ, val in tags:
        if isinstance(val, bytes):
            v = val + b"\x00"
            if len(v) <= 4:
                body += struct.pack("<HHI", tag, typ, len(v)) + v.ljust(4, b"\x00")
            else:
                body += struct.pack("<HHII", tag, typ, len(v), data_off + len(blob))
                blob += v
        else:
            body += struct.pack("<HHII", tag, typ, 1, val)
    return body + struct.pack("<I", next_off) + blob, data_off + len(blob)


def exif_payload() -> bytes:
    """An Exif block with a camera, an author, a serial, a GPS fix and a thumbnail."""
    gps = [(1, 2, b"N"), (3, 2, b"E")]
    sub = [(0xA431, 2, SERIAL.encode()), (0x9003, 2, b"2026:03:12 09:41:00")]
    base = [
        (0x010F, 2, CAMERA_MAKE.encode()),
        (0x0110, 2, CAMERA_MODEL.encode()),
        (0x013B, 2, ARTIST.encode()),
        (0x8769, 4, 0),
        (0x8825, 4, 0),
    ]
    # sizes do not depend on the pointer values, so one dry run gives every offset
    _, after0 = _ifd(8, base)
    sub_b, after_sub = _ifd(after0, sub)
    gps_b, after_gps = _ifd(after_sub, gps)
    _, after1 = _ifd(after_gps, [(0x0201, 4, 0), (0x0202, 4, len(THUMBNAIL))])
    ifd1_b, _ = _ifd(after_gps, [(0x0201, 4, after1), (0x0202, 4, len(THUMBNAIL))])
    base[3] = (0x8769, 4, after0)
    base[4] = (0x8825, 4, after_sub)
    ifd0_b, _ = _ifd(8, base, next_off=after_gps)
    tiff = b"II" + struct.pack("<HI", 42, 8) + ifd0_b + sub_b + gps_b + ifd1_b + THUMBNAIL
    return b"Exif\x00\x00" + tiff


def _segment(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def build_jpeg() -> bytes:
    """A real JPEG, then given an Exif block, a comment and Adobe's colour flag."""
    pm = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 240, 160))
    pm.set_rect(pm.irect, (228, 224, 214))
    jpeg = pm.tobytes("jpg")
    # APP14 says how the three channels are to be read; transform 1 is the YCbCr
    # that PyMuPDF just wrote. Getting it wrong would decode the image in the
    # wrong colours, which is not what this fixture is testing.
    adobe = b"Adobe\x00d\x00\x00\x00\x00\x01"
    extra = _segment(0xE1, exif_payload()) + _segment(0xFE, COMMENT) + _segment(0xEE, adobe)
    return jpeg[:2] + extra + jpeg[2:]


def build_pdf() -> bytes:
    """One page, one scan-like image carrying the Exif block."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(fitz.Rect(50, 50, 450, 320), stream=build_jpeg())
    page.insert_text((60, 500), "PUBLICTEXT", fontsize=12)
    out = doc.tobytes()
    doc.close()
    return out


def every_byte(pdf: bytes) -> bytes:
    """Raw bytes plus every object and decompressed stream."""
    chunks = [pdf]
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        for xref in range(1, doc.xref_length()):
            with contextlib.suppress(Exception):
                chunks.append(doc.xref_object(xref, compressed=False).encode("utf-8", "replace"))
            with contextlib.suppress(Exception):
                if doc.xref_is_stream(xref):
                    chunks.append(doc.xref_stream_raw(xref))
    finally:
        doc.close()
    return b"\n".join(chunks)


def open_doc(client, data: bytes) -> str:
    r = client.post("/api/open", files={"file": ("scan.pdf", data, "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()["sid"]


def export(client, sid, zones=None, strip_meta=True):
    r = client.post(
        "/api/export",
        json={
            "sid": sid,
            "zones": zones or {},
            "deleted_pages": [],
            "strip_meta": strip_meta,
            "watermark": "" if zones else "x",  # an export needs something to do
        },
    )
    assert r.status_code == 200, r.text
    dl = client.get(r.json()["download"])
    assert dl.status_code == 200
    return dl.content


# ---------------------------------------------------------------- the JPEG walk


def test_metadata_segments_are_found_and_structural_ones_are_not():
    labels = [label for _s, _e, label, _p in _jpeg_segments(build_jpeg())]
    assert "Exif" in labels
    assert "comment" in labels
    # Adobe's colour flag describes the pixels, not their author
    assert "APP14" not in labels


def test_the_colour_profile_is_not_metadata():
    """An ICC profile says how to read the colours: stripping it would change them.

    Kept out of the fixture the other tests decode, since a stand-in profile is
    not a real one and the decoder says so.
    """
    jpeg = build_jpeg()
    with_icc = jpeg[:2] + _segment(0xE2, b"ICC_PROFILE\x00" + b"\x01" * 40) + jpeg[2:]
    labels = [label for _s, _e, label, _p in _jpeg_segments(with_icc)]
    assert "APP2" not in labels
    assert b"ICC_PROFILE" in strip_jpeg(with_icc)


def test_strip_jpeg_removes_metadata_and_keeps_the_image_readable():
    jpeg = build_jpeg()
    cleaned = strip_jpeg(jpeg)
    assert cleaned is not None and len(cleaned) < len(jpeg)
    for marker in (CAMERA_MAKE.encode(), SERIAL.encode(), COMMENT, THUMBNAIL[:20]):
        assert marker not in cleaned
    # the colour flag survives, and so do the pixels: same size, same samples
    assert b"Adobe" in cleaned
    before, after = fitz.Pixmap(jpeg), fitz.Pixmap(cleaned)
    assert (after.width, after.height) == (before.width, before.height)
    assert after.samples == before.samples


def test_strip_jpeg_leaves_a_clean_jpeg_alone():
    pm = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 40))
    assert strip_jpeg(pm.tobytes("jpg")) is None


def test_a_non_jpeg_stream_is_left_alone():
    assert strip_jpeg(b"not a jpeg at all") is None
    assert _jpeg_segments(b"\xff\xd8truncated") == []


# ---------------------------------------------------------------- reading


def test_the_probe_reports_what_the_image_carries():
    data = build_pdf()
    page = inspect_document(data)["pages"][0]
    traces = [m for im in page["images"] for m in im["meta"]]
    assert {"Exif", "comment"} <= {t["kind"] for t in traces}
    fields = {t["field"]: t["value"] for t in traces if t["kind"] == "Exif"}
    assert fields.get("camera make") == CAMERA_MAKE
    assert fields.get("camera model") == CAMERA_MODEL
    assert fields.get("author") == ARTIST
    assert fields.get("body serial") == SERIAL
    assert "GPS" in fields and "thumbnail" in fields, fields


def test_the_inspect_route_carries_the_image_metadata(client):
    sid = open_doc(client, build_pdf())
    r = client.get(f"/api/inspect/{sid}")
    assert r.status_code == 200
    images = r.json()["pages"][0]["images"]
    assert any(im.get("meta") for im in images)


# ---------------------------------------------------------------- stripping


def test_the_export_removes_the_image_metadata(client):
    data = build_pdf()
    assert CAMERA_MAKE.encode() in every_byte(data)  # it really is in the source

    out = export(client, open_doc(client, data))
    haystack = every_byte(out)
    survivors = [
        m
        for m in (CAMERA_MAKE, CAMERA_MODEL, ARTIST, SERIAL, COMMENT.decode())
        if m.encode() in haystack
    ]
    assert not survivors, f"image metadata still in the exported PDF: {survivors}"
    assert THUMBNAIL[4:19] not in haystack, "the Exif thumbnail survived the export"


def test_the_exported_image_still_decodes(client):
    out = export(client, open_doc(client, build_pdf()))
    doc = fitz.open(stream=out, filetype="pdf")
    try:
        xrefs = [im[0] for im in doc[0].get_images(full=True)]
        assert xrefs, "the image disappeared from the export"
        for xref in xrefs:
            pix = fitz.Pixmap(doc, xref)
            assert pix.width and pix.height
        assert doc[0].get_pixmap().width  # the page as a whole still renders
    finally:
        doc.close()


def test_the_export_keeps_the_metadata_when_the_scrubbing_is_off(client):
    """What the inspector promises: unchecked, the pane says "kept" — and it is."""
    out = export(client, open_doc(client, build_pdf()), strip_meta=False)
    assert CAMERA_MAKE.encode() in every_byte(out)


def test_redacting_over_the_image_does_not_excuse_the_scrubbing(client):
    """A zone rewrites the pixels; the Exif goes only because the scrubbing runs."""
    zones = {"0": [{"type": "rect", "points": [[60, 60], [200, 60], [200, 200], [60, 200]]}]}
    out = export(client, open_doc(client, build_pdf()), zones=zones)
    assert SERIAL.encode() not in every_byte(out)


def test_stripping_is_idempotent_and_reports_nothing_left():
    doc = fitz.open(stream=build_pdf(), filetype="pdf")
    try:
        assert strip_image_metadata(doc) == 1
        assert strip_image_metadata(doc) == 0
        for xref in [im[0] for im in doc[0].get_images(full=True)]:
            assert image_traces(doc, xref) == []
    finally:
        doc.close()
