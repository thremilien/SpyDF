"""Tests for the login page: what it lets through, what it stops, what it logs."""

import hashlib
import logging

import pytest
from fastapi.testclient import TestClient

import src.auth as auth_module
from src.app import app
from src.auth import COOKIE_NAME

USER = "teacher"
PASSWORD = "correct horse battery"
SHA256_OF_PASSWORD = "sha256:" + hashlib.sha256(PASSWORD.encode()).hexdigest()


@pytest.fixture
def client():
    # follow_redirects off: the redirect itself is what most of these assert.
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def no_lockout():
    """Start every test from an empty failure table, and leave one behind."""
    auth_module._FAILURES.clear()
    yield
    auth_module._FAILURES.clear()


@pytest.fixture
def login_on(monkeypatch, no_lockout):
    monkeypatch.setenv("SPYDF_AUTH_USER", USER)
    monkeypatch.setenv("SPYDF_AUTH_PASSWORD", PASSWORD)
    monkeypatch.setenv("SPYDF_AUTH_SECRET", "test-secret")


def sign_in(client, user=USER, password=PASSWORD):
    return client.post("/login", data={"username": user, "password": password})


# ---------- off by default ----------


def test_no_password_configured_leaves_the_app_open(client, no_lockout, monkeypatch):
    monkeypatch.delenv("SPYDF_AUTH_PASSWORD", raising=False)
    assert client.get("/").status_code == 200


def test_login_page_redirects_to_the_app_when_there_is_nothing_to_sign_into(
    client, no_lockout, monkeypatch
):
    monkeypatch.delenv("SPYDF_AUTH_PASSWORD", raising=False)
    r = client.get("/login")
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_no_signout_button_without_a_password(client, no_lockout, monkeypatch):
    monkeypatch.delenv("SPYDF_AUTH_PASSWORD", raising=False)
    assert "/logout" not in client.get("/").text


# ---------- the gate ----------


def test_page_request_is_redirected_to_the_login_page(client, login_on):
    r = client.get("/", headers={"accept": "text/html"})
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_api_request_gets_a_401_not_a_login_page(client, login_on):
    r = client.get("/api/session/whatever")
    assert r.status_code == 401
    assert r.json()["detail"] == "not signed in"


def test_export_is_closed_too(client, login_on):
    assert client.post("/api/export", json={"sid": "x", "zones": {}}).status_code == 401


def test_the_login_page_and_its_stylesheet_stay_reachable(client, login_on):
    assert client.get("/login").status_code == 200
    assert client.get("/static/style.css").status_code == 200


# ---------- signing in ----------


def test_right_credentials_open_the_app(client, login_on):
    r = sign_in(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get("/").status_code == 200


def test_the_cookie_is_httponly_and_lax(client, login_on):
    sign_in(client)
    cookie = client.cookies.jar._cookies["testserver.local"]["/"][COOKIE_NAME]
    header = sign_in(client).headers["set-cookie"].lower()
    assert cookie.value
    assert "httponly" in header and "samesite=lax" in header


def test_a_sha256_password_is_accepted_too(client, login_on, monkeypatch):
    monkeypatch.setenv("SPYDF_AUTH_PASSWORD", SHA256_OF_PASSWORD)
    assert sign_in(client).status_code == 303
    assert client.get("/").status_code == 200


@pytest.mark.parametrize(
    "password",
    [
        "Mot2Passé",  # compare_digest refuses a non-ASCII str: it must see bytes
        "clé€uro",
        "  padded  ",  # a password may begin or end with a space
        "dollar$sign",
        "quote\"and'quote",
        "back\\slash",
    ],
)
def test_an_awkward_password_is_still_checked(client, login_on, monkeypatch, password):
    monkeypatch.setenv("SPYDF_AUTH_PASSWORD", password)
    assert sign_in(client, password=password).status_code == 303
    assert sign_in(client, password=password + "x").status_code == 401


def test_an_awkward_password_works_hashed_too(client, login_on, monkeypatch):
    password = "Mot2Passé€ !"
    monkeypatch.setenv(
        "SPYDF_AUTH_PASSWORD", "sha256:" + hashlib.sha256(password.encode()).hexdigest()
    )
    assert sign_in(client, password=password).status_code == 303


def test_wrong_password_is_refused_and_says_so(client, login_on):
    r = sign_in(client, password="nope")
    assert r.status_code == 401
    assert "Wrong user name or password" in r.text
    assert client.get("/", headers={"accept": "text/html"}).status_code == 303


def test_wrong_user_is_refused(client, login_on):
    assert sign_in(client, user="someone").status_code == 401


def test_the_submitted_user_comes_back_escaped(client, login_on):
    r = sign_in(client, user='"><script>alert(1)</script>', password="nope")
    assert "<script>alert(1)</script>" not in r.text
    assert 'value="&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"' in r.text


def test_signing_in_shows_the_signout_button(client, login_on):
    sign_in(client)
    assert 'action="/logout"' in client.get("/").text


def test_signing_out_drops_the_cookie(client, login_on):
    sign_in(client)
    r = client.post("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    client.cookies.clear()
    assert client.get("/", headers={"accept": "text/html"}).status_code == 303


# ---------- the cookie itself ----------


def test_a_forged_cookie_is_not_a_login(client, login_on):
    client.cookies.set(COOKIE_NAME, "99999999999.deadbeef")
    assert client.get("/", headers={"accept": "text/html"}).status_code == 303


def test_an_expired_cookie_is_not_a_login(login_on):
    token = auth_module.issue_token(now=0)
    assert not auth_module.token_is_valid(token)


def test_changing_the_password_invalidates_the_cookies_it_signed(client, login_on, monkeypatch):
    sign_in(client)
    assert client.get("/").status_code == 200
    monkeypatch.setenv("SPYDF_AUTH_PASSWORD", "something else")
    assert client.get("/", headers={"accept": "text/html"}).status_code == 303


# ---------- rate limiting ----------


def test_too_many_failures_lock_the_address_out(client, login_on, monkeypatch):
    monkeypatch.setattr(auth_module, "AUTH_MAX_TRIES", 3)
    for _ in range(3):
        assert sign_in(client, password="nope").status_code == 401
    r = sign_in(client, password="nope")
    assert r.status_code == 429
    assert "Too many attempts" in r.text
    # and the right password does not get through while the lockout holds
    assert sign_in(client).status_code == 429


def test_a_success_clears_the_count(client, login_on, monkeypatch):
    monkeypatch.setattr(auth_module, "AUTH_MAX_TRIES", 3)
    sign_in(client, password="nope")
    sign_in(client)
    assert auth_module._FAILURES == {}


# ---------- the log ----------


def test_the_log_never_carries_the_submitted_credentials(client, login_on, caplog):
    with caplog.at_level(logging.INFO, logger="spydf"):
        sign_in(client, user="jean.dupont", password="hunter2")
        sign_in(client)
    text = caplog.text
    assert "hunter2" not in text and "jean.dupont" not in text
    assert "event=login_rejected" in text and "reason=bad_credentials" in text
    assert "event=login " in text or text.rstrip().endswith("event=login")
