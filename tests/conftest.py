"""Shared fixtures: the OCR engine is off by default, since real recognition
costs seconds a page and would make the suite unbearable.
"""

import pytest

import src.ocr as ocr

# The true implementation, captured before any test gets a chance to patch it.
_real_get_engine = ocr._get_engine


@pytest.fixture(autouse=True)
def _no_ocr(monkeypatch):
    """Make the engine unavailable, and never let it leak into the next test."""
    monkeypatch.setattr(ocr, "_get_engine", lambda: None)
    monkeypatch.setattr(ocr, "_engine", None)
    monkeypatch.setattr(ocr, "_engine_broken", False)
    yield


@pytest.fixture
def real_ocr(monkeypatch):
    """Opt-in: restore the real engine for the few tests that must recognise text."""
    monkeypatch.setattr(ocr, "_get_engine", _real_get_engine)
    monkeypatch.setattr(ocr, "_engine", None)
    monkeypatch.setattr(ocr, "_engine_broken", False)
    yield
