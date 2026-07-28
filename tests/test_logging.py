"""Tests du journal d'audit (`src/logs.py` + les trois evenements de
`src/app.py`): connexion, import, export.

Le point sensible n'est pas la redaction (voir tests/test_redaction.py) mais
la confidentialite du journal lui-meme: le nom de fichier depose et le texte
d'une fuite ne doivent jamais y apparaitre, sauf opt-in explicite pour le
nom de fichier. On utilise `caplog` cote logger "spydf" plutot que de capturer
stderr, qui est fragile (formatage, ordre d'ecriture).
"""

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
    """setup_logging() modifie un etat global partage (handlers du logger,
    propagate, drapeau _configured): on le sauvegarde et on le restaure pour
    ne pas polluer les autres tests de ce fichier, qui comptent sur le
    NullHandler + propagate=True par defaut pour que caplog les capture."""
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


# ---------------------------------------------------------------- connexion


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
    # regle de confidentialite: par defaut, le nom du fichier depose (qui peut
    # etre identifiant, ex. "copie_jean_dupont.pdf") n'apparait nulle part.
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
    # SPYDF_LOG_FILENAMES est lu au moment de l'appel (pas a l'import du
    # module), donc le monkeypatch ci-dessus suffit sans recharger src.app.
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
    """La fuite est un mot litteralement extrait du document que l'operateur
    cherchait a effacer: le journal ne doit jamais le porter, seulement son
    nombre. On force artificiellement une fuite en substituant `_verify` --
    la fidelite de la redaction elle-meme est deja couverte par
    tests/test_redaction.py, ici on verifie seulement le journal."""
    leak_marker = "LEAKEDSECRETXYZ"
    fake_leaks = [
        {"page": 1, "kind": "texte", "text": leak_marker},
        {"page": 1, "kind": "texte", "text": leak_marker + "2"},
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
    """Le filigrane est du texte libre saisi par l'operateur: seul le
    booleen de sa presence est journalise."""
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
    """Regression: un premier passage n'emettait aucune ligne de succes pour
    l'export, seuls les refus l'etaient."""
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

    logs_module.setup_logging()  # ne doit jamais lever: un souci de logging ne
    # doit pas empecher l'appli de servir.

    err = capsys.readouterr().err
    assert "SPYDF_LOG_FILE" in err
