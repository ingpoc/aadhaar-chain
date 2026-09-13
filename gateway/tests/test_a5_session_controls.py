"""A5 session controls: rotation, revocation, isolation, step-up."""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.session_auth import (
    create_principal_session_token,
    create_session_token,
    parse_session_token,
    SESSION_COOKIE_NAME,
)
from app.session_registry import (
    bind_session_store,
    reset_session_registry,
    unbind_session_store,
)
from config import settings
from main import app

WALLET = "A5SessionControlWallet11111111111111111"


@pytest.fixture(autouse=True)
def _reset_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    # TestClient(app) lifespan must not apply migrations to Render DATABASE_URL from .env.
    monkeypatch.setattr(settings, "database_url", None)
    unbind_session_store()
    reset_session_registry()
    yield
    unbind_session_store()
    reset_session_registry()


def _client() -> TestClient:
    return TestClient(app)


def _mint(
    *,
    audience: str = "ondcbuyer",
    principal_id: str = "principal:auth0:google-oauth2:a5-user",
    identity_provider: str = "auth0",
    amr: list[str] | None = None,
) -> str:
    return create_principal_session_token(
        principal_id=principal_id,
        audience=audience,
        identity_provider=identity_provider,
        amr=amr,
    )


def test_session_secret_previous_accepts_rotated_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "session_secret", "a5-secret-v1")
    monkeypatch.setattr(settings, "session_secret_previous", None)
    token = _mint()
    assert parse_session_token(token) is not None

    monkeypatch.setattr(settings, "session_secret", "a5-secret-v2")
    monkeypatch.setattr(settings, "session_secret_previous", "a5-secret-v1")
    rotated = parse_session_token(token)
    assert rotated is not None
    assert rotated["principal_id"].endswith("a5-user")

    monkeypatch.setattr(settings, "session_secret_previous", None)
    assert parse_session_token(token) is None


def test_logout_denies_stolen_cookie_copy() -> None:
    client = _client()
    login = client.post("/api/auth/demo-continue", json={"audience": "ondcbuyer"})
    stolen = login.cookies.get(SESSION_COOKIE_NAME)
    assert stolen

    logout = client.post("/api/auth/logout", cookies=login.cookies)
    assert logout.status_code == 200

    me = client.get("/api/auth/me", cookies={SESSION_COOKIE_NAME: stolen})
    assert me.status_code == 200
    assert me.json()["data"] is None


def test_sessions_list_and_revoke_sid() -> None:
    client = _client()
    first = client.post("/api/auth/demo-continue", json={"audience": "ondcbuyer"})
    listed = client.get("/api/auth/sessions", cookies=first.cookies)
    assert listed.status_code == 200
    body = listed.json()["data"]
    assert body["current_sid"]
    assert len(body["sessions"]) == 1
    sid = body["current_sid"]

    revoked = client.post(
        "/api/auth/sessions/revoke",
        json={"sid": sid},
        cookies=first.cookies,
    )
    assert revoked.status_code == 200
    me = client.get("/api/auth/me", cookies=first.cookies)
    assert me.json()["data"] is None


def test_revoke_all_invalidates_sibling_sessions() -> None:
    token_a = _mint(principal_id="principal:auth0:google-oauth2:twins")
    token_b = _mint(principal_id="principal:auth0:google-oauth2:twins")
    client = _client()
    gone = client.post(
        "/api/auth/sessions/revoke-all",
        cookies={SESSION_COOKIE_NAME: token_a},
    )
    assert gone.status_code == 200
    assert parse_session_token(token_a) is None
    assert parse_session_token(token_b) is None

    fresh = _mint(principal_id="principal:auth0:google-oauth2:twins")
    assert parse_session_token(fresh) is not None


def test_buyer_session_cannot_evaluate_seller_refund() -> None:
    client = _client()
    login = client.post("/api/auth/demo-continue", json={"audience": "ondcbuyer"})
    res = client.post(
        "/api/agentguard/actions/evaluate",
        json={"action": "refund", "amount_inr": 100, "resource_id": "ord-a5-iso"},
        cookies=login.cookies,
    )
    assert res.status_code == 403
    assert res.json()["detail"] == "Session audience mismatch."


def test_seller_session_cannot_ensure_buyer_agent() -> None:
    client = _client()
    login = client.post("/api/auth/demo-continue", json={"audience": "ondcseller"})
    res = client.post(
        "/api/agentguard/agents/ensure",
        json={"role": "buyer"},
        cookies=login.cookies,
    )
    assert res.status_code == 403
    assert res.json()["detail"] == "Session audience mismatch."


def test_step_up_required_for_seller_refund_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.session_auth.get_runtime_mode", lambda: "production")
    token = create_session_token(
        wallet_address=WALLET,
        did="did:aadharchain:a5",
        audience="ondcseller",
    )
    client = _client()
    res = client.post(
        "/api/agentguard/actions/evaluate",
        json={"action": "refund", "amount_inr": 100, "resource_id": "ord-a5-step"},
        cookies={SESSION_COOKIE_NAME: token},
    )
    assert res.status_code == 403
    assert res.json()["detail"]["code"] == "step_up_required"

    mfa = create_principal_session_token(
        principal_id=f"wallet:{WALLET}",
        audience="ondcseller",
        identity_provider="wallet",
        wallet_address=WALLET,
        did="did:aadharchain:a5",
        amr=["pwd", "mfa"],
    )
    allowed = client.post(
        "/api/agentguard/actions/evaluate",
        json={"action": "refund", "amount_inr": 100, "resource_id": "ord-a5-mfa"},
        cookies={SESSION_COOKIE_NAME: mfa},
    )
    assert allowed.status_code == 200


def test_demo_mode_does_not_require_step_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.session_auth.get_runtime_mode", lambda: "demo")
    token = create_session_token(
        wallet_address=WALLET,
        did="did:aadharchain:a5-demo",
        audience="ondcseller",
    )
    res = _client().post(
        "/api/agentguard/actions/evaluate",
        json={"action": "refund", "amount_inr": 100, "resource_id": "ord-a5-demo"},
        cookies={SESSION_COOKIE_NAME: token},
    )
    assert res.status_code == 200


def test_auth0_start_step_up_adds_acr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth0_domain", "dev-example.us.auth0.com")
    monkeypatch.setattr(settings, "auth0_client_id", "client-id")
    monkeypatch.setattr(settings, "auth0_client_secret", "client-secret")
    res = _client().get(
        "/api/auth/auth0/start",
        params={
            "return": "http://127.0.0.1:43103/dashboard",
            "aud": "ondcseller",
            "step_up": "true",
        },
        follow_redirects=False,
    )
    assert res.status_code == 302
    location = urlparse(res.headers["location"])
    query = parse_qs(location.query)
    assert location.hostname == "dev-example.us.auth0.com"
    assert query["prompt"] == ["login"]
    assert query["acr_values"] == [
        "http://schemas.openid.net/pape/policies/2007/06/multi-factor"
    ]
    plain = _client().get(
        "/api/auth/auth0/start",
        params={"return": "http://127.0.0.1:43103/dashboard", "aud": "ondcseller"},
        follow_redirects=False,
    )
    plain_query = parse_qs(urlparse(plain.headers["location"]).query)
    assert "prompt" not in plain_query
    assert "acr_values" not in plain_query


def test_fail_closed_when_durable_registry_unready() -> None:
    bind_session_store(None, require_durable=True)
    token = _mint()
    assert parse_session_token(token) is None
