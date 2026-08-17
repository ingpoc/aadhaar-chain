"""Retail BAP track dispatch, on_track verify, on_confirm correlation, on_status subscriber."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app.ondc_crypto import create_authorization_header
from config import settings


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
    (tmp_path / "unique_key_id.txt").write_text("seller-uk\n", encoding="utf-8")
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
    monkeypatch.setattr(settings, "ondc_seller_keys_dir", str(ed25519_pem))
    monkeypatch.setattr(settings, "ondc_bpp_id", "ondcseller.aadharcha.in")
    monkeypatch.setattr(
        settings, "ondc_bpp_uri", "https://ondcseller.aadharcha.in/ondc"
    )
    monkeypatch.setattr(settings, "ondc_seller_unique_key_id", "seller-uk")
    monkeypatch.setattr(
        settings,
        "ondc_seller_signing_private_key_path",
        str(ed25519_pem / "signing_private.pem"),
    )


def _seller_key(ed25519_pem: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(
        (ed25519_pem / "signing_private.pem").read_bytes(),
        password=None,
    )
    assert isinstance(key, Ed25519PrivateKey)
    return key


def _retail_envelope(
    *,
    action: str,
    transaction_id: str,
    message_id: str,
    extra_context: dict | None = None,
    message: dict | None = None,
) -> dict:
    context = {
        "domain": "ONDC:RET10",
        "action": action,
        "core_version": "1.2.0",
        "bap_id": "ondcbuyer.aadharcha.in",
        "bap_uri": "https://ondcbuyer.aadharcha.in/ondc",
        "bpp_id": "ondcseller.aadharcha.in",
        "bpp_uri": "https://ondcseller.aadharcha.in/ondc",
        "transaction_id": transaction_id,
        "message_id": message_id,
        "timestamp": "2026-08-17T11:00:00.000Z",
        "ttl": "PT30S",
    }
    if extra_context:
        context.update(extra_context)
    return {
        "context": context,
        "message": message
        or {
            "tracking": {
                "url": "https://ondcseller.aadharcha.in/track/B5d77ff31",
                "status": "active",
            }
        },
    }


def test_buyer_track_dispatch_signs_and_posts_order_id(
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
    with patch("app.ondc_routes.httpx.AsyncClient", return_value=mock_client):
        client = TestClient(app)
        missing = client.post("/api/ondc/track", json={})
        assert missing.status_code == 422
        response = client.post(
            "/api/ondc/track",
            json={
                "order_id": "B5d77ff31",
                "bpp_id": "ondcseller.aadharcha.in",
                "bpp_uri": "https://ondcseller.aadharcha.in/ondc",
                "transaction_id": "txn-track-bap",
                "message_id": "msg-track-bap",
            },
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["dispatched"] is True
    assert data["ack"] == "ACK"
    assert data["target"] == "https://ondcseller.aadharcha.in/ondc/track"
    mock_client.post.assert_awaited()
    call = mock_client.post.await_args
    assert call.args[0].endswith("/track")
    assert "Authorization" in call.kwargs["headers"]
    sent = json.loads(call.kwargs["content"].decode("utf-8"))
    assert sent["context"]["action"] == "track"
    assert sent["context"]["domain"] == "ONDC:RET10"
    assert sent["context"]["message_id"] == "msg-track-bap"
    assert sent["message"] == {"order_id": "B5d77ff31"}


def test_on_track_verifies_signature_like_issue(
    tmp_path: Path, ed25519_pem: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_retail(tmp_path, ed25519_pem, monkeypatch)
    from main import app

    app.state.persistence_pool = None
    client = TestClient(app)
    envelope = _retail_envelope(
        action="on_track",
        transaction_id="txn-on-track",
        message_id="msg-on-track",
    )
    authorization = create_authorization_header(
        envelope,
        subscriber_id="ondcseller.aadharcha.in",
        unique_key_id="seller-uk",
        private_key=_seller_key(ed25519_pem),
    )
    good = client.post(
        "/ondc/on_track",
        json=envelope,
        headers={"Authorization": authorization},
    )
    assert good.status_code == 200
    assert good.json()["message"]["ack"]["status"] == "ACK"

    unsigned = client.post("/ondc/on_track", json=envelope)
    assert unsigned.status_code == 401
    assert unsigned.json()["message"]["ack"]["status"] == "NACK"
    assert "missing ONDC Authorization" in unsigned.json()["error"]["message"]

    tampered = client.post(
        "/ondc/on_track",
        json={
            **envelope,
            "context": {**envelope["context"], "message_id": "tampered"},
        },
        headers={"Authorization": authorization},
    )
    assert tampered.status_code == 401
    assert tampered.json()["message"]["ack"]["status"] == "NACK"


def test_on_confirm_requires_confirm_message_id(
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
    with patch("app.ondc_routes.httpx.AsyncClient", return_value=mock_client):
        client = TestClient(app)
        confirm = client.post(
            "/api/ondc/confirm",
            json={
                "transaction_id": "txn-confirm-corr",
                "message_id": "72b2829c-25b8-49ff-8edf-6cc9bfd334b0",
                "bpp_id": "ondcseller.aadharcha.in",
                "bpp_uri": "https://ondcseller.aadharcha.in/ondc",
                "order": {"id": "B5d77ff31"},
            },
        )
        assert confirm.status_code == 200, confirm.text

        matched = _retail_envelope(
            action="on_confirm",
            transaction_id="txn-confirm-corr",
            message_id="72b2829c-25b8-49ff-8edf-6cc9bfd334b0",
            message={"order": {"id": "B5d77ff31", "state": "Accepted"}},
        )
        ack = client.post("/ondc/on_confirm", json=matched)
        assert ack.status_code == 200
        assert ack.json()["message"]["ack"]["status"] == "ACK"

        mismatched = _retail_envelope(
            action="on_confirm",
            transaction_id="txn-confirm-corr",
            message_id="b4905f64-3064-49f9-8465-95f024d47a76",
            message={"order": {"id": "B5d77ff31", "state": "Accepted"}},
        )
        nack = client.post("/ondc/on_confirm", json=mismatched)
        assert nack.status_code == 409
        assert nack.json()["message"]["ack"]["status"] == "NACK"
        assert "72b2829c-25b8-49ff-8edf-6cc9bfd334b0" in nack.json()["error"]["message"]
        assert "b4905f64-3064-49f9-8465-95f024d47a76" in nack.json()["error"]["message"]


def test_on_status_accepts_camelcase_and_authorization_subscriber(
    tmp_path: Path, ed25519_pem: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_retail(tmp_path, ed25519_pem, monkeypatch)
    from main import app

    app.state.persistence_pool = None
    client = TestClient(app)
    envelope = _retail_envelope(
        action="on_status",
        transaction_id="txn-on-status",
        message_id="msg-on-status",
        extra_context={
            "bpp_id": "",
            "bap_id": "",
            "subscriberID": "ondcseller.aadharcha.in",
        },
        message={"order": {"id": "B5d77ff31", "state": "Accepted"}},
    )
    camel = client.post("/ondc/on_status", json=envelope)
    assert camel.status_code == 200, camel.text
    assert camel.json()["message"]["ack"]["status"] == "ACK"

    header_only = _retail_envelope(
        action="on_status",
        transaction_id="txn-on-status-auth",
        message_id="msg-on-status-auth",
        extra_context={"bpp_id": "", "bap_id": ""},
        message={"order": {"id": "B5d77ff31", "state": "Packed"}},
    )
    authorization = create_authorization_header(
        header_only,
        subscriber_id="ondcseller.aadharcha.in",
        unique_key_id="seller-uk",
        private_key=_seller_key(ed25519_pem),
    )
    via_header = client.post(
        "/ondc/on_status",
        json=header_only,
        headers={"Authorization": authorization},
    )
    assert via_header.status_code == 200, via_header.text
    assert via_header.json()["message"]["ack"]["status"] == "ACK"

    missing = _retail_envelope(
        action="on_status",
        transaction_id="txn-on-status-missing",
        message_id="msg-on-status-missing",
        extra_context={"bpp_id": "", "bap_id": ""},
        message={"order": {"id": "B5d77ff31", "state": "Picked"}},
    )
    nack = client.post("/ondc/on_status", json=missing)
    assert nack.status_code == 400
    assert nack.json()["message"]["ack"]["status"] == "NACK"
    assert "subscriber identifier" in nack.json()["error"]["message"]
