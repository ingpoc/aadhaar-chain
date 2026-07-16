"""Tests for social/demo principal sessions."""
import pytest
from fastapi.testclient import TestClient

from config import settings
from main import app


def _client() -> TestClient:
    return TestClient(app)


def test_demo_continue_issues_principal_session() -> None:
    client = _client()
    res = client.post(
        "/api/auth/demo-continue",
        json={"audience": "ondcbuyer", "display_name": "Booth Demo"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["data"]["principal_id"].startswith("principal:demo:")
    assert body["data"]["identity_provider"] == "demo"
    assert body["data"]["audience"] == "ondcbuyer"
    assert "wallet_address" not in body["data"]
    assert "aadharcha_session" in res.cookies

    me = client.get("/api/auth/me", cookies=res.cookies)
    assert me.status_code == 200
    assert me.json()["data"]["principal_id"] == body["data"]["principal_id"]
    assert me.json()["data"]["display_name"] == "Booth Demo"
    assert me.json()["data"]["audience"] == "ondcbuyer"


def test_demo_continue_get_redirects() -> None:
    client = _client()
    res = client.get(
        "/api/auth/demo-continue",
        params={"aud": "ondcseller", "return": "http://127.0.0.1:43103/dashboard"},
        follow_redirects=False,
    )
    assert res.status_code == 302
    assert res.headers["location"] == "http://127.0.0.1:43103/dashboard"
    assert "aadharcha_session" in res.cookies


def test_auth_providers_lists_demo(monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolate from local/.env Auth0 so CI and booth machines agree.
    monkeypatch.setattr(settings, "auth0_domain", None)
    monkeypatch.setattr(settings, "auth0_client_id", None)
    monkeypatch.setattr(settings, "auth0_client_secret", None)
    monkeypatch.setattr(settings, "auth_demo_continue", True)
    res = _client().get("/api/auth/providers")
    assert res.status_code == 200
    data = res.json()["data"]
    assert "auth0" in data
    assert "google" in data
    assert "runtime_mode" in data
    assert data["demo_continue"] is True
    assert data["auth0"] is False


def test_agentguard_uses_session_principal_without_wallet_body() -> None:
    client = _client()
    login = client.post(
        "/api/auth/demo-continue",
        json={"audience": "ondcbuyer"},
    )
    cookies = login.cookies
    ensure = client.post(
        "/api/agentguard/agents/ensure",
        json={"role": "buyer"},
        cookies=cookies,
    )
    assert ensure.status_code == 200
    assert ensure.json()["success"] is True
    agent = ensure.json()["data"]["agent"]
    assert agent["principal_id"].startswith("principal:demo:")


def test_body_wallet_rejected_on_social_session() -> None:
    client = _client()
    login = client.post("/api/auth/demo-continue", json={"audience": "ondcbuyer"})
    res = client.post(
        "/api/agentguard/agents/ensure",
        json={
            "role": "buyer",
            "wallet_address": "DifferentAgentGuardWallet11111111111111",
        },
        cookies=login.cookies,
    )
    assert res.status_code == 403
