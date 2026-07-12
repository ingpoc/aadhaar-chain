"""Local commerce exchange tests."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config import settings
from main import app


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    yield


def test_publish_search_order_and_idempotency() -> None:
    client = TestClient(app)

    created = client.post(
        "/api/demo-commerce/seller/items",
        json={
            "idempotency_key": "item-create-1",
            "title": "Token Nxt demo product",
            "price_inr": 1200,
            "inventory": 4,
            "seller_id": "seller-demo",
        },
    )
    assert created.status_code == 200
    item_id = created.json()["data"]["item"]["item_id"]

    published = client.post(
        f"/api/demo-commerce/seller/items/{item_id}/publish",
        json={"idempotency_key": "item-publish-1"},
    )
    assert published.status_code == 200
    assert published.json()["data"]["item"]["status"] == "published"

    search = client.get("/api/demo-commerce/buyer/search", params={"q": "Token Nxt"})
    assert search.status_code == 200
    assert search.json()["data"]["count"] == 1

    order_payload = {
        "idempotency_key": "order-create-1",
        "item_id": item_id,
        "quantity": 2,
        "buyer_id": "buyer-demo",
    }
    order = client.post("/api/demo-commerce/buyer/orders", json=order_payload)
    duplicate = client.post("/api/demo-commerce/buyer/orders", json=order_payload)
    assert order.status_code == 200
    assert duplicate.status_code == 200
    assert duplicate.json()["data"]["order"]["order_id"] == order.json()["data"]["order"]["order_id"]

    seller_orders = client.get("/api/demo-commerce/seller/orders", params={"seller_id": "seller-demo"})
    assert seller_orders.status_code == 200
    assert seller_orders.json()["data"]["count"] == 1
    assert seller_orders.json()["data"]["orders"][0]["buyer_id"] == "buyer-demo"
