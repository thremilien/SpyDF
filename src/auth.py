"""Who may use the app: the credentials, the signed session cookie, the lockout."""

import hashlib
import hmac
import secrets
import time

from src.config import (
    AUTH_LOCKOUT,
    AUTH_MAX_TRIES,
    AUTH_TTL,
    auth_password,
    auth_secret,
    auth_user,
)

COOKIE_NAME = "spydf_login"

# Generated once per process when SPYDF_AUTH_SECRET is unset: cookies then stop
# being valid at the next restart, which is a sane default for a single-process
# app and never a silent fixed key.
_PROCESS_SECRET = secrets.token_bytes(32)

# ip -> (failure count, first failure). Bounded by _forget_stale, since the key
# comes from the peer address and nothing else prunes it.
_FAILURES: dict[str, tuple[int, float]] = {}


def is_enabled() -> bool:
    """Whether a password is configured, and so whether anything is protected.

    With none set the app answers as it always did — it is meant to run on
    localhost, where a login page in the way would be friction and no defence.
    """
    return bool(auth_password())


def _signing_key() -> bytes:
    """Derive the cookie key from the secret and the credentials.

    Binding the key to the user name and password means changing either one
    invalidates every cookie already issued, without a revocation list.

    Returns:
        A 32-byte key.
    """
    secret = auth_secret().encode("utf-8") or _PROCESS_SECRET
    material = f"{auth_user()}\x00{auth_password()}".encode()
    return hmac.new(secret, b"spydf-login\x00" + material, hashlib.sha256).digest()


def _sign(expiry: int) -> str:
    return hmac.new(_signing_key(), str(expiry).encode("ascii"), hashlib.sha256).hexdigest()


# Constant-time equality of two secrets, as bytes: compare_digest refuses a
# str carrying anything but ASCII, and a password is free to carry more.
def _same(submitted: str | None, configured: str) -> bool:
    return hmac.compare_digest((submitted or "").encode("utf-8"), configured.encode("utf-8"))


def check_credentials(user: str, password: str) -> bool:
    """Whether a submitted pair matches the configured one.

    The password may be configured in clear or as `sha256:<hex>`; both are
    compared in constant time, so a wrong one costs the same as a right one.

    Everything is compared as UTF-8 bytes, never as `str`: `compare_digest`
    raises on a string holding non-ASCII, so an accented password would have
    blown up in here instead of being checked.

    Args:
        user: Submitted user name.
        password: Submitted password.

    Returns:
        True when both match.
    """
    configured = auth_password()
    if not configured:
        return False
    user_ok = _same(user, auth_user())
    if configured.startswith("sha256:"):
        digest = hashlib.sha256((password or "").encode("utf-8")).hexdigest()
        pw_ok = _same(digest, configured[len("sha256:") :].strip().lower())
    else:
        pw_ok = _same(password, configured)
    return user_ok and pw_ok


def issue_token(now: float | None = None) -> str:
    """Mint a cookie value good for `AUTH_TTL` seconds."""
    expiry = int((now if now is not None else time.time()) + AUTH_TTL)
    return f"{expiry}.{_sign(expiry)}"


def token_is_valid(token: str | None, now: float | None = None) -> bool:
    """Whether a cookie value is one we signed and has not expired.

    Args:
        token: The cookie value, or None when the browser sent none.
        now: Current time, injectable for the tests.

    Returns:
        True when the signature matches and the expiry is still ahead.
    """
    if not token or "." not in token:
        return False
    raw_expiry, _, signature = token.partition(".")
    try:
        expiry = int(raw_expiry)
    except ValueError:
        return False
    if expiry <= (now if now is not None else time.time()):
        return False
    return hmac.compare_digest(signature, _sign(expiry))


# Drops the counters whose window has run out, so _FAILURES cannot grow forever.
def _forget_stale(now: float) -> None:
    for ip in [ip for ip, (_, first) in _FAILURES.items() if now - first > AUTH_LOCKOUT]:
        _FAILURES.pop(ip, None)


def is_locked(ip: str, now: float | None = None) -> bool:
    """Whether an address has spent its attempts and must wait out the window.

    Rate limiting is per peer address and in memory only: it slows a script
    down, it is not an account lock — nothing here is persisted.

    Args:
        ip: The peer address.
        now: Current time, injectable for the tests.

    Returns:
        True while the address is locked out.
    """
    now = now if now is not None else time.time()
    _forget_stale(now)
    count, first = _FAILURES.get(ip, (0, 0.0))
    return count >= AUTH_MAX_TRIES and now - first <= AUTH_LOCKOUT


def note_failure(ip: str, now: float | None = None) -> int:
    """Count one failed attempt from an address.

    Args:
        ip: The peer address.
        now: Current time, injectable for the tests.

    Returns:
        How many failures that address has in the current window.
    """
    now = now if now is not None else time.time()
    _forget_stale(now)
    count, first = _FAILURES.get(ip, (0, now))
    _FAILURES[ip] = (count + 1, first)
    return count + 1


# A success clears the address, so a fumbled password costs nothing afterwards.
def note_success(ip: str) -> None:
    _FAILURES.pop(ip, None)
