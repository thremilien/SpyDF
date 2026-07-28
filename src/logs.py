"""Logger configuration for the operator-visible audit trail.

Three events matter: connection to the site, PDF import, PDF export. This
module owns the single `"spydf"` logger, its handlers, and a small
`log_event` helper that formats fields as greppable `key=value` pairs.

Nothing here decides *what* to log — that stays in src/app.py and
src/server.py, close to the data. This module only wires up *where* log
lines go and how they are formatted.
"""

import logging
import logging.handlers
import os
import re
import sys

LOGGER_NAME = "spydf"
DEFAULT_LEVEL = "INFO"

_logger = logging.getLogger(LOGGER_NAME)
_logger.addHandler(logging.NullHandler())  # bibliotheque: silencieux tant que
# setup_logging() n'a pas ete appele (cas des tests qui importent src.app
# directement, sans passer par src/server.py).

_configured = False  # protege contre les handlers en double si on appelle
# setup_logging() plusieurs fois (rechargement, tests).


def _resolve_level(raw: str) -> int:
    """Nom de niveau insensible a la casse; une valeur farfelue retombe sur
    INFO plutot que de faire planter le demarrage."""
    name = (raw or DEFAULT_LEVEL).strip().upper()
    level = logging.getLevelName(name)
    return level if isinstance(level, int) else logging.INFO


def setup_logging() -> logging.Logger:
    """Configure le logger "spydf": un handler stderr toujours present, et un
    handler fichier optionnel si SPYDF_LOG_FILE est defini. Idempotent: un
    second appel ne double pas les lignes."""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(_resolve_level(os.environ.get("SPYDF_LOG_LEVEL")))

    if _configured:
        return logger

    # on retire le NullHandler pose au chargement du module: une fois
    # configure, on veut les vrais handlers et rien d'autre.
    for h in list(logger.handlers):
        if isinstance(h, logging.NullHandler):
            logger.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    log_file = os.environ.get("SPYDF_LOG_FILE")
    if log_file:
        try:
            file_handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=1 * 1024 * 1024, backupCount=3,
                encoding="utf-8")
            file_handler.setFormatter(fmt)
            logger.addHandler(file_handler)
        except OSError as e:
            # un souci de logging ne doit jamais empecher l'appli de servir:
            # on avertit sur stderr et on continue avec stderr seul.
            print(f"spydf: impossible d'ouvrir SPYDF_LOG_FILE={log_file!r}: {e}",
                  file=sys.stderr)

    logger.propagate = False
    _configured = True
    return logger


_UNSAFE_VALUE = re.compile(r"[\s]")


def _fmt_value(value) -> str:
    """Rend une valeur greppable et sure: les booleens en true/false, et toute
    valeur contenant un espace ou un saut de ligne entre guillemets avec les
    guillemets et sauts de ligne internes neutralises — une valeur ne doit
    jamais pouvoir fabriquer une fausse seconde ligne de log (injection de
    log)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if _UNSAFE_VALUE.search(text):
        text = text.replace("\\", "\\\\").replace('"', '\\"')
        text = text.replace("\r", "\\r").replace("\n", "\\n")
        return f'"{text}"'
    return text


def log_event(name: str, level: int = logging.INFO, **fields) -> None:
    """Emet une ligne `event=<name> k=v ...` sur le logger "spydf"."""
    parts = [f"event={_fmt_value(name)}"]
    parts.extend(f"{k}={_fmt_value(v)}" for k, v in fields.items())
    logging.getLogger(LOGGER_NAME).log(level, " ".join(parts))
