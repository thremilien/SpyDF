"""Tests for the audit log, whose own privacy is the point: no filename, no leak text."""

import io
import logging
import logging.handlers

import fitz
import pytest
from fastapi.testclient import TestClient

import src.app as app_module
import src.logs as logs_module
from src.app import app
from tests.test_redaction import ZONE, build_pdf, open_doc, rect_points

LOGGER_NAME = "spydf"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def clean_logging_state():
    """Save and restore the global logger state.

    setup_logging() mutates shared state (handlers, propagate, the _configured
    flag). The other tests in this file rely on the default NullHandler +
    propagate=True for caplog to capture them.
    """
    logger = logging.getLogger(LOGGER_NAME)
    saved_handlers = list(logger.handlers)
    saved_propagate = logger.propagate
    saved_level = logger.level
    saved_configured = logs_module._configured
    yield
    logger.handlers = saved_handlers
    logger.propagate = saved_propagate
    logger.level = saved_level
    logs_module._configured = saved_configured


# ---------------------------------------------------------------- connection


def test_get_root_logs_a_connection_event_with_ip(client, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    r = client.get("/")
    assert r.status_code == 200
    lines = [lk.message for lk in caplog.records if "event=connect" in lk.message]
    assert len(lines) == 1
    assert "ip=" in lines[0]


# ---------------------------------------------------------------- import


def test_import_logs_size_and_pages_but_never_the_filename(client, caplog, monkeypatch):
    monkeypatch.delenv("SPYDF_LOG_FILENAMES", raising=False)
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    data = build_pdf()
    r = client.post(
        "/api/open",
        files={
            "file": ("copie_jean_dupont.pdf", data, "application/pdf"),
        },
    )
    assert r.status_code == 200

    text = "\n".join(lk.message for lk in caplog.records)
    assert "event=import " in text or text.endswith("event=import")
    assert f"size={len(data)}" in text
    assert "pages=1" in text
    # privacy rule: by default the uploaded file name (which can be identifying,
    # e.g. "copie_jean_dupont.pdf") appears nowhere.
    assert "copie_jean_dupont" not in text
    assert "filename=" not in text


def test_filenames_appear_only_when_opted_in(client, caplog, monkeypatch):
    monkeypatch.setenv("SPYDF_LOG_FILENAMES", "1")
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    data = build_pdf()
    r = client.post(
        "/api/open",
        files={
            "file": ("copie_jean_dupont.pdf", data, "application/pdf"),
        },
    )
    assert r.status_code == 200

    text = "\n".join(lk.message for lk in caplog.records)
    # SPYDF_LOG_FILENAMES is read at call time, not at module import, so the
    # monkeypatch above is enough without reloading src.app.
    assert "filename=copie_jean_dupont.pdf" in text


def test_rejected_upload_logs_a_warning(client, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    r = client.post("/api/open", files={"file": ("x.pdf", b"not a pdf", "application/pdf")})
    assert r.status_code == 400

    warnings = [lk for lk in caplog.records if lk.levelno == logging.WARNING]
    assert any("event=import_rejected" in lk.message for lk in warnings)
    assert any("reason=unreadable" in lk.message for lk in warnings)


# ---------------------------------------------------------------- export


def test_export_logs_leak_count_never_leak_text(client, caplog, monkeypatch):
    """A leak is a word taken literally from the document the operator was
    erasing: the log must carry its count, never the word.

    The leak is forced by substituting `_verify`; redaction fidelity itself is
    already covered by tests/test_redaction.py, only the log matters here.
    """
    leak_marker = "LEAKEDSECRETXYZ"
    fake_leaks = [
        {"page": 1, "kind": "text", "text": leak_marker},
        {"page": 1, "kind": "text", "text": leak_marker + "2"},
    ]
    monkeypatch.setattr(app_module, "_verify", lambda *a, **k: fake_leaks)

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    sid = open_doc(client, build_pdf())
    zones = {"0": [{"type": "rect", "points": rect_points(ZONE), "mode": "delete"}]}
    r = client.post("/api/export", json={"sid": sid, "zones": zones, "deleted_pages": []})
    assert r.status_code == 200
    assert r.json()["leak_count"] == 2

    text = "\n".join(lk.message for lk in caplog.records)
    assert "leaks=2" in text
    assert leak_marker not in text


def test_export_with_watermark_logs_flag_never_text(client, caplog):
    """The watermark is free text typed by the operator: only the boolean of its
    presence is logged."""
    watermark_text = "MARQUEURFILIGRANE"
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    sid = open_doc(client, build_pdf())
    zones = {"0": [{"type": "rect", "points": rect_points(ZONE), "mode": "delete"}]}
    r = client.post(
        "/api/export",
        json={
            "sid": sid,
            "zones": zones,
            "deleted_pages": [],
            "watermark": watermark_text,
        },
    )
    assert r.status_code == 200

    text = "\n".join(lk.message for lk in caplog.records)
    assert "watermark=true" in text
    assert watermark_text not in text


def test_export_success_event_is_logged(client, caplog):
    """Regression: a first pass logged no success line for the export, only its
    refusals."""
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    sid = open_doc(client, build_pdf())
    zones = {"0": [{"type": "rect", "points": rect_points(ZONE), "mode": "delete"}]}
    r = client.post("/api/export", json={"sid": sid, "zones": zones, "deleted_pages": []})
    assert r.status_code == 200

    exports = [lk.message for lk in caplog.records if lk.message.startswith("event=export ")]
    assert len(exports) == 1
    assert "strip_meta=true" in exports[0]
    assert "out_bytes=" in exports[0]


def test_export_refusals_are_logged_at_warning(client, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    r = client.post("/api/export", json={"sid": "deadbeef", "zones": {}, "deleted_pages": []})
    assert r.status_code == 404

    doc = fitz.open()
    doc.new_page()
    sid = open_doc(client, doc.tobytes())
    doc.close()

    r = client.post("/api/export", json={"sid": sid, "zones": {}, "deleted_pages": []})
    assert r.status_code == 400

    r = client.post("/api/export", json={"sid": sid, "zones": {}, "deleted_pages": [0]})
    assert r.status_code == 400

    warnings = [lk.message for lk in caplog.records if lk.levelno == logging.WARNING]
    assert any("reason=unknown_session" in m for m in warnings)
    assert any("reason=nothing_to_do" in m for m in warnings)
    assert any("reason=all_pages_deleted" in m for m in warnings)


def test_full_session_id_never_logged_only_a_prefix(client, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    sid = open_doc(client, build_pdf())
    zones = {"0": [{"type": "rect", "points": rect_points(ZONE), "mode": "delete"}]}
    r = client.post("/api/export", json={"sid": sid, "zones": zones, "deleted_pages": []})
    assert r.status_code == 200

    text = "\n".join(lk.message for lk in caplog.records)
    assert sid not in text
    assert sid[:8] in text


# ---------------------------------------------------------------- src/logs.py


def test_setup_logging_twice_does_not_duplicate_lines(clean_logging_state, monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(logs_module.sys, "stderr", buf)
    monkeypatch.delenv("SPYDF_LOG_FILE", raising=False)

    logs_module.setup_logging()
    logs_module.setup_logging()  # deuxieme appel: ne doit rien ajouter

    logger = logging.getLogger(LOGGER_NAME)
    stream_handlers = [h for h in logger.handlers if type(h) is logging.StreamHandler]
    assert len(stream_handlers) == 1

    logs_module.log_event("test_idempotent")
    lines = [lk for lk in buf.getvalue().splitlines() if "event=test_idempotent" in lk]
    assert len(lines) == 1


def test_log_file_env_var_really_writes_events(clean_logging_state, monkeypatch, tmp_path):
    log_file = tmp_path / "spydf.log"
    monkeypatch.setenv("SPYDF_LOG_FILE", str(log_file))

    logs_module.setup_logging()
    logs_module.log_event("test_file_event", foo="bar")
    for h in logging.getLogger(LOGGER_NAME).handlers:
        h.flush()

    content = log_file.read_text(encoding="utf-8")
    assert "event=test_file_event" in content
    assert "foo=bar" in content


def test_unwritable_log_file_path_warns_but_does_not_raise(
    clean_logging_state, monkeypatch, tmp_path, capsys
):
    bad_path = tmp_path / "no_such_directory" / "spydf.log"
    monkeypatch.setenv("SPYDF_LOG_FILE", str(bad_path))

    # must never raise: a logging problem must not stop the app from serving.
    logs_module.setup_logging()

    err = capsys.readouterr().err
    assert "SPYDF_LOG_FILE" in err
