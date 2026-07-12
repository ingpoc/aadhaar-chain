"""Seller BPP search → on_search from published demo-commerce items."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from config import settings


@pytest.fixture()
def seller_keys(tmp_path: Path) -> Path:
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "seller"
    path.mkdir()
    (path / "signing_private.pem").write_bytes(pem)
    (path / "encryption_private.pem").write_bytes(pem)
    (path / "unique_key_id.txt").write_text("seller-uk-id\n", encoding="utf-8")
    return path


def test_bpp_search_acks_and_posts_on_search(tmp_path: Path, seller_keys: Path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "ondc_enabled", True)
    monkeypatch.setattr(settings, "ondc_seller_keys_dir", str(seller_keys))
    monkeypatch.setattr(settings, "ondc_seller_unique_key_id", "seller-uk-id")
    monkeypatch.setattr(
        settings, "ondc_seller_signing_private_key_path", str(seller_keys / "signing_private.pem")
    )
    monkeypatch.setattr(settings, "ondc_bpp_id", "ondcseller.aadharcha.in")
    monkeypatch.setattr(settings, "ondc_bpp_uri", "https://ondcseller.aadharcha.in/ondc")

    from app.commerce_demo import create_item, publish_item

    created = create_item(
        {
            "title": "AgentGuard PreProd Atta 1kg",
            "description": "test",
            "price_inr": 89,
            "inventory": 10,
            "seller_id": "ondcseller.aadharcha.in",
        }
    )
    publish_item(created["item"]["item_id"])

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.text = '{"message":{"ack":{"status":"ACK"}}}'
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=mock_resp)

    from main import app

    with patch("app.ondc_bpp.httpx.AsyncClient", return_value=mock_client):
        client = TestClient(app)
        res = client.post(
            "/ondc/np/seller/search",
            json={
                "context": {
                    "action": "search",
                    "domain": "ONDC:RET10",
                    "bap_id": "ondcbuyer.aadharcha.in",
                    "bap_uri": "https://ondcbuyer.aadharcha.in/ondc",
                    "transaction_id": "txn-bpp-1",
                    "message_id": "msg-1",
                    "city": "std:080",
                    "country": "IND",
                    "core_version": "1.2.0",
                },
                "message": {"intent": {"item": {"descriptor": {"name": "Atta"}}}},
            },
        )
    assert res.status_code == 200
    assert res.json()["message"]["ack"]["status"] == "ACK"
    # BackgroundTasks run inline in TestClient
    assert mock_client.post.await_count >= 1
    call = mock_client.post.await_args
    assert call.args[0].endswith("/on_search")
    import json

    body = json.loads(call.kwargs["content"].decode("utf-8"))
    assert body["context"]["action"] == "on_search"
    assert body["context"]["bpp_id"] == "ondcseller.aadharcha.in"
    providers = body["message"]["catalog"]["providers"]
    assert providers
    names = [i["descriptor"]["name"] for i in providers[0]["items"]]
    assert any("Atta" in n for n in names)


def test_bpp_ensure_demo_item(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    from main import app

    client = TestClient(app)
    first = client.post("/api/ondc/bpp/ensure-demo-item")
    assert first.status_code == 200
    assert first.json()["data"]["item"]["title"] == "AgentGuard PreProd Atta 1kg"
    second = client.post("/api/ondc/bpp/ensure-demo-item")
    assert second.json()["data"]["created"] is False
