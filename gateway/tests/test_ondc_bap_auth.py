"""Unauthenticated BAP POSTs must not enqueue signed outbox rows."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app.session_auth import SESSION_COOKIE_NAME, create_principal_session_token
from config import settings

_BAP_OUTBOX_POSTS = (
    "/api/ondc/select",
    "/api/ondc/init",
    "/api/ondc/confirm",
    "/api/ondc/cancel",
    "/api/ondc/update",
    "/api/ondc/track",
    "/api/ondc/order-status",
    "/api/ondc/search",
    "/api/ondc/issue",
    "/api/ondc/issue_status",
    "/api/ondc/logistics/search",
    "/api/ondc/logistics/init",
    "/api/ondc/logistics/confirm",
    "/api/ondc/logistics/update",
    "/api/ondc/logistics/status",
    "/api/ondc/logistics/track",
)

_FAKE_BODY = {
    "transaction_id": "txn-unauth",
    "message_id": "msg-unauth",
    "order_id": "ord-unauth",
    "bpp_id": "ondcseller.aadharcha.in",
    "bpp_uri": "https://ondcseller.aadharcha.in/ondc",
    "order": {"id": "ord-unauth"},
}


@pytest.fixture()
def ed25519_pem(tmp_path: Path) -> Path:
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "signing_private.pem"
    path.write_bytes(pem)
    (tmp_path / "unique_key_id.txt").write_text("buyer-uk\n", encoding="utf-8")
    return tmp_path


def _enable_retail(
    tmp_path: Path,
    ed25519_pem: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "ondc_enabled", True)
    monkeypatch.setattr(settings, "ondc_subscriber_id", "ondcbuyer.aadharcha.in")
    monkeypatch.setattr(settings, "ondc_bap_id", "ondcbuyer.aadharcha.in")
    monkeypatch.setattr(settings, "ondc_bap_uri", "https://ondcbuyer.aadharcha.in/ondc")
    monkeypatch.setattr(settings, "ondc_unique_key_id", "buyer-uk")
    monkeypatch.setattr(
        settings,
        "ondc_signing_private_key_path",
        str(ed25519_pem / "signing_private.pem"),
    )
    monkeypatch.setattr(settings, "ondc_buyer_keys_dir", str(ed25519_pem))


def test_unauthenticated_bap_posts_are_401(
    tmp_path: Path, ed25519_pem: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_retail(tmp_path, ed25519_pem, monkeypatch)
    from main import app

    app.state.persistence_pool = None
    client = TestClient(app)
    for path in _BAP_OUTBOX_POSTS:
        response = client.post(path, json=_FAKE_BODY)
        assert response.status_code == 401, (path, response.status_code, response.text)
        assert response.json()["detail"] == "Authenticated principal required."


def test_get_track_and_local_order_track_stay_readable_without_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    from main import app

    app.state.persistence_pool = None
    client = TestClient(app)
    missing_get = client.get("/api/ondc/track")
    assert missing_get.status_code == 422
    missing_post = client.post("/api/ondc/order-track", json={})
    assert missing_post.status_code == 422
    unknown = client.get("/api/ondc/track?order_id=missing-order")
    assert unknown.status_code == 404


def test_authenticated_buyer_can_dispatch_track_and_order_status(
    tmp_path: Path, ed25519_pem: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_retail(tmp_path, ed25519_pem, monkeypatch)
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.json = lambda: {"message": {"ack": {"status": "ACK"}}}
    mock_resp.text = '{"message":{"ack":{"status":"ACK"}}}'
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=mock_resp)

    from main import app

    app.state.persistence_pool = None
    body = {
        "order_id": "B5d77ff31",
        "bpp_id": "ondcseller.aadharcha.in",
        "bpp_uri": "https://ondcseller.aadharcha.in/ondc",
        "transaction_id": "txn-auth-track",
        "message_id": "msg-auth-track",
    }
    with patch("app.ondc_routes.httpx.AsyncClient", return_value=mock_client):
        client = TestClient(app)
        client.cookies.set(
            SESSION_COOKIE_NAME,
            create_principal_session_token(
                principal_id="principal:auth0:ondc-buyer",
                audience="ondcbuyer",
                identity_provider="auth0",
            ),
        )
        for path, message_id in (
            ("/api/ondc/track", "msg-auth-track"),
            ("/api/ondc/order-status", "msg-auth-status"),
        ):
            response = client.post(path, json={**body, "message_id": message_id})
            assert response.status_code == 200, response.text
            assert response.json()["data"]["dispatched"] is True
            assert response.json()["data"]["ack"] == "ACK"


def test_seller_social_session_can_dispatch_track(
    tmp_path: Path, ed25519_pem: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_retail(tmp_path, ed25519_pem, monkeypatch)
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.json = lambda: {"message": {"ack": {"status": "ACK"}}}
    mock_resp.text = '{"message":{"ack":{"status":"ACK"}}}'
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=mock_resp)

    from main import app

    app.state.persistence_pool = None
    token = create_principal_session_token(
        principal_id="principal:auth0:shared-shopper",
        audience="ondcseller",
        identity_provider="auth0",
    )
    with patch("app.ondc_routes.httpx.AsyncClient", return_value=mock_client):
        client = TestClient(app)
        client.cookies.set(SESSION_COOKIE_NAME, token)
        response = client.post(
            "/api/ondc/track",
            json={
                "order_id": "B5d77ff31",
                "bpp_id": "ondcseller.aadharcha.in",
                "bpp_uri": "https://ondcseller.aadharcha.in/ondc",
                "transaction_id": "txn-seller-handoff",
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["dispatched"] is True
