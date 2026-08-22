"""Server-side ONDC / Beckn BAP adapter (Milestone 9 / P3).

Signing keys stay on the gateway — never in Vite.
Frontends call /api/ondc/* ; PreProd traffic requires ONDC_ENABLED + keys + subscriber.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app import commerce_demo, ondc_store
from app.commerce_compat import CommerceCompatibilityAdapter
from app.commerce_v1 import CommerceConflict, CommerceNotFound, CommerceV1, CommerceValidation
from app.ondc_crypto import (
    create_authorization_header,
    load_ed25519_private_pem,
    minify_json,
    verify_authorization_header,
)
from app.persistence.connection import live_connection_pool
from app.persistence.ondc_repository import (
    CorrelationMismatch,
    EnvelopeCommitmentMismatch,
    ONDCRepository,
    persist_callback_before_ack,
)
from app.persistence.transaction import UnitOfWork
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ondc"])

PREPROD_GATEWAY = "https://preprod.gateway.ondc.org/search"
PREPROD_LOOKUP = "https://preprod.registry.ondc.org/v2.0/lookup"
DEFAULT_CITY = "std:080"
DEFAULT_DOMAIN = "ONDC:RET10"
CORE_VERSION = "1.2.0"
LOGISTICS_DOMAIN = "ONDC:LOG10"
LOGISTICS_CORE_VERSION = "1.2.5"
_LOGISTICS_LIFECYCLE_CALLBACKS = {
    "on_init",
    "on_confirm",
    "on_update",
    "on_status",
    "on_track",
}
_IGM_ACTIONS = frozenset({"issue", "issue_status"})
_IGM_CALLBACKS = frozenset({"on_issue", "on_issue_status"})
_RETAIL_LIFECYCLE_CALLBACKS = frozenset(
    {
        "on_select",
        "on_init",
        "on_confirm",
        "on_status",
        "on_track",
        "on_cancel",
        "on_update",
    }
)
_RETAIL_SIGNED_CALLBACKS = _IGM_CALLBACKS | _RETAIL_LIFECYCLE_CALLBACKS
# Workbench mock BPP mints a new callback message_id. Correlate by transaction
# outbox, ACK, persist the callback message_id, and log the mismatch. Our BPP
# still echoes the request message_id on outbound on_confirm / on_issue.
_INBOUND_CALLBACK_REQUESTS = {
    "on_select": ("select",),
    "on_init": ("init",),
    "on_confirm": ("confirm",),
    "on_status": ("status", "confirm"),
    "on_track": ("track",),
    "on_update": ("update",),
    "on_cancel": ("cancel",),
    "on_issue": ("issue",),
    "on_issue_status": ("issue_status", "issue"),
}
_IGM_REASON_CATEGORY = {
    "fulfillment": "FULFILLMENT",
    "fulfilment": "FULFILLMENT",
    "payment": "PAYMENT",
    "cancellation": "ORDER",
    "post delivery": "FULFILLMENT",
    "post_delivery": "FULFILLMENT",
    "other": "OTHER",
    "buyer_support": "OTHER",
}
_IGM_COMPLAINANT_ACTIONS = frozenset({"OPEN", "CLOSE", "ESCALATE"})
_IGM_SUB_CATEGORY = {
    "FULFILLMENT": "FLM02",
    "ITEM": "ITM02",
    "ORDER": "ORD01",
    "PAYMENT": "PMT01",
}
_LOGISTICS_STATE_TARGETS = {
    "Pending": "preparing",
    "Searching-for-Agent": "preparing",
    "Agent-assigned": "preparing",
    "Order-picked-up": "shipped",
    "Out-for-delivery": "shipped",
    "Order-delivered": "delivered",
    "Delivered": "delivered",
    "Cancelled": "cancelled",
}
_RETAIL_STATE_TARGETS = {
    "Accepted": "confirmed",
    "In-progress": "preparing",
    "Packed": "preparing",
    "Agent-assigned": "preparing",
    "Order-picked-up": "shipped",
    "Out-for-delivery": "shipped",
    "Order-delivered": "delivered",
    "Completed": "delivered",
    "Cancelled": "cancelled",
}
_RETAIL_RETURN_TARGETS = {
    "Return_Initiated": "requested",
    "Return_Approved": "approved",
    "Return_Picked": "in_transit",
    "Return_Packed": "in_transit",
    "Return_Delivered": "received",
    "Return_Received": "received",
    "Liquidated": "refund_pending",
    "Refunded": "completed",
}


def _tag_values(tags: Any, code: str) -> dict[str, str]:
    if not isinstance(tags, list):
        return {}
    for tag in tags:
        if not isinstance(tag, dict) or tag.get("code") != code:
            continue
        values: dict[str, str] = {}
        for item in tag.get("list") or []:
            if isinstance(item, dict) and item.get("code") and item.get("value") is not None:
                values[str(item["code"])] = str(item["value"])
        return values
    return {}


def _logistics_fulfillments(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    order = (envelope.get("message") or {}).get("order") or {}
    fulfillments = order.get("fulfillments") or []
    return [item for item in fulfillments if isinstance(item, dict)]


def _on_init_conformance(envelope: dict[str, Any]) -> tuple[bool, str]:
    context = envelope.get("context") or {}
    if context.get("domain") != LOGISTICS_DOMAIN:
        return False, "callback domain is not ONDC:LOG10"
    if context.get("core_version") != LOGISTICS_CORE_VERSION:
        return False, "callback core_version is not 1.2.5"
    if context.get("action") != "on_init":
        return False, "a signed on_init callback is required"
    fulfillments = _logistics_fulfillments(envelope)
    delivery = [item for item in fulfillments if item.get("type") == "Delivery"]
    if not delivery:
        return False, "on_init requires a Delivery fulfillment"
    if len(delivery) != len(fulfillments):
        return False, "initial LOG10 scope allows Delivery fulfillments only"
    for fulfillment in delivery:
        rider = _tag_values(fulfillment.get("tags"), "rider_check")
        if rider.get("inline_check_for_rider") != "yes":
            return False, "Immediate Delivery requires rider_check/inline_check_for_rider=yes"
    return True, ""


def _contains_legal_terms(envelope: dict[str, Any]) -> bool:
    order = (envelope.get("message") or {}).get("order") or {}
    tag_lists = [order.get("tags")]
    tag_lists.extend(item.get("tags") for item in _logistics_fulfillments(envelope))
    for tags in tag_lists:
        if not isinstance(tags, list):
            continue
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            if tag.get("code") in {"bap_terms", "bpp_terms"}:
                return True
            if any(
                isinstance(item, dict) and item.get("code") == "accept_bpp_terms"
                for item in tag.get("list") or []
            ):
                return True
    return False


def _build_logistics_confirm_message(
    on_init: dict[str, Any], requested: dict[str, Any]
) -> dict[str, Any]:
    compliant, reason = _on_init_conformance(on_init)
    if not compliant:
        raise HTTPException(status_code=409, detail=reason)
    if _contains_legal_terms(on_init) or _contains_legal_terms(requested):
        raise HTTPException(
            status_code=409,
            detail="accept_bpp_terms requires explicit operator authority",
        )
    signed_order = deepcopy((on_init.get("message") or {}).get("order") or {})
    requested_order = (requested.get("order") or {}) if isinstance(requested, dict) else {}
    if requested_order.get("id") and requested_order.get("id") != signed_order.get("id"):
        raise HTTPException(
            status_code=409, detail="confirm order does not match signed on_init"
        )
    signed_order.pop("tags", None)
    requested_fulfillments = {
        item.get("id"): item
        for item in requested_order.get("fulfillments") or []
        if isinstance(item, dict) and item.get("id")
    }
    for fulfillment in signed_order.get("fulfillments") or []:
        if not isinstance(fulfillment, dict) or fulfillment.get("type") != "Delivery":
            continue
        requested_fulfillment = requested_fulfillments.get(fulfillment.get("id")) or {}
        linked_order = _tag_values(requested_fulfillment.get("tags"), "linked_order")
        if not linked_order.get("id") or not linked_order.get("prep_time"):
            raise HTTPException(
                status_code=409,
                detail="confirm requires linked_order id/prep_time",
            )
        instructions = ((requested_fulfillment.get("start") or {}).get("instructions") or {})
        if not instructions.get("code") or not instructions.get("short_desc"):
            raise HTTPException(
                status_code=409,
                detail="ready_to_ship=yes requires pickup instruction code/short_desc",
            )
        start = fulfillment.get("start")
        if not isinstance(start, dict):
            raise HTTPException(
                status_code=409,
                detail="signed on_init lacks a valid pickup start",
            )
        start["instructions"] = {
            key: str(instructions[key])
            for key in ("code", "short_desc", "long_desc")
            if instructions.get(key)
        }
        fulfillment["tags"] = [
            {
                "code": "linked_order",
                "list": [
                    {"code": "id", "value": linked_order["id"]},
                    {"code": "prep_time", "value": linked_order["prep_time"]},
                ],
            },
            {
                "code": "state",
                "list": [{"code": "ready_to_ship", "value": "yes"}],
            },
            {
                "code": "rto_action",
                "list": [{"code": "return_to_origin", "value": "no"}],
            },
        ]
    return {"order": signed_order}


def _on_init_matches_binding(
    envelope: dict[str, Any], logistics: dict[str, Any]
) -> tuple[bool, str]:
    order = (envelope.get("message") or {}).get("order") or {}
    provider = order.get("provider") or {}
    if provider.get("id") != logistics.get("provider_id"):
        return False, "on_init provider does not match the selected offer"
    selected_item = next(
        (
            item
            for item in order.get("items") or []
            if isinstance(item, dict) and item.get("id") == logistics.get("item_id")
        ),
        None,
    )
    if not selected_item or selected_item.get("fulfillment_id") != logistics.get(
        "fulfillment_id"
    ):
        return False, "on_init item does not match the selected offer"
    quote_price = (order.get("quote") or {}).get("price") or {}
    selected_price = logistics.get("price") or {}
    try:
        price_matches = (
            quote_price.get("currency") == selected_price.get("currency")
            and Decimal(str(quote_price.get("value")))
            == Decimal(str(selected_price.get("value")))
        )
    except (InvalidOperation, TypeError):
        price_matches = False
    if not price_matches:
        return False, "on_init quote does not match the selected offer"
    return True, ""


def _provider_state(fulfillment: dict[str, Any]) -> str:
    state = fulfillment.get("state")
    if isinstance(state, str):
        return state
    if not isinstance(state, dict):
        return ""
    descriptor = state.get("descriptor") or {}
    return str(descriptor.get("code") or state.get("code") or "")


def _normalize_logistics_callback(record: dict[str, Any]) -> dict[str, Any]:
    envelope = record.get("envelope") or {}
    context = envelope.get("context") or {}
    message = envelope.get("message") or {}
    order = message.get("order") or {}
    fulfillments = _logistics_fulfillments(envelope)
    fulfillment = fulfillments[0] if fulfillments else {}
    provider_status = _provider_state(fulfillment)
    state_descriptor = (
        (fulfillment.get("state") or {}).get("descriptor") or {}
        if isinstance(fulfillment.get("state"), dict)
        else {}
    )
    tracking = message.get("tracking") or fulfillment.get("tracking") or {}
    if not isinstance(tracking, dict):
        tracking = {}
    provider = order.get("provider") or {}
    provider_name = (
        (provider.get("descriptor") or {}).get("name")
        if isinstance(provider, dict)
        else None
    )
    tracking_id = (
        tracking.get("id")
        or fulfillment.get("tracking_id")
        or fulfillment.get("@ondc/org/awb_no")
    )
    raw_tracking_url = str(tracking.get("url") or "").strip()
    tracking_url = raw_tracking_url if raw_tracking_url.startswith("https://") else None
    action = str(record.get("action") or context.get("action") or "")
    target_status = (
        _LOGISTICS_STATE_TARGETS.get(provider_status)
        if action in {"on_update", "on_status"}
        else None
    )
    review_reason = ""
    conformance: dict[str, Any] | None = None
    status_message = str(
        state_descriptor.get("short_desc")
        or state_descriptor.get("name")
        or tracking.get("status")
        or ""
    )
    if action == "on_init":
        compliant, reason = _on_init_conformance(envelope)
        conformance = {
            "status": "accepted" if compliant else "rejected",
            "reason": reason or "ONDC LOG10 1.2.5 Immediate Delivery checks passed",
            "message_id": str(record.get("message_id") or ""),
        }
        if not compliant:
            review_reason = reason
            status_message = f"Delivery provider response rejected: {reason}"
        else:
            status_message = "Delivery provider passed ONDC Immediate Delivery checks."
    elif action in {"on_update", "on_status"} and not target_status:
        review_reason = (
            f"unknown LOG10 fulfillment state: {provider_status or 'missing'}"
        )
        status_message = status_message or review_reason
    if raw_tracking_url and tracking_url is None and not review_reason:
        review_reason = "tracking URL must use HTTPS"
        status_message = status_message or review_reason

    return {
        "action": action,
        "message_id": str(record.get("message_id") or context.get("message_id") or ""),
        "bpp_id": str(record.get("subscriber_id") or context.get("bpp_id") or ""),
        "provider_timestamp": context.get("timestamp"),
        "provider_status": provider_status,
        "provider_name": provider_name,
        "target_status": target_status,
        "lsp_order_id": order.get("id") if action == "on_confirm" else None,
        "tracking_id": tracking_id,
        "tracking_url": tracking_url,
        "tracking_location": tracking.get("location"),
        "status_message": status_message,
        "review_reason": review_reason,
        "conformance": conformance,
    }


def _retail_fulfillments(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    order = (envelope.get("message") or {}).get("order") or {}
    fulfillments = order.get("fulfillments") or []
    return [item for item in fulfillments if isinstance(item, dict)]


def _retail_order_state(envelope: dict[str, Any]) -> str:
    order = (envelope.get("message") or {}).get("order") or {}
    if not isinstance(order, dict):
        return ""
    return str(order.get("state") or "").strip()


def _retail_fulfillment_state(envelope: dict[str, Any]) -> str:
    fulfillments = _retail_fulfillments(envelope)
    if not fulfillments:
        return ""
    return _provider_state(fulfillments[0])


def _retail_price_paise(price: Any) -> int | None:
    if not isinstance(price, dict):
        return None
    value = price.get("value")
    if value in {None, ""}:
        return None
    try:
        return int((Decimal(str(value)) * 100).quantize(Decimal("1")))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _retail_return_code(envelope: dict[str, Any]) -> str:
    order = (envelope.get("message") or {}).get("order") or {}
    if not isinstance(order, dict):
        return ""
    for fulfillment in _retail_fulfillments(envelope):
        if str(fulfillment.get("type") or "").lower() != "return":
            continue
        code = _provider_state(fulfillment)
        if code:
            return code
    tags = _tag_values(order.get("tags"), "return_request")
    return str(tags.get("code") or tags.get("status") or "").strip()


def _retail_settlement(envelope: dict[str, Any]) -> dict[str, Any] | None:
    order = (envelope.get("message") or {}).get("order") or {}
    payment = order.get("payment") if isinstance(order, dict) else {}
    if not isinstance(payment, dict):
        return None
    details = (
        payment.get("@ondc/org/settlement_details")
        or payment.get("settlement_details")
        or payment.get("settlement")
    )
    if details is None or details == "" or details == []:
        return None
    if isinstance(details, list):
        details = details[0] if details and isinstance(details[0], dict) else {"items": details}
    if not isinstance(details, dict):
        return {"raw": details}
    return dict(details)


def _normalize_retail_callback(record: dict[str, Any]) -> dict[str, Any]:
    envelope = record.get("envelope") or {}
    context = envelope.get("context") or {}
    message = envelope.get("message") or {}
    order = message.get("order") if isinstance(message.get("order"), dict) else {}
    tracking = message.get("tracking") or {}
    if not isinstance(tracking, dict):
        tracking = {}
    action = str(record.get("action") or context.get("action") or "")
    fulfillments = _retail_fulfillments(envelope)
    fulfillment = fulfillments[0] if fulfillments else {}
    order_state = _retail_order_state(envelope)
    fulfillment_state = _provider_state(fulfillment) if fulfillment else ""
    provider_status = fulfillment_state or order_state
    return_code = _retail_return_code(envelope)
    settlement = _retail_settlement(envelope)
    tracking_id = (
        tracking.get("id")
        or fulfillment.get("tracking_id")
        or fulfillment.get("@ondc/org/awb_no")
    )
    raw_tracking_url = str(tracking.get("url") or "").strip()
    tracking_url = raw_tracking_url if raw_tracking_url.startswith("https://") else None
    target_status = None
    if action in {"on_confirm", "on_status", "on_cancel"}:
        target_status = _RETAIL_STATE_TARGETS.get(provider_status)
        if action == "on_cancel":
            target_status = "cancelled"
    review_reason = ""
    status_message = str(
        tracking.get("status") or order_state or fulfillment_state or return_code or ""
    )
    if action in {"on_status", "on_cancel"} and not target_status:
        review_reason = (
            f"unknown RET10 order state: {provider_status or 'missing'}"
        )
        status_message = status_message or review_reason
    if raw_tracking_url and tracking_url is None and not review_reason:
        review_reason = "tracking URL must use HTTPS"
        status_message = status_message or review_reason
    refund_amount = None
    if settlement is not None:
        refund_amount = _retail_price_paise(
            settlement.get("settlement_amount")
            or {"value": settlement.get("amount") or settlement.get("value")}
        )
        if refund_amount is None:
            refund_amount = _retail_price_paise((order.get("quote") or {}).get("price") or {})
        if refund_amount is None:
            refund_amount = _retail_price_paise(order.get("payment") or {})
    elif action == "on_update" and return_code in {"Liquidated", "Refunded"}:
        refund_amount = _retail_price_paise((order.get("quote") or {}).get("price") or {})

    return {
        "action": action,
        "message_id": str(record.get("message_id") or context.get("message_id") or ""),
        "bpp_id": str(record.get("subscriber_id") or context.get("bpp_id") or ""),
        "provider_timestamp": context.get("timestamp"),
        "provider_status": provider_status,
        "target_status": target_status,
        "protocol_order_id": order.get("id") or message.get("order_id"),
        "quote": order.get("quote") if isinstance(order.get("quote"), dict) else None,
        "billing": order.get("billing") if isinstance(order.get("billing"), dict) else None,
        "payment": order.get("payment") if isinstance(order.get("payment"), dict) else None,
        "tracking_id": tracking_id or None,
        "tracking_url": tracking_url,
        "status_message": status_message,
        "review_reason": review_reason,
        "return_status": _RETAIL_RETURN_TARGETS.get(return_code),
        "return_reason": return_code or None,
        "refund_amount_paise": refund_amount,
        "settlement": settlement,
    }


async def _stage_outbox_before_dispatch(
    request: Request,
    entry: dict[str, Any],
    *,
    destination: str,
) -> dict[str, Any]:
    """Persist a delivery intent, then claim it before any network effect."""
    pool = getattr(request.app.state, "persistence_pool", None)
    if pool is None:
        ondc_store.append_outbox(entry)
        return {
            "backend": "local_file",
            "public_id": entry["id"],
            "entry": entry,
        }

    context = entry["payload"]["context"]
    correlation_id = (
        request.headers.get("X-Correlation-ID") or str(context["transaction_id"])
    ).strip()
    async with UnitOfWork(pool) as unit_of_work:
        repository = ONDCRepository(unit_of_work)
        prior_records = await repository.list_for_transaction(
            "outbox",
            str(context["transaction_id"]),
            action=str(entry["action"]),
            limit=100,
        )
        for prior in prior_records:
            if (
                prior["message_id"] != str(context["message_id"])
                or prior["destination"] != destination
            ):
                continue
            prior_envelope = prior.get("envelope") or {}
            candidate_envelope = entry["payload"]
            prior_comparable = json.loads(json.dumps(prior_envelope))
            candidate_comparable = json.loads(json.dumps(candidate_envelope))
            (prior_comparable.get("context") or {}).pop("timestamp", None)
            (candidate_comparable.get("context") or {}).pop("timestamp", None)
            if prior_comparable == candidate_comparable:
                entry["payload"] = prior_envelope
                context = prior_envelope["context"]
            break
        created, persisted = await repository.enqueue_outbox(
            subscriber_id=str(context.get("bap_id") or _subscriber_id() or ""),
            transaction_id=str(context["transaction_id"]),
            message_id=str(context["message_id"]),
            action=str(entry["action"]),
            destination=destination,
            raw_envelope=entry["payload"],
            redacted_payload={"status": "queued"},
            correlation_id=correlation_id,
        )
        if persisted["state"] == "delivered":
            return {
                "backend": "postgres",
                "public_id": f"pg_out_{persisted['outbox_id']}",
                "persisted": persisted,
                "created": created,
                "already_delivered": True,
                "pool": pool,
            }
        claimed = await repository.claim_outbox_record(
            persisted["outbox_id"],
            worker_id=f"inline:{uuid.uuid4().hex}",
            lease_seconds=30,
        )
    if claimed is None:
        raise HTTPException(
            status_code=409,
            detail="ONDC delivery is already leased; retry with the same message_id",
        )
    return {
        "backend": "postgres",
        "public_id": f"pg_out_{claimed['outbox_id']}",
        "persisted": claimed,
        "created": created,
        "already_delivered": False,
        "pool": pool,
    }


async def _complete_outbox_delivery(
    staged: dict[str, Any],
    *,
    delivered: bool,
    error: str | None = None,
    file_updates: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Finish the active lease or make the durable delivery retryable."""
    if staged["backend"] == "local_file":
        ondc_store.update_outbox(staged["entry"]["id"], **(file_updates or {}))
        return None

    claimed = staged["persisted"]
    async with UnitOfWork(staged["pool"]) as unit_of_work:
        repository = ONDCRepository(unit_of_work)
        if delivered:
            return await repository.mark_delivered(
                "outbox", claimed["outbox_id"], claimed["lease_token"]
            )
        return await repository.schedule_retry(
            "outbox",
            claimed["outbox_id"],
            claimed["lease_token"],
            error=(error or "ONDC delivery failed")[:2000],
        )


def _buyer_paths() -> dict[str, Path]:
    from app.ondc_onboard_routes import _role_paths

    return _role_paths("buyer")


def _lbnp_paths() -> dict[str, Path]:
    from app.ondc_onboard_routes import _role_paths

    return _role_paths("lbnp")


def _key_id_from_paths(paths: dict[str, Path]) -> Optional[str]:
    if paths["uk_id"].is_file():
        value = paths["uk_id"].read_text(encoding="utf-8").strip()
        if value:
            return value
    if paths["meta"].is_file():
        try:
            meta = json.loads(paths["meta"].read_text(encoding="utf-8"))
            uk_id = meta.get("unique_key_id")
            if uk_id:
                return str(uk_id).strip()
        except json.JSONDecodeError:
            pass
    return None


def _unique_key_id(role: str = "buyer") -> Optional[str]:
    if role == "buyer" and getattr(settings, "ondc_unique_key_id", None):
        return str(settings.ondc_unique_key_id).strip() or None
    return _key_id_from_paths(_lbnp_paths() if role == "lbnp" else _buyer_paths())


def _buyer_uk_id() -> Optional[str]:
    return _unique_key_id("buyer")


def _authorization_subscriber_id(header: str) -> str:
    marker = 'keyId="'
    start = header.find(marker)
    if start < 0:
        return ""
    rest = header[start + len(marker) :]
    end = rest.find("|")
    if end < 0:
        end = rest.find('"')
    return rest[:end].strip() if end >= 0 else ""


def _callback_subscriber_id(ctx: dict[str, Any], request: Request) -> str:
    for key in ("bpp_id", "bap_id", "subscriber_id", "subscriberID"):
        value = str(ctx.get(key) or "").strip()
        if value:
            return value
    return _authorization_subscriber_id(request.headers.get("Authorization") or "")


def _subscriber_id(role: str = "buyer") -> Optional[str]:
    if role == "lbnp":
        from app.ondc_onboard_routes import _subscriber_id as onboard_subscriber_id

        return onboard_subscriber_id("lbnp")
    return (
        getattr(settings, "ondc_subscriber_id", None)
        or getattr(settings, "ondc_bap_id", None)
        or getattr(settings, "ondc_buyer_subscriber_id", None)
    )


def _bap_uri(role: str = "buyer") -> Optional[str]:
    configured = getattr(settings, "ondc_bap_uri", None) if role == "buyer" else None
    if configured:
        return configured.rstrip("/")
    sid = _subscriber_id(role)
    if sid and "." in sid:
        return f"https://{sid}/ondc"
    return None


def _gateway_url() -> str:
    return (getattr(settings, "ondc_gateway_url", None) or PREPROD_GATEWAY).strip()


def _registry_url() -> str:
    return (getattr(settings, "ondc_registry_url", None) or PREPROD_LOOKUP).strip()


def _signing_role_for_envelope(envelope: dict[str, Any]) -> str:
    context = envelope.get("context") or {}
    return "lbnp" if context.get("domain") == LOGISTICS_DOMAIN else "buyer"


def _signing_pem_path(role: str = "buyer") -> Optional[Path]:
    configured = (
        getattr(settings, "ondc_signing_private_key_path", None)
        if role == "buyer"
        else None
    )
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return path
    paths = _lbnp_paths() if role == "lbnp" else _buyer_paths()
    if paths["signing_pem"].is_file():
        return paths["signing_pem"]
    return None


def _load_signing_key(role: str = "buyer"):
    path = _signing_pem_path(role)
    if path is None:
        raise HTTPException(
            status_code=503,
            detail=f"ONDC {role} signing key missing",
        )
    return load_ed25519_private_pem(path.read_bytes())


def _ondc_configured(role: str = "buyer") -> bool:
    return bool(
        getattr(settings, "ondc_enabled", False)
        and _subscriber_id(role)
        and _bap_uri(role)
        and _unique_key_id(role)
        and _signing_pem_path(role) is not None
    )


def _status_payload() -> dict[str, Any]:
    pem = _signing_pem_path()
    return {
        "enabled": bool(getattr(settings, "ondc_enabled", False)),
        "configured": _ondc_configured(),
        "subscriber_id": _subscriber_id(),
        "bap_id": getattr(settings, "ondc_bap_id", None) or _subscriber_id(),
        "bap_uri": _bap_uri(),
        "gateway_url": _gateway_url(),
        "registry_url": _registry_url(),
        "signing_key_present": pem is not None,
        "unique_key_id": _buyer_uk_id(),
        "registry_env": getattr(settings, "ondc_registry_env", "preprod"),
        "outbox_depth": len(ondc_store.list_outbox(limit=500)),
        "inbox_depth": len(ondc_store.list_inbox(limit=500)),
        "note": (
            "PreProd: signed lookup + search + select/init/confirm when enabled+configured. "
            "Do not flip VITE_COMMERCE_DEMO_MODE without commerce_demo_mode_gate evidence."
        ),
    }


class SearchBody(BaseModel):
    intent: dict[str, Any] = Field(default_factory=dict)
    message_id: Optional[str] = None
    transaction_id: Optional[str] = None
    city: Optional[str] = None
    domain: Optional[str] = None
    query: Optional[str] = None
    include_configured_bpp: bool = False


class DeadLetterRecoveryBody(BaseModel):
    event_commitment: str = Field(min_length=64, max_length=64)


class OutboxDrainBody(BaseModel):
    worker_id: str = Field(default="ondc-recovery", min_length=1, max_length=128)
    limit: int = Field(default=10, ge=1, le=100)
    lease_seconds: int = Field(default=30, ge=1, le=300)


class OrderActionBody(BaseModel):
    """select / init / confirm / track / status / cancel / update."""

    order: dict[str, Any] = Field(default_factory=dict)
    order_id: Optional[str] = None
    commerce_order_id: Optional[str] = None
    message_id: Optional[str] = None
    transaction_id: Optional[str] = None
    bpp_id: Optional[str] = None
    bpp_uri: Optional[str] = None
    city: Optional[str] = None
    domain: Optional[str] = None


class IgmIssueBody(BaseModel):
    """Retail IGM issue / issue_status bound to a local or protocol-confirmed order."""

    issue_id: Optional[str] = None
    order_id: Optional[str] = None
    issue_type: str = "ISSUE"
    category: Optional[str] = None
    complainant_action: Optional[str] = None
    message_id: Optional[str] = None
    transaction_id: Optional[str] = None
    bpp_id: Optional[str] = None
    bpp_uri: Optional[str] = None
    city: Optional[str] = None
    domain: Optional[str] = None


class LogisticsActionBody(BaseModel):
    """Bounded LOG10 P2P forward-lifecycle message."""

    message: dict[str, Any] = Field(default_factory=dict)
    message_id: Optional[str] = None
    transaction_id: Optional[str] = None
    bpp_id: Optional[str] = None
    bpp_uri: Optional[str] = None
    city: Optional[str] = None


class ConfirmBody(OrderActionBody):
    """Backward-compatible alias for confirm."""


class LookupBody(BaseModel):
    subscriber_id: Optional[str] = None
    domain: Optional[str] = None
    ukId: Optional[str] = None
    type: Optional[str] = None
    country: Optional[str] = "IND"


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


def _build_search_envelope(body: SearchBody, *, role: str = "buyer") -> dict[str, Any]:
    message_id = body.message_id or str(uuid.uuid4())
    transaction_id = body.transaction_id or str(uuid.uuid4())
    intent = dict(body.intent or {})
    if role == "lbnp":
        category_id = str((intent.get("category") or {}).get("id") or "")
        fulfillment_type = str((intent.get("fulfillment") or {}).get("type") or "")
        if category_id != "Immediate Delivery" or fulfillment_type != "Delivery":
            raise HTTPException(
                status_code=422,
                detail=(
                    "LOG10 scope is Immediate Delivery, P2P, forward lifecycle "
                    "with fulfillment.type=Delivery"
                ),
            )
        holidays = (
            (((intent.get("provider") or {}).get("time") or {}).get("schedule") or {})
            .get("holidays")
        )
        try:
            valid_holidays = isinstance(holidays, list) and bool(holidays) and all(
                isinstance(value, str)
                and datetime.strptime(value, "%Y-%m-%d").date()
                > datetime.now(timezone.utc).date()
                for value in holidays
            )
        except ValueError:
            valid_holidays = False
        if not valid_holidays:
            raise HTTPException(
                status_code=422,
                detail=(
                    "LOG10 v1.2.5 requires at least one future-dated "
                    "provider.time.schedule.holidays entry"
                ),
            )
    if body.query and not (intent.get("item") or {}).get("descriptor"):
        intent.setdefault("item", {})
        intent["item"].setdefault("descriptor", {})
        intent["item"]["descriptor"]["name"] = body.query
    if role == "buyer" and "fulfillment" not in intent:
        intent["fulfillment"] = {
            "type": "Delivery",
            "end": {
                "location": {
                    "gps": "12.9715987,77.5945627",
                    "address": {"area_code": "560001"},
                }
            },
        }
    if role == "buyer":
        payment = intent.setdefault("payment", {})
        payment.setdefault("@ondc/org/buyer_app_finder_fee_type", "Percent")
        payment.setdefault("@ondc/org/buyer_app_finder_fee_amount", "0")
    bap_id = (
        getattr(settings, "ondc_bap_id", None) or _subscriber_id()
        if role == "buyer"
        else _subscriber_id("lbnp")
    )
    return {
        "context": {
            "domain": (
                body.domain or DEFAULT_DOMAIN if role == "buyer" else LOGISTICS_DOMAIN
            ),
            "action": "search",
            "country": "IND",
            "city": body.city or DEFAULT_CITY,
            "core_version": (
                CORE_VERSION if role == "buyer" else LOGISTICS_CORE_VERSION
            ),
            "bap_id": bap_id,
            "bap_uri": _bap_uri(role),
            "transaction_id": transaction_id,
            "message_id": message_id,
            "timestamp": _iso_now(),
            "ttl": "PT30S",
        },
        "message": {"intent": intent},
    }


async def _signed_post(
    url: str, payload: dict[str, Any], *, role: str = "buyer"
) -> tuple[int, Any, str]:
    subscriber_id = _subscriber_id(role)
    uk_id = _unique_key_id(role)
    if not subscriber_id or not uk_id:
        raise HTTPException(
            status_code=503, detail="ONDC subscriber_id / unique_key_id missing"
        )
    private_key = _load_signing_key(role)
    body_str = minify_json(payload)
    auth = create_authorization_header(
        body_str,
        subscriber_id=subscriber_id,
        unique_key_id=uk_id,
        private_key=private_key,
    )
    async with httpx.AsyncClient(timeout=45.0) as client:
        resp = await client.post(
            url,
            content=body_str.encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": auth,
            },
        )
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        data = {"raw": resp.text[:2000]}
    return resp.status_code, data, body_str


@router.get("/api/ondc/status")
async def ondc_status(request: Request) -> JSONResponse:
    data = _status_payload()
    pool = getattr(request.app.state, "persistence_pool", None)
    if pool is not None:
        async with UnitOfWork(pool) as unit_of_work:
            repository = ONDCRepository(unit_of_work)
            data["outbox_depth"] = len(
                await repository.list_records("outbox", limit=500)
            )
            data["inbox_depth"] = len(await repository.list_records("inbox", limit=500))
        data["persistence_backend"] = "postgres"
    else:
        data["persistence_backend"] = "local_file"
    return JSONResponse({"success": True, "data": data})


@router.post("/api/ondc/lookup")
async def ondc_lookup(body: LookupBody) -> JSONResponse:
    """Signed PreProd/staging registry lookup."""
    if not getattr(settings, "ondc_enabled", False):
        raise HTTPException(status_code=503, detail="ONDC_ENABLED=false")
    if _signing_pem_path() is None or not _buyer_uk_id():
        raise HTTPException(status_code=503, detail="ONDC Buyer keys / uk_id not ready")
    payload: dict[str, Any] = {
        "subscriber_id": body.subscriber_id or _subscriber_id(),
        "domain": body.domain or DEFAULT_DOMAIN,
        "country": body.country or "IND",
    }
    if body.type:
        payload["type"] = body.type
    if body.ukId:
        payload["ukId"] = body.ukId
    elif not body.subscriber_id or body.subscriber_id == _subscriber_id():
        payload["ukId"] = _buyer_uk_id()
    status, data, _ = await _signed_post(_registry_url(), payload)
    return JSONResponse(
        {
            "success": status < 400,
            "data": {
                "http_status": status,
                "registry_url": _registry_url(),
                "request": payload,
                "response": data,
            },
        },
        status_code=200 if status < 500 else 502,
    )


async def _dispatch_search(
    body: SearchBody, request: Request, *, role: str = "buyer"
) -> JSONResponse:
    """Signed Beckn search → PreProd gateway; persist outbox status."""
    if not _ondc_configured(role):
        raise HTTPException(
            status_code=503,
            detail=(
                f"ONDC {role} adapter not ready. Set ONDC_ENABLED=true, "
                "subscriber/bap_uri, signing PEM + unique_key_id."
            ),
        )
    envelope = _build_search_envelope(body, role=role)
    message_id = envelope["context"]["message_id"]
    transaction_id = envelope["context"]["transaction_id"]
    entry = {
        "id": f"out_{uuid.uuid4().hex[:12]}",
        "action": "search",
        "payload": envelope,
        "created_at": int(time.time()),
        "status": "queued",
        "message_id": message_id,
        "transaction_id": transaction_id,
        "idempotency_key": hashlib.sha256(
            json.dumps(envelope, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:24],
    }
    gateway_url = _gateway_url()
    staged = await _stage_outbox_before_dispatch(
        request, entry, destination=gateway_url
    )
    if staged.get("already_delivered"):
        return JSONResponse(
            {
                "success": True,
                "data": {
                    "queued": False,
                    "dispatched": False,
                    "deduplicated": True,
                    "outbox_id": staged["public_id"],
                    "message_id": message_id,
                    "transaction_id": transaction_id,
                    "gateway_url": gateway_url,
                },
            }
        )
    try:
        status, data, _ = await _signed_post(gateway_url, envelope, role=role)
    except HTTPException:
        await _complete_outbox_delivery(
            staged,
            delivered=False,
            error="signing/config",
            file_updates={"status": "error", "error": "signing/config"},
        )
        raise
    except Exception as exc:  # noqa: BLE001
        await _complete_outbox_delivery(
            staged,
            delivered=False,
            error=str(exc),
            file_updates={"status": "error", "error": str(exc)},
        )
        raise HTTPException(
            status_code=502, detail=f"ONDC gateway dispatch failed: {exc}"
        ) from exc

    ack = None
    if isinstance(data, dict):
        ack = ((data.get("message") or {}).get("ack") or {}).get("status")
    dispatch_status = "sent" if status < 400 else "nack"
    if ack == "NACK":
        dispatch_status = "nack"
    await _complete_outbox_delivery(
        staged,
        delivered=dispatch_status == "sent",
        error=f"gateway returned HTTP {status} ack={ack}",
        file_updates={
            "status": dispatch_status,
            "http_status": status,
            "gateway_response": data,
        },
    )
    direct_bpp: Optional[dict[str, Any]] = None
    if role == "buyer" and body.include_configured_bpp:
        # Portfolio proof: keep normal PreProd fanout, and send the same signed
        # Beckn search to the server-configured BPP. Callers cannot supply a URL.
        # An explicitly blank Render env used to suppress the Settings default,
        # leaving direct_bpp=null while gateway fanout still ACKed. Keep the
        # portfolio Seller route fail-closed and deterministic.
        bpp_uri = str(
            getattr(settings, "ondc_bpp_uri", None)
            or "https://ondcseller.aadharcha.in/ondc"
        ).rstrip("/")
        if bpp_uri:
            direct_target = f"{bpp_uri}/search"
            direct_staged = staged
            if staged["backend"] == "postgres":
                direct_entry = {
                    **entry,
                    "id": f"out_{uuid.uuid4().hex[:12]}",
                }
                direct_staged = await _stage_outbox_before_dispatch(
                    request, direct_entry, destination=direct_target
                )
            try:
                if direct_staged.get("already_delivered"):
                    direct_bpp = {
                        "bpp_uri": bpp_uri,
                        "ok": True,
                        "deduplicated": True,
                        "outbox_id": direct_staged["public_id"],
                    }
                    bpp_status = 200
                    bpp_data = {}
                else:
                    bpp_status, bpp_data, _ = await _signed_post(
                        direct_target, envelope
                    )
                bpp_ack = None
                if isinstance(bpp_data, dict):
                    bpp_ack = ((bpp_data.get("message") or {}).get("ack") or {}).get(
                        "status"
                    )
                bpp_delivered = bpp_status < 400 and bpp_ack != "NACK"
                if not direct_staged.get("already_delivered"):
                    await _complete_outbox_delivery(
                        direct_staged,
                        delivered=bpp_delivered,
                        error=f"configured BPP returned HTTP {bpp_status} ack={bpp_ack}",
                        file_updates={},
                    )
                    direct_bpp = {
                        "bpp_uri": bpp_uri,
                        "http_status": bpp_status,
                        "ack": bpp_ack,
                        "ok": bpp_delivered,
                    }
                    if direct_staged["backend"] == "postgres":
                        direct_bpp["outbox_id"] = direct_staged["public_id"]
                if staged["backend"] == "local_file":
                    ondc_store.update_outbox(
                        entry["id"],
                        direct_bpp_response=bpp_data,
                        direct_bpp_status=bpp_status,
                    )
            except Exception as exc:  # noqa: BLE001
                if direct_staged["backend"] == "postgres" and not direct_staged.get(
                    "already_delivered"
                ):
                    await _complete_outbox_delivery(
                        direct_staged,
                        delivered=False,
                        error=str(exc),
                    )
                direct_bpp = {"bpp_uri": bpp_uri, "ok": False, "error": str(exc)}
    return JSONResponse(
        {
            "success": dispatch_status == "sent",
            "data": {
                "queued": False,
                "dispatched": True,
                "outbox_id": staged["public_id"],
                "message_id": message_id,
                "transaction_id": transaction_id,
                "http_status": status,
                "ack": ack,
                "gateway_url": gateway_url,
                "gateway_response": data,
                "direct_bpp": direct_bpp,
                "note": "Poll GET /api/ondc/catalogs?transaction_id=… for on_search results.",
            },
        }
    )


@router.post("/api/ondc/search")
async def ondc_search(body: SearchBody, request: Request) -> JSONResponse:
    return await _dispatch_search(body, request)


@router.post("/api/ondc/logistics/search")
async def ondc_logistics_search(body: SearchBody, request: Request) -> JSONResponse:
    return await _dispatch_search(body, request, role="lbnp")


async def _resolve_bpp_target(
    request: Request,
    body: OrderActionBody | LogisticsActionBody,
    *,
    transaction_id: str,
    role: str = "buyer",
) -> tuple[str, str]:
    """Resolve bpp_id + bpp_uri from body or prior on_search catalogs."""
    bpp_id = (body.bpp_id or "").strip()
    bpp_uri = (body.bpp_uri or "").rstrip("/")
    if bpp_id and bpp_uri:
        return bpp_id, bpp_uri
    pool = getattr(request.app.state, "persistence_pool", None)
    if pool is None:
        catalogs = ondc_store.catalogs_for_transaction(transaction_id)
    else:
        async with UnitOfWork(pool) as unit_of_work:
            durable_callbacks = await ONDCRepository(unit_of_work).list_for_transaction(
                "inbox", transaction_id, action="on_search"
            )
        catalogs = [
            {
                "bpp_id": row["envelope"].get("context", {}).get("bpp_id"),
                "bpp_uri": row["envelope"].get("context", {}).get("bpp_uri"),
            }
            for row in durable_callbacks
        ]
    for row in catalogs:
        if not bpp_id and row.get("bpp_id"):
            bpp_id = str(row["bpp_id"])
        if not bpp_uri and row.get("bpp_uri"):
            bpp_uri = str(row["bpp_uri"]).rstrip("/")
        if bpp_id and bpp_uri:
            break
    if role == "lbnp" and (not bpp_id or not bpp_uri):
        raise HTTPException(
            status_code=422,
            detail="LOG10 bpp_id and bpp_uri must come from the selected on_search",
        )
    if not bpp_id:
        bpp_id = getattr(settings, "ondc_bpp_id", None) or "ondcseller.aadharcha.in"
    if not bpp_uri:
        bpp_uri = (
            getattr(settings, "ondc_bpp_uri", None) or f"https://{bpp_id}/ondc"
        ).rstrip("/")
    return bpp_id, bpp_uri


async def _maybe_bind_retail_order(
    request: Request,
    body: OrderActionBody | LogisticsActionBody,
    *,
    transaction_id: str,
    bpp_id: str,
    bpp_uri: str,
) -> None:
    commerce_order_id = str(getattr(body, "commerce_order_id", None) or "").strip()
    if not commerce_order_id:
        return
    pool = getattr(request.app.state, "persistence_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=409,
            detail="PostgreSQL CommerceV1 binding is required for RET10 protocol bind",
        )
    try:
        await CommerceV1(pool).bind_retail_protocol(
            order_id=commerce_order_id,
            retail={
                "transaction_id": transaction_id,
                "bpp_id": bpp_id,
                "bpp_uri": bpp_uri,
                "core_version": CORE_VERSION,
                "signature_verified": True,
            },
        )
    except (CommerceNotFound, CommerceConflict, CommerceValidation) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def _dispatch_order_action(
    request: Request,
    action: str,
    body: OrderActionBody | LogisticsActionBody,
    *,
    role: str = "buyer",
) -> JSONResponse:
    """Signed select/init/confirm/track/status/cancel/update → bpp_uri/{action}."""
    allowed_actions = (
        {"select", "init", "confirm", "track", "status", "cancel", "update"}
        if role == "buyer"
        else {"init", "confirm", "update", "status", "track"}
    )
    if action not in allowed_actions:
        raise HTTPException(status_code=400, detail=f"unsupported action: {action}")
    if not _ondc_configured(role):
        raise HTTPException(status_code=503, detail="ONDC adapter not ready.")
    message_id = body.message_id or str(uuid.uuid4())
    transaction_id = body.transaction_id or str(uuid.uuid4())
    bpp_id, bpp_uri = await _resolve_bpp_target(
        request, body, transaction_id=transaction_id, role=role
    )
    if role == "lbnp":
        bound_order = await _require_bound_logistics_order(
            request, transaction_id=transaction_id, bpp_id=bpp_id
        )
        message = dict(body.message)  # type: ignore[union-attr]
        if action == "confirm":
            on_init = await _latest_signed_logistics_on_init(
                request,
                transaction_id=transaction_id,
                bpp_id=bpp_id,
                logistics=dict((bound_order.get("fulfilment") or {}).get("logistics") or {}),
            )
            message = _build_logistics_confirm_message(on_init, message)
        if action in {"init", "confirm", "update"}:
            order = message.get("order")
            fulfillments = (
                order.get("fulfillments") if isinstance(order, dict) else None
            )
            if not isinstance(fulfillments, list) or not fulfillments:
                raise HTTPException(
                    status_code=422,
                    detail=f"LOG10 {action} requires message.order.fulfillments",
                )
            if any(
                not isinstance(item, dict) or item.get("type") != "Delivery"
                for item in fulfillments
            ):
                raise HTTPException(
                    status_code=422,
                    detail="LOG10 initial scope allows Delivery fulfillments only",
                )
        elif not str(message.get("order_id") or "").strip():
            raise HTTPException(
                status_code=422,
                detail=f"LOG10 {action} requires message.order_id",
            )
    elif action in {"track", "status"}:
        order_id = str(
            getattr(body, "order_id", None)
            or ((getattr(body, "order", None) or {}).get("id"))
            or ""
        ).strip()
        if not order_id:
            raise HTTPException(status_code=422, detail=f"{action} requires order_id")
        message = {"order_id": order_id}
    elif action == "cancel":
        order_id = str(
            getattr(body, "order_id", None)
            or ((getattr(body, "order", None) or {}).get("id"))
            or ""
        ).strip()
        if not order_id:
            raise HTTPException(status_code=422, detail="cancel requires order_id")
        reason = str(
            (getattr(body, "order", None) or {}).get("cancellation_reason_id") or "001"
        )
        message = {
            "order_id": order_id,
            "cancellation_reason_id": reason,
        }
        if getattr(body, "order", None):
            message["order"] = body.order
    else:
        message = {"order": body.order or {}}  # type: ignore[union-attr]
    if role == "buyer":
        await _maybe_bind_retail_order(
            request,
            body,
            transaction_id=transaction_id,
            bpp_id=bpp_id,
            bpp_uri=bpp_uri,
        )
    bap_id = (
        getattr(settings, "ondc_bap_id", None) or _subscriber_id()
        if role == "buyer"
        else _subscriber_id("lbnp")
    )
    envelope = {
        "context": {
            "domain": (
                body.domain or DEFAULT_DOMAIN  # type: ignore[union-attr]
                if role == "buyer"
                else LOGISTICS_DOMAIN
            ),
            "action": action,
            "country": "IND",
            "city": body.city or DEFAULT_CITY,
            "core_version": (
                CORE_VERSION if role == "buyer" else LOGISTICS_CORE_VERSION
            ),
            "bap_id": bap_id,
            "bap_uri": _bap_uri(role),
            "bpp_id": bpp_id,
            "bpp_uri": bpp_uri,
            "transaction_id": transaction_id,
            "message_id": message_id,
            "timestamp": _iso_now(),
            "ttl": "PT30S",
        },
        "message": message,
    }
    entry = {
        "id": f"out_{uuid.uuid4().hex[:12]}",
        "action": action,
        "payload": envelope,
        "created_at": int(time.time()),
        "status": "queued",
        "message_id": message_id,
        "transaction_id": transaction_id,
        "bpp_id": bpp_id,
        "bpp_uri": bpp_uri,
    }
    target = f"{bpp_uri}/{action}"
    staged = await _stage_outbox_before_dispatch(request, entry, destination=target)
    if staged.get("already_delivered"):
        return JSONResponse(
            {
                "success": True,
                "data": {
                    "queued": False,
                    "dispatched": False,
                    "deduplicated": True,
                    "outbox_id": staged["public_id"],
                    "message_id": message_id,
                    "transaction_id": transaction_id,
                    "bpp_id": bpp_id,
                    "bpp_uri": bpp_uri,
                    "target": target,
                },
            }
        )
    try:
        status, data, _ = await _signed_post(target, envelope, role=role)
    except HTTPException:
        await _complete_outbox_delivery(
            staged,
            delivered=False,
            error="signing/config",
            file_updates={"status": "error", "error": "signing/config"},
        )
        raise
    except Exception as exc:  # noqa: BLE001
        await _complete_outbox_delivery(
            staged,
            delivered=False,
            error=str(exc),
            file_updates={"status": "error", "error": str(exc)},
        )
        raise HTTPException(
            status_code=502, detail=f"ONDC BPP {action} failed: {exc}"
        ) from exc

    ack = None
    if isinstance(data, dict):
        ack = ((data.get("message") or {}).get("ack") or {}).get("status")
    dispatch_status = "sent" if status < 400 else "nack"
    if ack == "NACK":
        dispatch_status = "nack"
    await _complete_outbox_delivery(
        staged,
        delivered=dispatch_status == "sent",
        error=f"BPP returned HTTP {status} ack={ack}",
        file_updates={
            "status": dispatch_status,
            "http_status": status,
            "bpp_response": data,
        },
    )
    return JSONResponse(
        {
            "success": dispatch_status == "sent",
            "data": {
                "queued": False,
                "dispatched": True,
                "outbox_id": staged["public_id"],
                "message_id": message_id,
                "transaction_id": transaction_id,
                "bpp_id": bpp_id,
                "bpp_uri": bpp_uri,
                "http_status": status,
                "ack": ack,
                "target": target,
                "bpp_response": data,
                "note": f"Poll GET /api/ondc/inbox?action=on_{action} or /api/ondc/orders?transaction_id=…",
            },
        }
    )


@router.post("/api/ondc/select")
async def ondc_select(body: OrderActionBody, request: Request) -> JSONResponse:
    return await _dispatch_order_action(request, "select", body)


@router.post("/api/ondc/init")
async def ondc_init(body: OrderActionBody, request: Request) -> JSONResponse:
    return await _dispatch_order_action(request, "init", body)


@router.post("/api/ondc/confirm")
async def ondc_confirm(body: ConfirmBody, request: Request) -> JSONResponse:
    return await _dispatch_order_action(request, "confirm", body)


@router.post("/api/ondc/track")
async def ondc_track(body: OrderActionBody, request: Request) -> JSONResponse:
    return await _dispatch_order_action(request, "track", body)


class LocalTrackBody(BaseModel):
    order_id: Optional[str] = None


def _tracking_from_order(order: dict[str, Any]) -> dict[str, Any]:
    fulfilment = (
        order.get("fulfilment") if isinstance(order.get("fulfilment"), dict) else {}
    )
    logistics = (
        fulfilment.get("logistics")
        if isinstance(fulfilment.get("logistics"), dict)
        else {}
    )
    order_id = str(order.get("order_id") or order.get("id") or "")
    tracking_id = str(
        fulfilment.get("tracking_id")
        or logistics.get("lsp_order_id")
        or logistics.get("tracking_id")
        or order_id
    )
    tracking_url = (
        fulfilment.get("tracking_url")
        or logistics.get("tracking_url")
        or f"/api/ondc/track?order_id={order_id}"
    )
    location = logistics.get("tracking_location") or fulfilment.get("tracking_location")
    if not isinstance(location, dict):
        address = order.get("delivery_address") or fulfilment.get("delivery_address") or {}
        if not isinstance(address, dict):
            address = {}
        location = {
            "gps": None,
            "address": {
                "city": address.get("city") or "",
                "area_code": (
                    address.get("postalCode")
                    or address.get("pincode")
                    or address.get("pin")
                    or ""
                ),
            },
            "updated_at": order.get("updated_at") or fulfilment.get("updated_at"),
        }
    status = str(
        order.get("status")
        or order.get("state")
        or fulfilment.get("status")
        or "In-progress"
    )
    return {
        "order_id": order_id,
        "status": status,
        "tracking": {
            "id": tracking_id,
            "url": tracking_url,
            "status": "active",
            "location": location,
        },
    }


async def _local_order_track(request: Request, order_id: str | None) -> JSONResponse:
    resolved = str(order_id or "").strip()
    if not resolved:
        raise HTTPException(status_code=422, detail="order_id is required")
    pool = live_connection_pool(getattr(request.app.state, "persistence_pool", None))
    order: dict[str, Any] | None = None
    if pool is not None:
        try:
            order = await CommerceCompatibilityAdapter(pool).get_order(resolved)
        except (KeyError, ValueError, LookupError):
            order = None
    if order is None:
        try:
            order = commerce_demo.get_order(resolved)["order"]
        except KeyError:
            order = None
    if order is None:
        for item in ondc_store.list_orders(limit=200):
            nested = item.get("order") if isinstance(item.get("order"), dict) else {}
            candidates = {
                str(item.get("id") or ""),
                str(item.get("order_id") or ""),
                str(nested.get("id") or ""),
                str(nested.get("order_id") or ""),
            }
            if resolved in candidates:
                order = {**item, **nested} if nested else item
                break
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return JSONResponse(
        {"success": True, "data": _tracking_from_order(order)}
    )


@router.get("/api/ondc/track")
async def ondc_track_local_get(
    request: Request, order_id: Optional[str] = None
) -> JSONResponse:
    return await _local_order_track(request, order_id)


@router.post("/api/ondc/order-track")
async def ondc_track_local_post(
    body: LocalTrackBody, request: Request
) -> JSONResponse:
    return await _local_order_track(request, body.order_id)


@router.post("/api/ondc/order-status")
async def ondc_order_status(body: OrderActionBody, request: Request) -> JSONResponse:
    return await _dispatch_order_action(request, "status", body)


@router.post("/api/ondc/cancel")
async def ondc_cancel(body: OrderActionBody, request: Request) -> JSONResponse:
    return await _dispatch_order_action(request, "cancel", body)


@router.post("/api/ondc/update")
async def ondc_update(body: OrderActionBody, request: Request) -> JSONResponse:
    return await _dispatch_order_action(request, "update", body)


def _igm_issue_id(envelope: dict[str, Any]) -> str:
    message = envelope.get("message") or {}
    issue = message.get("issue") if isinstance(message.get("issue"), dict) else {}
    return str(issue.get("id") or message.get("issue_id") or "").strip()


def _igm_action_status(issue: dict[str, Any]) -> str:
    """Derive PROCESSING/RESOLVED/CLOSED from issue_actions when status is absent."""
    from app.domain_state_machines import IGM_NETWORK_CODES

    actions = (
        issue.get("issue_actions")
        if isinstance(issue.get("issue_actions"), dict)
        else {}
    )
    complainant = [
        str(item.get("complainant_action") or "").strip().upper()
        for item in (actions.get("complainant_actions") or [])
        if isinstance(item, dict)
    ]
    respondent = [
        str(item.get("respondent_action") or "").strip().upper()
        for item in (actions.get("respondent_actions") or [])
        if isinstance(item, dict)
    ]
    if complainant and complainant[-1] == "CLOSE":
        return "CLOSED"
    for code in reversed(respondent):
        if code in IGM_NETWORK_CODES:
            return code
    return ""


def _igm_network_status(envelope: dict[str, Any]) -> str:
    from app.domain_state_machines import IGM_NETWORK_CODES

    message = envelope.get("message") or {}
    issue = message.get("issue") if isinstance(message.get("issue"), dict) else {}
    status = str(issue.get("status") or "").strip().upper()
    if status in IGM_NETWORK_CODES:
        return status
    return _igm_action_status(issue)


def _igm_category(reason: str, override: str | None = None) -> str:
    if override:
        return str(override).strip().upper()
    return _IGM_REASON_CATEGORY.get(str(reason or "").strip().lower(), "OTHER")


def _igm_updated_by(subscriber_id: str, *, person: str = "Buyer") -> dict[str, Any]:
    return {
        "org": {"name": f"{subscriber_id}::{DEFAULT_DOMAIN}"},
        "contact": {
            "phone": "9999999999",
            "email": "support@ondcbuyer.aadharcha.in",
        },
        "person": {"name": person},
    }


def _igm_complainant_actions(
    *,
    action: str,
    timestamp: str,
    created_at: str,
    subscriber_id: str,
) -> list[dict[str, Any]]:
    """Visible IGM schema: issue_actions.complainant_actions. OPEN then CLOSE/ESCALATE."""
    opened = {
        "complainant_action": "OPEN",
        "short_desc": "Complaint created",
        "updated_at": created_at,
        "updated_by": _igm_updated_by(subscriber_id),
    }
    if action == "OPEN":
        return [opened]
    return [
        opened,
        {
            "complainant_action": action,
            "short_desc": f"Complaint {action.lower()}",
            "updated_at": timestamp,
            "updated_by": _igm_updated_by(subscriber_id),
        },
    ]


def _confirm_order_id(envelope: dict[str, Any]) -> str:
    message = envelope.get("message") or {}
    order = message.get("order") if isinstance(message.get("order"), dict) else {}
    return str(order.get("id") or message.get("order_id") or "").strip()


async def _find_confirmed_ondc_order(
    request: Request,
    *,
    order_id: str = "",
    transaction_id: str = "",
) -> dict[str, Any] | None:
    """Resolve an ONDC order from confirm outbox. Does not require on_confirm."""
    order_id = str(order_id or "").strip()
    transaction_id = str(transaction_id or "").strip()
    if not order_id and not transaction_id:
        return None
    records = await _persistent_records(
        request,
        "outbox",
        transaction_id=transaction_id or None,
        action="confirm",
        limit=100,
    )
    if records is None:
        records = []
        for item in ondc_store.list_outbox(limit=500):
            if item.get("action") != "confirm":
                continue
            if transaction_id and str(item.get("transaction_id") or "") != transaction_id:
                continue
            records.append(item)
    for record in records:
        envelope = record.get("envelope") or record.get("payload") or {}
        found_id = _confirm_order_id(envelope)
        found_txn = str(
            (envelope.get("context") or {}).get("transaction_id")
            or record.get("transaction_id")
            or ""
        ).strip()
        if order_id and found_id == order_id:
            return {
                "order_id": found_id,
                "transaction_id": found_txn,
                "envelope": envelope,
            }
        if not order_id and transaction_id and found_txn == transaction_id and found_id:
            return {
                "order_id": found_id,
                "transaction_id": found_txn,
                "envelope": envelope,
            }
    return None


async def _load_local_issue(request: Request, issue_id: str) -> dict[str, Any] | None:
    issue_id = str(issue_id or "").strip()
    if not issue_id:
        return None
    pool = getattr(request.app.state, "persistence_pool", None)
    if pool is not None:
        from app.commerce_compat import CommerceCompatibilityAdapter

        listed = await CommerceCompatibilityAdapter(pool).list_issues()
        for row in listed.get("issues") or []:
            if str(row.get("issue_id")) == issue_id:
                return row
        return None
    from app.commerce_demo import load_state

    return load_state().issues.get(issue_id)


async def _find_issue_for_order(
    request: Request, order_id: str
) -> dict[str, Any] | None:
    order_id = str(order_id or "").strip()
    if not order_id:
        return None
    pool = getattr(request.app.state, "persistence_pool", None)
    if pool is not None:
        from app.commerce_compat import CommerceCompatibilityAdapter

        return await CommerceCompatibilityAdapter(pool).find_issue_for_protocol_order(
            order_id
        )
    from app.commerce_demo import find_issue_for_protocol_order

    return find_issue_for_protocol_order(order_id)


async def _bind_protocol_issue(
    request: Request,
    *,
    order_id: str,
    transaction_id: str,
    bpp_id: str | None = None,
) -> dict[str, Any]:
    payload = {
        "principal_id": _subscriber_id() or "ondc-protocol",
        "seller_id": str(bpp_id or "").strip() or "ondc-bpp",
        "reason": "fulfillment",
        "description": "Protocol IGM issue",
    }
    pool = getattr(request.app.state, "persistence_pool", None)
    if pool is not None:
        from app.commerce_compat import CommerceCompatibilityAdapter

        bound = await CommerceCompatibilityAdapter(pool).bind_protocol_issue(
            order_id,
            payload,
            transaction_id=transaction_id,
        )
        return bound["issue"]
    from app.commerce_demo import bind_protocol_issue

    return bind_protocol_issue(
        order_id,
        payload,
        transaction_id=transaction_id,
    )["issue"]


async def _resolve_or_bind_local_issue(
    request: Request, body: IgmIssueBody
) -> dict[str, Any]:
    """Load an existing issue, or bind one from a confirm-outbox ONDC order."""
    issue_id = str(body.issue_id or "").strip()
    order_id = str(body.order_id or "").strip()
    transaction_id = str(body.transaction_id or "").strip()
    if not issue_id and not order_id:
        raise HTTPException(
            status_code=422,
            detail="issue requires issue_id or a confirmed order_id",
        )
    if issue_id:
        existing = await _load_local_issue(request, issue_id)
        if existing is not None:
            return existing
    lookup_order_id = order_id or issue_id
    existing_for_order = await _find_issue_for_order(request, lookup_order_id)
    if existing_for_order is not None:
        return existing_for_order
    confirmed = await _find_confirmed_ondc_order(
        request,
        order_id=lookup_order_id,
        transaction_id=transaction_id,
    )
    if confirmed is None:
        if order_id and not issue_id:
            raise HTTPException(status_code=404, detail="Unknown order")
        raise HTTPException(status_code=404, detail="Unknown issue")
    return await _bind_protocol_issue(
        request,
        order_id=confirmed["order_id"],
        transaction_id=confirmed["transaction_id"] or transaction_id,
        bpp_id=body.bpp_id,
    )


async def _record_igm_correlation(
    envelope: dict[str, Any],
    *,
    signature_verified: bool,
    note: str = "",
    pool: Any | None = None,
) -> dict[str, Any] | None:
    issue_id = _igm_issue_id(envelope)
    if not issue_id:
        return None
    context = envelope.get("context") or {}
    transaction_id = str(context.get("transaction_id") or "").strip()
    message_id = str(context.get("message_id") or "").strip()
    action = str(context.get("action") or "").strip()
    try:
        if pool is not None:
            from app.commerce_compat import CommerceCompatibilityAdapter
            from uuid import UUID

            try:
                UUID(issue_id)
            except ValueError:
                return None
            return await CommerceCompatibilityAdapter(pool).record_igm_network_event(
                issue_id,
                action=action,
                transaction_id=transaction_id,
                message_id=message_id,
                network_status=_igm_network_status(envelope),
                note=note,
                signature_verified=signature_verified,
            )
        from app.commerce_demo import record_igm_network_event

        return record_igm_network_event(
            issue_id,
            action=action,
            transaction_id=transaction_id,
            message_id=message_id,
            network_status=_igm_network_status(envelope),
            note=note,
            signature_verified=signature_verified,
        )
    except KeyError:
        return None


def _configured_seller_public_key_b64() -> tuple[str, str] | None:
    from app.ondc_crypto import load_ed25519_private_pem
    from cryptography.hazmat.primitives import serialization as _ser

    pem_path: Path | None = None
    uk = getattr(settings, "ondc_seller_unique_key_id", None)
    configured = getattr(settings, "ondc_seller_signing_private_key_path", None)
    if configured:
        candidate = Path(str(configured)).expanduser()
        if candidate.is_file():
            pem_path = candidate
    if pem_path is None or not uk:
        try:
            from app.ondc_onboard_routes import _role_paths

            paths = _role_paths("seller")
        except Exception:  # noqa: BLE001
            paths = None
        if paths is not None:
            if pem_path is None and paths["signing_pem"].is_file():
                pem_path = paths["signing_pem"]
            if not uk and paths["uk_id"].is_file():
                uk = paths["uk_id"].read_text(encoding="utf-8").strip()
    if pem_path is None or not pem_path.is_file() or not uk:
        return None
    public = load_ed25519_private_pem(pem_path.read_bytes()).public_key()
    public_b64 = base64.b64encode(
        public.public_bytes(encoding=_ser.Encoding.Raw, format=_ser.PublicFormat.Raw)
    ).decode("ascii")
    return str(uk).strip(), public_b64


async def _verify_retail_callback(
    request: Request,
    body: dict[str, Any],
    *,
    action: str,
) -> tuple[bool, str]:
    context = body.get("context") or {}
    if context.get("domain") != DEFAULT_DOMAIN:
        return False, "callback domain must be ONDC:RET10"
    if context.get("action") != action:
        return False, "callback action does not match the callback route"
    version = str(context.get("core_version") or "")
    if version != CORE_VERSION:
        return False, f"unsupported Retail core_version: {version or 'missing'}"
    bpp_id = str(context.get("bpp_id") or "").strip()
    if not bpp_id:
        bpp_id = _callback_subscriber_id(context, request)
    authorization = (request.headers.get("Authorization") or "").strip()
    if not authorization:
        return False, "missing ONDC Authorization header"
    if not bpp_id:
        return False, "callback bpp_id or Authorization subscriber is required"

    local = _configured_seller_public_key_b64()
    configured_bpp = (
        getattr(settings, "ondc_bpp_id", None)
        or getattr(settings, "ondc_seller_subscriber_id", None)
        or "ondcseller.aadharcha.in"
    )
    if local and bpp_id == configured_bpp:
        unique_key_id, public_key = local
        if verify_authorization_header(
            body,
            authorization,
            signing_public_key_b64=public_key,
            expected_subscriber_id=bpp_id,
            expected_unique_key_id=unique_key_id,
        ):
            return True, ""

    query = {
        "subscriber_id": bpp_id,
        "domain": DEFAULT_DOMAIN,
        "type": "BPP",
        "country": "IND",
    }
    try:
        status, response, _ = await _signed_post(_registry_url(), query, role="buyer")
    except Exception:  # noqa: BLE001
        return False, "Retail registry verification unavailable"
    if status >= 400 or not isinstance(response, list):
        return False, "Retail BPP registry lookup failed"
    for record in response:
        if (
            record.get("subscriber_id") != bpp_id
            or record.get("domain") != DEFAULT_DOMAIN
            or record.get("type") != "BPP"
            or record.get("status") != "SUBSCRIBED"
        ):
            continue
        unique_key_id = str(record.get("ukId") or "").strip()
        public_key = str(record.get("signing_public_key") or "").strip()
        if (
            unique_key_id
            and public_key
            and verify_authorization_header(
                body,
                authorization,
                signing_public_key_b64=public_key,
                expected_subscriber_id=bpp_id,
                expected_unique_key_id=unique_key_id,
            )
        ):
            return True, ""
    return False, "Retail callback signature did not match the registry"


async def _verify_retail_igm_callback(
    request: Request,
    body: dict[str, Any],
    *,
    action: str,
) -> tuple[bool, str]:
    return await _verify_retail_callback(request, body, action=action)


async def _dispatch_igm_action(
    request: Request,
    action: str,
    body: IgmIssueBody,
) -> JSONResponse:
    """Signed Retail IGM issue / issue_status → bpp_uri/{action}; persist outbox."""
    if action not in _IGM_ACTIONS:
        raise HTTPException(status_code=400, detail=f"unsupported IGM action: {action}")
    if not _ondc_configured("buyer"):
        raise HTTPException(status_code=503, detail="ONDC adapter not ready.")
    domain = body.domain or DEFAULT_DOMAIN
    if domain != DEFAULT_DOMAIN:
        raise HTTPException(
            status_code=422,
            detail="A2 IGM scope is Retail B2C v1.2 (ONDC:RET10) only",
        )
    issue_type = str(body.issue_type or "ISSUE").strip().upper()
    if issue_type not in {"ISSUE", "GRIEVANCE"}:
        raise HTTPException(status_code=422, detail="issue_type must be ISSUE or GRIEVANCE")
    local = await _resolve_or_bind_local_issue(request, body)
    message_id = body.message_id or str(uuid.uuid4())
    transaction_id = body.transaction_id or str(uuid.uuid4())
    bpp_id, bpp_uri = await _resolve_bpp_target(
        request,
        OrderActionBody(
            bpp_id=body.bpp_id,
            bpp_uri=body.bpp_uri,
            transaction_id=transaction_id,
        ),
        transaction_id=transaction_id,
    )
    timestamp = _iso_now()
    bap_id = getattr(settings, "ondc_bap_id", None) or _subscriber_id()
    complainant_action = str(body.complainant_action or "OPEN").strip().upper()
    if complainant_action not in _IGM_COMPLAINANT_ACTIONS:
        raise HTTPException(
            status_code=422,
            detail="complainant_action must be OPEN, CLOSE, or ESCALATE",
        )
    if action == "issue_status":
        message: dict[str, Any] = {"issue_id": str(local["issue_id"])}
    else:
        category = _igm_category(str(local.get("reason") or ""), body.category)
        created_at = timestamp if complainant_action == "OPEN" else str(
            local.get("created_at") or timestamp
        )
        igm_status = "CLOSED" if complainant_action == "CLOSE" else "OPEN"
        issue: dict[str, Any] = {
            "id": str(local["issue_id"]),
            "category": category,
            "complainant_info": {
                "person": {"name": "Buyer"},
                "contact": {
                    "phone": "9999999999",
                    "email": "support@ondcbuyer.aadharcha.in",
                },
            },
            "order_details": {"id": str(local.get("order_id") or "")},
            "description": {
                "short_desc": str(local.get("reason") or "buyer_support"),
                "long_desc": str(local.get("description") or ""),
            },
            "source": {
                "network_participant_id": _subscriber_id() or "",
                "type": "CONSUMER",
            },
            "expected_response_time": {"duration": "PT2H"},
            "expected_resolution_time": {"duration": "P1D"},
            "status": igm_status,
            "issue_type": issue_type,
            "issue_actions": {
                "complainant_actions": _igm_complainant_actions(
                    action=complainant_action,
                    timestamp=timestamp,
                    created_at=created_at,
                    subscriber_id=str(bap_id or ""),
                )
            },
            "created_at": created_at,
            "updated_at": timestamp,
        }
        sub_category = _IGM_SUB_CATEGORY.get(category)
        if sub_category:
            issue["sub_category"] = sub_category
        message = {"issue": issue}
    envelope = {
        "context": {
            "domain": DEFAULT_DOMAIN,
            "action": action,
            "country": "IND",
            "city": body.city or DEFAULT_CITY,
            "core_version": CORE_VERSION,
            "bap_id": bap_id,
            "bap_uri": _bap_uri(),
            "bpp_id": bpp_id,
            "bpp_uri": bpp_uri,
            "transaction_id": transaction_id,
            "message_id": message_id,
            "timestamp": timestamp,
            "ttl": "PT30S",
        },
        "message": message,
    }
    entry = {
        "id": f"out_{uuid.uuid4().hex[:12]}",
        "action": action,
        "payload": envelope,
        "created_at": int(time.time()),
        "status": "queued",
        "message_id": message_id,
        "transaction_id": transaction_id,
        "bpp_id": bpp_id,
        "bpp_uri": bpp_uri,
    }
    target = f"{bpp_uri}/{action}"
    staged = await _stage_outbox_before_dispatch(request, entry, destination=target)
    if staged.get("already_delivered"):
        return JSONResponse(
            {
                "success": True,
                "data": {
                    "queued": False,
                    "dispatched": False,
                    "deduplicated": True,
                    "outbox_id": staged["public_id"],
                    "message_id": message_id,
                    "transaction_id": transaction_id,
                    "bpp_id": bpp_id,
                    "bpp_uri": bpp_uri,
                    "target": target,
                    "issue_id": str(local["issue_id"]),
                },
            }
        )
    try:
        status, data, _ = await _signed_post(target, envelope, role="buyer")
    except HTTPException:
        await _complete_outbox_delivery(
            staged,
            delivered=False,
            error="signing/config",
            file_updates={"status": "error", "error": "signing/config"},
        )
        await _record_igm_correlation(
            envelope,
            signature_verified=False,
            note=f"IGM {action} dispatch failed: signing/config",
            pool=getattr(request.app.state, "persistence_pool", None),
        )
        raise
    except Exception as exc:  # noqa: BLE001
        await _complete_outbox_delivery(
            staged,
            delivered=False,
            error=str(exc),
            file_updates={"status": "error", "error": str(exc)},
        )
        await _record_igm_correlation(
            envelope,
            signature_verified=False,
            note=f"IGM {action} dispatch failed: {exc}",
            pool=getattr(request.app.state, "persistence_pool", None),
        )
        raise HTTPException(
            status_code=502, detail=f"ONDC BPP {action} failed: {exc}"
        ) from exc

    ack = None
    if isinstance(data, dict):
        ack = ((data.get("message") or {}).get("ack") or {}).get("status")
    dispatch_status = "sent" if status < 400 else "nack"
    if ack == "NACK":
        dispatch_status = "nack"
    await _complete_outbox_delivery(
        staged,
        delivered=dispatch_status == "sent",
        error=f"BPP returned HTTP {status} ack={ack}",
        file_updates={
            "status": dispatch_status,
            "http_status": status,
            "bpp_response": data,
        },
    )
    await _record_igm_correlation(
        envelope,
        signature_verified=dispatch_status == "sent",
        note=(
            f"IGM {action} dispatched"
            if dispatch_status == "sent"
            else f"IGM {action} dispatch nack HTTP {status}"
        ),
        pool=getattr(request.app.state, "persistence_pool", None),
    )
    return JSONResponse(
        {
            "success": dispatch_status == "sent",
            "data": {
                "queued": False,
                "dispatched": True,
                "outbox_id": staged["public_id"],
                "message_id": message_id,
                "transaction_id": transaction_id,
                "bpp_id": bpp_id,
                "bpp_uri": bpp_uri,
                "http_status": status,
                "ack": ack,
                "target": target,
                "issue_id": str(local["issue_id"]),
                "note": f"Poll GET /api/ondc/inbox?action=on_{action}&transaction_id=…",
            },
        }
    )


async def _order_buyer_id(request: Request, order_id: str) -> str | None:
    if not order_id:
        return None
    pool = getattr(request.app.state, "persistence_pool", None)
    if pool is not None:
        try:
            order = await CommerceV1(pool).get_order(order_id)
        except Exception:  # noqa: BLE001
            return None
        return str(order.get("buyer_id") or "") or None
    from app.commerce_demo import get_order

    try:
        return str(get_order(order_id)["order"].get("buyer_id") or "") or None
    except Exception:  # noqa: BLE001
        return None


@router.post("/api/ondc/issue")
async def ondc_issue(body: IgmIssueBody, request: Request) -> JSONResponse:
    return await _dispatch_igm_action(request, "issue", body)


@router.post("/api/ondc/issue_status")
async def ondc_issue_status(body: IgmIssueBody, request: Request) -> JSONResponse:
    return await _dispatch_igm_action(request, "issue_status", body)


@router.post("/api/ondc/logistics/init")
async def ondc_logistics_init(
    body: LogisticsActionBody, request: Request
) -> JSONResponse:
    return await _dispatch_order_action(request, "init", body, role="lbnp")


@router.post("/api/ondc/logistics/confirm")
async def ondc_logistics_confirm(
    body: LogisticsActionBody, request: Request
) -> JSONResponse:
    return await _dispatch_order_action(request, "confirm", body, role="lbnp")


@router.post("/api/ondc/logistics/update")
async def ondc_logistics_update(
    body: LogisticsActionBody, request: Request
) -> JSONResponse:
    return await _dispatch_order_action(request, "update", body, role="lbnp")


@router.post("/api/ondc/logistics/status")
async def ondc_logistics_status(
    body: LogisticsActionBody, request: Request
) -> JSONResponse:
    return await _dispatch_order_action(request, "status", body, role="lbnp")


@router.post("/api/ondc/logistics/track")
async def ondc_logistics_track(
    body: LogisticsActionBody, request: Request
) -> JSONResponse:
    return await _dispatch_order_action(request, "track", body, role="lbnp")


def _queue_record(row: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe operational projection of a durable protocol record."""
    return {
        key: (
            value.isoformat()
            if isinstance(value, datetime)
            else str(value)
            if isinstance(value, uuid.UUID)
            else value
        )
        for key, value in row.items()
    }


async def _persistent_records(
    request: Request,
    queue: str,
    *,
    transaction_id: str | None = None,
    action: str | None = None,
    state: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]] | None:
    pool = live_connection_pool(getattr(request.app.state, "persistence_pool", None))
    if pool is None:
        return None
    async with UnitOfWork(pool) as unit_of_work:
        rows = await ONDCRepository(unit_of_work).list_records(
            queue,  # type: ignore[arg-type]
            transaction_id=transaction_id,
            action=action,
            state=state,
            limit=limit,
        )
    return [_queue_record(row) for row in rows]


async def _require_bound_logistics_order(
    request: Request, *, transaction_id: str, bpp_id: str
) -> dict[str, Any]:
    pool = getattr(request.app.state, "persistence_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=409,
            detail="PostgreSQL CommerceV1 binding is required for LOG10 actions",
        )
    try:
        order = await CommerceV1(pool).get_logistics_binding(transaction_id)
    except (CommerceNotFound, CommerceConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logistics = dict((order.get("fulfilment") or {}).get("logistics") or {})
    if logistics.get("bpp_id") != bpp_id:
        raise HTTPException(
            status_code=409,
            detail="LOG10 target does not match the AgentGuard-bound provider",
        )
    return order


async def _latest_signed_logistics_on_init(
    request: Request, *, transaction_id: str, bpp_id: str, logistics: dict[str, Any]
) -> dict[str, Any]:
    records = await _persistent_records(
        request,
        "inbox",
        transaction_id=transaction_id,
        action="on_init",
        limit=100,
    )
    if records is None:
        raise HTTPException(
            status_code=409,
            detail="PostgreSQL callback persistence is required before LOG10 confirm",
        )
    for record in records:
        envelope = record.get("envelope") or {}
        context = envelope.get("context") or {}
        if (
            record.get("subscriber_id") == bpp_id
            and (record.get("redacted_payload") or {}).get("signature_verified")
            is True
            and context.get("domain") == LOGISTICS_DOMAIN
            and context.get("core_version") == LOGISTICS_CORE_VERSION
            and context.get("bpp_id") == bpp_id
            and context.get("transaction_id") == transaction_id
        ):
            compliant, reason = _on_init_conformance(envelope)
            if not compliant:
                raise HTTPException(status_code=409, detail=reason)
            matched, reason = _on_init_matches_binding(envelope, logistics)
            if not matched:
                raise HTTPException(status_code=409, detail=reason)
            return envelope
    raise HTTPException(
        status_code=409,
        detail="a compliant signature-verified LOG10 1.2.5 on_init is required",
    )


async def _verify_logistics_callback(
    request: Request,
    body: dict[str, Any],
    *,
    action: str,
) -> tuple[bool, str]:
    context = body.get("context") or {}
    if context.get("domain") != LOGISTICS_DOMAIN:
        return False, "LOG10 callback domain must be ONDC:LOG10"
    if context.get("action") != action:
        return False, "LOG10 callback action does not match the callback route"
    version = str(context.get("core_version") or "")
    if version != LOGISTICS_CORE_VERSION:
        return False, f"unsupported LOG10 core_version: {version or 'missing'}"
    bpp_id = str(context.get("bpp_id") or "").strip()

    authorization = (request.headers.get("Authorization") or "").strip()
    if not authorization:
        return False, "missing ONDC Authorization header"
    query = {
        "subscriber_id": bpp_id,
        "domain": LOGISTICS_DOMAIN,
        "type": "BPP",
        "country": "IND",
    }
    try:
        status, response, _ = await _signed_post(_registry_url(), query, role="lbnp")
    except Exception:  # noqa: BLE001
        return False, "LOG10 registry verification unavailable"
    if status >= 400 or not isinstance(response, list):
        return False, "LOG10 BPP registry lookup failed"
    for record in response:
        if (
            record.get("subscriber_id") != bpp_id
            or record.get("domain") != LOGISTICS_DOMAIN
            or record.get("type") != "BPP"
            or record.get("status") != "SUBSCRIBED"
        ):
            continue
        unique_key_id = str(record.get("ukId") or "").strip()
        public_key = str(record.get("signing_public_key") or "").strip()
        if (
            unique_key_id
            and public_key
            and verify_authorization_header(
                body,
                authorization,
                signing_public_key_b64=public_key,
                expected_subscriber_id=bpp_id,
                expected_unique_key_id=unique_key_id,
            )
        ):
            return True, ""
    return False, "LOG10 callback signature did not match the registry"


def _require_recovery_write_contract(request: Request) -> tuple[str, str]:
    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    correlation_id = (request.headers.get("X-Correlation-ID") or "").strip()
    if not idempotency_key or not correlation_id:
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key and X-Correlation-ID headers are required",
        )
    return idempotency_key, correlation_id


async def _reconcile_claimed_inbox(pool: Any, record: dict[str, Any]) -> dict[str, Any]:
    error = ""
    delivered = False
    try:
        envelope = record.get("envelope") or {}
        context = envelope.get("context") or {}
        if (
            context.get("domain") == LOGISTICS_DOMAIN
            and record.get("action") in _LOGISTICS_LIFECYCLE_CALLBACKS
        ):
            await CommerceV1(pool).apply_logistics_update(
                transaction_id=str(record["transaction_id"]),
                event_commitment=str(record["event_commitment"]),
                update=_normalize_logistics_callback(record),
            )
        elif (
            context.get("domain") == DEFAULT_DOMAIN
            and record.get("action") in _RETAIL_LIFECYCLE_CALLBACKS
        ):
            await CommerceV1(pool).apply_retail_update(
                transaction_id=str(record["transaction_id"]),
                event_commitment=str(record["event_commitment"]),
                update=_normalize_retail_callback(record),
            )
        elif record.get("action") in _IGM_CALLBACKS:
            await _record_igm_correlation(
                envelope,
                signature_verified=bool(
                    (record.get("redacted_payload") or {}).get("signature_verified")
                ),
                note=f"IGM {record.get('action')} reconciled",
                pool=pool,
            )
        delivered = True
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
    async with UnitOfWork(pool) as unit_of_work:
        repository = ONDCRepository(unit_of_work)
        if delivered:
            return await repository.mark_delivered(
                "inbox", record["inbox_id"], record["lease_token"]
            )
        return await repository.schedule_retry(
            "inbox",
            record["inbox_id"],
            record["lease_token"],
            error=error or "callback reconciliation failed",
        )


async def _process_inbox_record(pool: Any, record_id: int) -> dict[str, Any] | None:
    async with UnitOfWork(pool) as unit_of_work:
        record = await ONDCRepository(unit_of_work).claim_inbox_record(
            record_id,
            worker_id=f"ondc-callback-{record_id}",
            lease_seconds=30,
        )
    if record is None:
        return None
    return await _reconcile_claimed_inbox(pool, record)


@router.get("/api/ondc/orders")
async def ondc_orders(
    request: Request, transaction_id: Optional[str] = None
) -> JSONResponse:
    outbox = await _persistent_records(
        request, "outbox", transaction_id=transaction_id, limit=500
    )
    if outbox is not None:
        inbox = (
            await _persistent_records(
                request, "inbox", transaction_id=transaction_id, limit=500
            )
            or []
        )
        order_actions = {"select", "init", "confirm"}
        items = []
        for record in outbox:
            if record["action"] not in order_actions:
                continue
            envelope = record.get("envelope") or {}
            order = (envelope.get("message") or {}).get("order") or {}
            items.append(
                {
                    "id": order.get("id") or f"pg_out_{record['outbox_id']}",
                    "transaction_id": record["transaction_id"],
                    "bpp_id": (envelope.get("context") or {}).get("bpp_id"),
                    "state": order.get("state") or record["state"],
                    "order": order,
                    "created_at": record["created_at"],
                    "delivery_state": record["state"],
                }
            )
        return JSONResponse(
            {"success": True, "data": {"items": items, "callbacks": inbox}}
        )
    return JSONResponse(
        {
            "success": True,
            "data": {
                "items": ondc_store.list_orders(transaction_id=transaction_id),
                "callbacks": (
                    ondc_store.callbacks_for_transaction(transaction_id)
                    if transaction_id
                    else []
                ),
            },
        }
    )


async def _lifecycle_outbox_message_id(
    request: Request, transaction_id: str, actions: tuple[str, ...]
) -> str | None:
    for action in actions:
        records = await _persistent_records(
            request,
            "outbox",
            transaction_id=transaction_id,
            action=action,
            limit=100,
        )
        if records is not None:
            for record in records:
                if record.get("action") == action and record.get("message_id"):
                    return str(record["message_id"])
            continue
        for item in ondc_store.list_outbox(limit=500):
            if (
                item.get("action") == action
                and item.get("transaction_id") == transaction_id
                and item.get("message_id")
            ):
                return str(item["message_id"])
    return None


async def _ingest_callback(
    request: Request, action: str, body: dict[str, Any]
) -> JSONResponse:
    ctx = body.get("context") or {}
    normalized_action = action if action.startswith("on_") else f"on_{action}"
    transaction_id = str(ctx.get("transaction_id") or "").strip()
    message_id = str(ctx.get("message_id") or "").strip()
    subscriber_id = _callback_subscriber_id(ctx, request)
    if not transaction_id or not message_id or not subscriber_id:
        return JSONResponse(
            {
                "message": {"ack": {"status": "NACK"}},
                "error": {
                    "type": "CORE-ERROR",
                    "code": "30000",
                    "message": (
                        "context.transaction_id, context.message_id, and a "
                        "subscriber identifier are required"
                    ),
                },
            },
            status_code=400,
        )

    request_actions = _INBOUND_CALLBACK_REQUESTS.get(normalized_action)
    if request_actions:
        expected = await _lifecycle_outbox_message_id(
            request, transaction_id, request_actions
        )
        if expected and expected != message_id:
            logger.warning(
                "BAP %s message_id mismatch transaction_id=%s expected=%s "
                "found=%s; ACK and persist callback",
                normalized_action,
                transaction_id,
                expected,
                message_id,
            )

    signature_verified = False
    if normalized_action in _IGM_CALLBACKS:
        signature_verified, verification_error = await _verify_retail_callback(
            request,
            body,
            action=normalized_action,
        )
        if not signature_verified:
            return JSONResponse(
                {
                    "message": {"ack": {"status": "NACK"}},
                    "error": {
                        "type": "CORE-ERROR",
                        "code": "30000",
                        "message": verification_error,
                    },
                },
                status_code=401,
            )
    elif (
        ctx.get("domain") == LOGISTICS_DOMAIN
        or ctx.get("core_version") == LOGISTICS_CORE_VERSION
    ):
        signature_verified, verification_error = await _verify_logistics_callback(
            request,
            body,
            action=normalized_action,
        )
        if not signature_verified:
            return JSONResponse(
                {
                    "message": {"ack": {"status": "NACK"}},
                    "error": {
                        "type": "CORE-ERROR",
                        "code": "30000",
                        "message": verification_error,
                    },
                },
                status_code=401,
            )
    elif normalized_action in _RETAIL_LIFECYCLE_CALLBACKS:
        signature_verified, verification_error = await _verify_retail_callback(
            request,
            body,
            action=normalized_action,
        )
        if not signature_verified:
            return JSONResponse(
                {
                    "message": {"ack": {"status": "NACK"}},
                    "error": {
                        "type": "CORE-ERROR",
                        "code": "30000",
                        "message": verification_error,
                    },
                },
                status_code=401,
            )

    persistence_pool = getattr(request.app.state, "persistence_pool", None)
    if ctx.get("domain") == LOGISTICS_DOMAIN and persistence_pool is None:
        return JSONResponse(
            {
                "message": {"ack": {"status": "NACK"}},
                "error": {
                    "type": "DOMAIN-ERROR",
                    "code": "50000",
                    "message": "PostgreSQL persistence is required for LOG10 callbacks",
                },
            },
            status_code=503,
        )
    if persistence_pool is not None:
        correlation_id = (
            request.headers.get("X-Correlation-ID") or transaction_id
        ).strip()
        try:
            _, persisted = await persist_callback_before_ack(
                persistence_pool,
                subscriber_id=subscriber_id,
                transaction_id=transaction_id,
                message_id=message_id,
                action=normalized_action,
                correlation_id=correlation_id,
                raw_envelope=body,
                redacted_payload={
                    "status": "received",
                    "signature_verified": signature_verified,
                    "core_version": ctx.get("core_version"),
                },
            )
        except (
            CorrelationMismatch,
            EnvelopeCommitmentMismatch,
            ValueError,
        ) as exc:
            return JSONResponse(
                {
                    "message": {"ack": {"status": "NACK"}},
                    "error": {
                        "type": "CORE-ERROR",
                        "code": "30000",
                        "message": str(exc),
                    },
                },
                status_code=409,
            )
        except Exception:
            return JSONResponse(
                {
                    "message": {"ack": {"status": "NACK"}},
                    "error": {
                        "type": "DOMAIN-ERROR",
                        "code": "50000",
                        "message": "callback persistence unavailable",
                    },
                },
                status_code=503,
            )
        background = None
        if (
            (
                ctx.get("domain") == LOGISTICS_DOMAIN
                and normalized_action in _LOGISTICS_LIFECYCLE_CALLBACKS
            )
            or (
                ctx.get("domain") == DEFAULT_DOMAIN
                and normalized_action in _RETAIL_LIFECYCLE_CALLBACKS
            )
            or normalized_action in _IGM_CALLBACKS
        ):
            background = BackgroundTask(
                _process_inbox_record,
                persistence_pool,
                int(persisted["inbox_id"]),
            )
        return JSONResponse(
            {"message": {"ack": {"status": "ACK"}}},
            background=background,
        )

    entry = {
        "id": f"in_{uuid.uuid4().hex[:12]}",
        "action": normalized_action,
        "payload": body,
        "received_at": int(time.time()),
        "transaction_id": transaction_id,
        "message_id": message_id,
        "bpp_id": subscriber_id,
        "signature_verified": signature_verified,
    }
    ondc_store.append_inbox(entry)
    if normalized_action in _IGM_CALLBACKS:
        await _record_igm_correlation(
            body,
            signature_verified=signature_verified,
            note=f"IGM {normalized_action} received",
            pool=None,
        )
    return JSONResponse({"message": {"ack": {"status": "ACK"}}})


@router.post("/api/ondc/callback/{action}")
async def ondc_callback_api(
    action: str, request: Request, body: dict[str, Any]
) -> JSONResponse:
    return await _ingest_callback(request, action, body)


_BECKN_CALLBACK_ACTIONS = (
    "search",
    "select",
    "init",
    "confirm",
    "status",
    "track",
    "cancel",
    "update",
    "rating",
    "support",
    "issue",
    "issue_status",
)


def _register_beckn_callbacks() -> None:
    """Explicit paths so /ondc/on_subscribe stays on onboard router."""

    for act in _BECKN_CALLBACK_ACTIONS:

        async def _root(request: Request, action: str = act) -> JSONResponse:
            body = await request.json()
            return await _ingest_callback(request, f"on_{action}", body)

        async def _np(role: str, request: Request, action: str = act) -> JSONResponse:
            if role not in {"buyer", "seller"}:
                raise HTTPException(status_code=404, detail="role must be buyer|seller")
            body = await request.json()
            return await _ingest_callback(request, f"on_{action}", body)

        router.add_api_route(
            f"/ondc/on_{act}",
            _root,
            methods=["POST"],
            name=f"ondc_on_{act}",
        )
        router.add_api_route(
            f"/ondc/np/{{role}}/on_{act}",
            _np,
            methods=["POST"],
            name=f"ondc_np_on_{act}",
        )


_register_beckn_callbacks()


@router.get("/api/ondc/outbox")
async def ondc_outbox(
    request: Request,
    state: Optional[str] = None,
    transaction_id: Optional[str] = None,
) -> JSONResponse:
    items = await _persistent_records(
        request, "outbox", state=state, transaction_id=transaction_id
    )
    if items is not None:
        return JSONResponse({"success": True, "data": {"items": items}})
    return JSONResponse({"success": True, "data": {"items": ondc_store.list_outbox()}})


@router.get("/api/ondc/inbox")
async def ondc_inbox(
    request: Request,
    action: Optional[str] = None,
    state: Optional[str] = None,
    transaction_id: Optional[str] = None,
) -> JSONResponse:
    items = await _persistent_records(
        request,
        "inbox",
        action=action,
        state=state,
        transaction_id=transaction_id,
    )
    if items is not None:
        return JSONResponse({"success": True, "data": {"items": items}})
    return JSONResponse(
        {"success": True, "data": {"items": ondc_store.list_inbox(action=action)}}
    )


@router.get("/api/ondc/catalogs")
async def ondc_catalogs(request: Request, transaction_id: str) -> JSONResponse:
    if not transaction_id.strip():
        raise HTTPException(status_code=400, detail="transaction_id required")
    records = await _persistent_records(
        request,
        "inbox",
        transaction_id=transaction_id.strip(),
        action="on_search",
        limit=500,
    )
    if records is None:
        items = ondc_store.catalogs_for_transaction(transaction_id.strip())
    else:
        items = []
        for record in records:
            message = (record.get("envelope") or {}).get("message") or {}
            providers = (message.get("catalog") or {}).get("bpp/providers") or []
            items.extend(providers)
    return JSONResponse(
        {
            "success": True,
            "data": {
                "transaction_id": transaction_id,
                "items": items,
                "count": len(items),
                "source": "ondc-network",
            },
        }
    )


@router.post("/api/ondc/outbox/drain")
async def drain_ondc_outbox(request: Request, body: OutboxDrainBody) -> JSONResponse:
    """Lease and deliver due intents; safe to call after process restart."""
    _require_recovery_write_contract(request)
    pool = getattr(request.app.state, "persistence_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=409, detail="PostgreSQL persistence is required"
        )
    async with UnitOfWork(pool) as unit_of_work:
        claimed = await ONDCRepository(unit_of_work).claim_outbox(
            worker_id=body.worker_id,
            lease_seconds=body.lease_seconds,
            limit=body.limit,
        )
    results: list[dict[str, Any]] = []
    for record in claimed:
        delivered = False
        error = ""
        try:
            status, response, _ = await _signed_post(
                record["destination"],
                record["envelope"],
                role=_signing_role_for_envelope(record["envelope"]),
            )
            ack = (
                ((response.get("message") or {}).get("ack") or {}).get("status")
                if isinstance(response, dict)
                else None
            )
            delivered = status < 400 and ack != "NACK"
            error = f"destination returned HTTP {status} ack={ack}"
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        async with UnitOfWork(pool) as unit_of_work:
            repository = ONDCRepository(unit_of_work)
            if delivered:
                updated = await repository.mark_delivered(
                    "outbox", record["outbox_id"], record["lease_token"]
                )
            else:
                updated = await repository.schedule_retry(
                    "outbox",
                    record["outbox_id"],
                    record["lease_token"],
                    error=error,
                )
        results.append(_queue_record(updated))
    return JSONResponse(
        {"success": True, "data": {"claimed": len(claimed), "items": results}}
    )


@router.post("/api/ondc/inbox/drain")
async def drain_ondc_inbox(request: Request, body: OutboxDrainBody) -> JSONResponse:
    """Lease and reconcile persisted callbacks; safe after process restart."""
    _require_recovery_write_contract(request)
    pool = getattr(request.app.state, "persistence_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=409, detail="PostgreSQL persistence is required"
        )
    async with UnitOfWork(pool) as unit_of_work:
        claimed = await ONDCRepository(unit_of_work).claim_inbox(
            worker_id=body.worker_id,
            lease_seconds=body.lease_seconds,
            limit=body.limit,
        )
    results = [
        _queue_record(await _reconcile_claimed_inbox(pool, record))
        for record in claimed
    ]
    return JSONResponse(
        {"success": True, "data": {"claimed": len(claimed), "items": results}}
    )


@router.post("/api/ondc/{queue}/dead-letter/{record_id}/requeue")
async def requeue_ondc_dead_letter(
    queue: str,
    record_id: int,
    request: Request,
    body: DeadLetterRecoveryBody,
) -> JSONResponse:
    _require_recovery_write_contract(request)
    if queue not in {"inbox", "outbox"}:
        raise HTTPException(status_code=404, detail="queue must be inbox|outbox")
    pool = getattr(request.app.state, "persistence_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=409, detail="PostgreSQL persistence is required"
        )
    async with UnitOfWork(pool) as unit_of_work:
        updated = await ONDCRepository(unit_of_work).requeue_dead_letter(
            queue,  # type: ignore[arg-type]
            record_id,
            event_commitment=body.event_commitment,
        )
    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="dead letter not found or event commitment did not match",
        )
    return JSONResponse({"success": True, "data": _queue_record(updated)})
