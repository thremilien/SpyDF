"""The paper colour of a page, which is what a delete zone is filled with by default.

A mean of the page is the wrong answer: every line of text drags it towards
grey. So is sampling around a zone, which is what the client used to do — drawn
tight against black text, the samples are half ink. What the paper is, is the
colour most of the page shares.

The page is rendered small, its pixels are sorted into coarse colour buckets,
and the bucket with the most pixels — counting its immediate neighbours, so a
paper tone sitting on a bucket boundary is not split in two — is roughly where
the paper is. That estimate is then refined by a few mean-shift steps: the mean
of only the pixels within `BG_RADIUS` of it, again and again, until it settles
on the densest tone. Text, its anti-aliased edges and a scanner's dark margins
lie outside that window and cannot pull it anywhere.
"""

from collections import Counter

import fitz

from src.config import BG_BUCKET_BITS, BG_CANDIDATES, BG_RADIUS, BG_SAMPLE_SIDE

WHITE = (255, 255, 255)
NEIGHBOURS = [(a, b, c) for a in (-1, 0, 1) for b in (-1, 0, 1) for c in (-1, 0, 1)]
MAX_SHIFTS = 8  # mean-shift steps; it settles in two or three on a real page


def _mean(exact: Counter, keep) -> tuple[float, float, float] | None:
    """Weighted mean of the colours `keep` accepts, or None when it accepts none."""
    total = r = g = b = 0
    for colour, n in exact.items():
        if keep(colour):
            total += n
            r += colour[0] * n
            g += colour[1] * n
            b += colour[2] * n
    return (r / total, g / total, b / total) if total else None


def page_background(page) -> tuple[int, int, int]:
    """The dominant colour of a page, 0-255 per channel.

    Args:
        page: The source page.

    Returns:
        The paper colour; white when the page cannot be rendered or is empty.
    """
    side = max(page.rect.width, page.rect.height)
    if side <= 0:
        return WHITE
    s = BG_SAMPLE_SIDE / side
    try:
        pm = page.get_pixmap(matrix=fitz.Matrix(s, s), colorspace=fitz.csRGB, alpha=False)
    except Exception:
        return WHITE
    data = pm.samples
    if not data:
        return WHITE

    # A scan holds far fewer distinct colours than pixels: counting those first
    # keeps the rest of the work off the per-pixel loop.
    exact = Counter(data[i : i + 3] for i in range(0, len(data), 3))
    shift = 8 - BG_BUCKET_BITS
    coarse: Counter = Counter()
    for colour, n in exact.items():
        coarse[(colour[0] >> shift, colour[1] >> shift, colour[2] >> shift)] += n

    # Only the few fullest buckets can win once their neighbours are added.
    def around(k):
        return sum(coarse.get((k[0] + a, k[1] + b, k[2] + c), 0) for a, b, c in NEIGHBOURS)

    paper = max((k for k, _ in coarse.most_common(BG_CANDIDATES)), key=around)

    def in_paper(c):
        return all(abs((c[i] >> shift) - paper[i]) <= 1 for i in range(3))

    centre = _mean(exact, in_paper)
    for _ in range(MAX_SHIFTS):
        at = centre

        def near(c, at=at):
            return all(abs(c[i] - at[i]) <= BG_RADIUS for i in range(3))

        centre = _mean(exact, near) or at
        if all(abs(centre[i] - at[i]) < 0.5 for i in range(3)):
            break
    return (round(centre[0]), round(centre[1]), round(centre[2]))
