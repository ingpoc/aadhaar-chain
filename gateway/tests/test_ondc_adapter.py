"""Tests for ONDC crypto + BAP adapter (PreProd wiring)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app.ondc_crypto import create_authorization_header, minify_json
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
    (tmp_path / "unique_key_id.txt").write_text("test-uk-id\n", encoding="utf-8")
    (tmp_path / "encryption_private.pem").write_bytes(pem)  # unused placeholder
    return tmp_path


def test_minify_and_auth_header_stable(ed25519_pem: Path):
    key = serialization.load_pem_private_key(
        (ed25519_pem / "signing_private.pem").read_bytes(), password=None
    )
    body = {"a": 1, "b": [2, 3]}
    header = create_authorization_header(
        body,
        subscriber_id="ondcbuyer.aadharcha.in",
        unique_key_id="test-uk-id",
        private_key=key,
        created=1700000000,
        expires=1700003600,
    )
    assert header.startswith("Signature keyId=")
    assert "ondcbuyer.aadharcha.in|test-uk-id|ed25519" in header
    assert 'algorithm="ed25519"' in header
    assert minify_json(body) == '{"a":1,"b":[2,3]}'


def test_ondc_search_dispatches_when_configured(tmp_path: Path, ed25519_pem: Path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "ondc_enabled", True)
    monkeypatch.setattr(settings, "ondc_subscriber_id", "ondcbuyer.aadharcha.in")
    monkeypatch.setattr(settings, "ondc_bap_id", "ondcbuyer.aadharcha.in")
    monkeypatch.setattr(settings, "ondc_bap_uri", "https://ondcbuyer.aadharcha.in/ondc")
    monkeypatch.setattr(settings, "ondc_unique_key_id", "test-uk-id")
    monkeypatch.setattr(
        settings, "ondc_signing_private_key_path", str(ed25519_pem / "signing_private.pem")
    )
    monkeypatch.setattr(settings, "ondc_gateway_url", "https://preprod.gateway.ondc.org/search")
    monkeypatch.setattr(settings, "ondc_buyer_keys_dir", str(ed25519_pem))

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.json = lambda: {"message": {"ack": {"status": "ACK"}}}
    mock_resp.text = '{"message":{"ack":{"status":"ACK"}}}'

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=mock_resp)

    from main import app

    with patch("app.ondc_routes.httpx.AsyncClient", return_value=mock_client):
        client = TestClient(app)
        res = client.post("/api/ondc/search", json={"query": "banana", "city": "std:080"})
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["dispatched"] is True
    assert data["ack"] == "ACK"
    assert data["transaction_id"]
    mock_client.post.assert_awaited()
    call_kwargs = mock_client.post.await_args
    assert "Authorization" in call_kwargs.kwargs["headers"]
    sent = json.loads(call_kwargs.kwargs["content"].decode("utf-8"))
    assert sent["context"]["action"] == "search"
    assert sent["context"]["domain"] == "ONDC:RET10"
    assert sent["message"]["intent"]["item"]["descriptor"]["name"] == "banana"
    assert sent["message"]["intent"]["payment"] == {
        "@ondc/org/buyer_app_finder_fee_type": "percent",
        "@ondc/org/buyer_app_finder_fee_amount": "0",
    }


def test_on_search_inbox_and_catalogs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    from main import app

    client = TestClient(app)
    txn = "txn-test-1"
    payload = {
        "context": {
            "action": "on_search",
            "transaction_id": txn,
            "message_id": "msg-1",
            "bpp_id": "seller.example",
            "bpp_uri": "https://seller.example/ondc",
        },
        "message": {
            "catalog": {
                "providers": [
                    {
                        "id": "p1",
                        "descriptor": {"name": "Demo Store"},
                        "items": [
                            {
                                "id": "sku-1",
                                "descriptor": {"name": "Robusta Bananas"},
                                "price": {"currency": "INR", "value": "40"},
                            }
                        ],
                    }
                ]
            }
        },
    }
    ack = client.post("/ondc/on_search", json=payload)
    assert ack.status_code == 200
    assert ack.json()["message"]["ack"]["status"] == "ACK"
    catalogs = client.get(f"/api/ondc/catalogs?transaction_id={txn}")
    assert catalogs.status_code == 200
    items = catalogs.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["name"] == "Robusta Bananas"
    assert items[0]["bpp_id"] == "seller.example"


def test_ondc_status_disabled_by_default(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "ondc_enabled", False)
    from main import app

    client = TestClient(app)
    res = client.get("/api/ondc/status")
    assert res.status_code == 200
    assert res.json()["data"]["enabled"] is False
    assert res.json()["data"]["configured"] is False


def test_ondc_select_init_confirm_dispatch(tmp_path: Path, ed25519_pem: Path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "ondc_enabled", True)
    monkeypatch.setattr(settings, "ondc_subscriber_id", "ondcbuyer.aadharcha.in")
    monkeypatch.setattr(settings, "ondc_bap_id", "ondcbuyer.aadharcha.in")
    monkeypatch.setattr(settings, "ondc_bap_uri", "https://ondcbuyer.aadharcha.in/ondc")
    monkeypatch.setattr(settings, "ondc_unique_key_id", "test-uk-id")
    monkeypatch.setattr(
        settings, "ondc_signing_private_key_path", str(ed25519_pem / "signing_private.pem")
    )
    monkeypatch.setattr(settings, "ondc_buyer_keys_dir", str(ed25519_pem))

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.json = lambda: {"message": {"ack": {"status": "ACK"}}}
    mock_resp.text = '{"message":{"ack":{"status":"ACK"}}}'
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=mock_resp)

    from main import app

    body = {
        "transaction_id": "txn-order-bap",
        "bpp_id": "ondcseller.aadharcha.in",
        "bpp_uri": "https://ondcseller.aadharcha.in/ondc",
        "order": {"items": [{"id": "item_atta", "quantity": {"count": "1"}}]},
    }
    with patch("app.ondc_routes.httpx.AsyncClient", return_value=mock_client):
        client = TestClient(app)
        for action in ("select", "init", "confirm"):
            res = client.post(f"/api/ondc/{action}", json=body)
            assert res.status_code == 200, res.text
            data = res.json()["data"]
            assert data["dispatched"] is True
            assert data["ack"] == "ACK"
            assert data["bpp_uri"] == "https://ondcseller.aadharcha.in/ondc"

    targets = [c.args[0] for c in mock_client.post.await_args_list]
    assert any(t.endswith("/select") for t in targets)
    assert any(t.endswith("/init") for t in targets)
    assert any(t.endswith("/confirm") for t in targets)
    sent = json.loads(mock_client.post.await_args_list[0].kwargs["content"].decode("utf-8"))
    assert sent["context"]["bpp_id"] == "ondcseller.aadharcha.in"
    assert "Authorization" in mock_client.post.await_args_list[0].kwargs["headers"]
