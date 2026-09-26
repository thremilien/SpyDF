"""Unit tests for src.flatten: the painting and re-indexing logic, no HTTP."""

import fitz

from src.app import _is_box, _zone_color
from src.flatten import _spans, flatten

A4 = (595, 842)
ZONE = (50, 80, 260, 175)
# Wide at the top, pointed at the bottom: its bounding box juts out well to
# either side of the outline itself.
TRIANGLE = [(60, 60), (400, 60), (230, 140)]
PAPER = (0.9, 0.96, 0.78)


def _zone(points, mode="delete", color=(255, 255, 255)):
    """Build a zone dict the way src.app parses one out of a request."""
    pts = [fitz.Point(x, y) for x, y in points]
    rect = fitz.Rect(pts[0], pts[0])
    for p in pts:
        rect.include_point(p)
    return {
        "points": pts,
        "rect": rect,
        "box": _is_box(pts, rect),
        "mode": mode,
        "color": _zone_color(list(color)),
    }


def rect_points(rect):
    x0, y0, x1, y1 = rect
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def colored_page(width=595, height=842, fill=PAPER):
    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    page.draw_rect(page.rect, color=None, fill=fill)
    data = doc.tobytes()
    doc.close()
    return data


def render(data):
    """The first page of an exported document, at one pixel per point."""
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        return doc[0].get_pixmap()
    finally:
        doc.close()


# ---------------------------------------------------------------- delete zones


def test_delete_zone_paints_its_own_colour(monkeypatch):
    monkeypatch.setattr("src.flatten.EXPORT_JPEG_QUALITY", 0)
    data = colored_page()
    zones = {0: [_zone(rect_points(ZONE), color=[229, 244, 198])]}
    out, report = flatten(data, zones, set())
    pm = render(out)
    assert pm.pixel(150, 120) == (229, 244, 198)
    assert report["pages"] == 1


def test_pixels_outside_the_outline_are_unchanged(monkeypatch):
    monkeypatch.setattr("src.flatten.EXPORT_JPEG_QUALITY", 0)
    data = colored_page()
    zones = {0: [_zone(rect_points(ZONE), color=[229, 244, 198])]}
    zoned, _ = flatten(data, zones, set())
    plain, _ = flatten(data, {}, set())
    point = (ZONE[2] + 30, ZONE[1] + 10)  # well clear of the zone
    assert render(zoned).pixel(*point) == render(plain).pixel(*point)


def test_polygon_zone_does_not_square_off_into_its_bounding_box(monkeypatch):
    """A point inside the bounding box but outside the drawn outline keeps its
    original colour: what disappears follows the stroke, not the box."""
    monkeypatch.setattr("src.flatten.EXPORT_JPEG_QUALITY", 0)
    data = colored_page()
    zones = {0: [_zone(TRIANGLE, color=[0, 0, 0])]}
    zoned, _ = flatten(data, zones, set())
    plain, _ = flatten(data, {}, set())
    pz, pp = render(zoned), render(plain)
    # (70, 130): inside the triangle's bounding box, outside the triangle
    assert pz.pixel(70, 130) == pp.pixel(70, 130)
    # (230, 80): squarely inside the triangle
    assert pz.pixel(230, 80) == (0, 0, 0)


# ---------------------------------------------------------------- pixelate zones


def test_pixelate_zone_is_unreadable_but_not_flat(monkeypatch):
    monkeypatch.setattr("src.flatten.EXPORT_JPEG_QUALITY", 0)
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 100), "SECRETNAME", fontsize=40)
    data = doc.tobytes()
    doc.close()

    zone_rect = (50, 60, 400, 120)
    zones = {0: [_zone(rect_points(zone_rect), mode="pixelate")]}
    out, _ = flatten(data, zones, set())
    plain, _ = flatten(data, {}, set())

    exported = fitz.open(stream=out, filetype="pdf")
    plain_doc = fitz.open(stream=plain, filetype="pdf")
    try:
        crop = exported[0].get_pixmap(clip=fitz.Rect(*zone_rect))
        plain_crop = plain_doc[0].get_pixmap(clip=fitz.Rect(*zone_rect))
        # not a single flat colour: the mosaic still carries the shape's blocks
        assert min(crop.samples) != max(crop.samples)
        # blurred beyond recognition: far fewer distinct tones than the sharp
        # original text.
        assert len(set(crop.samples)) < len(set(plain_crop.samples))
    finally:
        exported.close()
        plain_doc.close()


def test_pixelate_zone_stays_inside_its_outline(monkeypatch):
    monkeypatch.setattr("src.flatten.EXPORT_JPEG_QUALITY", 0)
    data = colored_page()
    zones = {0: [_zone(TRIANGLE, mode="pixelate")]}
    zoned, _ = flatten(data, zones, set())
    plain, _ = flatten(data, {}, set())
    pz, pp = render(zoned), render(plain)
    # inside the bounding box, outside the triangle: left untouched
    assert pz.pixel(70, 130) == pp.pixel(70, 130)


# ---------------------------------------------------------------- pages


def test_page_geometry_and_count_survive_a_deletion_and_zones_still_land():
    doc = fitz.open()
    sizes = [(595, 842), (400, 600), (300, 300)]
    for w, h in sizes:
        p = doc.new_page(width=w, height=h)
        p.draw_rect(p.rect, color=None, fill=PAPER)
    data = doc.tobytes()
    doc.close()

    # zone on the last surviving source page, keyed by its *source* index
    zones = {2: [_zone(rect_points((10, 10, 100, 100)), color=[10, 20, 30])]}
    out, report = flatten(data, zones, {0})

    exported = fitz.open(stream=out, filetype="pdf")
    try:
        assert exported.page_count == 2
        assert report["pages"] == 2
        assert (exported[0].rect.width, exported[0].rect.height) == sizes[1]
        assert (exported[1].rect.width, exported[1].rect.height) == sizes[2]
        pm = exported[1].get_pixmap()
        assert pm.pixel(50, 50) == (10, 20, 30)
    finally:
        exported.close()


# ---------------------------------------------------------------- shape of the output


def test_output_has_one_image_per_page_and_nothing_else():
    doc = fitz.open()
    doc.new_page(width=595, height=842)
    doc.new_page(width=595, height=842)
    data = doc.tobytes()
    doc.close()

    out, _ = flatten(data, {}, set())
    exported = fitz.open(stream=out, filetype="pdf")
    try:
        assert exported.get_toc() == []
        # "format" is PyMuPDF's own read of the PDF version, not user metadata
        assert not any(v for k, v in exported.metadata.items() if k != "format")
        for page in exported:
            assert len(page.get_images(full=True)) == 1
            assert page.get_drawings() == []
            assert list(page.annots()) == []
            assert list(page.widgets()) == []
    finally:
        exported.close()


# ---------------------------------------------------------------- winding rule


def test_spans_applies_the_non_zero_winding_rule():
    """A stroke traced twice around the same rectangle double-winds it: the
    non-zero rule keeps it solid where the even-odd rule would carve a hole."""
    rect = [(0, 0), (20, 0), (20, 20), (0, 20)]
    points = [fitz.Point(x, y) for x, y in rect + rect]
    assert _spans(points, 10) == [(0.0, 20.0)]


def test_a_poster_sized_page_is_shrunk_to_fit_a4_and_its_zones_follow():
    # an A4 scan stored at one point per pixel: 84 x 119 cm
    w, h = 2384, 3371
    doc = fitz.open()
    p = doc.new_page(width=w, height=h)
    p.draw_rect(p.rect, color=None, fill=PAPER)
    data = doc.tobytes()
    doc.close()

    zones = {0: [_zone(rect_points((0, 0, w / 2, h / 2)), color=[10, 20, 30])]}
    out, _ = flatten(data, zones, set())

    exported = fitz.open(stream=out, filetype="pdf")
    try:
        rect = exported[0].rect
        assert rect.width <= 595.3 and rect.height <= 841.9
        assert abs(rect.width / rect.height - w / h) < 1e-3
        pm = exported[0].get_pixmap()
        assert pm.pixel(pm.width // 4, pm.height // 4) == (10, 20, 30)
        assert pm.pixel(3 * pm.width // 4, 3 * pm.height // 4) != (10, 20, 30)
    finally:
        exported.close()


def test_pages_read_in_parallel_keep_their_own_text(monkeypatch):
    # each page's paper says which page it is; the first pages are the slowest
    # to read, so the answers come back out of order
    import time

    import src.ocr as ocr

    shades = [40, 80, 120, 160, 200, 240]
    doc = fitz.open()
    for shade in shades:
        p = doc.new_page(width=200, height=200)
        p.draw_rect(p.rect, color=None, fill=[shade / 255] * 3)
        p.draw_rect(fitz.Rect(0, 0, 10, 10), color=None, fill=(1, 0, 0))
    data = doc.tobytes()
    doc.close()

    class Engine:
        def __call__(self, png):
            shade = fitz.Pixmap(png).pixel(100, 100)[0]
            time.sleep((255 - shade) / 2000)
            box = [[10, 10], [150, 10], [150, 40], [10, 40]]
            return [(box, f"shade{shade}", 0.99)], None

    engine = Engine()
    monkeypatch.setattr(ocr, "_get_engine", lambda: engine)
    monkeypatch.setattr(ocr, "_pool", None)
    monkeypatch.setattr("src.config.OCR_WORKERS", 3)
    monkeypatch.setattr(ocr, "OCR_WORKERS", 3)
    seen = []
    out, report = flatten(data, {}, set(), progress=lambda d, t: seen.append(d))

    exported = fitz.open(stream=out, filetype="pdf")
    try:
        assert report["indexed"] == len(shades)
        for page, shade in zip(exported, shades, strict=True):
            assert page.get_text().split() == [f"shade{shade}"]
        assert seen == list(range(1, len(shades) + 1))
    finally:
        exported.close()
