"""Session CSRF recovery must keep the existing authentication boundaries."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from http.cookies import SimpleCookie
from threading import Barrier

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.main import app
from app.shared.config import settings
from app.shared.db import SessionLocal, utcnow
from app.shared.models import AuthSession, User
from app.shared.security import digest, token
from app.worker import tick
from conftest import invite, login, mutate
import test_pdf_imports as pdf_imports


ORIGIN = "http://testserver"


def replace_csrf_cookie(client, value):
    client.cookies.delete("notes_csrf")
    if value is not None:
        client.cookies.set("notes_csrf", value, domain="testserver.local", path="/")


def bootstrap(client):
    response = client.get("/api/v1/auth/csrf")
    assert response.status_code == 200, response.text
    value = response.json()["csrf_token"]
    assert value and value == client.cookies.get("notes_csrf")
    assert response.headers["cache-control"] == "no-store"
    return value


def create_note(client, csrf=None, origin=ORIGIN):
    headers = {}
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    if origin is not None:
        headers["Origin"] = origin
    return client.post("/api/v1/notes", json={"title": "CSRF 恢复后的笔记"}, headers=headers)


def cookies_from(response):
    cookies = SimpleCookie()
    for value in response.headers.get_list("set-cookie"):
        cookies.load(value)
    return cookies


@pytest.mark.parametrize("cookie", [None, "stale-csrf-cookie"])
def test_authenticated_bootstrap_recovers_missing_or_stale_csrf_cookie(client, cookie):
    login(client)
    replace_csrf_cookie(client, cookie)
    assert client.get("/api/v1/auth/me").status_code == 200
    response = mutate(client, "POST", "/notes", {"title": "CSRF 恢复后的笔记"})
    assert response.status_code == 201, response.text


@pytest.mark.parametrize("legacy", [False, True])
def test_repeated_and_parallel_bootstrap_remain_stable_for_one_session(client, legacy):
    login(client)
    raw = client.cookies.get("notes_session")
    with SessionLocal.begin() as db:
        session = db.scalar(select(AuthSession).where(AuthSession.token_hash == digest(raw)))
        expiry = session.expires_at
        if legacy:
            session.csrf_hash = digest(token())

    # Each tab has its own cookie jar. All requests intentionally arrive with
    # a missing/stale token, including the first upgrade of an old random token.
    barrier = Barrier(4)

    def tab(index):
        with TestClient(app) as other:
            other.cookies.set("notes_session", raw, domain="testserver.local", path="/")
            if index % 2:
                replace_csrf_cookie(other, "stale-parallel-cookie")
            barrier.wait(timeout=10)
            return bootstrap(other)

    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(tab, range(4)))
    assert len(set(values)) == 1
    expected = values[0]
    for stale in [None, "a-late-response-from-another-tab", None]:
        replace_csrf_cookie(client, stale)
        assert bootstrap(client) == expected
    assert create_note(client, expected).status_code == 201
    with SessionLocal() as db:
        session = db.scalar(select(AuthSession).where(AuthSession.token_hash == digest(raw)))
        assert session.csrf_hash == digest(expected)
        assert session.expires_at == expiry


def test_tokens_are_bound_to_each_session_even_for_the_same_account(client):
    login(client)
    first = bootstrap(client)
    with TestClient(app) as other:
        login(other)
        second = bootstrap(other)
        assert first != second
        for target, foreign_token in [(client, second), (other, first)]:
            denied = create_note(target, foreign_token)
            assert denied.status_code == 403
            assert denied.json()["code"] == "csrf_invalid"
        assert create_note(client, first).status_code == 201
        assert create_note(other, second).status_code == 201


@pytest.mark.parametrize("supplied", [None, "unrelated-csrf-value"])
def test_recovery_keeps_missing_and_incorrect_headers_forbidden(client, supplied):
    login(client)
    expected = bootstrap(client)
    # Matching an arbitrary cookie and header must not satisfy a private session.
    replace_csrf_cookie(client, supplied)
    response = create_note(client, supplied)
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_invalid"
    assert create_note(client, expected).status_code == 201


@pytest.mark.parametrize("origin", [None, "https://untrusted.example"])
def test_recovery_keeps_origin_validation(client, origin):
    login(client)
    response = create_note(client, bootstrap(client), origin)
    assert response.status_code == 403
    assert response.json()["code"] == "origin_invalid"


@pytest.mark.parametrize("headers", [
    {"Origin": "https://untrusted.example"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"},
])
@pytest.mark.parametrize("authenticated", [False, True])
def test_foreign_bootstrap_get_is_rejected_without_changing_legacy_session(client, headers, authenticated):
    raw = None
    if authenticated:
        login(client)
        raw = client.cookies.get("notes_session")
        with SessionLocal.begin() as db:
            session = db.scalar(select(AuthSession).where(AuthSession.token_hash == digest(raw)))
            session.csrf_hash = digest("legacy-before-foreign-bootstrap")
            original = (session.csrf_hash, session.expires_at, session.revoked_at)
    replace_csrf_cookie(client, None)
    response = client.get("/api/v1/auth/csrf", headers=headers)
    assert response.status_code == 403
    assert "notes_csrf" not in cookies_from(response)
    if raw:
        with SessionLocal() as db:
            session = db.scalar(select(AuthSession).where(AuthSession.token_hash == digest(raw)))
            assert (session.csrf_hash, session.expires_at, session.revoked_at) == original


@pytest.mark.parametrize("headers", [
    {},
    {"Origin": ORIGIN},
    {"Sec-Fetch-Site": "same-origin"},
    {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
])
def test_same_origin_bootstrap_get_remains_allowed(client, headers):
    login(client)
    replace_csrf_cookie(client, None)
    response = client.get("/api/v1/auth/csrf", headers=headers)
    assert response.status_code == 200
    value = response.json()["csrf_token"]
    assert create_note(client, value).status_code == 201


@pytest.mark.parametrize("state", ["expired", "revoked", "disabled"])
def test_bootstrap_cannot_restore_invalid_authentication_or_change_its_session(client, state):
    login(client)
    raw = client.cookies.get("notes_session")
    with SessionLocal.begin() as db:
        session = db.scalar(select(AuthSession).where(AuthSession.token_hash == digest(raw)))
        session.csrf_hash = digest("legacy-invalid-session-token")
        if state == "expired":
            session.expires_at = utcnow() - timedelta(seconds=1)
        elif state == "revoked":
            session.revoked_at = utcnow()
        else:
            db.get(User, session.user_id).status = "disabled"
        original = (session.csrf_hash, session.expires_at, session.revoked_at)
    replace_csrf_cookie(client, None)
    assert client.get("/api/v1/auth/me").status_code == 401
    value = bootstrap(client)
    assert client.get("/api/v1/auth/me").status_code == 401
    assert create_note(client, value).status_code == 401
    with SessionLocal() as db:
        session = db.scalar(select(AuthSession).where(AuthSession.token_hash == digest(raw)))
        assert (session.csrf_hash, session.expires_at, session.revoked_at) == original


def test_anonymous_bootstrap_still_supports_public_login_with_double_submit(client):
    public_token = bootstrap(client)
    assert not client.cookies.get("notes_session")
    credentials = {"username": "admin", "password": "admin-test-password"}
    denied = client.post("/api/v1/auth/login", json=credentials,
        headers={"Origin": ORIGIN, "X-CSRF-Token": "incorrect-public-token"})
    assert denied.status_code == 403
    response = client.post("/api/v1/auth/login", json=credentials,
        headers={"Origin": ORIGIN, "X-CSRF-Token": public_token})
    assert response.status_code == 200
    login_cookies = cookies_from(response)
    assert login_cookies["notes_session"]["httponly"]
    assert not login_cookies["notes_csrf"]["httponly"]
    private_token = client.cookies.get("notes_csrf")
    assert private_token != public_token
    assert private_token != client.cookies.get("notes_session")
    assert create_note(client, private_token).status_code == 201
    assert create_note(client, public_token).status_code == 403
    assert bootstrap(client) == private_token


def test_anonymous_bootstrap_still_supports_invitation_registration(client):
    invitation = invite(client)
    with TestClient(app) as member:
        public_token = bootstrap(member)
        body = {"username": "csrf_member", "password": "synthetic-member-password",
            "display_name": "CSRF 注册测试", "invite_code": invitation["code"]}
        denied = member.post("/api/v1/auth/register", json=body,
            headers={"Origin": ORIGIN, "X-CSRF-Token": "incorrect-public-token"})
        assert denied.status_code == 403
        response = member.post("/api/v1/auth/register", json=body,
            headers={"Origin": ORIGIN, "X-CSRF-Token": public_token})
        assert response.status_code == 201
        assert member.get("/api/v1/auth/me").json()["username"] == body["username"]
        private_token = member.cookies.get("notes_csrf")
        assert private_token != public_token
        assert create_note(member, private_token).status_code == 201
        assert bootstrap(member) == private_token


def test_bootstrap_preserves_persistent_cookie_and_does_not_extend_session(client):
    login(client)
    raw = client.cookies.get("notes_session")
    with SessionLocal.begin() as db:
        session = db.scalar(select(AuthSession).where(AuthSession.token_hash == digest(raw)))
        session.expires_at = utcnow() + timedelta(minutes=12)
        expiry = session.expires_at
    before = utcnow()
    replace_csrf_cookie(client, None)
    response = client.get("/api/v1/auth/csrf")
    assert response.status_code == 200
    cookies = cookies_from(response)
    csrf_cookie = cookies["notes_csrf"]
    assert 0 < int(csrf_cookie["max-age"]) <= int((expiry - before).total_seconds())
    assert not csrf_cookie["httponly"]
    assert csrf_cookie["path"] == "/"
    assert csrf_cookie["samesite"].lower() == "lax"
    assert client.cookies.get("notes_session") == raw
    with SessionLocal() as db:
        session = db.scalar(select(AuthSession).where(AuthSession.token_hash == digest(raw)))
        assert session.expires_at == expiry


def test_pdf_import_recovers_csrf_at_create_upload_and_conversion_start(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings(), "storage_root", str(tmp_path / "private"))
    login(client)
    replace_csrf_cookie(client, None)
    item, original = pdf_imports.create(client, key="csrf-recovery-pdf-001")
    replace_csrf_cookie(client, "stale-token-before-binary-upload")
    response = pdf_imports.send(client, item, original)
    assert response.status_code == 200, response.text
    replace_csrf_cookie(client, None)
    response = pdf_imports.begin(client, item)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"
    assert tick()
    result = pdf_imports.get(client, item).json()
    assert result["status"] == "ready"
    assert result["page_count"] == result["completed_pages"] == 3
    pages = client.get(f"/api/v1/notes/{item['note_id']}/pages").json()
    assert len(pages) == 3
    replace_csrf_cookie(client, "stale-token-before-original-download")
    ticket = mutate(client, "POST", f"/files/{item['upload']['file']['id']}/ticket?kind=original", {})
    assert ticket.status_code == 200
    assert client.get(ticket.json()["url"]).content == original
