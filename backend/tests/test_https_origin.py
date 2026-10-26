"""HTTPS-origin application regressions on the disposable PostgreSQL database."""
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.shared.config import Settings, settings

ORIGIN = "https://47.111.12.85"


@pytest.fixture
def https_client(monkeypatch):
    # Keep test storage local and the model key empty; only the existing origin
    # and cookie settings change. TestClient does not establish a TLS socket.
    monkeypatch.setattr(settings(), "app_origin", ORIGIN)
    monkeypatch.setattr(settings(), "cookie_secure", True)
    with TestClient(app, base_url=ORIGIN) as client:
        yield client


def tab_login(client):
    response = client.post("/api/v1/auth/tab/login", headers={
        "Origin": ORIGIN, "X-Notes-Client": "tab"},
        json={"username": "admin", "password": "admin-test-password"})
    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    return response.json()


@pytest.mark.parametrize("origin", [None, "http://47.111.12.85:8080", "http://47.111.12.85",
                                   "https://47.111.12.85:8080", "https://untrusted.invalid"])
def test_https_login_rejects_old_or_different_origins(https_client, origin):
    headers = {"X-Notes-Client": "tab"}
    if origin is not None:
        headers["Origin"] = origin
    response = https_client.post("/api/v1/auth/tab/login", headers=headers,
        json={"username": "admin", "password": "admin-test-password"})
    assert response.status_code == 403 and response.json()["code"] == "origin_invalid"


def test_https_tab_bearer_csrf_and_independent_logout(https_client):
    first, second = tab_login(https_client), tab_login(https_client)
    def headers(session, csrf=None):
        return {"Origin": ORIGIN, "Authorization": "Bearer " + session["session_token"],
                "X-CSRF-Token": session["csrf_token"] if csrf is None else csrf}
    for session in (first, second):
        assert https_client.get("/api/v1/auth/me", headers=headers(session)).status_code == 200
    rejected = https_client.post("/api/v1/auth/logout", headers=headers(first, "incorrect"), json={})
    assert rejected.status_code == 403 and rejected.json()["code"] == "csrf_invalid"
    assert https_client.post("/api/v1/auth/logout", headers=headers(first), json={}).status_code == 200
    assert https_client.get("/api/v1/auth/me", headers=headers(first)).status_code == 401
    assert https_client.get("/api/v1/auth/me", headers=headers(second)).status_code == 200


def test_https_legacy_cookie_flow_remains_secure_and_csrf_guarded(https_client):
    token = https_client.get("/api/v1/auth/csrf").json()["csrf_token"]
    response = https_client.post("/api/v1/auth/login", headers={"Origin": ORIGIN, "X-CSRF-Token": token},
        json={"username": "admin", "password": "admin-test-password"})
    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2 and all("; Secure" in value and "SameSite=lax" in value for value in cookies)
    assert any(value.startswith("notes_session=") and "HttpOnly" in value for value in cookies)
    assert https_client.get("/api/v1/auth/me").status_code == 200
    response = https_client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN}, json={})
    assert response.status_code == 403 and response.json()["code"] == "csrf_invalid"


def test_production_accepts_ip_https_and_requires_secure_cookie():
    fixture = dict(_env_file=None, app_env="production", app_origin=ORIGIN, cookie_secure=True,
        storage_backend="oss", oss_region="cn-hangzhou", oss_bucket="fixture-notes",
        oss_prefix="ai-notes/", oss_endpoint="", oss_access_key_id="fixture-key-id",
        oss_access_key_secret="fixture-key-secret", oss_security_token="", deepseek_api_key="")
    assert Settings(**fixture).app_origin == ORIGIN
    for changes in ({"cookie_secure": False}, {"app_origin": "http://47.111.12.85:8080"}):
        with pytest.raises(ValidationError):
            Settings(**{**fixture, **changes})
