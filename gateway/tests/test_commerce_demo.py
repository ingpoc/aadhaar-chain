"""Local commerce exchange tests."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config import settings
from main import app
from app.commerce_demo import load_state


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
    assert search.json()["data"]["items"][0]["inventory"] == 4

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


def test_cleanup_removes_only_deterministic_test_artifacts() -> None:
    client = TestClient(app)
    fixture = client.post(
        "/api/demo-commerce/seller/items",
        json={
            "title": "Matrix Fresh Atta 123456",
            "description": "Fresh local Samantha checkout fixture",
            "price_inr": 100,
            "inventory": 1,
        },
    ).json()["data"]["item"]
    real = client.post(
        "/api/demo-commerce/seller/items",
        json={
            "title": "Whole Wheat Atta 1kg",
            "description": "Stone-ground whole wheat flour",
            "price_inr": 120,
            "inventory": 10,
        },
    ).json()["data"]["item"]

    cleanup = client.post("/api/demo-commerce/test-fixtures/cleanup")

    assert cleanup.status_code == 200
    assert cleanup.json()["data"]["items"] == 1
    ids = set(load_state().items)
    assert fixture["item_id"] not in ids
    assert real["item_id"] in ids


def test_cleanup_exact_order_restores_real_item_inventory() -> None:
    client = TestClient(app)
    item = client.post(
        "/api/demo-commerce/seller/items",
        json={"title": "Millet Flour", "price_inr": 120, "inventory": 3},
    ).json()["data"]["item"]
    client.post(f"/api/demo-commerce/seller/items/{item['item_id']}/publish")
    order = client.post(
        "/api/demo-commerce/buyer/orders",
        json={"item_id": item["item_id"], "quantity": 2},
    ).json()["data"]["order"]

    cleanup = client.post(
        "/api/demo-commerce/test-fixtures/cleanup",
        json={"data": {"order_ids": [order["order_id"]]}},
    )

    assert cleanup.status_code == 200
    assert cleanup.json()["data"]["orders"] == 1
    assert cleanup.json()["data"]["restored_inventory"] == 2
    assert load_state().inventory[item["item_id"]] == 3
    assert order["order_id"] not in load_state().orders
