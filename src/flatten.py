"""Every page rendered flat, its zones painted into the pixels, then re-indexed.

This is what an export is. The source document is never edited and never saved:
each page is rendered to a bitmap, the zones are painted *into* that bitmap, and
a brand-new document is built holding those bitmaps and nothing else. What the
old redaction had to hunt down one carrier at a time — text under a cover, an
annotation's author, a form field's value, the structure tree, an image's Exif,
a layer's name, an attachment, a script — simply has nowhere left to be. A
raster has no such things, and nothing is painted under anything.

The price is paid deliberately: the result is an image, so its resolution is
fixed at export time and its text is only as good as the recognition that reads
it back (`src/ocr.py`, called here on the very bitmap the page will carry).

Two rules hold everywhere below. Nothing outside a drawn outline is ever
modified — the fill follows the outline row by row, not its bounding box. And
the OCR only ever sees the bitmap *after* the zones are painted, so the text
layer cannot describe what a zone hides.
"""

from collections.abc import Callable

import fitz

from src import ocr
from src.config import EXPORT_DPI, EXPORT_JPEG_QUALITY, MOSAIC_BLOCKS, OCR_MAX_PAGES

RGB_MAX = 255


def _spans(points, y) -> list[tuple[float, float]]:
    """Horizontal intervals lying inside the outline at ordinate y.

    Uses the non-zero winding rule (not even-odd), which is what the browser's
    SVG preview of the zone already applies. A freehand stroke crossing itself
    is therefore solid here exactly as it is on screen.

    Args:
        points: The outline vertices.
        y: The scan line.

    Returns:
        (start, end) pairs, left to right and disjoint.
    """
    xs = []
    for i, a in enumerate(points):
        b = points[(i + 1) % len(points)]
        if a.y == b.y:
            continue
        top, bot = (a, b) if a.y < b.y else (b, a)
        if not top.y <= y < bot.y:
            continue
        xs.append((a.x + (y - a.y) * (b.x - a.x) / (b.y - a.y), 1 if b.y > a.y else -1))
    xs.sort()
    out, wind, start = [], 0, 0.0
    for x, direction in xs:
        if wind == 0:
            start = x
        wind += direction
        if wind == 0 and x > start:
            out.append((start, x))
    return out


def _mosaic(page, rect):
    """Render a zone very small, to be laid back over it a block at a time.

    Downsampled that hard, the content is unreadable: that is the whole of the
    pixelate mode. It is taken from the page before anything is painted.

    Args:
        page: The source page.
        rect: The zone's bounding box, in page coordinates.

    Returns:
        The tiny pixmap, or None if it could not be rendered.
    """
    w, h = max(rect.width, 1.0), max(rect.height, 1.0)
    s = MOSAIC_BLOCKS / max(w, h)
    try:
        pm = page.get_pixmap(matrix=fitz.Matrix(s, s), clip=rect, alpha=False)
    except Exception:
        return None
    return pm if pm.width and pm.height else None


def _rows(zone, page_rect, pm):
    """Walk the pixel rows a zone covers, giving the runs to fill on each.

    Args:
        zone: The parsed zone.
        page_rect: The page rectangle the bitmap was rendered from.
        pm: The page bitmap.

    Yields:
        (row, y, x0, x1) — the pixel row, the page height it stands at, and one
        run of pixel columns inside the outline, already clamped to the bitmap.
    """
    sx = pm.width / page_rect.width if page_rect.width else 0
    sy = pm.height / page_rect.height if page_rect.height else 0
    if sx <= 0 or sy <= 0:
        return
    rect = zone["rect"]
    j0 = max(0, int((rect.y0 - page_rect.y0) * sy))
    j1 = min(pm.height, int((rect.y1 - page_rect.y0) * sy) + 1)
    for j in range(j0, j1):
        y = page_rect.y0 + (j + 0.5) / sy
        # a rectangle needs no cutting: its outline is its bounding box
        runs = [(rect.x0, rect.x1)] if zone["box"] else _spans(zone["points"], y)
        for a, b in runs:
            i0 = max(0, int(round((a - page_rect.x0) * sx)))
            i1 = min(pm.width, int(round((b - page_rect.x0) * sx)))
            if i1 > i0:
                yield j, y, i0, i1


def _paint_delete(pm, zone, page_rect):
    """Fill the zone with its own colour, row by row along the outline."""
    color = tuple(min(RGB_MAX, max(0, round(c * RGB_MAX))) for c in zone["color"])
    for j, _y, i0, i1 in _rows(zone, page_rect, pm):
        pm.set_rect(fitz.IRect(i0, j, i1, j + 1), color)


def _paint_pixelate(pm, zone, page_rect, mosaic):
    """Lay the mosaic back over the zone, one block at a time.

    The blocks are cut to the outline exactly as a fill is, so a pixelated
    polygon never squares off into its bounding box.

    Args:
        pm: The page bitmap, modified in place.
        zone: The parsed zone.
        page_rect: The page rectangle the bitmap was rendered from.
        mosaic: The tiny pixmap of `_mosaic`.
    """
    rect = zone["rect"]
    if rect.width <= 0 or rect.height <= 0:
        return
    sx = pm.width / page_rect.width
    bw = rect.width / mosaic.width  # one block, in page units
    for j, y, i0, i1 in _rows(zone, page_rect, pm):
        by = min(mosaic.height - 1, max(0, int((y - rect.y0) / rect.height * mosaic.height)))
        x = i0
        while x < i1:
            page_x = page_rect.x0 + x / sx
            bx = min(mosaic.width - 1, max(0, int((page_x - rect.x0) / bw)))
            # right edge of that block, in pixels, never below one pixel wide
            edge = rect.x0 + (bx + 1) * bw
            stop = min(i1, max(x + 1, int(round((edge - page_rect.x0) * sx))))
            pm.set_rect(fitz.IRect(x, j, stop, j + 1), mosaic.pixel(bx, by))
            x = stop


def _page_image(page, zones):
    """Render one page and paint its zones into the bitmap.

    Args:
        page: The source page.
        zones: Its parsed zones, or an empty list.

    Returns:
        The bitmap, zones included, ready to be written into the export.
    """
    # The mosaics come from the untouched page, before any zone is painted: a
    # pixelate zone overlapping a delete zone must show the original blurred,
    # not the cover that is about to land on it.
    mosaics = [(z, _mosaic(page, z["rect"])) for z in zones if z["mode"] == "pixelate"]
    pm = page.get_pixmap(dpi=EXPORT_DPI, alpha=False)
    for z in zones:
        if z["mode"] == "delete":
            _paint_delete(pm, z, page.rect)
    for z, mosaic in mosaics:
        if mosaic is not None:
            _paint_pixelate(pm, z, page.rect, mosaic)
    return pm


def _insert(new_page, pm):
    """Write the bitmap into the page, as JPEG unless the quality is turned off."""
    if EXPORT_JPEG_QUALITY > 0:
        stream = pm.tobytes("jpeg", jpg_quality=EXPORT_JPEG_QUALITY)
        new_page.insert_image(new_page.rect, stream=stream)
    else:
        new_page.insert_image(new_page.rect, pixmap=pm)


def flatten(
    data: bytes,
    zones_by_page: dict,
    deleted_pages: set,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[bytes, dict]:
    """Build the exported document: flat pages, painted zones, an OCR text layer.

    Args:
        data: The source PDF bytes.
        zones_by_page: Zero-based page number -> parsed zones, as `src.app`
            builds them ({"points", "rect", "box", "mode", "color"}).
        deleted_pages: Zero-based page numbers to drop entirely.
        progress: Called with (done, total) once each kept page is rendered,
            painted and indexed; `total` counts only the pages not deleted.

    Returns:
        (the PDF bytes, a report): {"pages", "indexed", "fragments", "ocr"},
        `ocr` being "ok", "partial" when the page budget ran out, or
        "unavailable" when no engine could be loaded.
    """
    engine = ocr.available()
    src = fitz.open(stream=data, filetype="pdf")
    out = fitz.open()
    pages = indexed = fragments = 0
    truncated = False
    total = sum(1 for n in range(src.page_count) if n not in deleted_pages)
    try:
        for n, page in enumerate(src):
            if n in deleted_pages:
                continue
            pm = _page_image(page, zones_by_page.get(n) or [])
            new_page = out.new_page(width=page.rect.width, height=page.rect.height)
            _insert(new_page, pm)
            pages += 1
            if engine and indexed >= OCR_MAX_PAGES:
                truncated = True
            elif engine:
                fragments += ocr.write_layer(new_page, ocr.bitmap_words(pm))
                indexed += 1
            if progress is not None:
                progress(pages, total)
        # A new document carries almost nothing; these two make it nothing.
        out.set_metadata({})
        out.del_xml_metadata()
        result = out.tobytes(garbage=4, deflate=True, clean=True)
    finally:
        out.close()
        src.close()
    status = "ok" if engine else "unavailable"
    if engine and truncated:
        status = "partial"
    return result, {"pages": pages, "indexed": indexed, "fragments": fragments, "ocr": status}
