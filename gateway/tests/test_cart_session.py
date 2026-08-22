"""Buyer SPA cart/billing persist: missing profile is 200 upsert, never 404."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config import settings
from main import app


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "aadhaar_chain_env", "demo")
    yield


def test_billing_save_upserts_missing_profile_and_never_404s() -> None:
    client = TestClient(app)
    session_id = "ondc-session-billing-1"

    missing = client.get("/api/cart")
    assert missing.status_code == 422

    empty = client.get(f"/api/cart?sessionId={session_id}")
    assert empty.status_code == 200, empty.text
    assert empty.json()["session"]["id"] == session_id
    assert empty.json()["session"]["items"] == []

    created = client.patch(
        f"/api/cart/buyer/{session_id}",
        json={},
    )
    assert created.status_code == 200, created.text
    assert created.json()["session"]["id"] == session_id

    saved = client.patch(
        f"/api/cart/buyer/{session_id}",
        json={
            "name": "Gurusharan",
            "email": "guru@example.com",
            "phone": "+919876543210",
            "taxId": "29ABCDE1234F1Z5",
            "line1": "12 Market Road",
            "city": "Bengaluru",
            "state": "Karnataka",
            "postalCode": "560001",
        },
    )
    assert saved.status_code == 200, saved.text
    buyer = saved.json()["session"]["buyer"]
    assert buyer["name"] == "Gurusharan"
    assert buyer["email"] == "guru@example.com"
    assert buyer["contact"]["email"] == "guru@example.com"
    assert buyer["pincode"] == "560001"
    assert buyer["street"] == "12 Market Road"

    loaded = client.get(f"/api/cart?sessionId={session_id}")
    assert loaded.status_code == 200
    assert loaded.json()["session"]["buyer"]["name"] == "Gurusharan"
    assert loaded.json()["session"]["buyer"]["contact"]["phone"] == "+919876543210"


def test_cart_item_persist_keeps_session_for_checkout_refresh() -> None:
    client = TestClient(app)
    session_id = "ondc-session-cart-1"
    added = client.post(
        "/api/cart",
        json={
            "sessionId": session_id,
            "item": {"id": "sku-atta-1", "name": "Sampoorna Atta 1kg"},
            "quantity": 2,
        },
    )
    assert added.status_code == 200, added.text
    assert added.json()["session"]["items"][0]["quantity"] == 2

    client.patch(
        f"/api/cart/buyer/{session_id}",
        json={"name": "Buyer", "email": "buyer@example.com", "phone": "+911234567890"},
    )
    loaded = client.get(f"/api/cart?sessionId={session_id}")
    session = loaded.json()["session"]
    assert session["items"][0]["item"]["id"] == "sku-atta-1"
    assert session["buyer"]["name"] == "Buyer"
