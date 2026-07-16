"""File-backed local commerce exchange for the AgentGuard demo."""
from __future__ import annotations

import json
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


def create_item(payload: dict[str, Any], *, idempotency_key: Optional[str] = None) -> dict[str, Any]:
    state = load_state()
    idem = _idempotency_key("seller.items.create", idempotency_key)
    if idem and idem in state.idempotency:
        return state.idempotency[idem]

    item_id = _new_id("item")
    quantity = int(payload.get("inventory") or payload.get("quantity") or 0)
    item = {
        "item_id": item_id,
        "version": 1,
        "status": "draft",
        "seller_id": payload.get("seller_id") or payload.get("wallet_address") or "demo-seller",
        "title": payload.get("title") or payload.get("name") or "Demo item",
        "description": payload.get("description") or "Simulated local commerce item",
        "price_inr": int(payload.get("price_inr") or payload.get("amount_inr") or 0),
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
        **{key: value for key, value in payload.items() if key in {"title", "description", "price_inr"}},
        "version": int(item["version"]) + 1,
        "updated_at": _utcnow(),
    }
    if "inventory" in payload:
        state.inventory[item_id] = int(payload["inventory"])
    state.items[item_id] = updated
    save_state(state)
    return {"item": updated, "inventory": state.inventory.get(item_id, 0), "message_id": _new_id("msg")}


def cleanup_test_artifacts(*, explicit_order_ids: Optional[set[str]] = None) -> dict[str, int]:
    """Remove deterministic local test fixtures without touching operator-created listings."""
    state = load_state()
    fixture_descriptions = {
        "Shared local commerce item for AgentGuard two-sided proof.",
        "Local Samantha order-lifecycle fixture",
        "Fresh local Samantha checkout fixture",
    }
    item_ids = {
        item_id
        for item_id, item in state.items.items()
        if str(item.get("title") or "").startswith(
            ("Token Nxt proof SKU ", "Matrix ", "Evening Ragi Flour")
        )
        or str(item.get("description") or "") in fixture_descriptions
        or str(item.get("seller_id") or "").startswith("seller-ag-")
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
    for issue_id in issue_ids:
        state.issues.pop(issue_id, None)
    for remedy_id in remedy_ids:
        state.remedies.pop(remedy_id, None)

    artifact_ids = item_ids | order_ids | issue_ids | remedy_ids
    for key, value in list(state.idempotency.items()):
        serialized = json.dumps(value, sort_keys=True)
        if any(artifact_id in serialized for artifact_id in artifact_ids):
            state.idempotency.pop(key, None)

    save_state(state)
    return {
        "items": len(item_ids),
        "orders": len(order_ids),
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


def search_items(query: Optional[str] = None) -> dict[str, Any]:
    state = load_state()
    rows = [
        {**item, "inventory": state.inventory.get(item_id, 0)}
        for item_id, item in state.items.items()
        if item.get("status") == "published"
    ]
    if query:
        lowered = query.lower()
        rows = [item for item in rows if lowered in str(item.get("title", "")).lower()]
    return {"items": rows, "count": len(rows)}


def get_item(item_id: str) -> dict[str, Any]:
    state = load_state()
    item = state.items.get(item_id)
    if not item:
        raise KeyError(f"Unknown item: {item_id}")
    return {"item": item, "inventory": state.inventory.get(item_id, 0)}


def create_order(payload: dict[str, Any], *, idempotency_key: Optional[str] = None) -> dict[str, Any]:
    state = load_state()
    idem = _idempotency_key("buyer.orders.create", idempotency_key)
    if idem and idem in state.idempotency:
        return state.idempotency[idem]

    item_id = payload.get("item_id")
    quantity = int(payload.get("quantity") or 1)
    item = state.items.get(item_id) if item_id else None
    # Buyer local-cart / Samantha checkout uses mock SKUs outside commerce_demo.
    # Still run AgentGuard-backed simulated payment when amount_inr is present.
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
            "buyer_id": payload.get("buyer_id") or payload.get("wallet_address") or "demo-buyer",
            "seller_id": payload.get("seller_id") or "demo-seller",
            "item_id": item_id or "local-cart",
            "item_version": 1,
            "quantity": quantity,
            "amount_inr": amount_inr,
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
    order = {
        "order_id": order_id,
        "transaction_id": transaction_id,
        "message_id": _new_id("msg"),
        "buyer_id": payload.get("buyer_id") or payload.get("wallet_address") or "demo-buyer",
        "seller_id": item.get("seller_id") or "demo-seller",
        "item_id": item_id,
        "item_version": item["version"],
        "quantity": quantity,
        "amount_inr": amount_inr,
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


def transition_order(order_id: str, status: str, *, idempotency_key: Optional[str] = None) -> dict[str, Any]:
    state = load_state()
    idem = _idempotency_key(f"seller.orders.transition.{order_id}.{status}", idempotency_key)
    if idem and idem in state.idempotency:
        return state.idempotency[idem]
    order = state.orders.get(order_id)
    if not order:
        raise KeyError(f"Unknown order: {order_id}")
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
    response = {"remedy": remedy, "message_id": _new_id("msg")}
    _remember(state, idem, response)
    save_state(state)
    return response


def publish_item_from_payload(payload: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
    item_id = payload.get("item_id")
    if not item_id:
        created = create_item(payload, idempotency_key=f"{idempotency_key}:create")
        item_id = created["item"]["item_id"]
    return publish_item(str(item_id), idempotency_key=idempotency_key)


def create_order_from_payload(payload: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
    return create_order(payload, idempotency_key=idempotency_key)


def transition_order_from_payload(action: str, payload: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
    status = {
        "seller.order.accept": "accepted",
        "seller.order.reject": "rejected",
        "seller.fulfilment.commit": "fulfilled",
    }[action]
    return transition_order(str(payload["order_id"]), status, idempotency_key=idempotency_key)


def issue_from_payload(action: str, payload: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
    if action == "buyer.order.cancel":
        return transition_order(str(payload["order_id"]), "cancelled", idempotency_key=idempotency_key)
    return create_issue(str(payload["order_id"]), payload, idempotency_key=idempotency_key)


def remedy_from_payload(payload: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
    return propose_remedy(str(payload["issue_id"]), payload, idempotency_key=idempotency_key)


def refund_from_payload(payload: dict[str, Any], *, amount_inr: int, idempotency_key: str) -> dict[str, Any]:
    payment_id = payload.get("payment_id") or payload.get("reference_payment_id") or "demo-payment"
    refund = payment_adapter.refund(
        idempotency_key=idempotency_key,
        payment_id=str(payment_id),
        amount_inr=amount_inr,
        mode=payload.get("payment_mode") or "success",
    )
    return {"refund": refund}
