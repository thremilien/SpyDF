"""The paper colour read at opening: the default fill of a delete zone."""

import random

import fitz
import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.background import page_background

PAPER = (0.93, 0.89, 0.74)  # a yellowed sheet
PAPER_RGB = tuple(round(c * 255) for c in PAPER)


@pytest.fixture
def client():
    return TestClient(app)


def close(a, b, tol=3):
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


def page_of(fill, *, ink=0.0, blocks=(), width=595, height=842):
    """One page of `fill`, with `ink` of its area in black lines and some solid blocks."""
    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    page.draw_rect(page.rect, color=None, fill=fill)
    y = 40
    while ink and y < height - 40:
        # lines of dense "text": black bars, their height a share of the gap
        page.draw_rect(fitz.Rect(40, y, width - 40, y + 20 * ink / (1 - ink)), color=None, fill=0)
        y += 20 / (1 - ink)
    for rect in blocks:
        page.draw_rect(fitz.Rect(rect), color=None, fill=0)
    return doc


def test_a_blank_page_is_white():
    doc = page_of((1, 1, 1))
    assert page_background(doc[0]) == (255, 255, 255)


def test_coloured_paper_is_read_exactly():
    doc = page_of(PAPER)
    assert close(page_background(doc[0]), PAPER_RGB, tol=1)


def test_dense_text_does_not_turn_the_paper_grey():
    """A third of the page in ink: a mean would land far into grey, the mode does not."""
    doc = page_of(PAPER, ink=0.33)
    assert close(page_background(doc[0]), PAPER_RGB)


def test_a_large_black_block_does_not_win():
    doc = page_of(PAPER, blocks=[(0, 0, 595, 380)])  # 45% of the page solid black
    assert close(page_background(doc[0]), PAPER_RGB)


def test_paper_grain_straddling_a_bucket_boundary_is_not_split():
    """A scan's paper wobbles around one tone; half of it in the next bucket up must not lose."""
    rng = random.Random(0)
    doc = fitz.open()
    page = doc.new_page(width=200, height=200)
    # paper centred on 128 (a bucket edge at 4 bits), speckled +-6, plus a
    # single-tone grey smudge that would win a plain one-bucket vote
    for x in range(0, 200, 4):
        for y in range(0, 200, 4):
            v = (128 + rng.randint(-6, 6)) / 255
            page.draw_rect(fitz.Rect(x, y, x + 4, y + 4), color=None, fill=(v, v, v))
    page.draw_rect(fitz.Rect(0, 0, 200, 70), color=None, fill=(0.35, 0.35, 0.35))
    bg = page_background(doc[0])
    assert close(bg, (128, 128, 128), tol=4)


def test_opening_reports_each_pages_own_paper(client):
    doc = page_of(PAPER, ink=0.2)
    blue = page_of((0.8, 0.88, 0.97), ink=0.2)
    doc.insert_pdf(blue)
    data = doc.tobytes()

    r = client.post("/api/open", files={"file": ("exam.pdf", data, "application/pdf")})
    assert r.status_code == 200
    pages = r.json()["pages"]
    assert close(pages[0]["bg"], PAPER_RGB)
    assert close(pages[1]["bg"], (204, 224, 247))

    # a reload gets the very same colours back
    again = client.get(f"/api/session/{r.json()['sid']}").json()
    assert again["pages"] == pages
