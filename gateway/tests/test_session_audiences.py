"""Buyer and Seller sessions coexist without cross-role privilege.

Preprod defect (2026-10-06): signing into Seller (auth0 aud=ondcseller)
overwrote the single gateway session cookie, so Buyer AgentGuard calls
(`POST /api/agentguard/mandates/compile`, `GET /api/agentguard/agents/current
?role=buyer`) returned 403 until the Buyer signed in again.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.session_auth import (
    BUYER_SESSION_COOKIE_NAME,
    SELLER_SESSION_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    create_principal_session_token,
)
from main import app

BUYER_ORIGIN = {"Origin": "https://ondcbuyer.aadharcha.in"}
SELLER_ORIGIN = {"Origin": "https://ondcseller.aadharcha.in"}
SHARED_PRINCIPAL = "principal:auth0:google-oauth2:coexist-1"


@pytest.fixture()
def client() -> TestClient:
    app.state.persistence_pool = None
    return TestClient(app)


def _token(audience: str, principal_id: str = SHARED_PRINCIPAL) -> str:
    return create_principal_session_token(
        principal_id=principal_id, audience=audience, identity_provider="auth0"
    )


def _login_both_demo(client: TestClient) -> None:
    buyer = client.post("/api/auth/demo-continue", json={"audience": "ondcbuyer"})
    assert buyer.status_code == 200, buyer.text
    seller = client.post("/api/auth/demo-continue", json={"audience": "ondcseller"})
    assert seller.status_code == 200, seller.text


def test_login_sets_audience_scoped_cookie_without_clobbering_other_role(
    client: TestClient,
) -> None:
    buyer = client.post("/api/auth/demo-continue", json={"audience": "ondcbuyer"})
    assert BUYER_SESSION_COOKIE_NAME in buyer.cookies
    assert SESSION_COOKIE_NAME in buyer.cookies
    seller = client.post("/api/auth/demo-continue", json={"audience": "ondcseller"})
    assert SELLER_SESSION_COOKIE_NAME in seller.cookies
    assert BUYER_SESSION_COOKIE_NAME not in seller.cookies  # not overwritten/cleared
    assert client.cookies.get(BUYER_SESSION_COOKIE_NAME)
    assert client.cookies.get(SELLER_SESSION_COOKIE_NAME)


def test_seller_login_then_buyer_agentguard_calls_succeed(client: TestClient) -> None:
    _login_both_demo(client)

    current = client.get(
        "/api/agentguard/agents/current", params={"role": "buyer"}, headers=BUYER_ORIGIN
    )
    assert current.status_code == 200, current.text
    assert current.json()["data"]["agent"]["principal_id"] == "principal:demo:buyer"

    compiled = client.post(
        "/api/agentguard/mandates/compile",
        json={"role": "buyer", "limits": {"checkout_auto_max_inr": 500}},
        headers=BUYER_ORIGIN,
    )
    assert compiled.status_code == 200, compiled.text

    # Role-scoped calls work even without an Origin hint.
    assert (
        client.get("/api/agentguard/agents/current", params={"role": "buyer"}).status_code
        == 200
    )
    seller_current = client.get(
        "/api/agentguard/agents/current", params={"role": "seller"}, headers=SELLER_ORIGIN
    )
    assert seller_current.status_code == 200, seller_current.text
    assert (
        seller_current.json()["data"]["agent"]["principal_id"] == "principal:demo:seller"
    )


def test_auth_me_follows_calling_app_origin(client: TestClient) -> None:
    _login_both_demo(client)
    buyer_me = client.get("/api/auth/me", headers=BUYER_ORIGIN).json()["data"]
    seller_me = client.get("/api/auth/me", headers=SELLER_ORIGIN).json()["data"]
    assert buyer_me["audience"] == "ondcbuyer"
    assert buyer_me["principal_id"] == "principal:demo:buyer"
    assert seller_me["audience"] == "ondcseller"
    assert seller_me["principal_id"] == "principal:demo:seller"
    validate = client.get("/api/auth/validate", headers=BUYER_ORIGIN).json()["data"]
    assert validate["valid"] is True
    assert validate["user"]["audience"] == "ondcbuyer"


def test_buyer_session_cannot_act_as_seller(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, _token("ondcbuyer"))
    client.cookies.set(BUYER_SESSION_COOKIE_NAME, _token("ondcbuyer"))
    res = client.get(
        "/api/agentguard/agents/current", params={"role": "seller"}, headers=SELLER_ORIGIN
    )
    assert res.status_code == 403
    compiled = client.post(
        "/api/agentguard/mandates/compile", json={"role": "seller"}, headers=SELLER_ORIGIN
    )
    assert compiled.status_code == 403


def test_seller_session_cannot_act_as_buyer(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, _token("ondcseller"))
    client.cookies.set(SELLER_SESSION_COOKIE_NAME, _token("ondcseller"))
    res = client.get(
        "/api/agentguard/agents/current", params={"role": "buyer"}, headers=BUYER_ORIGIN
    )
    assert res.status_code == 403
    compiled = client.post(
        "/api/agentguard/mandates/compile", json={"role": "buyer"}, headers=BUYER_ORIGIN
    )
    assert compiled.status_code == 403


def test_seller_token_in_buyer_cookie_slot_is_rejected(client: TestClient) -> None:
    client.cookies.set(BUYER_SESSION_COOKIE_NAME, _token("ondcseller"))
    res = client.get(
        "/api/agentguard/agents/current", params={"role": "buyer"}, headers=BUYER_ORIGIN
    )
    assert res.status_code in (401, 403)
    me = client.get("/api/auth/me", headers=BUYER_ORIGIN).json()
    assert me["data"] is None


def test_logout_revokes_and_clears_every_session_cookie(client: TestClient) -> None:
    _login_both_demo(client)
    buyer_token = client.cookies.get(BUYER_SESSION_COOKIE_NAME)
    res = client.post("/api/auth/logout", headers=BUYER_ORIGIN)
    assert res.status_code == 200
    set_cookie = " ".join(res.headers.get_list("set-cookie"))
    for name in (SESSION_COOKIE_NAME, BUYER_SESSION_COOKIE_NAME, SELLER_SESSION_COOKIE_NAME):
        assert f"{name}=" in set_cookie
    assert client.get("/api/auth/me", headers=BUYER_ORIGIN).json()["data"] is None
    assert client.get("/api/auth/me", headers=SELLER_ORIGIN).json()["data"] is None
    # The revoked buyer token is dead even if replayed.
    replay = TestClient(app)
    replay.cookies.set(BUYER_SESSION_COOKIE_NAME, buyer_token)
    assert replay.get("/api/auth/me", headers=BUYER_ORIGIN).json()["data"] is None
