"""Every tunable in one place, read from the environment (and from .env if present)."""

import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _load_env_file(path: Path = ENV_FILE) -> None:
    """Load `KEY=value` lines from a .env file, without overriding the real env.

    Deliberately minimal — no quoting rules beyond stripping one matching pair,
    no interpolation, no export syntax. A real environment variable always wins,
    so a container's `environment:` block keeps priority over a stray file.

    Args:
        path: The file to read. Missing or unreadable is not an error.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_env_file()


def env_str(name: str, default: str) -> str:
    """Read a string setting, falling back to `default` when unset or empty."""
    return os.environ.get(name, "").strip() or default


def env_int(name: str, default: int) -> int:
    """Read an int setting, falling back to `default` when unset or unparsable."""
    try:
        return int(env_str(name, str(default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    """Read a float setting, falling back to `default` when unset or unparsable."""
    try:
        return float(env_str(name, str(default)))
    except ValueError:
        return default


def env_flag(name: str) -> bool:
    """Whether a boolean setting is on, read at call time so tests can patch it."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


# ---------- server ----------
HOST = env_str("HOST", "127.0.0.1")
PORT = env_int("PORT", 8765)

# ---------- sessions ----------
MAX_UPLOAD_BYTES = env_int("SPYDF_MAX_UPLOAD_BYTES", 200 * 1024 * 1024)
SESSION_TTL = env_int("SPYDF_SESSION_TTL", 2 * 3600)  # a forgotten doc must not sit in RAM
MAX_SESSIONS = env_int("SPYDF_MAX_SESSIONS", 32)

# ---------- page rendering ----------
RENDER_ZOOM = env_float("SPYDF_RENDER_ZOOM", 4.0)  # fallback when no width is asked for
MIN_ZOOM = env_float("SPYDF_MIN_ZOOM", 1.5)
MAX_ZOOM = env_float("SPYDF_MAX_ZOOM", 8.0)  # memory guard rail: 8x on A4 = ~128 Mpx

# ---------- redaction ----------
MOSAIC_BLOCKS = env_int("SPYDF_MOSAIC_BLOCKS", 14)  # pixelated zone width, in "big pixels"
STRIP_HEIGHT = env_float("SPYDF_STRIP_HEIGHT", 2.0)  # one redaction strip, in PDF points
MAX_STRIPS = env_int("SPYDF_MAX_STRIPS", 200)  # a tall zone must not yield a thousand rects
MASK_MAX_PX = env_int("SPYDF_MASK_MAX_PX", 240)  # mask clipping a mosaic to the outline
LEAK_COVERAGE = env_float("SPYDF_LEAK_COVERAGE", 0.15)  # word coverage above which it leaks

# ---------- watermark ----------
WATERMARK_MAX_LEN = env_int("SPYDF_WATERMARK_MAX_LEN", 80)
WATERMARK_MIN_SIZE = env_float("SPYDF_WATERMARK_MIN_SIZE", 8)
WATERMARK_DIAGONAL_RATIO = env_float("SPYDF_WATERMARK_DIAGONAL_RATIO", 0.78)
WATERMARK_FONT = env_str("SPYDF_WATERMARK_FONT", "helv")  # base-14, nothing to embed
# No absolute cap on the font size: _watermark_fit_size is geometric, and so
# scale-invariant. An absolute one in points would render differently on a page
# measured in points and on a scan measured in pixels.


# ---------- logging ----------
# Level and destination are read at call time, not at import: an operator (or a
# test) can set them after this module has been imported.
def log_level() -> str:
    """The configured log level name."""
    return env_str("SPYDF_LOG_LEVEL", "INFO")


def log_file() -> str:
    """The configured log file path, empty for stderr only."""
    return env_str("SPYDF_LOG_FILE", "")


LOG_FILE_MAX_BYTES = env_int("SPYDF_LOG_FILE_MAX_BYTES", 1024 * 1024)
LOG_FILE_BACKUPS = env_int("SPYDF_LOG_FILE_BACKUPS", 3)
LOG_FILENAMES_VAR = "SPYDF_LOG_FILENAMES"  # read at call time, see env_flag
# A whole sid is an access capability (/api/download/{key}): only this prefix
# is ever logged.
SID_LOG_LEN = env_int("SPYDF_SID_LOG_LEN", 8)
UA_MAX_LEN = env_int("SPYDF_UA_MAX_LEN", 120)  # client-supplied, bound before logging
