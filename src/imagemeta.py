"""The metadata an embedded image carries, read and removed from the one place."""

import struct

MAX_VALUE = 120  # a reported value is a label, not the whole field
MAX_IFD_ENTRIES = 200  # guard rail on a malformed or hostile Exif block

# JPEG markers. Everything before SOS is a marker segment; SOS starts the
# entropy-coded data and there is nothing to read past it.
SOI = 0xD8
EOI = 0xD9
SOS = 0xDA
COM = 0xFE
APP0, APP15 = 0xE0, 0xEF
APP1, APP2, APP13 = 0xE1, 0xE2, 0xED

# The APP segments a decoder needs: APP0 carries the JFIF pixel density and
# APP14 Adobe's colour transform, without which a CMYK scan decodes inverted.
# Dropping either would change how the image reads, which is not what this does.
JPEG_KEEP_APP = {APP0, 0xEE}

# The namespace an APP1 segment opens with when it holds XMP rather than Exif.
XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"

# Exif tags worth naming. The rest is exposure and lens data: it identifies a
# camera far less than these do, and listing it all would bury them.
EXIF_TAGS = {
    0x010F: "camera make",
    0x0110: "camera model",
    0x0131: "software",
    0x0132: "date",
    0x013B: "author",
    0x8298: "copyright",
    0x9003: "original date",
    0xA430: "camera owner",
    0xA431: "body serial",
    0xC62F: "camera serial",
}
EXIF_SUB_IFD = 0x8769
GPS_IFD = 0x8825
THUMBNAIL_OFFSET = 0x0201
THUMBNAIL_LENGTH = 0x0202

# Bytes an Exif/TIFF value type takes, for the types that appear in these tags.
TIFF_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}


def _short(text: str) -> str:
    text = " ".join(str(text).split())
    return text[:MAX_VALUE]


def _read_ifd(buf: bytes, off: int, be: bool) -> tuple[dict[int, tuple[int, int, int]], int]:
    """Read one TIFF image file directory.

    Args:
        buf: The Exif payload, starting at the TIFF header; every offset in the
            structure is relative to that start.
        off: Offset of this directory.
        be: Whether the file is big-endian.

    Returns:
        (tag -> (type, count, absolute offset of its value), offset of the next
        directory). Malformed input yields ({}, 0).
    """
    fmt = ">" if be else "<"
    if off < 2 or off + 2 > len(buf):
        return {}, 0
    (count,) = struct.unpack_from(fmt + "H", buf, off)
    count = min(count, MAX_IFD_ENTRIES)
    out = {}
    for i in range(count):
        entry = off + 2 + i * 12
        if entry + 12 > len(buf):
            return out, 0
        tag, typ, n, val = struct.unpack_from(fmt + "HHII", buf, entry)
        # a value of four bytes or fewer sits in the entry itself, not at an offset
        inline = TIFF_TYPE_SIZE.get(typ, 0) * n <= 4
        out[tag] = (typ, n, entry + 8 if inline else val)
    end = off + 2 + count * 12
    if end + 4 > len(buf):
        return out, 0
    return out, struct.unpack_from(fmt + "I", buf, end)[0]


def _tag_value(buf: bytes, entry: tuple[int, int, int], be: bool) -> str:
    """Render one directory entry as text, for the ASCII and integer types."""
    typ, n, at = entry
    size = TIFF_TYPE_SIZE.get(typ, 0)
    if not size or at < 0 or at + size * n > len(buf):
        return ""
    if typ in (2, 7):  # ASCII, or undefined bytes holding text
        return _short(buf[at : at + n].split(b"\x00")[0].decode("utf-8", "replace"))
    if typ in (3, 4):
        fmt = (">" if be else "<") + ("H" if typ == 3 else "I")
        return _short(str(struct.unpack_from(fmt, buf, at)[0]))
    return ""


def _pointer(buf: bytes, ifd: dict, tag: int, be: bool) -> int:
    """The offset a sub-directory pointer holds.

    A LONG is four bytes, so the pointer sits inside its own entry rather than
    at an offset: what is wanted here is the value, not where it lives.
    """
    value = _tag_value(buf, ifd[tag], be) if tag in ifd else ""
    return int(value) if value.isdigit() else 0


def _exif_fields(payload: bytes) -> list[tuple[str, str]]:
    r"""Read an Exif block: who took the picture, when, where, and with what.

    Also reports the thumbnail, which matters more than it looks: it is a second,
    complete copy of the image in miniature. Blanking pixels in the main image
    does not touch it, so a redacted scan whose Exif survives ships a small
    picture of the unredacted original.

    Args:
        payload: The APP1 payload, "Exif\0\0" header included.

    Returns:
        (name, value) per readable field, empty when nothing is recognisable.
    """
    buf = payload[6:]  # past "Exif\0\0"
    if len(buf) < 8 or buf[:2] not in (b"II", b"MM"):
        return []
    be = buf[:2] == b"MM"
    fmt = ">" if be else "<"
    magic, first = struct.unpack_from(fmt + "HI", buf, 2)
    if magic != 42:
        return []

    found = []
    ifd0, next_off = _read_ifd(buf, first, be)
    ifds = [ifd0]
    # the sub-directories are reached by pointer; only these two are followed,
    # so a file pointing a directory at itself cannot loop here
    if EXIF_SUB_IFD in ifd0:
        ifds.append(_read_ifd(buf, _pointer(buf, ifd0, EXIF_SUB_IFD, be), be)[0])

    for ifd in ifds:
        for tag, label in EXIF_TAGS.items():
            value = _tag_value(buf, ifd[tag], be) if tag in ifd else ""
            if value:
                found.append((label, value))

    if GPS_IFD in ifd0 and _read_ifd(buf, _pointer(buf, ifd0, GPS_IFD, be), be)[0]:
        found.append(("GPS", "location recorded"))

    # IFD1 is the thumbnail directory, chained after IFD0
    ifd1 = _read_ifd(buf, next_off, be)[0] if next_off else {}
    if THUMBNAIL_OFFSET in ifd1:
        size = _tag_value(buf, ifd1[THUMBNAIL_LENGTH], be) if THUMBNAIL_LENGTH in ifd1 else ""
        found.append(("thumbnail", f"{size} bytes" if size else "embedded"))

    # deduplicated, order kept: the same tag can sit in IFD0 and the sub-IFD
    return list(dict.fromkeys(found))


def _label_segment(marker: int, payload: bytes) -> str:
    """Name a JPEG marker segment, or return "" for one that must be kept.

    Args:
        marker: The marker byte, without its leading 0xFF.
        payload: The segment's payload, length bytes excluded.

    Returns:
        What the segment carries, or "" when it is structural.
    """
    if marker == COM:
        return "comment"
    if not APP0 <= marker <= APP15 or marker in JPEG_KEEP_APP:
        return ""
    if marker == APP2 and payload.startswith(b"ICC_PROFILE"):
        return ""  # the colour profile describes the pixels, not their author
    if marker == APP1:
        if payload.startswith(b"Exif\x00"):
            return "Exif"
        if payload.startswith(XMP_HEADER):
            return "XMP"
    if marker == APP13 and payload.startswith(b"Photoshop"):
        return "IPTC"
    return f"APP{marker - APP0}"


def _jpeg_segments(data: bytes) -> list[tuple[int, int, str, bytes]]:
    """Walk a JPEG's marker segments and return the ones carrying metadata.

    Args:
        data: The raw JPEG stream.

    Returns:
        (start, end, label, payload) per metadata segment, in file order. Not a
        JPEG, or a stream that stops making sense, yields what was read so far.
    """
    if len(data) < 4 or data[0] != 0xFF or data[1] != SOI:
        return []
    out, i, n = [], 2, len(data)
    while i + 3 < n:
        if data[i] != 0xFF:
            break  # desynchronised: stop rather than guess at an offset
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1  # fill byte
            continue
        if marker in (SOS, EOI):
            break  # entropy-coded data from here on
        length = (data[i + 2] << 8) | data[i + 3]
        end = i + 2 + length
        if length < 2 or end > n:
            break
        payload = data[i + 4 : end]
        label = _label_segment(marker, payload)
        if label:
            out.append((i, end, label, payload))
        i = end
    return out


def strip_jpeg(data: bytes) -> bytes | None:
    """Remove the metadata segments from a JPEG, leaving the pixels untouched.

    This is a byte-level cut between marker segments: the entropy-coded data is
    never decoded, so nothing is re-encoded and no generation is lost.

    Args:
        data: The raw JPEG stream.

    Returns:
        The cleaned stream, or None when there was nothing to remove.
    """
    segments = _jpeg_segments(data)
    if not segments:
        return None
    out, prev = bytearray(), 0
    for start, end, _label, _payload in segments:
        out += data[prev:start]
        prev = end
    out += data[prev:]
    return bytes(out)


def image_traces(doc, xref: int) -> list[dict]:
    """Report what one image object carries besides its pixels.

    Args:
        doc: The open document.
        xref: The image object's xref number.

    Returns:
        One entry per trace, {"kind", "field", "value"}. An Exif block yields one
        entry per readable field rather than one long line: the inspector lists
        them the way it lists the document's own metadata.
    """
    out = []
    try:
        if doc.xref_get_key(xref, "Metadata")[0] != "null":
            out.append({"kind": "XMP", "field": "XMP", "value": "stream attached to the image"})
    except Exception:
        pass
    try:
        raw = doc.xref_stream_raw(xref)
    except Exception:
        return out
    for _start, _end, label, payload in _jpeg_segments(raw):
        if label == "Exif":
            fields = _exif_fields(payload)
            out += [{"kind": label, "field": f, "value": v} for f, v in fields]
            if fields:
                continue
        value = ""
        if label == "XMP":
            value = _short(payload[len(XMP_HEADER) :].decode("utf-8", "replace"))
        out.append({"kind": label, "field": label, "value": value or f"{len(payload)} bytes"})
    return out


def strip_image_metadata(doc) -> int:
    """Strip the metadata embedded in every image of the document.

    Redaction rewrites only the images a zone touches; every other image ships
    with the bytes it came in with, Exif included. Neither `scrub()` nor
    `rewrite_images()` looks inside an image stream, so this is the only step
    that reaches a phone-photographed copy's camera, serial number, GPS fix and
    thumbnail.

    Only JPEG streams (`DCTDecode`) and an image's own `/Metadata` are handled.
    An image stored as raw samples has no container to carry metadata in;
    JPEG 2000 boxes are not read.

    Args:
        doc: The document to clean, in place.

    Returns:
        How many images were changed.
    """
    changed = 0
    for xref in range(1, doc.xref_length()):
        try:
            if not doc.xref_is_stream(xref):
                continue
            if doc.xref_get_key(xref, "Subtype")[1] != "/Image":
                continue
            touched = False
            if doc.xref_get_key(xref, "Metadata")[0] != "null":
                doc.xref_set_key(xref, "Metadata", "null")  # "null" deletes the key
                touched = True
            raw = doc.xref_stream_raw(xref)
            cleaned = strip_jpeg(raw)
            if cleaned is not None and len(cleaned) < len(raw):
                # compress=False: the stream keeps its own /DCTDecode filter, and
                # deflating a JPEG here would both corrupt it and inflate it.
                doc.update_stream(xref, cleaned, new=False, compress=False)
                touched = True
            changed += touched
        except Exception:
            continue  # one unreadable image must not fail an export
    return changed
