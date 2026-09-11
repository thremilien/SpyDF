"""Tests for the export progress: the flatten callback and the polled route."""

import fitz
import pytest
from fastapi.testclient import TestClient

import src.app as app_module
from src.app import app
from src.flatten import flatten


def make_pdf(pages=3):
    doc = fitz.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"page {i + 1}")
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("SPYDF_AUTH_PASSWORD", raising=False)
    return TestClient(app)


# Opens a fresh document and returns its session id.
def open_doc(client, pages=3):
    r = client.post("/api/open", files={"file": ("exam.pdf", make_pdf(pages), "application/pdf")})
    assert r.status_code == 200
    return r.json()["sid"]


def test_flatten_reports_each_kept_page():
    calls = []
    _, report = flatten(make_pdf(4), {}, {1}, progress=lambda d, t: calls.append((d, t)))
    assert calls == [(1, 3), (2, 3), (3, 3)]
    assert report["pages"] == 3


def test_flatten_without_callback_still_works():
    _, report = flatten(make_pdf(2), {}, set())
    assert report["pages"] == 2


def test_progress_is_idle_for_a_fresh_session(client):
    sid = open_doc(client)
    r = client.get(f"/api/export/progress/{sid}")
    assert r.status_code == 200
    assert r.json() == {"active": False, "done": 0, "total": 0, "phase": "idle"}


def test_progress_unknown_session_is_404(client):
    assert client.get("/api/export/progress/deadbeef").status_code == 404


def test_export_without_zones_returns_a_download(client):
    sid = open_doc(client)
    r = client.post("/api/export", json={"sid": sid, "zones": {}, "deleted_pages": []})
    assert r.status_code == 200
    body = r.json()
    assert body["download"].startswith("/api/download/")
    assert body["pages"] == 3
    assert client.get(body["download"]).status_code == 200


def test_progress_is_tracked_during_export_and_cleared_after(client, monkeypatch):
    sid = open_doc(client)
    seen = []
    real = app_module.flatten

    def spy(data, zones, deleted, progress=None):
        seen.append(dict(app_module.EXPORTS[sid]))

        def relay(done, total):
            progress(done, total)
            seen.append(dict(app_module.EXPORTS[sid]))

        return real(data, zones, deleted, relay)

    monkeypatch.setattr(app_module, "flatten", spy)
    r = client.post("/api/export", json={"sid": sid, "zones": {}, "deleted_pages": [0]})
    assert r.status_code == 200
    assert seen == [
        {"done": 0, "total": 2, "phase": "starting"},
        {"done": 1, "total": 2, "phase": "pages"},
        {"done": 2, "total": 2, "phase": "pages"},
    ]
    assert client.get(f"/api/export/progress/{sid}").json()["phase"] == "idle"


def test_progress_is_cleared_when_export_fails(client, monkeypatch):
    sid = open_doc(client)

    def boom(*args, **kwargs):
        raise RuntimeError("render failed")

    monkeypatch.setattr(app_module, "flatten", boom)
    with pytest.raises(RuntimeError):
        client.post("/api/export", json={"sid": sid, "zones": {}, "deleted_pages": []})
    assert sid not in app_module.EXPORTS
