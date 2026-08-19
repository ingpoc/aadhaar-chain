"""Signed Retail BAP lifecycle dispatch and callback verification (B4)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app.ondc_crypto import create_authorization_header
from app.ondc_routes import _normalize_retail_callback
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


def _sign(envelope: dict, ed25519_pem: Path) -> str:
    return create_authorization_header(
        envelope,
        subscriber_id="ondcseller.aadharcha.in",
        unique_key_id="seller-uk",
        private_key=_seller_key(ed25519_pem),
    )


def _retail_envelope(
    *,
    action: str,
    transaction_id: str,
    message_id: str,
    message: dict | None = None,
) -> dict:
    return {
        "context": {
            "domain": "ONDC:RET10",
            "action": action,
            "core_version": "1.2.0",
            "bap_id": "ondcbuyer.aadharcha.in",
            "bap_uri": "https://ondcbuyer.aadharcha.in/ondc",
            "bpp_id": "ondcseller.aadharcha.in",
            "bpp_uri": "https://ondcseller.aadharcha.in/ondc",
            "transaction_id": transaction_id,
            "message_id": message_id,
            "timestamp": "2026-08-19T11:00:00.000Z",
            "ttl": "PT30S",
        },
        "message": message
        or {
            "order": {
                "id": "ord_retail_1",
                "state": "Accepted",
                "quote": {"price": {"currency": "INR", "value": "250.00"}},
            }
        },
    }


def test_buyer_status_cancel_update_dispatch_signs(
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
    bodies = {
        "order-status": {
            "order_id": "ord_retail_1",
            "transaction_id": "txn-retail-dispatch",
            "message_id": "msg-status",
            "bpp_id": "ondcseller.aadharcha.in",
            "bpp_uri": "https://ondcseller.aadharcha.in/ondc",
        },
        "cancel": {
            "order_id": "ord_retail_1",
            "transaction_id": "txn-retail-dispatch",
            "message_id": "msg-cancel",
            "bpp_id": "ondcseller.aadharcha.in",
            "bpp_uri": "https://ondcseller.aadharcha.in/ondc",
            "order": {"id": "ord_retail_1", "cancellation_reason_id": "001"},
        },
        "update": {
            "transaction_id": "txn-retail-dispatch",
            "message_id": "msg-update",
            "bpp_id": "ondcseller.aadharcha.in",
            "bpp_uri": "https://ondcseller.aadharcha.in/ondc",
            "order": {
                "id": "ord_retail_1",
                "fulfillments": [{"id": "R1", "type": "Return"}],
            },
        },
    }
    with patch("app.ondc_routes.httpx.AsyncClient", return_value=mock_client):
        client = TestClient(app)
        for path, body in bodies.items():
            response = client.post(f"/api/ondc/{path}", json=body)
            assert response.status_code == 200, response.text
            assert response.json()["data"]["dispatched"] is True
            assert response.json()["data"]["ack"] == "ACK"

    sent = [
        json.loads(call.kwargs["content"].decode("utf-8"))
        for call in mock_client.post.await_args_list
    ]
    assert [payload["context"]["action"] for payload in sent] == [
        "status",
        "cancel",
        "update",
    ]
    assert all(payload["context"]["domain"] == "ONDC:RET10" for payload in sent)
    assert all(payload["context"]["core_version"] == "1.2.0" for payload in sent)
    assert all("Authorization" in call.kwargs["headers"] for call in mock_client.post.await_args_list)
    targets = [call.args[0] for call in mock_client.post.await_args_list]
    assert any(target.endswith("/status") for target in targets)
    assert any(target.endswith("/cancel") for target in targets)
    assert any(target.endswith("/update") for target in targets)


def test_retail_lifecycle_callbacks_require_signature(
    tmp_path: Path, ed25519_pem: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_retail(tmp_path, ed25519_pem, monkeypatch)
    from main import app

    app.state.persistence_pool = None
    client = TestClient(app)
    for action, message in (
        ("on_select", {"order": {"items": [{"id": "sku-1"}]}}),
        ("on_init", {"order": {"billing": {"name": "Buyer"}}}),
        ("on_confirm", {"order": {"id": "ord_retail_1", "state": "Accepted"}}),
        ("on_status", {"order": {"id": "ord_retail_1", "state": "Packed"}}),
        ("on_cancel", {"order": {"id": "ord_retail_1", "state": "Cancelled"}}),
        (
            "on_update",
            {
                "order": {
                    "id": "ord_retail_1",
                    "fulfillments": [
                        {
                            "type": "Return",
                            "state": {"descriptor": {"code": "Return_Initiated"}},
                        }
                    ],
                }
            },
        ),
    ):
        envelope = _retail_envelope(
            action=action,
            transaction_id=f"txn-{action}",
            message_id=f"msg-{action}",
            message=message,
        )
        unsigned = client.post(f"/ondc/{action}", json=envelope)
        assert unsigned.status_code == 401, action
        assert unsigned.json()["message"]["ack"]["status"] == "NACK"
        good = client.post(
            f"/ondc/{action}",
            json=envelope,
            headers={"Authorization": _sign(envelope, ed25519_pem)},
        )
        assert good.status_code == 200, good.text
        assert good.json()["message"]["ack"]["status"] == "ACK"


def test_logistics_on_init_is_not_verified_as_retail(
    tmp_path: Path, ed25519_pem: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_retail(tmp_path, ed25519_pem, monkeypatch)
    from main import app

    app.state.persistence_pool = object()
    envelope = {
        "context": {
            "domain": "ONDC:LOG10",
            "action": "on_init",
            "core_version": "1.2.5",
            "bpp_id": "ondc.bringg.space",
            "transaction_id": "txn-log10-not-retail",
            "message_id": "msg-log10-not-retail",
        },
        "message": {"order": {"id": "LO1"}},
    }
    with patch(
        "app.ondc_routes._verify_logistics_callback",
        new=AsyncMock(return_value=(False, "LOG10 callback signature did not match the registry")),
    ):
        client = TestClient(app)
        response = client.post("/ondc/on_init", json=envelope)
    assert response.status_code == 401
    assert "LOG10" in response.json()["error"]["message"]
    app.state.persistence_pool = None


def test_normalize_retail_callback_maps_select_to_settlement() -> None:
    select = _normalize_retail_callback(
        {
            "action": "on_select",
            "message_id": "msg-select",
            "subscriber_id": "ondcseller.aadharcha.in",
            "envelope": _retail_envelope(
                action="on_select",
                transaction_id="txn-1",
                message_id="msg-select",
                message={"order": {"quote": {"price": {"currency": "INR", "value": "10.00"}}}},
            ),
        }
    )
    assert select["target_status"] is None
    assert select["quote"]["price"]["value"] == "10.00"

    confirm = _normalize_retail_callback(
        {
            "action": "on_confirm",
            "message_id": "msg-confirm",
            "subscriber_id": "ondcseller.aadharcha.in",
            "envelope": _retail_envelope(
                action="on_confirm",
                transaction_id="txn-1",
                message_id="msg-confirm",
                message={"order": {"id": "ord_1", "state": "Accepted"}},
            ),
        }
    )
    assert confirm["target_status"] == "confirmed"
    assert confirm["protocol_order_id"] == "ord_1"

    packed = _normalize_retail_callback(
        {
            "action": "on_status",
            "message_id": "msg-packed",
            "subscriber_id": "ondcseller.aadharcha.in",
            "envelope": _retail_envelope(
                action="on_status",
                transaction_id="txn-1",
                message_id="msg-packed",
                message={
                    "order": {
                        "id": "ord_1",
                        "state": "In-progress",
                        "fulfillments": [
                            {"state": {"descriptor": {"code": "Packed"}}}
                        ],
                    }
                },
            ),
        }
    )
    assert packed["target_status"] == "preparing"
    assert packed["provider_status"] == "Packed"

    returned = _normalize_retail_callback(
        {
            "action": "on_update",
            "message_id": "msg-return",
            "subscriber_id": "ondcseller.aadharcha.in",
            "envelope": _retail_envelope(
                action="on_update",
                transaction_id="txn-1",
                message_id="msg-return",
                message={
                    "order": {
                        "id": "ord_1",
                        "fulfillments": [
                            {
                                "type": "Return",
                                "state": {"descriptor": {"code": "Return_Initiated"}},
                            }
                        ],
                    }
                },
            ),
        }
    )
    assert returned["return_status"] == "requested"
    assert returned["target_status"] is None

    settled = _normalize_retail_callback(
        {
            "action": "on_update",
            "message_id": "msg-settle",
            "subscriber_id": "ondcseller.aadharcha.in",
            "envelope": _retail_envelope(
                action="on_update",
                transaction_id="txn-1",
                message_id="msg-settle",
                message={
                    "order": {
                        "id": "ord_1",
                        "quote": {"price": {"currency": "INR", "value": "250.00"}},
                        "payment": {
                            "@ondc/org/settlement_details": [
                                {
                                    "settlement_amount": {
                                        "currency": "INR",
                                        "value": "250.00",
                                    }
                                }
                            ]
                        },
                    }
                },
            ),
        }
    )
    assert settled["refund_amount_paise"] == 25_000
    assert settled["settlement"]["settlement_amount"]["value"] == "250.00"
