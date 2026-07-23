"""Local commerce exchange tests."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config import settings
from main import app
from app import agentguard_routes
from app.session_auth import SESSION_COOKIE_NAME, create_principal_session_token
from app.commerce_demo import (
    archive_item_from_payload,
    create_item,
    load_state,
    publish_item,
    publish_item_from_payload,
    search_items,
    update_item,
)


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    yield


@pytest.mark.parametrize(
    ("method", "registered_path", "request_path", "payload"),
    [
        ("POST", "/api/demo-commerce/seller/items", "/api/demo-commerce/seller/items", {"title": "Bypass item", "price_inr": 1}),
        ("PATCH", "/api/demo-commerce/seller/items/{item_id}", "/api/demo-commerce/seller/items/item_missing", {"title": "Bypass update"}),
        ("POST", "/api/demo-commerce/seller/items/{item_id}/publish", "/api/demo-commerce/seller/items/item_missing/publish", {}),
        ("POST", "/api/demo-commerce/buyer/orders", "/api/demo-commerce/buyer/orders", {"item_id": "item_missing", "quantity": 1}),
        ("POST", "/api/demo-commerce/seller/orders/{order_id}/transition", "/api/demo-commerce/seller/orders/order_missing/transition", {"status": "accepted"}),
        ("POST", "/api/demo-commerce/buyer/orders/{order_id}/issues", "/api/demo-commerce/buyer/orders/order_missing/issues", {"reason": "Bypass issue"}),
        ("POST", "/api/demo-commerce/seller/issues/{issue_id}/respond", "/api/demo-commerce/seller/issues/issue_missing/respond", {"response": "Bypass"}),
        ("POST", "/api/demo-commerce/seller/issues/{issue_id}/remedy", "/api/demo-commerce/seller/issues/issue_missing/remedy", {"data": {"amount_inr": 1}}),
        ("POST", "/api/ondc/bpp/ensure-catalog-item", "/api/ondc/bpp/ensure-catalog-item", {}),
        ("POST", "/api/commerce-integrations/payments/intents", "/api/commerce-integrations/payments/intents", {"amount_inr": 1, "order_id": "order_missing", "agentguard_receipt_id": "receipt_unverified"}),
        ("POST", "/api/commerce-integrations/logistics/transitions", "/api/commerce-integrations/logistics/transitions", {"order_id": "order_missing", "to_state": "fulfilled"}),
        ("POST", "/api/commerce-integrations/igm/issues", "/api/commerce-integrations/igm/issues", {"order_id": "order_missing", "description": "Bypass issue"}),
    ],
)
def test_public_mutation_routes_cannot_bypass_agentguard(
    method: str,
    registered_path: str,
    request_path: str,
    payload: dict[str, object],
) -> None:
    client = TestClient(app)
    before = load_state().model_dump(mode="json")

    assert method.lower() not in app.openapi().get("paths", {}).get(registered_path, {})
    response = client.request(method, request_path, json=payload)

    assert response.status_code in {404, 405}
    assert load_state().model_dump(mode="json") == before


@pytest.mark.parametrize("runtime_mode", ["production", "staging", "prodction", None])
def test_fixture_mutations_are_hidden_outside_demo_runtime(
    runtime_mode: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "aadhaar_chain_env", runtime_mode)
    for name in ("AADHAAR_CHAIN_ENV", "APP_ENV", "ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)
    client = TestClient(app)

    response = client.post(
        "/api/demo-commerce/test-fixtures/seller/items",
        json={"title": "Hidden fixture", "price_inr": 1},
    )

    assert response.status_code == 404
    assert load_state().items == {}


def _signed_in_client(audience: str, principal_id: str | None = None) -> tuple[TestClient, str]:
    client = TestClient(app)
    if principal_id:
        token = create_principal_session_token(
            principal_id=principal_id,
            audience=audience,
            identity_provider="auth0",
        )
        client.cookies.set(SESSION_COOKIE_NAME, token)
        return client, principal_id
    login = client.post("/api/auth/demo-continue", json={"audience": audience})
    assert login.status_code == 200
    return client, str(login.json()["data"]["principal_id"])


def test_commerce_reads_are_session_scoped_by_audience_and_principal() -> None:
    seller, seller_id = _signed_in_client("ondcseller")
    other_seller, _ = _signed_in_client("ondcseller", "principal:auth0:other-seller")
    buyer, buyer_id = _signed_in_client("ondcbuyer")
    other_buyer, _ = _signed_in_client("ondcbuyer", "principal:auth0:other-buyer")
    fixtures = TestClient(app)

    item = fixtures.post(
        "/api/demo-commerce/test-fixtures/seller/items",
        json={"title": "Scoped item", "price_inr": 50, "inventory": 2, "seller_id": seller_id},
    ).json()["data"]["item"]
    fixtures.post(f"/api/demo-commerce/test-fixtures/seller/items/{item['item_id']}/publish")
    other_item = fixtures.post(
        "/api/demo-commerce/test-fixtures/seller/items",
        json={"title": "Other item", "price_inr": 60, "inventory": 3, "seller_id": "another-seller"},
    ).json()["data"]["item"]
    fixtures.post(f"/api/demo-commerce/test-fixtures/seller/items/{other_item['item_id']}/publish")
    order = fixtures.post(
        "/api/demo-commerce/test-fixtures/buyer/orders",
        json={"item_id": item["item_id"], "quantity": 1, "buyer_id": buyer_id},
    ).json()["data"]["order"]
    fixtures.post(
        f"/api/demo-commerce/test-fixtures/buyer/orders/{order['order_id']}/issues",
        json={"reason": "Scoped issue"},
    )

    assert buyer.get("/api/demo-commerce/buyer/orders").json()["data"]["count"] == 1
    assert other_buyer.get(
        "/api/demo-commerce/buyer/orders",
        params={"buyer_id": buyer_id},
    ).json()["data"]["count"] == 0
    assert buyer.get(f"/api/demo-commerce/buyer/orders/{order['order_id']}").status_code == 200
    assert other_buyer.get(f"/api/demo-commerce/buyer/orders/{order['order_id']}").status_code == 404

    assert seller.get("/api/demo-commerce/seller/orders").json()["data"]["count"] == 1
    assert other_seller.get("/api/demo-commerce/seller/orders").json()["data"]["count"] == 0
    assert seller.get(f"/api/demo-commerce/seller/orders/{order['order_id']}").status_code == 200
    assert other_seller.get(f"/api/demo-commerce/seller/orders/{order['order_id']}").status_code == 404

    seller_items = seller.get("/api/demo-commerce/seller/items").json()["data"]
    assert seller_items["count"] == 1
    assert seller_items["items"][0]["item_id"] == item["item_id"]
    assert seller.get(f"/api/demo-commerce/seller/items/{item['item_id']}").status_code == 200
    assert other_seller.get(f"/api/demo-commerce/seller/items/{item['item_id']}").status_code == 404

    assert buyer.get("/api/demo-commerce/buyer/issues").json()["data"]["count"] == 1
    assert other_buyer.get("/api/demo-commerce/buyer/issues").json()["data"]["count"] == 0
    assert other_buyer.get(
        "/api/demo-commerce/buyer/issues",
        params={"order_id": order["order_id"]},
    ).status_code == 404
    assert seller.get("/api/demo-commerce/seller/issues").json()["data"]["count"] == 1
    assert other_seller.get("/api/demo-commerce/seller/issues").json()["data"]["count"] == 0

    assert buyer.get("/api/demo-commerce/seller/orders").status_code == 403
    assert seller.get("/api/demo-commerce/buyer/orders").status_code == 403
    assert buyer.get("/api/demo-commerce/seller/items").status_code == 403
    assert TestClient(app).get("/api/demo-commerce/buyer/orders").status_code == 401


def test_publish_search_order_and_idempotency() -> None:
    client = TestClient(app)

    created = client.post(
        "/api/demo-commerce/test-fixtures/seller/items",
        json={
            "idempotency_key": "item-create-1",
            "title": "Token Nxt demo product",
            "price_inr": 1200,
            "inventory": 4,
            "seller_id": "seller-demo",
            "seller_name": "Seller Demo Store",
        },
    )
    assert created.status_code == 200
    item_id = created.json()["data"]["item"]["item_id"]

    published = client.post(
        f"/api/demo-commerce/test-fixtures/seller/items/{item_id}/publish",
        json={"idempotency_key": "item-publish-1"},  # gitleaks:allow
    )
    assert published.status_code == 200
    assert published.json()["data"]["item"]["status"] == "published"

    search = client.get("/api/demo-commerce/buyer/search", params={"q": "Token Nxt"})
    assert search.status_code == 200
    assert search.json()["data"]["count"] == 1
    assert search.json()["data"]["items"][0]["inventory"] == 4
    browse = client.get("/api/demo-commerce/buyer/search", params={"q": "grocery"})
    assert browse.status_code == 200
    assert browse.json()["data"]["count"] == 1

    order_payload = {
        "idempotency_key": "order-create-1",
        "item_id": item_id,
        "quantity": 2,
        "buyer_id": "buyer-demo",
        "item_title": "Token Nxt product",
        "delivery_address": {
            "line1": "12 Market Road",
            "city": "Pune",
            "state": "Maharashtra",
            "postalCode": "411001",
            "country": "IND",
        },
    }
    order = client.post("/api/demo-commerce/test-fixtures/buyer/orders", json=order_payload)
    duplicate = client.post("/api/demo-commerce/test-fixtures/buyer/orders", json=order_payload)
    assert order.status_code == 200
    assert duplicate.status_code == 200
    assert duplicate.json()["data"]["order"]["order_id"] == order.json()["data"]["order"]["order_id"]
    assert order.json()["data"]["order"]["item_title"] == "Token Nxt demo product"
    assert order.json()["data"]["order"]["delivery_address"]["city"] == "Pune"
    state_after_order = load_state()
    assert len(state_after_order.orders) == 1
    assert len(state_after_order.reservations) == 1
    reservation = next(iter(state_after_order.reservations.values()))
    assert reservation["order_id"] == order.json()["data"]["order"]["order_id"]
    assert reservation["quantity"] == 2
    assert state_after_order.inventory[item_id] == 2

    seller_orders = client.get("/api/demo-commerce/test-fixtures/seller/orders", params={"seller_id": "seller-demo"})
    assert seller_orders.status_code == 200
    assert seller_orders.json()["data"]["count"] == 1
    assert seller_orders.json()["data"]["orders"][0]["buyer_id"] == "buyer-demo"

    buyer_orders = client.get("/api/demo-commerce/test-fixtures/buyer/orders", params={"buyer_id": "buyer-demo"})
    assert buyer_orders.status_code == 200
    assert buyer_orders.json()["data"]["orders"][0]["order_id"] == order.json()["data"]["order"]["order_id"]

    issue = client.post(
        f"/api/demo-commerce/test-fixtures/buyer/orders/{order.json()['data']['order']['order_id']}/issues",
        json={"reason": "fulfillment", "description": "Package delayed"},
    )
    assert issue.status_code == 200
    buyer_issues = client.get(
        "/api/demo-commerce/test-fixtures/buyer/issues",
        params={"order_id": order.json()["data"]["order"]["order_id"]},
    )
    assert buyer_issues.status_code == 200
    assert buyer_issues.json()["data"]["issues"][0]["issue_id"] == issue.json()["data"]["issue"]["issue_id"]


def test_buyer_search_excludes_offer_without_customer_safe_seller_name() -> None:
    created = create_item(
        {
            "title": "Unattributed Atta",
            "price_inr": 90,
            "inventory": 2,
            "seller_id": "principal:demo:legacy-seller",
        }
    )
    publish_item(created["item"]["item_id"])

    found = search_items("atta")
    assert all(row.get("title") != "Unattributed Atta" for row in found["items"])


def test_buyer_search_supplies_safe_fallback_commerce_terms() -> None:
    created = create_item(
        {
            "title": "Sampoorna Atta 1kg",
            "description": "Stone-ground flour",
            "price_inr": 90,
            "inventory": 2,
            "seller_id": "ondcseller.example",
        }
    )
    publish_item(created["item"]["item_id"])

    row = next(item for item in search_items("atta")["items"] if item["title"] == "Sampoorna Atta 1kg")
    assert row["delivery_estimate"] == "Delivery timing confirmed at checkout"
    assert row["return_policy"] == "Return eligibility confirmed before order placement"


def test_buyer_search_rice_does_not_match_poha_description() -> None:
    poha = create_item(
        {
            "title": "Harvest House Poha 500g",
            "description": "Light, clean flattened rice for quick breakfasts and snacks.",
            "price_inr": 78,
            "inventory": 5,
            "seller_id": "principal:demo:poha-seller",
            "seller_name": "Harvest House",
        }
    )
    publish_item(poha["item"]["item_id"])
    rice = create_item(
        {
            "title": "India Gate Basmati Rice 1kg",
            "description": "Long-grain basmati",
            "price_inr": 165,
            "inventory": 5,
            "seller_id": "principal:demo:rice-seller",
            "seller_name": "Grain Mart",
        }
    )
    publish_item(rice["item"]["item_id"])

    rice_hits = search_items("rice")
    assert rice_hits["count"] == 1
    assert rice_hits["items"][0]["title"].startswith("India Gate Basmati Rice")
    assert search_items("poha")["count"] == 1
    assert search_items("flattened rice")["count"] == 1


def test_buyer_search_relevance_rejects_unrelated_titles() -> None:
    oil = create_item(
        {
            "title": "Cold-Pressed Groundnut Oil 1L",
            "price_inr": 320,
            "inventory": 5,
            "seller_id": "principal:demo:oil-seller",
            "seller_name": "Oil Co",
        }
    )
    publish_item(oil["item"]["item_id"])
    tv = create_item(
        {
            "title": "Horizon LED TV 32 inch",
            "price_inr": 12999,
            "inventory": 3,
            "seller_id": "principal:demo:tv-seller",
            "seller_name": "Vision Mart",
        }
    )
    publish_item(tv["item"]["item_id"])

    assert search_items("tv")["count"] == 1
    assert search_items("tv")["items"][0]["title"].startswith("Horizon LED TV")
    assert search_items("oil")["count"] >= 1
    assert all("oil" in str(row.get("title", "")).lower() for row in search_items("oil")["items"])
    assert search_items("television-remote-xyz")["count"] == 0


def test_catalog_item_preserves_seller_image_reference() -> None:
    created = create_item(
        {
            "title": "Fresh Farm Toor Dal 1kg",
            "price_inr": 149,
            "inventory": 7,
            "seller_id": "principal:demo:fresh-farm",
            "seller_name": "Fresh Farm Foods",
            "image_url": "/products/toor-dal-lentils.jpg",
            "image_caption": "Ingredient photo; packaging may vary",
            "delivery_areas": ["Pune", "411001"],
        }
    )

    assert created["item"]["image_url"] == "/products/toor-dal-lentils.jpg"
    assert created["item"]["image_caption"] == "Ingredient photo; packaging may vary"
    assert created["item"]["delivery_areas"] == ["Pune", "411001"]
    updated = update_item(
        created["item"]["item_id"],
        {
            "image_url": "/products/dal-pack.jpg",
            "image_caption": "Exact package photo",
            "delivery_areas": ["Mumbai", "400001"],
        },
    )
    assert updated["item"]["image_url"] == "/products/dal-pack.jpg"
    assert updated["item"]["image_caption"] == "Exact package photo"
    assert updated["item"]["delivery_areas"] == ["Mumbai", "400001"]


def test_sold_out_item_is_not_returned_to_buyer_search() -> None:
    client = TestClient(app)
    item = client.post(
        "/api/demo-commerce/test-fixtures/seller/items",
        json={
            "title": "Last Pack Atta",
            "price_inr": 75,
            "inventory": 1,
            "seller_name": "Last Pack Foods",
        },
    ).json()["data"]["item"]
    item_id = item["item_id"]
    client.post(f"/api/demo-commerce/test-fixtures/seller/items/{item_id}/publish")

    order = client.post(
        "/api/demo-commerce/test-fixtures/buyer/orders",
        json={"item_id": item_id, "quantity": 1, "buyer_id": "buyer-sold-out"},
    )
    assert order.status_code == 200

    search = client.get("/api/demo-commerce/buyer/search", params={"q": "Last Pack Atta"})
    assert search.status_code == 200
    assert search.json()["data"] == {"items": [], "count": 0}


def test_agentguard_execute_returns_conflict_for_inventory_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _principal_id = _signed_in_client("ondcbuyer")

    def _sold_out(**_kwargs):
        raise ValueError("Insufficient inventory.")

    monkeypatch.setattr(agentguard_routes.agentguard, "execute_action", _sold_out)
    response = client.post(
        "/api/agentguard/actions/execute",
        json={
            "action": "buyer.checkout.commit",
            "amount_inr": 89,
            "resource_id": "cart-sold-out",
            "idempotency_key": "sold-out-conflict",
            "payload": {"item_id": "item-sold-out", "quantity": 1},
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Insufficient inventory."


def test_seller_fulfilment_and_remedy_reach_buyer_state() -> None:
    client = TestClient(app)
    item = client.post(
        "/api/demo-commerce/test-fixtures/seller/items",
        json={"title": "Lifecycle Atta", "price_inr": 120, "inventory": 3, "seller_id": "seller-lifecycle"},
    ).json()["data"]["item"]
    client.post(f"/api/demo-commerce/test-fixtures/seller/items/{item['item_id']}/publish")
    order = client.post(
        "/api/demo-commerce/test-fixtures/buyer/orders",
        json={
            "item_id": item["item_id"],
            "quantity": 1,
            "buyer_id": "buyer-lifecycle",
            "delivery_address": {
                "name": "Asha Rao",
                "phone": "+919876543210",
                "line1": "12 Market Road",
                "city": "Pune",
                "state": "Maharashtra",
                "postalCode": "411001",
                "country": "IND",
            },
        },
    ).json()["data"]["order"]

    accepted = client.post(
        f"/api/demo-commerce/test-fixtures/seller/orders/{order['order_id']}/transition",
        json={"status": "accepted", "idempotency_key": "accept-lifecycle-1"},
    )
    assert accepted.status_code == 200
    fulfilled = client.post(
        f"/api/demo-commerce/test-fixtures/seller/orders/{order['order_id']}/transition",
        json={"status": "fulfilled", "idempotency_key": "fulfil-lifecycle-1"},
    )
    assert fulfilled.status_code == 200
    buyer_orders = client.get("/api/demo-commerce/test-fixtures/buyer/orders", params={"buyer_id": "buyer-lifecycle"})
    assert buyer_orders.json()["data"]["orders"][0]["status"] == "fulfilled"

    issue = client.post(
        f"/api/demo-commerce/test-fixtures/buyer/orders/{order['order_id']}/issues",
        json={"reason": "fulfillment", "description": "Damaged package"},
    ).json()["data"]["issue"]
    seller_issues = client.get("/api/demo-commerce/test-fixtures/seller/issues")
    assert seller_issues.json()["data"]["issues"][0]["issue_id"] == issue["issue_id"]
    response = client.post(
        f"/api/demo-commerce/test-fixtures/seller/issues/{issue['issue_id']}/respond",
        json={"response": "Refund promised"},
    )
    assert response.status_code == 200
    remedy = client.post(
        f"/api/demo-commerce/test-fixtures/seller/issues/{issue['issue_id']}/remedy",
        json={
            "idempotency_key": "remedy-lifecycle-1",
            "data": {"type": "refund", "amount_inr": 120},
        },
    )
    assert remedy.status_code == 200

    buyer_issue = client.get("/api/demo-commerce/test-fixtures/buyer/issues", params={"order_id": order["order_id"]})
    visible_issue = buyer_issue.json()["data"]["issues"][0]
    assert visible_issue["status"] == "resolution_proposed"
    assert visible_issue["remedy_id"] == remedy.json()["data"]["remedy"]["remedy_id"]
    assert remedy.json()["data"]["remedy"]["order_id"] == order["order_id"]
    assert remedy.json()["data"]["remedy"]["amount_inr"] == 120


def test_agentguard_catalog_executor_updates_existing_item_before_publish() -> None:
    created = create_item(
        {"title": "Draft Atta", "price_inr": 80, "inventory": 2, "seller_id": "seller-a"},
        idempotency_key="draft-create",
    )
    item_id = created["item"]["item_id"]

    executed = publish_item_from_payload(
        {
            "item_id": item_id,
            "title": "Published Atta",
            "price_inr": 95,
            "inventory": 7,
            "category_id": "Grocery",
        },
        principal_id="seller-a",
        resource_id=item_id,
        idempotency_key="agentguard-publish",
    )

    assert executed["item"]["status"] == "published"
    assert executed["item"]["title"] == "Published Atta"
    assert executed["item"]["price_inr"] == 95
    assert executed["item"]["category_id"] == "Grocery"
    assert executed["inventory"] == 7

    archived = archive_item_from_payload(
        {"item_id": item_id},
        principal_id="seller-a",
        resource_id=item_id,
        idempotency_key="agentguard-archive",
    )
    assert archived["item"]["status"] == "archived"
    assert TestClient(app).get("/api/demo-commerce/buyer/search", params={"q": "Published Atta"}).json()["data"]["count"] == 0


def test_cleanup_removes_only_deterministic_test_artifacts() -> None:
    client = TestClient(app)
    fixture = client.post(
        "/api/demo-commerce/test-fixtures/seller/items",
        json={
            "title": "Matrix Fresh Atta 123456",
            "description": "Fresh local Samantha checkout fixture",
            "price_inr": 100,
            "inventory": 1,
        },
    ).json()["data"]["item"]
    real = client.post(
        "/api/demo-commerce/test-fixtures/seller/items",
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
        "/api/demo-commerce/test-fixtures/seller/items",
        json={"title": "Millet Flour", "price_inr": 120, "inventory": 3},
    ).json()["data"]["item"]
    client.post(f"/api/demo-commerce/test-fixtures/seller/items/{item['item_id']}/publish")
    order = client.post(
        "/api/demo-commerce/test-fixtures/buyer/orders",
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


def test_cleanup_discovers_dispatch_proof_catalog_litter() -> None:
    client = TestClient(app)
    fixture = client.post(
        "/api/demo-commerce/test-fixtures/seller/items",
        json={
            "title": "Dispatch Proof Atta 1kg",
            "description": "Fixture for Hermes Dispatch inject proof",
            "price_inr": 100,
            "inventory": 2,
        },
    ).json()["data"]["item"]
    real = client.post(
        "/api/demo-commerce/test-fixtures/seller/items",
        json={
            "title": "Farmhouse Atta 1kg",
            "description": "Stone-ground whole wheat flour",
            "price_inr": 120,
            "inventory": 2,
        },
    ).json()["data"]["item"]

    cleanup = client.post("/api/demo-commerce/test-fixtures/cleanup")

    assert cleanup.status_code == 200
    ids = set(load_state().items)
    assert fixture["item_id"] not in ids
    assert real["item_id"] in ids


def test_cleanup_exact_item_does_not_remove_another_fixture() -> None:
    client = TestClient(app)
    first = client.post(
        "/api/demo-commerce/test-fixtures/seller/items",
        json={
            "title": "Matrix Exact Cleanup One",
            "description": "Fresh local Samantha checkout fixture",
            "price_inr": 100,
            "inventory": 1,
        },
    ).json()["data"]["item"]
    second = client.post(
        "/api/demo-commerce/test-fixtures/seller/items",
        json={
            "title": "Matrix Exact Cleanup Two",
            "description": "Fresh local Samantha checkout fixture",
            "price_inr": 100,
            "inventory": 1,
        },
    ).json()["data"]["item"]

    cleanup = client.post(
        "/api/demo-commerce/test-fixtures/cleanup",
        json={"data": {"item_ids": [first["item_id"]]}},
    )

    assert cleanup.status_code == 200
    assert cleanup.json()["data"]["items"] == 1
    ids = set(load_state().items)
    assert first["item_id"] not in ids
    assert second["item_id"] in ids
