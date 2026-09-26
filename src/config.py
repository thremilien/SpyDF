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


# ---------- login ----------
# Off entirely until a password is set: on localhost the app is reachable by
# whoever is already at the keyboard, and a login page there would be friction
# rather than a defence. Behind a domain, set SPYDF_AUTH_PASSWORD — in clear or
# as `sha256:<hex>` — and the app asks for it on its own page instead of leaving
# it to the browser's dialog. All three are read at call time so a test (or an
# operator reloading the process) can set them after this module was imported.
def auth_user() -> str:
    """The single account name the login page accepts."""
    return env_str("SPYDF_AUTH_USER", "admin")


def auth_password() -> str:
    """The configured password, in clear or as `sha256:<hex>`. Empty disables the login.

    Read raw, not through `env_str`: that one strips, and a password is
    entitled to begin or end with a space. Trimming it here would refuse the
    very password the operator set, with nothing to show why.
    """
    return os.environ.get("SPYDF_AUTH_PASSWORD", "")


def auth_secret() -> str:
    """Key signing the login cookie; empty means a fresh one per process."""
    return env_str("SPYDF_AUTH_SECRET", "")


AUTH_TTL = env_int("SPYDF_AUTH_TTL", 12 * 3600)  # how long one login stays valid
AUTH_MAX_TRIES = env_int("SPYDF_AUTH_MAX_TRIES", 10)  # failures per address before the wait
AUTH_LOCKOUT = env_int("SPYDF_AUTH_LOCKOUT", 300)  # length of that wait, and of the window

# ---------- sessions ----------
MAX_UPLOAD_BYTES = env_int("SPYDF_MAX_UPLOAD_BYTES", 200 * 1024 * 1024)
SESSION_TTL = env_int("SPYDF_SESSION_TTL", 2 * 3600)  # a forgotten doc must not sit in RAM
MAX_SESSIONS = env_int("SPYDF_MAX_SESSIONS", 32)

# ---------- page rendering ----------
RENDER_ZOOM = env_float("SPYDF_RENDER_ZOOM", 4.0)  # fallback when no width is asked for
MIN_ZOOM = env_float("SPYDF_MIN_ZOOM", 1.5)
MAX_ZOOM = env_float("SPYDF_MAX_ZOOM", 8.0)  # memory guard rail: 8x on A4 = ~128 Mpx

# ---------- redaction ----------
# A zone is painted straight into the page bitmap, so how coarse a mosaic is
# has become the only knob: nothing about strips or masks is left, a raster
# being filled row by row, exactly along the drawn outline.
MOSAIC_BLOCKS = env_int("SPYDF_MOSAIC_BLOCKS", 14)  # pixelated zone width, in "big pixels"

# ---------- background colour ----------
# Each page's paper colour is read once, when the document is opened, and is the
# default fill of a delete zone. The page is rendered this small for it: the
# paper covers most of it, so a few hundred pixels a side read it as well as a
# full render, at a fraction of the cost.
BG_SAMPLE_SIDE = env_int("SPYDF_BG_SAMPLE_SIDE", 160)
# Bits kept per channel when sorting pixels into buckets: 4 gives buckets 16
# levels wide, broad enough to hold a scan's paper grain, narrow enough to keep
# grey text edges out of it.
BG_BUCKET_BITS = min(8, max(1, env_int("SPYDF_BG_BUCKET_BITS", 4)))
BG_CANDIDATES = env_int("SPYDF_BG_CANDIDATES", 8)  # fullest buckets weighed with their neighbours
# Half-width, in levels per channel, of the window the estimate is refined in: a
# scan's paper grain fits inside it, the grey edges of its text do not.
BG_RADIUS = env_int("SPYDF_BG_RADIUS", 12)

# ---------- export ----------
# Every page is rendered flat before it is written back, which is what makes an
# export carry nothing but its pixels. The resolution is therefore the quality
# of the exported document, and the input to the OCR that re-indexes it: 300 dpi
# puts an A4 at about 2480 x 3508, print quality, near the scan it came from.
EXPORT_DPI = env_int("SPYDF_EXPORT_DPI", 300)
# A page larger than this, in either orientation, is shrunk to fit inside it
# before rendering, its proportions kept. Some scanners store an A4 scan as a
# poster-sized page; at EXPORT_DPI that would be twenty times the pixels of the
# scan itself, all of them interpolated. A page already smaller is left alone.
EXPORT_MAX_SHORT_MM = env_float("SPYDF_EXPORT_MAX_SHORT_MM", 210.0)
EXPORT_MAX_LONG_MM = env_float("SPYDF_EXPORT_MAX_LONG_MM", 297.0)
# JPEG quality of the flattened pages; 95 is near-lossless to the eye, 0 keeps
# them truly lossless, which multiplies the size of a scan by five or six.
EXPORT_JPEG_QUALITY = env_int("SPYDF_EXPORT_JPEG_QUALITY", 95)

# ---------- OCR ----------
# The export reads back the pages it has just flattened and lays the recognised
# text over them as invisible ink, so the result stays searchable. Longest side
# a page bitmap is shrunk to before recognition: above this the engine costs a
# lot and reads no better.
OCR_MAX_SIDE = env_int("SPYDF_OCR_MAX_SIDE", 2400)
OCR_MIN_SCORE = env_float("SPYDF_OCR_MIN_SCORE", 0.5)  # below this it is noise, not text
OCR_MAX_PAGES = env_int("SPYDF_OCR_MAX_PAGES", 100)  # guard rail on a very long document
OCR_SNIPPET = env_int("SPYDF_OCR_SNIPPET", 200)  # one fragment is a line, never a page
# Pages read at once, each by its own engine with an equal share of the cores.
# One engine alone keeps a dozen cores only two-thirds busy; three side by side
# read a page in about 60% of the time. 0 picks it from the core count: one
# engine per two cores, three at most, beyond which nothing more is gained.
OCR_WORKERS = env_int("SPYDF_OCR_WORKERS", 0)
OCR_AUTO_MAX_WORKERS = 3
OCR_CORES_PER_WORKER = 2
OCR_QUEUE_PER_WORKER = 2  # pages queued ahead of each reader during an export


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
