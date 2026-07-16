"""Tests for portfolio SSO session cookies and sso_login proof-token flow."""
from solders.keypair import Keypair
from fastapi.testclient import TestClient

from app.routes import agent_manager, identities, identity_proof_tokens
from main import app

def _client() -> TestClient:
    return TestClient(app)


def _clear_state() -> None:
    identities.clear()
    identity_proof_tokens.clear()
    agent_manager.verification_records.clear()


def test_sso_login_proof_token_works_without_identity_anchor() -> None:
    client = _client()
    _clear_state()
    keypair = Keypair()
    wallet_address = str(keypair.pubkey())

    try:
        issue = client.post(
            f"/api/identity/{wallet_address}/proof-token",
            json={"audience": "buyer", "purpose": "sso_login"},
        )
        assert issue.status_code == 200
        issued = issue.json()["data"]
        assert issued["purpose"] == "sso_login"

        signature = keypair.sign_message(issued["message"].encode("utf-8"))
        verify = client.post(
            "/api/identity/proof-token/verify",
            json={
                "token_id": issued["token_id"],
                "wallet_address": wallet_address,
                "audience": "buyer",
                "message": issued["message"],
                "signature": str(signature),
            },
        )
        assert verify.status_code == 200
        assert verify.json()["data"]["valid"] is True
        assert "aadharcha_session" in verify.cookies

        me = client.get("/api/auth/me", cookies=verify.cookies)
        assert me.status_code == 200
        body = me.json()
        assert body["data"]["wallet_address"] == wallet_address
        assert body["data"]["principal_id"] == f"wallet:{wallet_address}"
        assert body["data"]["did"] == f"did:solana:{wallet_address}"
        assert body["data"]["audience"] == "buyer"

        validate = client.get("/api/auth/validate", cookies=verify.cookies)
        assert validate.json()["data"]["valid"] is True
        assert validate.json()["data"]["user"]["wallet_address"] == wallet_address
    finally:
        _clear_state()


def test_auth_me_without_cookie_returns_null_user() -> None:
    response = _client().get("/api/auth/me")
    assert response.status_code == 200
    assert response.json()["data"] is None


def test_logout_clears_session_cookie() -> None:
    client = _client()
    _clear_state()
    keypair = Keypair()
    wallet_address = str(keypair.pubkey())

    try:
        issue = client.post(
            f"/api/identity/{wallet_address}/proof-token",
            json={"audience": "seller", "purpose": "sso_login"},
        )
        issued = issue.json()["data"]
        signature = keypair.sign_message(issued["message"].encode("utf-8"))
        verify = client.post(
            "/api/identity/proof-token/verify",
            json={
                "token_id": issued["token_id"],
                "wallet_address": wallet_address,
                "audience": "seller",
                "message": issued["message"],
                "signature": str(signature),
            },
        )
        cookies = verify.cookies

        logout = client.post("/api/auth/logout", cookies=cookies)
        assert logout.status_code == 200

        me = client.get("/api/auth/me", cookies=logout.cookies)
        assert me.json()["data"] is None
    finally:
        _clear_state()


def test_elevated_proof_token_still_requires_verified_trust() -> None:
    client = _client()
    _clear_state()
    wallet_address = str(Keypair().pubkey())

    try:
        response = client.post(
            f"/api/identity/{wallet_address}/proof-token",
            json={"audience": "buyer", "purpose": "buyer_checkout_identity_proof"},
        )
        assert response.status_code == 404
    finally:
        _clear_state()
