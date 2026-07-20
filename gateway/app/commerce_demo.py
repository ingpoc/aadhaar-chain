"""File-backed local commerce exchange for the AgentGuard demo."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.payment_adapter import payment_adapter
from config import settings

STATE_FILE = "commerce-demo-state.json"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class CommerceState(BaseModel):
    version: int = 1
    items: dict[str, dict[str, Any]] = Field(default_factory=dict)
    inventory: dict[str, int] = Field(default_factory=dict)
    orders: dict[str, dict[str, Any]] = Field(default_factory=dict)
    reservations: dict[str, dict[str, Any]] = Field(default_factory=dict)
    issues: dict[str, dict[str, Any]] = Field(default_factory=dict)
    remedies: dict[str, dict[str, Any]] = Field(default_factory=dict)
    idempotency: dict[str, dict[str, Any]] = Field(default_factory=dict)


def _state_path() -> Path:
    return Path(settings.data_dir).expanduser() / STATE_FILE


def load_state() -> CommerceState:
    path = _state_path()
    if not path.is_file():
        return CommerceState()
    return CommerceState.model_validate(json.loads(path.read_text(encoding="utf-8") or "{}"))


def save_state(state: CommerceState) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state.model_dump(mode="json"), indent=2), encoding="utf-8")
    tmp.replace(path)


def _idempotency_key(scope: str, key: Optional[str]) -> Optional[str]:
    return f"{scope}:{key}" if key else None


def _remember(state: CommerceState, key: Optional[str], response: dict[str, Any]) -> dict[str, Any]:
    if key:
        state.idempotency[key] = response
    return response


def _default_category_id(title: str) -> str:
    """Avoid defaulting TVs into Grocery when sellers omit category_id."""
    blob = (title or "").lower()
    if re.search(r"\b(tv|television|laptop|phone|mobile|headphone)\b", blob):
        return "Electronics"
    if re.search(r"\b(shirt|saree|jeans|kurta|dress)\b", blob):
        return "Fashion"
    return "Grocery"


def create_item(payload: dict[str, Any], *, idempotency_key: Optional[str] = None) -> dict[str, Any]:
    state = load_state()
    idem = _idempotency_key("seller.items.create", idempotency_key)
    if idem and idem in state.idempotency:
        return state.idempotency[idem]

    item_id = _new_id("item")
    quantity = int(payload.get("inventory") or payload.get("quantity") or 0)
    title = str(payload.get("title") or payload.get("name") or "Catalog item")
    item = {
        "item_id": item_id,
        "version": 1,
        "status": "draft",
        "seller_id": payload.get("seller_id") or payload.get("wallet_address") or "seller",
        "seller_name": payload.get("seller_name") or payload.get("provider_name"),
        "title": title,
        "description": payload.get("description") or "Product details provided by seller.",
        "price_inr": int(payload.get("price_inr") or payload.get("amount_inr") or 0),
        "category_id": payload.get("category_id") or _default_category_id(title),
        "delivery_estimate": payload.get("delivery_estimate"),
        "return_policy": payload.get("return_policy"),
        "image_url": payload.get("image_url"),
        "image_caption": payload.get("image_caption"),
        "delivery_areas": payload.get("delivery_areas") or [],
        "created_at": _utcnow(),
        "updated_at": _utcnow(),
    }
    state.items[item_id] = item
    state.inventory[item_id] = quantity
    response = {"item": item, "inventory": quantity, "message_id": _new_id("msg")}
    _remember(state, idem, response)
    save_state(state)
    return response


def update_item(item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    state = load_state()
    item = state.items.get(item_id)
    if not item:
        raise KeyError(f"Unknown item: {item_id}")
    updated = {
        **item,
        **{
            key: value
            for key, value in payload.items()
            if key in {
                "title",
                "description",
                "price_inr",
                "category_id",
                "seller_name",
                "delivery_estimate",
                "return_policy",
                "image_url",
                "image_caption",
                "delivery_areas",
            }
        },
        "version": int(item["version"]) + 1,
        "updated_at": _utcnow(),
    }
    if "inventory" in payload:
        state.inventory[item_id] = int(payload["inventory"])
    state.items[item_id] = updated
    save_state(state)
    return {"item": updated, "inventory": state.inventory.get(item_id, 0), "message_id": _new_id("msg")}


def cleanup_test_artifacts(
    *,
    explicit_order_ids: Optional[set[str]] = None,
    explicit_item_ids: Optional[set[str]] = None,
    include_discovered: bool = True,
) -> dict[str, int]:
    """Remove deterministic local test fixtures without touching operator-created listings."""
    state = load_state()
    fixture_descriptions = {
        "Shared local commerce item for AgentGuard two-sided proof.",
        "Local Samantha order-lifecycle fixture",
        "Fresh local Samantha checkout fixture",
    }
    if include_discovered:
        item_ids = {
            item_id
            for item_id, item in state.items.items()
            if str(item.get("title") or "").startswith(
                ("Token Nxt proof SKU ", "Matrix ", "Evening Ragi Flour")
            )
            or str(item.get("description") or "") in fixture_descriptions
            or str(item.get("seller_id") or "").startswith("seller-ag-")
        }
    else:
        item_ids = {
            item_id for item_id in (explicit_item_ids or set()) if item_id in state.items
        }
    order_ids = {
        order_id
        for order_id, order in state.orders.items()
        if order.get("item_id") in item_ids
        or order.get("item_id") in {"local-cart", "demo-atta-5kg"}
        or str(order.get("seller_id") or "").startswith("seller-ag-")
    }
    explicit_order_ids = explicit_order_ids or set()
    explicit_orders = {
        order_id: state.orders[order_id]
        for order_id in explicit_order_ids
        if order_id in state.orders
    }
    order_ids.update(explicit_orders)
    issue_ids = {
        issue_id
        for issue_id, issue in state.issues.items()
        if issue.get("order_id") in order_ids
    }
    remedy_ids = {
        remedy_id
        for remedy_id, remedy in state.remedies.items()
        if remedy.get("order_id") in order_ids or remedy.get("issue_id") in issue_ids
    }
    reservation_ids = {
        reservation_id
        for reservation_id, reservation in state.reservations.items()
        if reservation.get("order_id") in order_ids or reservation.get("item_id") in item_ids
    }

    for item_id in item_ids:
        state.items.pop(item_id, None)
        state.inventory.pop(item_id, None)
    restored_inventory = 0
    for order_id, order in explicit_orders.items():
        item_id = str(order.get("item_id") or "")
        quantity = int(order.get("quantity") or 0)
        if item_id in state.items and quantity > 0:
            state.inventory[item_id] = state.inventory.get(item_id, 0) + quantity
            restored_inventory += quantity
    for order_id in order_ids:
        state.orders.pop(order_id, None)
    for reservation_id in reservation_ids:
        state.reservations.pop(reservation_id, None)
    for issue_id in issue_ids:
        state.issues.pop(issue_id, None)
    for remedy_id in remedy_ids:
        state.remedies.pop(remedy_id, None)

    artifact_ids = item_ids | order_ids | reservation_ids | issue_ids | remedy_ids
    for key, value in list(state.idempotency.items()):
        serialized = json.dumps(value, sort_keys=True)
        if any(artifact_id in serialized for artifact_id in artifact_ids):
            state.idempotency.pop(key, None)

    save_state(state)
    return {
        "items": len(item_ids),
        "orders": len(order_ids),
        "reservations": len(reservation_ids),
        "issues": len(issue_ids),
        "remedies": len(remedy_ids),
        "restored_inventory": restored_inventory,
    }


def publish_item(item_id: str, *, idempotency_key: Optional[str] = None) -> dict[str, Any]:
    state = load_state()
    idem = _idempotency_key(f"seller.items.publish.{item_id}", idempotency_key)
    if idem and idem in state.idempotency:
        return state.idempotency[idem]
    item = state.items.get(item_id)
    if not item:
        raise KeyError(f"Unknown item: {item_id}")
    published = {**item, "status": "published", "version": int(item["version"]) + 1, "updated_at": _utcnow()}
    state.items[item_id] = published
    response = {"item": published, "inventory": state.inventory.get(item_id, 0), "message_id": _new_id("msg")}
    _remember(state, idem, response)
    save_state(state)
    return response


def archive_item(item_id: str, *, idempotency_key: Optional[str] = None) -> dict[str, Any]:
    state = load_state()
    idem = _idempotency_key(f"seller.items.archive.{item_id}", idempotency_key)
    if idem and idem in state.idempotency:
        return state.idempotency[idem]
    item = state.items.get(item_id)
    if not item:
        raise KeyError(f"Unknown item: {item_id}")
    archived = {**item, "status": "archived", "version": int(item["version"]) + 1, "updated_at": _utcnow()}
    state.items[item_id] = archived
    response = {"item": archived, "inventory": state.inventory.get(item_id, 0), "message_id": _new_id("msg")}
    _remember(state, idem, response)
    save_state(state)
    return response


_BROWSE_SEARCH_TERMS = frozenset({"all", "food", "foods", "groceries", "grocery", "products"})
_SEARCH_STOP = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "pack",
        "packet",
        "kg",
        "gms",
        "gram",
        "grams",
        "litre",
        "liter",
        "ml",
    }
)


def item_matches_search_query(item: dict[str, Any], query: str) -> bool:
    """Strict relevance: empty match stays empty (no unrelated oil for \"tv\").

    Single-token queries match the title only — description phrases like
    \"flattened rice\" must not make Poha a hit for \"rice\".
    """
    lowered = (query or "").strip().lower()
    if not lowered or lowered in _BROWSE_SEARCH_TERMS:
        return True
    title = str(item.get("title", "")).lower()
    haystack = " ".join(
        [
            title,
            str(item.get("description", "")),
            str(item.get("category_id", "")),
        ]
    ).lower()
    if lowered in title:
        return True
    tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", lowered)
        if token not in _SEARCH_STOP and (len(token) >= 2)
    ]
    if not tokens:
        return False
    # Title carries the product noun (atta / rice / tv / oil).
    if all(token in title for token in tokens):
        return True
    # Multi-word specialty asks may live in description ("flattened rice" → poha)
    # only when every token appears somewhere and at least one hits the title,
    # or when the full phrase is in the description and the query is multi-token.
    if len(tokens) >= 2 and all(token in haystack for token in tokens):
        return True
    return False


def search_items(query: Optional[str] = None) -> dict[str, Any]:
    state = load_state()
    # Published + in-stock is enough. seller_name is display sugar — fixture /
    # AgentGuard seeds often set only seller_id; requiring a name hid real SKUs
    # from Buyer search and made Samantha look empty or invent cached ghosts.
    rows = [
        {**item, "inventory": state.inventory.get(item_id, 0)}
        for item_id, item in state.items.items()
        if item.get("status") == "published" and state.inventory.get(item_id, 0) > 0
    ]
    if query and str(query).strip():
        rows = [item for item in rows if item_matches_search_query(item, str(query))]
    return {"items": rows, "count": len(rows)}


def get_item(item_id: str) -> dict[str, Any]:
    state = load_state()
    item = state.items.get(item_id)
    if not item:
        raise KeyError(f"Unknown item: {item_id}")
    return {"item": item, "inventory": state.inventory.get(item_id, 0)}


def list_seller_items(seller_id: str) -> dict[str, Any]:
    state = load_state()
    rows = [
        {**item, "inventory": state.inventory.get(item_id, 0)}
        for item_id, item in state.items.items()
        if item.get("seller_id") == seller_id
    ]
    return {"items": rows, "count": len(rows)}


def create_order(payload: dict[str, Any], *, idempotency_key: Optional[str] = None) -> dict[str, Any]:
    state = load_state()
    idem = _idempotency_key("buyer.orders.create", idempotency_key)
    if idem and idem in state.idempotency:
        return state.idempotency[idem]

    item_id = payload.get("item_id")
    quantity = int(payload.get("quantity") or 1)
    item = state.items.get(item_id) if item_id else None
    # Buyer local-cart checkout can use SKUs outside the shared commerce catalog.
    # AgentGuard still authorizes the configured payment adapter when an amount is present.
    if not item or item.get("status") != "published":
        amount_inr = int(payload.get("amount_inr") or 0)
        if amount_inr <= 0:
            raise ValueError("Item is not published.")
        order_id = _new_id("order")
        transaction_id = _new_id("txn")
        payment = payment_adapter.charge(
            idempotency_key=idempotency_key or order_id,
            amount_inr=amount_inr,
            mode=payload.get("payment_mode") or "success",
            reference_id=transaction_id,
        )
        order = {
            "order_id": order_id,
            "transaction_id": transaction_id,
            "message_id": _new_id("msg"),
            "buyer_id": payload.get("buyer_id") or payload.get("wallet_address") or "buyer",
            "seller_id": payload.get("seller_id") or "seller",
            "seller_name": payload.get("seller_name") or payload.get("provider_name"),
            "item_id": item_id or "local-cart",
            "item_title": payload.get("item_title") or item_id or "Order item",
            "item_version": 1,
            "quantity": quantity,
            "amount_inr": amount_inr,
            "delivery_address": payload.get("delivery_address"),
            "status": "paid" if payment["status"] == "succeeded" else payment["status"],
            "payment": payment,
            "source": "agentguard_local_cart",
            "created_at": _utcnow(),
            "updated_at": _utcnow(),
        }
        state.orders[order_id] = order
        response = {"order": order, "transaction_id": transaction_id, "message_id": order["message_id"]}
        _remember(state, idem, response)
        save_state(state)
        return response

    available = state.inventory.get(item_id, 0)
    if available < quantity:
        raise ValueError("Insufficient inventory.")

    amount_inr = int(payload.get("amount_inr") or item["price_inr"] * quantity)
    order_id = _new_id("order")
    transaction_id = _new_id("txn")
    payment = payment_adapter.charge(
        idempotency_key=idempotency_key or order_id,
        amount_inr=amount_inr,
        mode=payload.get("payment_mode") or "success",
        reference_id=transaction_id,
    )
    state.inventory[item_id] = available - quantity
    reservation_id = _new_id("rsv")
    reservation = {
        "reservation_id": reservation_id,
        "order_id": order_id,
        "item_id": item_id,
        "quantity": quantity,
        "status": "reserved",
        "created_at": _utcnow(),
    }
    state.reservations[reservation_id] = reservation
    order = {
        "order_id": order_id,
        "transaction_id": transaction_id,
        "message_id": _new_id("msg"),
        "buyer_id": payload.get("buyer_id") or payload.get("wallet_address") or "buyer",
        "seller_id": item.get("seller_id") or "seller",
        "seller_name": item.get("seller_name"),
        "item_id": item_id,
        "item_title": item.get("title") or payload.get("item_title") or item_id,
        "item_version": item["version"],
        "reservation_id": reservation_id,
        "quantity": quantity,
        "amount_inr": amount_inr,
        "delivery_address": payload.get("delivery_address"),
        "status": "paid" if payment["status"] == "succeeded" else payment["status"],
        "payment": payment,
        "created_at": _utcnow(),
        "updated_at": _utcnow(),
    }
    state.orders[order_id] = order
    response = {"order": order, "transaction_id": transaction_id, "message_id": order["message_id"]}
    _remember(state, idem, response)
    save_state(state)
    return response


def record_order_authorization(order_id: str, authorization: dict[str, Any]) -> dict[str, Any]:
    """Attach the completed AgentGuard decision to the durable commerce order."""
    state = load_state()
    order = state.orders.get(order_id)
    if not order:
        raise KeyError(f"Unknown order: {order_id}")

    updated = {
        **order,
        "authorization": authorization,
        "updated_at": _utcnow(),
    }
    state.orders[order_id] = updated

    # Idempotent checkout replays return the same enriched order rather than the
    # pre-authorization snapshot retained when inventory was first reserved.
    for response in state.idempotency.values():
        response_order = response.get("order") if isinstance(response, dict) else None
        if isinstance(response_order, dict) and response_order.get("order_id") == order_id:
            response["order"] = updated

    save_state(state)
    return updated


def get_order(order_id: str) -> dict[str, Any]:
    state = load_state()
    order = state.orders.get(order_id)
    if not order:
        raise KeyError(f"Unknown order: {order_id}")
    return {"order": order}


def list_seller_orders(seller_id: Optional[str] = None) -> dict[str, Any]:
    state = load_state()
    orders = list(state.orders.values())
    if seller_id:
        orders = [order for order in orders if order.get("seller_id") == seller_id]
    return {"orders": orders, "count": len(orders)}


def list_buyer_orders(buyer_id: Optional[str] = None) -> dict[str, Any]:
    state = load_state()
    orders = list(state.orders.values())
    if buyer_id:
        orders = [order for order in orders if order.get("buyer_id") == buyer_id]
    return {"orders": orders, "count": len(orders)}


def transition_order(order_id: str, status: str, *, idempotency_key: Optional[str] = None) -> dict[str, Any]:
    state = load_state()
    idem = _idempotency_key(f"seller.orders.transition.{order_id}.{status}", idempotency_key)
    if idem and idem in state.idempotency:
        return state.idempotency[idem]
    order = state.orders.get(order_id)
    if not order:
        raise KeyError(f"Unknown order: {order_id}")
    if status == "accepted":
        address = order.get("delivery_address") or {}
        required = ("name", "phone", "line1", "city", "state", "postalCode", "country")
        missing = [field for field in required if not str(address.get(field) or "").strip()]
        if missing:
            raise ValueError("Delivery details are incomplete; the order cannot be accepted.")
    valid = {
        "paid": {"accepted", "rejected", "cancelled"},
        "accepted": {"fulfilled", "cancelled"},
        "fulfilled": {"closed"},
        "unknown": {"cancelled"},
    }
    if status not in valid.get(order["status"], set()):
        raise ValueError(f"Invalid order transition {order['status']} -> {status}")
    updated = {**order, "status": status, "updated_at": _utcnow()}
    state.orders[order_id] = updated
    response = {"order": updated, "message_id": _new_id("msg")}
    _remember(state, idem, response)
    save_state(state)
    return response


def create_issue(order_id: str, payload: dict[str, Any], *, idempotency_key: Optional[str] = None) -> dict[str, Any]:
    state = load_state()
    idem = _idempotency_key(f"buyer.issues.create.{order_id}", idempotency_key)
    if idem and idem in state.idempotency:
        return state.idempotency[idem]
    if order_id not in state.orders:
        raise KeyError(f"Unknown order: {order_id}")
    issue_id = _new_id("issue")
    issue = {
        "issue_id": issue_id,
        "order_id": order_id,
        "status": "open",
        "reason": payload.get("reason") or "buyer_support",
        "description": payload.get("description") or "",
        "created_at": _utcnow(),
        "updated_at": _utcnow(),
    }
    state.issues[issue_id] = issue
    response = {"issue": issue, "message_id": _new_id("msg")}
    _remember(state, idem, response)
    save_state(state)
    return response


def list_seller_issues() -> dict[str, Any]:
    state = load_state()
    issues = list(state.issues.values())
    return {"issues": issues, "count": len(issues)}


def list_buyer_issues(order_id: Optional[str] = None) -> dict[str, Any]:
    state = load_state()
    issues = list(state.issues.values())
    if order_id:
        issues = [issue for issue in issues if issue.get("order_id") == order_id]
    return {"issues": issues, "count": len(issues)}


def respond_issue(issue_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    state = load_state()
    issue = state.issues.get(issue_id)
    if not issue:
        raise KeyError(f"Unknown issue: {issue_id}")
    updated = {
        **issue,
        "status": "responded",
        "response": payload.get("response") or payload.get("message") or "",
        "updated_at": _utcnow(),
    }
    state.issues[issue_id] = updated
    save_state(state)
    return {"issue": updated, "message_id": _new_id("msg")}


def propose_remedy(issue_id: str, payload: dict[str, Any], *, idempotency_key: Optional[str] = None) -> dict[str, Any]:
    state = load_state()
    idem = _idempotency_key(f"seller.issues.remedy.{issue_id}", idempotency_key)
    if idem and idem in state.idempotency:
        return state.idempotency[idem]
    issue = state.issues.get(issue_id)
    if not issue:
        raise KeyError(f"Unknown issue: {issue_id}")
    remedy_id = _new_id("remedy")
    remedy = {
        "remedy_id": remedy_id,
        "issue_id": issue_id,
        "order_id": issue["order_id"],
        "status": "promised",
        "type": payload.get("type") or "refund",
        "amount_inr": int(payload.get("amount_inr") or 0),
        "created_at": _utcnow(),
    }
    state.remedies[remedy_id] = remedy
    state.issues[issue_id] = {
        **issue,
        "status": "resolution_proposed",
        "remedy_id": remedy_id,
        "updated_at": _utcnow(),
    }
    response = {"remedy": remedy, "message_id": _new_id("msg")}
    _remember(state, idem, response)
    save_state(state)
    return response


def accept_remedy(remedy_id: str, *, idempotency_key: Optional[str] = None) -> dict[str, Any]:
    state = load_state()
    idem = _idempotency_key(f"buyer.remedies.accept.{remedy_id}", idempotency_key)
    if idem and idem in state.idempotency:
        return state.idempotency[idem]
    remedy = state.remedies.get(remedy_id)
    if not remedy:
        raise KeyError(f"Unknown remedy: {remedy_id}")
    if remedy.get("status") != "promised":
        raise ValueError(f"Invalid remedy transition {remedy.get('status')} -> accepted")
    issue_id = str(remedy.get("issue_id") or "")
    issue = state.issues.get(issue_id)
    if not issue:
        raise KeyError(f"Unknown issue: {issue_id}")
    accepted = {**remedy, "status": "accepted", "updated_at": _utcnow()}
    state.remedies[remedy_id] = accepted
    state.issues[issue_id] = {**issue, "status": "closed", "updated_at": _utcnow()}
    response = {"remedy": accepted, "issue": state.issues[issue_id], "message_id": _new_id("msg")}
    _remember(state, idem, response)
    save_state(state)
    return response


def _require_item_owner(item_id: str, principal_id: str) -> dict[str, Any]:
    item = load_state().items.get(item_id)
    if not item:
        raise KeyError(f"Unknown item: {item_id}")
    if item.get("seller_id") != principal_id:
        raise PermissionError("Catalog item belongs to another principal.")
    return item


def _require_order_owner(order_id: str, principal_id: str, owner_field: str) -> dict[str, Any]:
    order = load_state().orders.get(order_id)
    if not order:
        raise KeyError(f"Unknown order: {order_id}")
    if order.get(owner_field) != principal_id:
        raise PermissionError("Order belongs to another principal.")
    return order


def _require_issue_seller(issue_id: str, principal_id: str) -> dict[str, Any]:
    state = load_state()
    issue = state.issues.get(issue_id)
    if not issue:
        raise KeyError(f"Unknown issue: {issue_id}")
    order = state.orders.get(str(issue.get("order_id") or ""))
    if not order:
        raise KeyError(f"Unknown order: {issue.get('order_id')}")
    if order.get("seller_id") != principal_id:
        raise PermissionError("Issue belongs to another principal.")
    return issue


def _require_remedy_buyer(remedy_id: str, principal_id: str) -> dict[str, Any]:
    state = load_state()
    remedy = state.remedies.get(remedy_id)
    if not remedy:
        raise KeyError(f"Unknown remedy: {remedy_id}")
    issue = state.issues.get(str(remedy.get("issue_id") or ""))
    if not issue:
        raise KeyError(f"Unknown issue: {remedy.get('issue_id')}")
    order = state.orders.get(str(issue.get("order_id") or ""))
    if not order:
        raise KeyError(f"Unknown order: {issue.get('order_id')}")
    if order.get("buyer_id") != principal_id:
        raise PermissionError("Remedy belongs to another principal.")
    return remedy


def _require_bound_resource(payload: dict[str, Any], field: str, resource_id: str) -> str:
    target_id = str(payload.get(field) or "")
    if not target_id:
        raise ValueError(f"{field} is required.")
    if target_id != resource_id:
        raise ValueError("Protected resource does not match the action payload.")
    return target_id


def publish_item_from_payload(
    payload: dict[str, Any],
    *,
    principal_id: str,
    resource_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    item_id = payload.get("item_id")
    effective_payload = {**payload, "seller_id": principal_id}
    if not item_id:
        created = create_item(effective_payload, idempotency_key=f"{idempotency_key}:create")
        item_id = created["item"]["item_id"]
    else:
        item_id = str(item_id)
        _require_bound_resource(payload, "item_id", resource_id)
        _require_item_owner(item_id, principal_id)
    if payload.get("item_id") and any(
        key in payload
        for key in {
            "title",
            "description",
            "price_inr",
            "inventory",
            "quantity",
            "category_id",
            "image_url",
            "image_caption",
            "delivery_areas",
        }
    ):
        update_item(item_id, effective_payload)
    return publish_item(str(item_id), idempotency_key=idempotency_key)


def archive_item_from_payload(
    payload: dict[str, Any],
    *,
    principal_id: str,
    resource_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    item_id = _require_bound_resource(payload, "item_id", resource_id)
    _require_item_owner(item_id, principal_id)
    return archive_item(item_id, idempotency_key=idempotency_key)


def create_order_from_payload(
    payload: dict[str, Any],
    *,
    principal_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    return create_order({**payload, "buyer_id": principal_id}, idempotency_key=idempotency_key)


def transition_order_from_payload(
    action: str,
    payload: dict[str, Any],
    *,
    principal_id: str,
    resource_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    status = {
        "seller.order.accept": "accepted",
        "seller.order.reject": "rejected",
        "seller.fulfilment.commit": "fulfilled",
    }[action]
    order_id = _require_bound_resource(payload, "order_id", resource_id)
    _require_order_owner(order_id, principal_id, "seller_id")
    return transition_order(order_id, status, idempotency_key=idempotency_key)


def issue_from_payload(
    action: str,
    payload: dict[str, Any],
    *,
    principal_id: str,
    resource_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    order_id = _require_bound_resource(payload, "order_id", resource_id)
    _require_order_owner(order_id, principal_id, "buyer_id")
    if action == "buyer.order.cancel":
        return transition_order(order_id, "cancelled", idempotency_key=idempotency_key)
    return create_issue(order_id, payload, idempotency_key=idempotency_key)


def accept_remedy_from_payload(
    payload: dict[str, Any],
    *,
    principal_id: str,
    resource_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    remedy_id = _require_bound_resource(payload, "remedy_id", resource_id)
    _require_remedy_buyer(remedy_id, principal_id)
    return accept_remedy(remedy_id, idempotency_key=idempotency_key)


def remedy_from_payload(
    payload: dict[str, Any],
    *,
    principal_id: str,
    resource_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    issue_id = _require_bound_resource(payload, "issue_id", resource_id)
    _require_issue_seller(issue_id, principal_id)
    return propose_remedy(issue_id, payload, idempotency_key=idempotency_key)


def refund_from_payload(
    payload: dict[str, Any],
    *,
    principal_id: str,
    resource_id: str,
    amount_inr: int,
    idempotency_key: str,
) -> dict[str, Any]:
    order_id = _require_bound_resource(payload, "order_id", resource_id)
    state = load_state()
    order = state.orders.get(order_id)
    if not order:
        raise KeyError(f"Unknown order: {order_id}")
    if order.get("seller_id") != principal_id:
        raise PermissionError("Order belongs to another principal.")
    idem = _idempotency_key(f"seller.refunds.issue.{order_id}", idempotency_key)
    if idem and idem in state.idempotency:
        return state.idempotency[idem]
    refunded_amount = int(order.get("refunded_amount_inr") or 0)
    remaining_amount = int(order.get("amount_inr") or 0) - refunded_amount
    if amount_inr <= 0:
        raise ValueError("Refund amount must be positive.")
    if amount_inr > remaining_amount:
        raise ValueError("Refund amount exceeds the remaining order amount.")
    payment_id = str((order.get("payment") or {}).get("payment_id") or "")
    if not payment_id:
        raise ValueError("Order payment is unavailable for refund.")
    refund = payment_adapter.refund(
        idempotency_key=idempotency_key,
        payment_id=payment_id,
        amount_inr=amount_inr,
        mode=payload.get("payment_mode") or "success",
    )
    new_refunded_amount = refunded_amount + amount_inr
    updated_order = {
        **order,
        "status": "cancelled" if new_refunded_amount == int(order["amount_inr"]) else order["status"],
        "refunded_amount_inr": new_refunded_amount,
        "refund_status": "refunded" if new_refunded_amount == int(order["amount_inr"]) else "partially_refunded",
        "last_refund": refund,
        "updated_at": _utcnow(),
    }
    state.orders[order_id] = updated_order
    response = {"refund": refund, "order": updated_order}
    _remember(state, idem, response)
    save_state(state)
    return response
