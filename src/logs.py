"""The "spydf" logger: where the audit-trail lines go and how they are formatted."""

import logging
import logging.handlers
import re
import sys

from src.config import LOG_FILE_BACKUPS, LOG_FILE_MAX_BYTES, log_file, log_level

LOGGER_NAME = "spydf"
DEFAULT_LEVEL = "INFO"

# Library-side silence: importing src.app without setup_logging() (the tests do)
# must neither crash nor print.
_logger = logging.getLogger(LOGGER_NAME)
_logger.addHandler(logging.NullHandler())

_configured = False


def _resolve_level(raw: str) -> int:
    """Turn a level name into its number.

    Args:
        raw: A level name in any case, or None. Anything unrecognised falls back
            to INFO rather than breaking startup.

    Returns:
        The matching `logging` level.
    """
    name = (raw or DEFAULT_LEVEL).strip().upper()
    level = logging.getLevelName(name)
    return level if isinstance(level, int) else logging.INFO


def setup_logging() -> logging.Logger:
    """Configure the logger from `src.config`. Idempotent.

    A stderr handler is always installed, since that is where a container's logs
    belong. A configured log file adds a bounded rotating handler next to it; if
    that path cannot be opened the app warns and carries on with stderr alone,
    because a logging problem must never stop it from serving.

    Returns:
        The configured logger. Calling this twice does not duplicate lines.
    """
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(_resolve_level(log_level()))

    if _configured:
        return logger

    for h in list(logger.handlers):
        if isinstance(h, logging.NullHandler):
            logger.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    path = log_file()
    if path:
        try:
            file_handler = logging.handlers.RotatingFileHandler(
                path,
                maxBytes=LOG_FILE_MAX_BYTES,
                backupCount=LOG_FILE_BACKUPS,
                encoding="utf-8",
            )
            file_handler.setFormatter(fmt)
            logger.addHandler(file_handler)
        except OSError as e:
            print(f"spydf: cannot open SPYDF_LOG_FILE={path!r}: {e}", file=sys.stderr)

    logger.propagate = False
    _configured = True
    return logger


_UNSAFE_VALUE = re.compile(r"[\s]")


def _fmt_value(value) -> str:
    """Render one field value, greppable and safe.

    Booleans become true/false. A value holding whitespace is quoted, with inner
    quotes and newlines escaped, so no field can ever forge a second log line.

    Args:
        value: Any value to render.

    Returns:
        The rendered value.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if _UNSAFE_VALUE.search(text):
        text = text.replace("\\", "\\\\").replace('"', '\\"')
        text = text.replace("\r", "\\r").replace("\n", "\\n")
        return f'"{text}"'
    return text


def log_event(name: str, level: int = logging.INFO, **fields) -> None:
    """Emit one `event=<name> k=v ...` line on the "spydf" logger.

    Args:
        name: The event name, e.g. "import" or "export_rejected".
        level: Logging level, INFO by default.
        **fields: Key/value pairs appended to the line, in order.
    """
    parts = [f"event={_fmt_value(name)}"]
    parts.extend(f"{k}={_fmt_value(v)}" for k, v in fields.items())
    logging.getLogger(LOGGER_NAME).log(level, " ".join(parts))
