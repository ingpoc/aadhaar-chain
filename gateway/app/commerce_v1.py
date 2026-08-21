"""Durable, single-seller commerce application service.

Prepare, result recording, and reconciliation are separate durable operations.
The money owner is this CommerceV1 payment/ledger. The default rail is simulated.
When Razorpay Test Mode keys (``rzp_test_``) are configured, checkout can capture
via Razorpay Orders + Checkout HMAC + webhooks, still posting this same ledger.
Live Razorpay keys are refused.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .domain_state_machines import (
    PAYMENT_ORDER_TARGETS,
    TransitionError,
    require_transition,
)
from .persistence.commerce_repository import CommerceRepository
from .persistence.connection import ConnectionPool
from .persistence.repositories import IdempotencyConflict, IdempotencyRepository
from .persistence.transaction import UnitOfWork
from .razorpay_test import (
    CURRENCY,
    PROVIDER as RAZORPAY_PROVIDER,
    RazorpayApiError,
    RazorpayConfigError,
    RazorpayNotConfigured,
    RazorpayTestClient,
    razorpay_payment_id_from_result,
    require_razorpay_client,
    require_webhook_secret,
    resolve_payment_rail,
    verify_checkout_signature,
    verify_webhook_signature,
)


class CommerceConflict(RuntimeError):
    """A stale version, invalid transition, or changed quote was rejected."""


class CommerceNotFound(LookupError):
    """A principal-owned commerce resource was not found."""


class CommerceValidation(ValueError):
    """A commerce command did not satisfy domain invariants."""


Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (UUID, datetime)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _request_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


STAFF_PERMISSIONS = {
    "owner": (
        "store.write",
        "staff.manage",
        "catalog.write",
        "order.write",
        "refund.issue",
    ),
    "manager": ("catalog.write", "order.write", "refund.issue"),
    "fulfilment": ("catalog.read", "order.write"),
    "support": ("catalog.read", "order.read"),
    "viewer": ("catalog.read", "order.read"),
}

ACTIVE_STAFF_STATUSES = frozenset({"active", "invited"})


def staff_permissions_for(role: str) -> set[str]:
    return set(STAFF_PERMISSIONS.get(str(role or "viewer"), STAFF_PERMISSIONS["viewer"]))


def owner_staff_row(seller_id: str) -> dict[str, Any]:
    digest = hashlib.sha256(seller_id.encode("utf-8")).hexdigest()[:16]
    return {
        "staff_id": f"staff_owner_{digest}",
        "seller_id": seller_id,
        "member_principal_id": seller_id,
        "display_name": "Store owner",
        "email": "",
        "role": "owner",
        "status": "active",
        "version": 1,
    }


def _serviceability_tokens(body: dict[str, Any]) -> list[str]:
    tokens = body.get("serviceability_tokens")
    if tokens in (None, ""):
        tokens = body.get("serviceability") or []
    if isinstance(tokens, str):
        tokens = [token.strip() for token in tokens.split(",") if token.strip()]
    elif not isinstance(tokens, list):
        tokens = []
    return [str(token).strip() for token in tokens if str(token).strip()]


def normalize_store_payload(seller_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Validate Seller /business fields. Empty store is a draft, not an error."""
    tokens = _serviceability_tokens(body)
    sla = body.get("fulfilment_sla_hours")
    returns = body.get("return_window_days")
    sla_hours = None if sla in (None, "") else int(sla)
    return_days = None if returns in (None, "") else int(returns)
    if sla_hours is not None and not 1 <= sla_hours <= 72:
        raise CommerceValidation("fulfilment SLA must be between 1 and 72 hours")
    if return_days is not None and not 0 <= return_days <= 30:
        raise CommerceValidation("return window must be between 0 and 30 days")
    store_name = str(body.get("store_name") or "").strip()
    city = str(body.get("city") or "").strip()
    pin = str(body.get("pin") or body.get("pincode") or "").strip()
    fields_complete = bool(store_name and city and pin)
    # Seller UI sends complete=false for Save draft and complete=true for Open store.
    if body.get("complete") is False:
        status = "draft"
    elif fields_complete:
        status = "ready"
    else:
        status = "draft"
    return {
        "seller_id": seller_id,
        "store_name": store_name,
        "city": city,
        "state": str(body.get("state") or "").strip(),
        "pin": pin,
        "serviceability_tokens": tokens,
        "fulfilment_sla_hours": sla_hours,
        "return_window_days": return_days,
        "support_hours": str(body.get("support_hours") or "").strip(),
        "status": status,
    }


def normalize_staff_payload(
    seller_id: str,
    body: dict[str, Any],
    *,
    actor_principal_id: str,
) -> dict[str, Any]:
    del actor_principal_id
    role = str(body.get("role") or "viewer").strip().lower()
    if role not in STAFF_PERMISSIONS:
        raise CommerceValidation("unsupported staff role")
    member = str(body.get("member_principal_id") or "").strip()
    if not member:
        raise CommerceValidation("member_principal_id is required")
    status = str(body.get("status") or "invited").strip().lower()
    if status == "disabled":
        status = "revoked"
    if status not in {"invited", "active", "revoked"}:
        raise CommerceValidation("unsupported staff status")
    return {
        "seller_id": seller_id,
        "member_principal_id": member,
        "display_name": str(body.get("display_name") or "").strip(),
        "email": str(body.get("email") or "").strip(),
        "role": role,
        "status": status,
    }


def parse_catalog_csv(csv_text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(csv_text))
    return [dict(row) for row in reader]


def evaluate_catalog_import_row(
    row: dict[str, Any], *, index: int
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    title = str(row.get("title") or row.get("name") or "").strip()
    issues: list[dict[str, Any]] = []
    if not title:
        issues.append({"row": index, "field": "title", "message": "title is required"})
        return None, issues
    try:
        price_inr = int(row.get("price_inr") or 0)
        inventory = int(row.get("inventory") or row.get("quantity") or 0)
    except (TypeError, ValueError):
        issues.append(
            {"row": index, "field": "price_inr", "message": "price and inventory must be integers"}
        )
        return None, issues
    if price_inr < 0 or inventory < 0:
        issues.append(
            {"row": index, "field": "price_inr", "message": "price and inventory must be non-negative"}
        )
        return None, issues
    return {
        "item_id": str(row.get("item_id") or row.get("sku") or "").strip() or None,
        "title": title,
        "description": str(row.get("description") or "").strip(),
        "price_inr": price_inr,
        "inventory": inventory,
        "seller_name": str(row.get("seller_name") or "").strip() or None,
        "category_id": str(row.get("category_id") or "").strip() or None,
        "delivery_estimate": str(row.get("delivery_estimate") or "").strip() or None,
        "return_policy": str(row.get("return_policy") or "").strip() or None,
    }, issues


def shared_seller_ids_from_settings() -> list[str]:
    from config import settings

    raw = getattr(settings, "commerce_shared_seller_ids", "") or ""
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def operated_seller_ids(
    principal_id: str,
    memberships: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    *,
    extra: list[str] | tuple[str, ...] = (),
) -> list[str]:
    """Session principal plus merchants this principal is staff of."""
    ids: list[str] = []
    for candidate in (
        principal_id,
        *extra,
        *shared_seller_ids_from_settings(),
        *(str(row.get("seller_id") or "") for row in memberships),
    ):
        text = str(candidate or "").strip()
        if text and text not in ids:
            ids.append(text)
    return ids


def empty_store(seller_id: str) -> dict[str, Any]:
    return {
        "seller_id": seller_id,
        "store_name": "",
        "city": "",
        "state": "",
        "pin": "",
        "serviceability_tokens": [],
        "fulfilment_sla_hours": None,
        "return_window_days": None,
        "support_hours": "",
        "status": "draft",
        "setup_required": True,
        "version": 0,
        "created_at": None,
        "updated_at": None,
    }


class CommerceV1:
    """Application boundary for durable INR/paise single-seller commerce."""

    operation = "commerce.checkout.prepare.v1"

    def __init__(self, pool: ConnectionPool, *, clock: Clock = _utcnow) -> None:
        self.pool = pool
        self.clock = clock

    async def get_logistics_binding(self, transaction_id: str) -> dict[str, Any]:
        if not transaction_id.strip():
            raise CommerceValidation("logistics transaction_id is required")
        async with UnitOfWork(self.pool) as unit_of_work:
            try:
                order = await CommerceRepository(
                    unit_of_work
                ).get_order_by_logistics_transaction(transaction_id.strip())
            except LookupError as exc:
                raise CommerceNotFound(str(exc)) from exc
            except ValueError as exc:
                raise CommerceConflict(str(exc)) from exc
        return _jsonable(order)

    async def apply_logistics_update(
        self,
        *,
        transaction_id: str,
        event_commitment: str,
        update: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply one normalized, signature-verified LOG10 callback exactly once."""
        transaction_id = transaction_id.strip()
        event_commitment = event_commitment.strip()
        if (
            not transaction_id
            or len(event_commitment) != 64
            or any(character not in "0123456789abcdef" for character in event_commitment)
        ):
            raise CommerceValidation(
                "transaction_id and SHA-256 event_commitment are required"
            )
        action = str(update.get("action") or "").strip()
        bpp_id = str(update.get("bpp_id") or "").strip()
        message_id = str(update.get("message_id") or "").strip()
        if not action or not bpp_id or not message_id:
            raise CommerceValidation("callback action, bpp_id, and message_id are required")

        async with UnitOfWork(self.pool) as unit_of_work:
            repository = CommerceRepository(unit_of_work)
            try:
                order = await repository.get_order_by_logistics_transaction(
                    transaction_id, lock=True
                )
            except LookupError as exc:
                raise CommerceNotFound(str(exc)) from exc
            except ValueError as exc:
                raise CommerceConflict(str(exc)) from exc

            fulfilment = dict(order.get("fulfilment") or {})
            logistics = dict(fulfilment.get("logistics") or {})
            if str(logistics.get("bpp_id") or "") != bpp_id:
                raise CommerceConflict("callback BPP does not match the bound provider")
            if str(logistics.get("core_version") or "") != "1.2.5":
                raise CommerceConflict("bound logistics version is not LOG10 1.2.5")

            processed = list(logistics.get("processed_callbacks") or [])
            if event_commitment in processed:
                return _jsonable({"order": order, "duplicate": True})

            current_status = str(order["status"])
            next_status = current_status
            review_reason = str(update.get("review_reason") or "").strip()
            provider_timestamp = str(update.get("provider_timestamp") or "").strip()
            latest_provider_timestamp = str(
                logistics.get("latest_provider_timestamp")
                or (logistics.get("last_callback") or {}).get("provider_timestamp")
                or ""
            ).strip()
            stale_callback = False
            if provider_timestamp and latest_provider_timestamp:
                try:
                    current_provider_time = datetime.fromisoformat(
                        provider_timestamp.replace("Z", "+00:00")
                    )
                    previous_provider_time = datetime.fromisoformat(
                        latest_provider_timestamp.replace("Z", "+00:00")
                    )
                    if current_provider_time <= previous_provider_time:
                        review_reason = "stale LOG10 callback timestamp"
                        stale_callback = True
                except ValueError:
                    review_reason = "invalid LOG10 callback timestamp"
                    stale_callback = True
            target_status = str(update.get("target_status") or "").strip()
            if not review_reason and target_status and target_status != current_status:
                try:
                    require_transition("order", current_status, target_status)
                    next_status = target_status
                except TransitionError as exc:
                    review_reason = str(exc)

            recorded_at = self.clock().isoformat()
            provider_status = str(update.get("provider_status") or "").strip()
            status_message = str(update.get("status_message") or "").strip()
            if review_reason:
                status_message = status_message or review_reason
            if update.get("provider_name") and not stale_callback:
                fulfilment["provider_name"] = str(update["provider_name"])
            if update.get("tracking_id") and not stale_callback:
                fulfilment["tracking_id"] = str(update["tracking_id"])
            if update.get("tracking_url") and not stale_callback:
                fulfilment["tracking_url"] = str(update["tracking_url"])
            if status_message:
                fulfilment["status_message"] = status_message

            logistics.update(
                {
                    "last_callback": {
                        "action": action,
                        "message_id": message_id,
                        "event_commitment": event_commitment,
                        "received_at": recorded_at,
                        "provider_timestamp": provider_timestamp or None,
                    },
                    "processed_callbacks": [*processed, event_commitment],
                    "review_required": bool(review_reason),
                }
            )
            if review_reason:
                logistics["review_reason"] = review_reason
            else:
                logistics.pop("review_reason", None)
            if provider_timestamp and not stale_callback:
                logistics["latest_provider_timestamp"] = provider_timestamp
            for source, target in (
                ("provider_status", "provider_status"),
                ("lsp_order_id", "lsp_order_id"),
                ("tracking_url", "tracking_url"),
                ("tracking_location", "tracking_location"),
            ):
                if update.get(source) and not stale_callback:
                    logistics[target] = update[source]
            if update.get("conformance") and not stale_callback:
                logistics["conformance"] = dict(update["conformance"])

            event = {
                "status": next_status,
                "recorded_at": recorded_at,
                "source": "ondc-log10",
                "callback_action": action,
                "message_id": message_id,
                "event_commitment": event_commitment,
            }
            if provider_status:
                event["provider_status"] = provider_status
            if update.get("tracking_id"):
                event["tracking_id"] = str(update["tracking_id"])
            elif fulfilment.get("tracking_id"):
                event["tracking_id"] = fulfilment["tracking_id"]
            if status_message:
                event["status_message"] = status_message
            fulfilment["status"] = next_status
            fulfilment["last_verified_update_at"] = recorded_at
            fulfilment["logistics"] = logistics
            fulfilment["history"] = [
                *list(fulfilment.get("history") or []),
                event,
            ]
            order = await repository.update_order_fulfilment(
                order["order_id"],
                expected_version=int(order["version"]),
                status=next_status,
                fulfilment=fulfilment,
            )
        return _jsonable({"order": order, "duplicate": False})

    async def rebind_rejected_logistics_provider(
        self, *, order_id: str, logistics: dict[str, Any]
    ) -> dict[str, Any]:
        """Replace only a rejected pre-shipment LSP binding under AgentGuard."""
        required = (
            "transaction_id",
            "bpp_id",
            "provider_name",
            "core_version",
            "signature_verified",
        )
        if any(logistics.get(field) in {None, ""} for field in required):
            raise CommerceValidation("complete signed logistics binding is required")
        if (
            logistics["core_version"] != "1.2.5"
            or logistics["signature_verified"] is not True
        ):
            raise CommerceValidation("replacement LSP must be signed LOG10 1.2.5")
        async with UnitOfWork(self.pool) as unit_of_work:
            repository = CommerceRepository(unit_of_work)
            try:
                order = await repository.get_order(UUID(order_id), lock=True)
            except LookupError as exc:
                raise CommerceNotFound(str(exc)) from exc
            fulfilment = dict(order.get("fulfilment") or {})
            prior = dict(fulfilment.get("logistics") or {})
            if order["status"] != "preparing":
                raise CommerceConflict("replacement LSP requires a preparing order")
            if (prior.get("conformance") or {}).get("status") != "rejected":
                raise CommerceConflict("current LSP has not been rejected")
            if prior.get("bpp_id") == logistics.get("bpp_id"):
                raise CommerceConflict("replacement LSP must be a different provider")
            recorded_at = self.clock().isoformat()
            attempts = list(prior.get("attempts") or [])
            attempts.append(
                {
                    "transaction_id": prior.get("transaction_id"),
                    "bpp_id": prior.get("bpp_id"),
                    "provider_name": fulfilment.get("provider_name"),
                    "conformance": prior.get("conformance"),
                    "last_callback": prior.get("last_callback"),
                }
            )
            replacement = dict(logistics)
            replacement["attempts"] = attempts
            fulfilment.update(
                {
                    "provider_name": str(logistics["provider_name"]),
                    "status": "preparing",
                    "status_message": "Trying another ONDC-compliant delivery provider.",
                    "logistics": replacement,
                }
            )
            fulfilment["history"] = [
                *list(fulfilment.get("history") or []),
                {
                    "status": "preparing",
                    "recorded_at": recorded_at,
                    "source": "agentguard-logistics-rebind",
                    "status_message": fulfilment["status_message"],
                    "logistics_transaction_id": str(logistics["transaction_id"]),
                },
            ]
            order = await repository.update_order_fulfilment(
                order["order_id"],
                expected_version=int(order["version"]),
                status="preparing",
                fulfilment=fulfilment,
            )
        return _jsonable(order)

    async def get_retail_binding(self, transaction_id: str) -> dict[str, Any]:
        if not transaction_id.strip():
            raise CommerceValidation("retail transaction_id is required")
        async with UnitOfWork(self.pool) as unit_of_work:
            try:
                order = await CommerceRepository(
                    unit_of_work
                ).get_order_by_retail_transaction(transaction_id.strip())
            except LookupError as exc:
                raise CommerceNotFound(str(exc)) from exc
            except ValueError as exc:
                raise CommerceConflict(str(exc)) from exc
        return _jsonable(order)

    async def bind_retail_protocol(
        self, *, order_id: str, retail: dict[str, Any]
    ) -> dict[str, Any]:
        """Attach one signed RET10 transaction to a CommerceV1 order."""
        required = ("transaction_id", "bpp_id", "core_version")
        if any(retail.get(field) in {None, ""} for field in required):
            raise CommerceValidation("complete signed retail binding is required")
        if retail["core_version"] != "1.2.0":
            raise CommerceValidation("retail binding must be RET10 1.2.0")
        transaction_id = str(retail["transaction_id"]).strip()
        bpp_id = str(retail["bpp_id"]).strip()
        async with UnitOfWork(self.pool) as unit_of_work:
            repository = CommerceRepository(unit_of_work)
            try:
                order = await repository.get_order(UUID(order_id), lock=True)
            except LookupError as exc:
                raise CommerceNotFound(str(exc)) from exc
            fulfilment = dict(order.get("fulfilment") or {})
            prior = dict(fulfilment.get("retail") or {})
            if prior.get("transaction_id") not in {None, "", transaction_id}:
                raise CommerceConflict("order is already bound to a different RET10 transaction")
            if (
                prior.get("transaction_id") == transaction_id
                and prior.get("bpp_id") not in {None, "", bpp_id}
            ):
                raise CommerceConflict("callback BPP does not match the bound seller")
            if prior.get("transaction_id") == transaction_id and prior.get("bpp_id") == bpp_id:
                return _jsonable(order)
            try:
                existing = await repository.get_order_by_retail_transaction(
                    transaction_id, lock=True
                )
            except LookupError:
                existing = None
            except ValueError as exc:
                raise CommerceConflict(str(exc)) from exc
            if existing is not None and existing["order_id"] != order["order_id"]:
                raise CommerceConflict("retail transaction is bound to another order")
            recorded_at = self.clock().isoformat()
            bound = dict(prior)
            bound.update(
                {
                    "transaction_id": transaction_id,
                    "bpp_id": bpp_id,
                    "bpp_uri": str(retail.get("bpp_uri") or bound.get("bpp_uri") or ""),
                    "core_version": "1.2.0",
                    "signature_verified": bool(retail.get("signature_verified", True)),
                }
            )
            if retail.get("protocol_order_id"):
                bound["protocol_order_id"] = str(retail["protocol_order_id"])
            fulfilment["retail"] = bound
            fulfilment["history"] = [
                *list(fulfilment.get("history") or []),
                {
                    "status": order["status"],
                    "recorded_at": recorded_at,
                    "source": "ondc-ret10-bind",
                    "retail_transaction_id": transaction_id,
                },
            ]
            order = await repository.update_order_fulfilment(
                order["order_id"],
                expected_version=int(order["version"]),
                status=str(order["status"]),
                fulfilment=fulfilment,
            )
        return _jsonable(order)

    async def apply_retail_update(
        self,
        *,
        transaction_id: str,
        event_commitment: str,
        update: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply one normalized, signature-verified RET10 callback exactly once."""
        transaction_id = transaction_id.strip()
        event_commitment = event_commitment.strip()
        if (
            not transaction_id
            or len(event_commitment) != 64
            or any(character not in "0123456789abcdef" for character in event_commitment)
        ):
            raise CommerceValidation(
                "transaction_id and SHA-256 event_commitment are required"
            )
        action = str(update.get("action") or "").strip()
        bpp_id = str(update.get("bpp_id") or "").strip()
        message_id = str(update.get("message_id") or "").strip()
        if not action or not bpp_id or not message_id:
            raise CommerceValidation("callback action, bpp_id, and message_id are required")

        async with UnitOfWork(self.pool) as unit_of_work:
            repository = CommerceRepository(unit_of_work)
            try:
                order = await repository.get_order_by_retail_transaction(
                    transaction_id, lock=True
                )
            except LookupError as exc:
                raise CommerceNotFound(str(exc)) from exc
            except ValueError as exc:
                raise CommerceConflict(str(exc)) from exc

            fulfilment = dict(order.get("fulfilment") or {})
            retail = dict(fulfilment.get("retail") or {})
            if str(retail.get("bpp_id") or "") != bpp_id:
                raise CommerceConflict("callback BPP does not match the bound seller")
            if str(retail.get("core_version") or "") != "1.2.0":
                raise CommerceConflict("bound retail version is not RET10 1.2.0")

            processed = list(retail.get("processed_callbacks") or [])
            if event_commitment in processed:
                return _jsonable({"order": order, "duplicate": True})

            current_status = str(order["status"])
            next_status = current_status
            review_reason = str(update.get("review_reason") or "").strip()
            provider_timestamp = str(update.get("provider_timestamp") or "").strip()
            latest_provider_timestamp = str(
                retail.get("latest_provider_timestamp")
                or (retail.get("last_callback") or {}).get("provider_timestamp")
                or ""
            ).strip()
            stale_callback = False
            if provider_timestamp and latest_provider_timestamp:
                try:
                    current_provider_time = datetime.fromisoformat(
                        provider_timestamp.replace("Z", "+00:00")
                    )
                    previous_provider_time = datetime.fromisoformat(
                        latest_provider_timestamp.replace("Z", "+00:00")
                    )
                    if current_provider_time <= previous_provider_time:
                        review_reason = "stale RET10 callback timestamp"
                        stale_callback = True
                except ValueError:
                    review_reason = "invalid RET10 callback timestamp"
                    stale_callback = True
            target_status = str(update.get("target_status") or "").strip()
            if not review_reason and target_status and target_status != current_status:
                try:
                    require_transition("order", current_status, target_status)
                    next_status = target_status
                except TransitionError as exc:
                    review_reason = str(exc)

            recorded_at = self.clock().isoformat()
            provider_status = str(update.get("provider_status") or "").strip()
            status_message = str(update.get("status_message") or "").strip()
            if review_reason:
                status_message = status_message or review_reason
            if update.get("tracking_id") and not stale_callback:
                fulfilment["tracking_id"] = str(update["tracking_id"])
            if update.get("tracking_url") and not stale_callback:
                fulfilment["tracking_url"] = str(update["tracking_url"])
            if status_message:
                fulfilment["status_message"] = status_message
            if update.get("protocol_order_id") and not stale_callback:
                retail["protocol_order_id"] = str(update["protocol_order_id"])
            if update.get("quote") and not stale_callback:
                retail["quote"] = dict(update["quote"])
            if update.get("billing") and not stale_callback:
                retail["billing"] = dict(update["billing"])
            if update.get("payment") and not stale_callback:
                retail["payment"] = dict(update["payment"])
            if update.get("settlement") and not stale_callback:
                retail["settlement"] = dict(update["settlement"])

            retail.update(
                {
                    "last_callback": {
                        "action": action,
                        "message_id": message_id,
                        "event_commitment": event_commitment,
                        "received_at": recorded_at,
                        "provider_timestamp": provider_timestamp or None,
                    },
                    "processed_callbacks": [*processed, event_commitment],
                    "review_required": bool(review_reason),
                }
            )
            if review_reason:
                retail["review_reason"] = review_reason
            else:
                retail.pop("review_reason", None)
            if provider_timestamp and not stale_callback:
                retail["latest_provider_timestamp"] = provider_timestamp
            if provider_status and not stale_callback:
                retail["provider_status"] = provider_status

            refund_row: dict[str, Any] | None = None
            return_row: dict[str, Any] | None = None
            if not stale_callback and not review_reason:
                try:
                    return_row = await self._apply_retail_return(
                        repository, order, update, recorded_at
                    )
                    if return_row is not None:
                        retail["return_id"] = str(return_row["return_id"])
                        retail["return_status"] = return_row["status"]
                    refund_row = await self._apply_retail_refund(
                        repository, order, update, event_commitment
                    )
                    if refund_row is not None:
                        retail["refund_id"] = str(refund_row["refund_id"])
                        retail["refund_status"] = refund_row["status"]
                except (CommerceConflict, CommerceValidation) as exc:
                    review_reason = str(exc)
                    status_message = status_message or review_reason
                    retail["review_required"] = True
                    retail["review_reason"] = review_reason
                    fulfilment["status_message"] = status_message

            event = {
                "status": next_status,
                "recorded_at": recorded_at,
                "source": "ondc-ret10",
                "callback_action": action,
                "message_id": message_id,
                "event_commitment": event_commitment,
            }
            if provider_status:
                event["provider_status"] = provider_status
            if update.get("tracking_id"):
                event["tracking_id"] = str(update["tracking_id"])
            elif fulfilment.get("tracking_id"):
                event["tracking_id"] = fulfilment["tracking_id"]
            if status_message:
                event["status_message"] = status_message
            fulfilment["status"] = next_status
            fulfilment["last_verified_update_at"] = recorded_at
            fulfilment["retail"] = retail
            fulfilment["history"] = [
                *list(fulfilment.get("history") or []),
                event,
            ]
            order = await repository.update_order_fulfilment(
                order["order_id"],
                expected_version=int(order["version"]),
                status=next_status,
                fulfilment=fulfilment,
            )
        response = {"order": order, "duplicate": False}
        if return_row is not None:
            response["return"] = return_row
        if refund_row is not None:
            response["refund"] = refund_row
        return _jsonable(response)

    async def _apply_retail_return(
        self,
        repository: CommerceRepository,
        order: dict[str, Any],
        update: dict[str, Any],
        recorded_at: str,
    ) -> dict[str, Any] | None:
        target = str(update.get("return_status") or "").strip()
        if not target:
            return None
        reason = str(update.get("return_reason") or "Buyer requested return").strip()
        existing = await repository.get_return_for_order(order["order_id"], lock=True)
        if existing is None:
            row, _created = await repository.create_or_get_return(
                return_id=uuid5(
                    NAMESPACE_URL, f"commerce-return:{order['order_id']}"
                ),
                order_id=order["order_id"],
                principal_id=str(order["principal_id"]),
                seller_id=str(order["seller_id"]),
                reason=reason,
            )
            existing = row
        current = str(existing["status"])
        hops = {
            "requested": (),
            "approved": ("approved",),
            "in_transit": ("approved", "in_transit"),
            "received": ("approved", "in_transit", "received"),
            "refund_pending": ("approved", "in_transit", "received", "refund_pending"),
            "completed": (
                "approved",
                "in_transit",
                "received",
                "refund_pending",
                "completed",
            ),
        }.get(target)
        if hops is None:
            raise CommerceConflict(f"unknown return status: {target}")
        row = existing
        for hop in hops:
            if current == hop:
                continue
            try:
                require_transition("return", current, hop)
            except TransitionError as exc:
                raise CommerceConflict(str(exc)) from exc
            row = await repository.set_return_status(
                row["return_id"],
                expected_version=int(row["version"]),
                status=hop,
                resolution={
                    "source": "ondc-ret10",
                    "updated_at": recorded_at,
                    "provider_status": update.get("provider_status"),
                },
            )
            current = hop
        return row

    async def _apply_retail_refund(
        self,
        repository: CommerceRepository,
        order: dict[str, Any],
        update: dict[str, Any],
        event_commitment: str,
    ) -> dict[str, Any] | None:
        amount_paise = update.get("refund_amount_paise")
        if amount_paise in {None, ""}:
            return None
        try:
            amount_paise = int(amount_paise)
        except (TypeError, ValueError) as exc:
            raise CommerceValidation("refund amount must be an integer") from exc
        if amount_paise <= 0:
            raise CommerceValidation("refund amount must be positive")
        refundable = await repository.get_refundable_order(
            order["order_id"], str(order["seller_id"]), lock=True
        )
        if refundable["payment_status"] not in {"succeeded", "reconciled"}:
            raise CommerceConflict("only a verified paid order can be refunded")
        if amount_paise > refundable["payment_amount_paise"]:
            raise CommerceConflict("refund exceeds the verified paid amount")
        seller_id = str(order["seller_id"])
        idempotency_key = f"ondc-ret10:{event_commitment}"
        razorpay_payment_id = razorpay_payment_id_from_result(
            refundable.get("payment_result")
        )
        refund_namespace = f"commerce-refund:{seller_id}:{idempotency_key}"
        refund, created = await repository.create_or_get_refund(
            refund_id=uuid5(NAMESPACE_URL, refund_namespace),
            order_id=order["order_id"],
            payment_attempt_id=refundable["payment_attempt_id"],
            seller_id=seller_id,
            principal_id=str(order["principal_id"]),
            amount_paise=amount_paise,
            idempotency_key=idempotency_key,
            correlation_id=str(update.get("message_id") or event_commitment),
        )
        if created:
            if (
                refundable.get("payment_provider") == RAZORPAY_PROVIDER
                and razorpay_payment_id
            ):
                try:
                    await require_razorpay_client().refund(
                        payment_id=razorpay_payment_id,
                        amount_paise=amount_paise,
                        idempotency_key=idempotency_key,
                    )
                except RazorpayConfigError:
                    raise
                except Exception as exc:
                    raise CommerceConflict(
                        f"Razorpay Test refund failed: {exc}"
                    ) from exc
            require_transition("refund", refund["status"], "succeeded")
            transaction_id = uuid5(NAMESPACE_URL, f"{refund_namespace}:ledger")
            await repository.post_balanced_ledger(
                transaction_id,
                order["order_id"],
                refundable["payment_attempt_id"],
                "refund",
                amount_paise,
                (
                    (uuid5(transaction_id, "debit"), "seller_payable", "debit"),
                    (uuid5(transaction_id, "credit"), "payment_clearing", "credit"),
                ),
            )
            refund = await repository.set_refund_status(
                refund["refund_id"], "pending", "succeeded"
            )
        return refund

    async def upsert_inventory(
        self,
        *,
        seller_id: str,
        sku: str,
        title: str,
        unit_price_paise: int,
        available_quantity: int,
    ) -> dict[str, Any]:
        if not seller_id or not sku or not title:
            raise CommerceValidation("seller_id, sku, and title are required")
        if unit_price_paise < 0 or available_quantity < 0:
            raise CommerceValidation("price and inventory must be non-negative")
        async with UnitOfWork(self.pool) as unit_of_work:
            try:
                row = await CommerceRepository(unit_of_work).upsert_inventory(
                    seller_id, sku, title, unit_price_paise, available_quantity
                )
            except ValueError as exc:
                raise CommerceValidation(str(exc)) from exc
        return _jsonable(row)

    async def create_cart(
        self,
        *,
        principal_id: str,
        seller_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not principal_id or not seller_id:
            raise CommerceValidation("principal_id and seller_id are required")
        async with UnitOfWork(self.pool) as unit_of_work:
            repository = CommerceRepository(unit_of_work)
            idempotency = IdempotencyRepository(unit_of_work)
            if idempotency_key:
                created, record = await idempotency.create_or_get(
                    principal_id,
                    "commerce.cart.create.v1",
                    idempotency_key,
                    _request_hash({"seller_id": seller_id}),
                    resource=f"seller:{seller_id}",
                )
                if not created:
                    if record["status"] != "success" or record["response"] is None:
                        raise CommerceConflict("cart creation is incomplete")
                    return record["response"]
            row = await repository.create_cart(uuid4(), principal_id, seller_id)
            response = _jsonable({**row, "lines": []})
            if idempotency_key:
                await idempotency.update_response(
                    principal_id,
                    "commerce.cart.create.v1",
                    idempotency_key,
                    "success",
                    response,
                )
        return response

    async def get_cart(
        self, *, principal_id: str, cart_id: str | UUID
    ) -> dict[str, Any]:
        async with UnitOfWork(self.pool) as unit_of_work:
            try:
                row = await CommerceRepository(unit_of_work).get_cart_with_lines(
                    UUID(str(cart_id)), principal_id
                )
            except LookupError as exc:
                raise CommerceNotFound(str(exc)) from exc
        return _jsonable(row)

    async def set_cart_line(
        self,
        *,
        principal_id: str,
        cart_id: str | UUID,
        sku: str,
        quantity: int,
        expected_version: int,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if quantity < 0:
            raise CommerceValidation("quantity must be non-negative")
        async with UnitOfWork(self.pool) as unit_of_work:
            idempotency = IdempotencyRepository(unit_of_work)
            try:
                if idempotency_key:
                    created, record = await idempotency.create_or_get(
                        principal_id,
                        "commerce.cart.line.set.v1",
                        idempotency_key,
                        _request_hash(
                            {
                                "cart_id": str(cart_id),
                                "sku": sku,
                                "quantity": quantity,
                                "expected_version": expected_version,
                            }
                        ),
                        resource=f"cart:{cart_id}",
                    )
                    if not created:
                        if record["status"] != "success" or record["response"] is None:
                            raise CommerceConflict("cart update is incomplete")
                        return record["response"]
                row = await CommerceRepository(unit_of_work).set_cart_line(
                    UUID(str(cart_id)), principal_id, sku, quantity, expected_version
                )
                response = _jsonable(row)
                if idempotency_key:
                    await idempotency.update_response(
                        principal_id,
                        "commerce.cart.line.set.v1",
                        idempotency_key,
                        "success",
                        response,
                    )
            except LookupError as exc:
                raise CommerceNotFound(str(exc)) from exc
            except RuntimeError as exc:
                raise CommerceConflict(str(exc)) from exc
            except ValueError as exc:
                raise CommerceValidation(str(exc)) from exc
        return response

    async def preview_checkout(
        self,
        *,
        principal_id: str,
        cart_id: str | UUID,
        expected_version: int,
        landed_total_paise: int | None = None,
        ttl_seconds: int = 300,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if ttl_seconds <= 0:
            raise CommerceValidation("quote ttl must be positive")
        async with UnitOfWork(self.pool) as unit_of_work:
            repository = CommerceRepository(unit_of_work)
            idempotency = IdempotencyRepository(unit_of_work)
            try:
                if idempotency_key:
                    created, record = await idempotency.create_or_get(
                        principal_id,
                        "commerce.checkout.preview.v1",
                        idempotency_key,
                        _request_hash(
                            {
                                "cart_id": str(cart_id),
                                "expected_version": expected_version,
                                "landed_total_paise": landed_total_paise,
                                "ttl_seconds": ttl_seconds,
                            }
                        ),
                        resource=f"cart:{cart_id}",
                    )
                    if not created:
                        if record["status"] != "success" or record["response"] is None:
                            raise CommerceConflict("checkout preview is incomplete")
                        return record["response"]
                await repository.release_expired_quotes(self.clock())
                cart = await repository.get_cart_with_lines(
                    UUID(str(cart_id)), principal_id, lock=True
                )
                if cart["status"] != "open":
                    raise CommerceConflict("cart is not open")
                if cart["version"] != expected_version:
                    raise CommerceConflict("stale cart version")
                subtotal = sum(
                    line["quantity"] * line["unit_price_paise"]
                    for line in cart["lines"]
                )
                total = subtotal if landed_total_paise is None else landed_total_paise
                quote = await repository.create_quote(
                    uuid4(), cart, total, self.clock() + timedelta(seconds=ttl_seconds)
                )
                response = _jsonable(quote)
                if idempotency_key:
                    await idempotency.update_response(
                        principal_id,
                        "commerce.checkout.preview.v1",
                        idempotency_key,
                        "success",
                        response,
                    )
            except LookupError as exc:
                raise CommerceNotFound(str(exc)) from exc
            except CommerceConflict:
                raise
            except ValueError as exc:
                raise CommerceValidation(str(exc)) from exc
        return response

    async def get_order(
        self, *, principal_id: str, order_id: str | UUID
    ) -> dict[str, Any]:
        async with UnitOfWork(self.pool) as unit_of_work:
            try:
                row = await CommerceRepository(unit_of_work).get_order(
                    UUID(str(order_id))
                )
            except LookupError as exc:
                raise CommerceNotFound(str(exc)) from exc
            if row["principal_id"] != principal_id:
                raise CommerceNotFound("order not found")
        return _jsonable(row)

    async def get_payment_state(
        self, *, principal_id: str, payment_attempt_id: str | UUID
    ) -> dict[str, Any]:
        async with UnitOfWork(self.pool) as unit_of_work:
            repository = CommerceRepository(unit_of_work)
            try:
                payment = await repository.get_payment(UUID(str(payment_attempt_id)))
                if payment["principal_id"] != principal_id:
                    raise CommerceNotFound("payment attempt not found")
                order = await repository.get_order(payment["order_id"])
            except LookupError as exc:
                raise CommerceNotFound(str(exc)) from exc
        return _jsonable({"order": order, "payment_attempt": payment})

    async def create_razorpay_checkout_order(
        self,
        *,
        principal_id: str,
        order_id: str | UUID,
        client: RazorpayTestClient | None = None,
    ) -> dict[str, Any]:
        """Create a Razorpay Test Order for a CommerceV1 order that is ready to pay."""
        rail = resolve_payment_rail()
        if rail["rail"] != RAZORPAY_PROVIDER:
            raise RazorpayNotConfigured(str(rail["message"]))
        razorpay = client or require_razorpay_client()
        try:
            order_uuid = UUID(str(order_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise CommerceNotFound("order not found") from exc
        async with UnitOfWork(self.pool) as unit_of_work:
            repository = CommerceRepository(unit_of_work)
            try:
                order = await repository.get_order(order_uuid, lock=True)
                if order["principal_id"] != principal_id:
                    raise CommerceNotFound("order not found")
                if order["status"] != "payment_pending":
                    raise CommerceConflict("order is not ready to pay")
                payment = await repository.get_payment_for_order(order_uuid, lock=True)
                if payment["status"] != "pending":
                    raise CommerceConflict("payment attempt is not pending")
                existing_ref = str(payment.get("provider_reference") or "")
                if existing_ref and payment.get("provider") == RAZORPAY_PROVIDER:
                    razorpay_order_id = existing_ref
                else:
                    created = await razorpay.create_order(
                        amount_paise=int(payment["amount_paise"]),
                        receipt=str(payment["payment_attempt_id"]),
                        notes={
                            "commerce_order_id": str(order["order_id"]),
                            "payment_attempt_id": str(payment["payment_attempt_id"]),
                        },
                    )
                    razorpay_order_id = str(created.get("id") or "")
                    if not razorpay_order_id:
                        raise RazorpayApiError("Razorpay order create returned no id")
                    if int(created.get("amount") or 0) != int(payment["amount_paise"]):
                        raise RazorpayApiError(
                            "Razorpay order amount does not match commerce paise"
                        )
                    if str(created.get("currency") or CURRENCY) != CURRENCY:
                        raise RazorpayApiError("Razorpay order currency must be INR")
                    try:
                        payment = await repository.attach_provider_order(
                            payment["payment_attempt_id"],
                            provider=RAZORPAY_PROVIDER,
                            provider_reference=razorpay_order_id,
                            result={
                                "rail": RAZORPAY_PROVIDER,
                                "razorpay_order_id": razorpay_order_id,
                                "simulated": False,
                                "source": "create_order",
                            },
                        )
                    except ValueError as exc:
                        raise CommerceConflict(str(exc)) from exc
            except LookupError as exc:
                raise CommerceNotFound(str(exc)) from exc
        return _jsonable(
            {
                "order": order,
                "payment_attempt": payment,
                "payment_rail": rail,
                "razorpay": {
                    "key": rail["key_id"],
                    "amount": int(payment["amount_paise"]),
                    "currency": CURRENCY,
                    "order_id": razorpay_order_id,
                    "name": "AgentGuard",
                    "description": "ONDC Buyer checkout (Razorpay Test Mode)",
                },
            }
        )

    async def confirm_razorpay_checkout(
        self,
        *,
        principal_id: str,
        order_id: str | UUID,
        razorpay_order_id: str,
        razorpay_payment_id: str,
        razorpay_signature: str,
    ) -> dict[str, Any]:
        """Verify Checkout HMAC and capture into the CommerceV1 ledger."""
        rail = resolve_payment_rail()
        if rail["rail"] != RAZORPAY_PROVIDER:
            raise RazorpayNotConfigured(str(rail["message"]))
        verify_checkout_signature(
            razorpay_order_id=razorpay_order_id,
            razorpay_payment_id=razorpay_payment_id,
            razorpay_signature=razorpay_signature,
        )
        try:
            order_uuid = UUID(str(order_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise CommerceNotFound("order not found") from exc
        async with UnitOfWork(self.pool) as unit_of_work:
            repository = CommerceRepository(unit_of_work)
            try:
                order = await repository.get_order(order_uuid, lock=True)
                if order["principal_id"] != principal_id:
                    raise CommerceNotFound("order not found")
                payment = await repository.get_payment_for_order(order_uuid, lock=True)
            except LookupError as exc:
                raise CommerceNotFound(str(exc)) from exc
            if str(payment.get("provider_reference") or "") != razorpay_order_id:
                raise CommerceConflict(
                    "razorpay_order_id does not match this commerce order"
                )
        return await self.apply_provider_payment_result(
            principal_id=principal_id,
            payment_attempt_id=payment["payment_attempt_id"],
            status="succeeded",
            provider_payment_id=razorpay_payment_id,
            detail={
                "rail": RAZORPAY_PROVIDER,
                "razorpay_order_id": razorpay_order_id,
                "razorpay_payment_id": razorpay_payment_id,
                "simulated": False,
                "source": "checkout",
            },
        )

    async def apply_razorpay_webhook(
        self,
        *,
        body: bytes,
        signature: str,
        event_id: str | None,
        webhook_secret: str | None = None,
    ) -> dict[str, Any]:
        """Verify webhook HMAC and reconcile payment.captured / payment.failed."""
        resolve_payment_rail()
        secret = webhook_secret if webhook_secret is not None else require_webhook_secret()
        verify_webhook_signature(body=body, signature=signature, secret=secret)
        try:
            event = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CommerceValidation("webhook body must be JSON") from exc
        if not isinstance(event, dict):
            raise CommerceValidation("webhook body must be a JSON object")
        event_name = str(event.get("event") or "")
        stable_event_id = (event_id or "").strip() or str(event.get("id") or "")
        if not stable_event_id:
            stable_event_id = hashlib.sha256(body).hexdigest()
        request_hash = hashlib.sha256(body).hexdigest()
        if event_name not in {"payment.captured", "payment.failed"}:
            return {
                "ignored": True,
                "reason": "unhandled_event",
                "event": event_name,
                "event_id": stable_event_id,
            }
        payment_entity = ((event.get("payload") or {}).get("payment") or {}).get(
            "entity"
        ) or {}
        if not isinstance(payment_entity, dict):
            raise CommerceValidation("webhook payment entity is missing")
        razorpay_order_id = str(payment_entity.get("order_id") or "")
        razorpay_payment_id = str(payment_entity.get("id") or "")
        if not razorpay_order_id or not razorpay_payment_id:
            raise CommerceValidation("webhook payment is missing order_id or id")
        status = "succeeded" if event_name == "payment.captured" else "failed"
        async with UnitOfWork(self.pool) as unit_of_work:
            idempotency = IdempotencyRepository(unit_of_work)
            created, record = await idempotency.create_or_get(
                "razorpay:webhook",
                "commerce.razorpay.webhook.v1",
                stable_event_id,
                request_hash,
                resource=razorpay_order_id,
            )
            if not created and record["status"] == "success" and record["response"] is not None:
                return {**record["response"], "duplicate": True}
        try:
            applied = await self.apply_provider_payment_result(
                principal_id=None,
                provider_reference=razorpay_order_id,
                status=status,
                provider_payment_id=razorpay_payment_id,
                expected_amount_paise=int(payment_entity["amount"])
                if payment_entity.get("amount") is not None
                else None,
                expected_currency=str(payment_entity.get("currency") or CURRENCY),
                detail={
                    "rail": RAZORPAY_PROVIDER,
                    "razorpay_order_id": razorpay_order_id,
                    "razorpay_payment_id": razorpay_payment_id,
                    "simulated": False,
                    "source": "webhook",
                    "event": event_name,
                    "event_id": stable_event_id,
                },
            )
        except CommerceNotFound:
            applied = {
                "ignored": True,
                "reason": "payment_not_found",
                "event": event_name,
                "event_id": stable_event_id,
                "razorpay_order_id": razorpay_order_id,
            }
        except Exception as error:
            async with UnitOfWork(self.pool) as unit_of_work:
                await IdempotencyRepository(unit_of_work).update_response(
                    "razorpay:webhook",
                    "commerce.razorpay.webhook.v1",
                    stable_event_id,
                    "failure",
                    {"error": str(error)},
                )
            raise
        response = _jsonable(
            {
                "duplicate": False,
                "event": event_name,
                "event_id": stable_event_id,
                **applied,
            }
        )
        async with UnitOfWork(self.pool) as unit_of_work:
            await IdempotencyRepository(unit_of_work).update_response(
                "razorpay:webhook",
                "commerce.razorpay.webhook.v1",
                stable_event_id,
                "success",
                response,
            )
        return response

    async def apply_provider_payment_result(
        self,
        *,
        principal_id: str | None,
        status: str,
        payment_attempt_id: str | UUID | None = None,
        provider_reference: str | None = None,
        provider_payment_id: str | None = None,
        expected_amount_paise: int | None = None,
        expected_currency: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Idempotently capture or fail a pending CommerceV1 payment attempt."""
        if status not in {"succeeded", "failed"}:
            raise CommerceValidation("provider result must be succeeded or failed")
        async with UnitOfWork(self.pool) as unit_of_work:
            repository = CommerceRepository(unit_of_work)
            try:
                if payment_attempt_id is not None:
                    payment = await repository.get_payment(
                        UUID(str(payment_attempt_id)), lock=True
                    )
                elif provider_reference:
                    payment = await repository.get_payment_by_provider_reference(
                        provider_reference, lock=True
                    )
                else:
                    raise CommerceValidation("payment attempt is required")
                if principal_id is not None and payment["principal_id"] != principal_id:
                    raise CommerceNotFound("payment attempt not found")
            except LookupError as exc:
                raise CommerceNotFound(str(exc)) from exc
            if expected_currency and expected_currency != CURRENCY:
                raise CommerceConflict("Razorpay payment currency must be INR")
            if (
                expected_amount_paise is not None
                and int(expected_amount_paise) != int(payment["amount_paise"])
            ):
                raise CommerceConflict("Razorpay payment amount does not match order paise")
            stored_payment_id = razorpay_payment_id_from_result(payment.get("result"))
            current = payment["status"]
            if current == "pending":
                attempt_id = payment["payment_attempt_id"]
                owner = payment["principal_id"]
            elif current in {"succeeded", "reconciled"} and status == "succeeded":
                if (
                    stored_payment_id
                    and provider_payment_id
                    and stored_payment_id != provider_payment_id
                ):
                    raise CommerceConflict("payment already captured with a different id")
                order = await repository.get_order(payment["order_id"])
                return _jsonable({"order": order, "payment_attempt": payment})
            elif current == "failed" and status == "failed":
                order = await repository.get_order(payment["order_id"])
                return _jsonable({"order": order, "payment_attempt": payment})
            elif current in {"succeeded", "reconciled"} and status == "failed":
                order = await repository.get_order(payment["order_id"])
                return _jsonable({"order": order, "payment_attempt": payment})
            else:
                raise CommerceConflict(
                    f"payment attempt is {current}, not pending for {status}"
                )
        merged = {**(payment.get("result") or {}), **(detail or {})}
        if provider_payment_id:
            merged["razorpay_payment_id"] = provider_payment_id
        try:
            return await self.record_payment_result(
                principal_id=owner,
                payment_attempt_id=attempt_id,
                status=status,
                provider_reference=provider_reference,
                detail=merged,
            )
        except CommerceConflict:
            state = await self.get_payment_state(
                principal_id=owner, payment_attempt_id=attempt_id
            )
            current_status = state["payment_attempt"]["status"]
            if status == "succeeded" and current_status in {"succeeded", "reconciled"}:
                return state
            if status == "failed" and current_status == "failed":
                return state
            raise

    async def prepare_checkout(
        self,
        *,
        principal_id: str,
        quote_id: str | UUID,
        idempotency_key: str,
        request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically create one order, reservation binding, and pending attempt."""
        if not idempotency_key:
            raise CommerceValidation("idempotency key is required")
        quote_uuid = UUID(str(quote_id))
        payload = {"quote_id": str(quote_uuid), "request": request or {}}
        request_hash = _request_hash(payload)
        rejected: Exception | None = None
        response: dict[str, Any] | None = None
        async with UnitOfWork(self.pool) as unit_of_work:
            repository = CommerceRepository(unit_of_work)
            try:
                idempotency = IdempotencyRepository(unit_of_work)
                created, record = await idempotency.create_or_get(
                    principal_id,
                    self.operation,
                    idempotency_key,
                    request_hash,
                    resource=f"quote:{quote_uuid}",
                )
                if not created:
                    if record["status"] == "success" and record["response"] is not None:
                        response = record["response"]
                    elif record["status"] == "failure":
                        rejected = CommerceConflict(
                            (record["response"] or {}).get(
                                "error", "checkout was rejected"
                            )
                        )
                    else:
                        raise CommerceConflict(
                            "checkout idempotency record is incomplete"
                        )
                if response is not None or rejected is not None:
                    quote = None
                else:
                    quote = await repository.get_quote(
                        quote_uuid, principal_id, lock=True
                    )
                if quote is not None and quote["status"] != "open":
                    raise CommerceConflict("quote is not open")
                if quote is not None and quote["expires_at"] <= self.clock():
                    await repository.release_quote(quote_uuid, "expired")
                    rejected = CommerceConflict("quote expired")
                elif quote is not None:
                    cart = await repository.get_cart_with_lines(
                        quote["cart_id"], principal_id, lock=True
                    )
                    changed = cart["version"] != quote["cart_version"]
                    current = {line["sku"]: line for line in cart["lines"]}
                    for snapshot in quote["line_snapshot"]:
                        line = current.get(snapshot["sku"])
                        changed = (
                            changed
                            or line is None
                            or (
                                line["unit_price_paise"] != snapshot["unit_price_paise"]
                                or line["inventory_version"]
                                != snapshot["inventory_version"]
                                or line["quantity"] != snapshot["quantity"]
                            )
                        )
                    if changed:
                        await repository.release_quote(quote_uuid)
                        rejected = CommerceConflict("quote changed since preview")
                if rejected is not None and created:
                    await idempotency.update_response(
                        principal_id,
                        self.operation,
                        idempotency_key,
                        "failure",
                        {"error": str(rejected)},
                    )
                elif response is None and quote is not None:
                    order, payment = await repository.create_order_and_payment(
                        uuid4(), uuid4(), quote
                    )
                    response = _jsonable({"order": order, "payment_attempt": payment})
                    await idempotency.update_response(
                        principal_id,
                        self.operation,
                        idempotency_key,
                        "success",
                        response,
                    )
            except IdempotencyConflict:
                raise
            except LookupError as exc:
                raise CommerceNotFound(str(exc)) from exc
        if rejected is not None:
            raise rejected
        if response is None:  # pragma: no cover - all non-error paths set it.
            raise RuntimeError("checkout prepare produced no response")
        return response

    async def record_payment_result(
        self,
        *,
        principal_id: str,
        payment_attempt_id: str | UUID,
        status: str,
        provider_reference: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record a simulated provider result; this is not a real provider call."""
        if status not in {"succeeded", "failed", "unknown"}:
            raise CommerceValidation(
                "payment result must be succeeded, failed, or unknown"
            )
        attempt_uuid = UUID(str(payment_attempt_id))
        async with UnitOfWork(self.pool) as unit_of_work:
            repository = CommerceRepository(unit_of_work)
            try:
                payment = await repository.get_payment(attempt_uuid, lock=True)
                if payment["principal_id"] != principal_id:
                    raise CommerceNotFound("payment attempt not found")
                if payment["status"] != "pending":
                    raise CommerceConflict("payment attempt is not pending")
                order_before = await repository.get_order(payment["order_id"], lock=True)
                require_transition("payment", payment["status"], status)
                require_transition(
                    "order",
                    order_before["status"],
                    PAYMENT_ORDER_TARGETS[status],
                )
                order, payment = await repository.set_payment_status(
                    attempt_uuid, status, detail or {}, provider_reference
                )
                if status == "succeeded":
                    await repository.consume_reservations(order["order_id"])
                    await self._post_payment(repository, order, payment, "payment")
                elif status == "failed":
                    await repository.release_order_reservations(order["order_id"])
            except LookupError as exc:
                raise CommerceNotFound(str(exc)) from exc
            except TransitionError as exc:
                raise CommerceConflict(str(exc)) from exc
        return _jsonable({"order": order, "payment_attempt": payment})

    async def reconcile_payment(
        self,
        *,
        principal_id: str,
        payment_attempt_id: str | UUID,
        outcome: str,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolve an unknown simulated result to reconciled success or failure."""
        if outcome not in {"succeeded", "failed"}:
            raise CommerceValidation(
                "reconciliation outcome must be succeeded or failed"
            )
        attempt_uuid = UUID(str(payment_attempt_id))
        async with UnitOfWork(self.pool) as unit_of_work:
            repository = CommerceRepository(unit_of_work)
            try:
                payment = await repository.get_payment(attempt_uuid, lock=True)
                if payment["principal_id"] != principal_id:
                    raise CommerceNotFound("payment attempt not found")
                persisted_status = "reconciled" if outcome == "succeeded" else "failed"
                order_before = await repository.get_order(payment["order_id"], lock=True)
                require_transition("payment", payment["status"], persisted_status)
                require_transition(
                    "order",
                    order_before["status"],
                    PAYMENT_ORDER_TARGETS[persisted_status],
                )
                order, payment = await repository.set_payment_status(
                    attempt_uuid, persisted_status, detail or {}
                )
                if outcome == "succeeded":
                    await repository.consume_reservations(order["order_id"])
                    await self._post_payment(
                        repository, order, payment, "reconciliation"
                    )
                else:
                    await repository.release_order_reservations(order["order_id"])
            except LookupError as exc:
                raise CommerceNotFound(str(exc)) from exc
            except TransitionError as exc:
                raise CommerceConflict(str(exc)) from exc
        return _jsonable({"order": order, "payment_attempt": payment})

    async def expire_quote(
        self, *, principal_id: str, quote_id: str | UUID
    ) -> dict[str, Any]:
        quote_uuid = UUID(str(quote_id))
        async with UnitOfWork(self.pool) as unit_of_work:
            repository = CommerceRepository(unit_of_work)
            try:
                quote = await repository.get_quote(quote_uuid, principal_id, lock=True)
                if quote["status"] != "open":
                    raise CommerceConflict("quote is not open")
                if quote["expires_at"] > self.clock():
                    raise CommerceConflict("quote has not expired")
                await repository.release_quote(quote_uuid, "expired")
                quote = await repository.get_quote(quote_uuid, principal_id)
            except LookupError as exc:
                raise CommerceNotFound(str(exc)) from exc
        return _jsonable(quote)

    async def issue_refund(
        self,
        *,
        seller_id: str,
        order_id: str | UUID,
        amount_paise: int,
        idempotency_key: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        """Record one simulated refund and its balanced reversal ledger."""
        if amount_paise <= 0:
            raise CommerceValidation("refund amount must be positive")
        if not idempotency_key or not correlation_id:
            raise CommerceValidation("refund idempotency and correlation are required")
        try:
            order_uuid = UUID(str(order_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise CommerceNotFound("order not found") from exc
        async with UnitOfWork(self.pool) as unit_of_work:
            repository = CommerceRepository(unit_of_work)
            try:
                order = await repository.get_refundable_order(
                    order_uuid, seller_id, lock=True
                )
                if order["payment_status"] not in {"succeeded", "reconciled"}:
                    raise CommerceConflict("only a verified paid order can be refunded")
                if amount_paise > order["payment_amount_paise"]:
                    raise CommerceConflict("refund exceeds the verified paid amount")
                refund_namespace = f"commerce-refund:{seller_id}:{idempotency_key}"
                refund, created = await repository.create_or_get_refund(
                    refund_id=uuid5(NAMESPACE_URL, refund_namespace),
                    order_id=order_uuid,
                    payment_attempt_id=order["payment_attempt_id"],
                    seller_id=seller_id,
                    principal_id=order["principal_id"],
                    amount_paise=amount_paise,
                    idempotency_key=idempotency_key,
                    correlation_id=correlation_id,
                )
                if created:
                    razorpay_payment_id = razorpay_payment_id_from_result(
                        order.get("payment_result")
                    )
                    if (
                        order.get("payment_provider") == RAZORPAY_PROVIDER
                        and razorpay_payment_id
                    ):
                        try:
                            await require_razorpay_client().refund(
                                payment_id=razorpay_payment_id,
                                amount_paise=amount_paise,
                                idempotency_key=idempotency_key,
                            )
                        except RazorpayConfigError:
                            raise
                        except Exception as exc:
                            raise CommerceConflict(
                                f"Razorpay Test refund failed: {exc}"
                            ) from exc
                    require_transition("refund", refund["status"], "succeeded")
                    transaction_id = uuid5(
                        NAMESPACE_URL, f"{refund_namespace}:ledger"
                    )
                    await repository.post_balanced_ledger(
                        transaction_id,
                        order_uuid,
                        order["payment_attempt_id"],
                        "refund",
                        amount_paise,
                        (
                            (uuid5(transaction_id, "debit"), "seller_payable", "debit"),
                            (uuid5(transaction_id, "credit"), "payment_clearing", "credit"),
                        ),
                    )
                    refund = await repository.set_refund_status(
                        refund["refund_id"], "pending", "succeeded"
                    )
            except LookupError as exc:
                raise CommerceNotFound(str(exc)) from exc
            except ValueError as exc:
                raise CommerceConflict(str(exc)) from exc
        return _jsonable({"refund": refund, "order": order})

    async def get_store(self, seller_id: str) -> dict[str, Any] | None:
        async with UnitOfWork(self.pool) as unit_of_work:
            return await CommerceRepository(unit_of_work).get_store(seller_id)

    async def upsert_store(
        self, *, seller_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        payload = normalize_store_payload(seller_id, body)
        async with UnitOfWork(self.pool) as unit_of_work:
            repository = CommerceRepository(unit_of_work)
            row = await repository.upsert_store(payload)
            await repository.ensure_owner_staff(seller_id)
        return _jsonable(row)

    async def list_staff(self, seller_id: str) -> list[dict[str, Any]]:
        async with UnitOfWork(self.pool) as unit_of_work:
            rows = await CommerceRepository(unit_of_work).list_staff(seller_id)
        return [_jsonable(row) for row in rows]

    async def invite_staff(
        self,
        *,
        seller_id: str,
        actor_principal_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        if actor_principal_id != seller_id:
            raise CommerceValidation("Staff permission denied.")
        payload = normalize_staff_payload(
            seller_id, body, actor_principal_id=actor_principal_id
        )
        async with UnitOfWork(self.pool) as unit_of_work:
            repository = CommerceRepository(unit_of_work)
            await repository.ensure_owner_staff(seller_id)
            row = await repository.upsert_staff_member(payload)
        return _jsonable(row)

    async def update_staff(
        self,
        *,
        seller_id: str,
        staff_id: str,
        actor_principal_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        if actor_principal_id != seller_id:
            raise CommerceValidation("Staff permission denied.")
        async with UnitOfWork(self.pool) as unit_of_work:
            repository = CommerceRepository(unit_of_work)
            current = await repository.get_staff(seller_id, staff_id)
            if current is None:
                raise CommerceNotFound("Staff member not found")
            if current.get("role") == "owner":
                raise CommerceValidation("Cannot modify the store owner row")
            merged = {
                "member_principal_id": current["member_principal_id"],
                "display_name": body.get("display_name", current.get("display_name")),
                "email": body.get("email", current.get("email")),
                "role": body.get("role", current.get("role")),
                "status": body.get("status", current.get("status")),
            }
            payload = normalize_staff_payload(
                seller_id, merged, actor_principal_id=actor_principal_id
            )
            row = await repository.upsert_staff_member(payload, staff_id=staff_id)
        return _jsonable(row)

    async def find_staff_membership(
        self, member_principal_id: str
    ) -> dict[str, Any] | None:
        rows = await self.list_staff_memberships(member_principal_id)
        non_owners = [row for row in rows if row.get("role") != "owner"]
        return non_owners[0] if non_owners else None

    async def list_staff_memberships(
        self, member_principal_id: str
    ) -> list[dict[str, Any]]:
        async with UnitOfWork(self.pool) as unit_of_work:
            rows = await CommerceRepository(unit_of_work).list_staff_memberships(
                member_principal_id
            )
        return [_jsonable(row) for row in rows]

    async def list_operated_seller_ids(self, principal_id: str) -> list[str]:
        memberships = await self.list_staff_memberships(principal_id)
        return operated_seller_ids(principal_id, memberships)

    async def _post_payment(
        self,
        repository: CommerceRepository,
        order: dict[str, Any],
        payment: dict[str, Any],
        posting_type: str,
    ) -> None:
        namespace = f"commerce-ledger:{payment['payment_attempt_id']}:{posting_type}"
        transaction_id = uuid5(NAMESPACE_URL, namespace)
        await repository.post_balanced_ledger(
            transaction_id,
            order["order_id"],
            payment["payment_attempt_id"],
            posting_type,
            order["landed_total_paise"],
            (
                (uuid5(transaction_id, "debit"), "payment_clearing", "debit"),
                (uuid5(transaction_id, "credit"), "seller_payable", "credit"),
            ),
        )


__all__ = [
    "CommerceConflict",
    "CommerceNotFound",
    "CommerceV1",
    "CommerceValidation",
    "IdempotencyConflict",
    "ACTIVE_STAFF_STATUSES",
    "empty_store",
    "evaluate_catalog_import_row",
    "normalize_staff_payload",
    "normalize_store_payload",
    "operated_seller_ids",
    "owner_staff_row",
    "parse_catalog_csv",
    "shared_seller_ids_from_settings",
    "staff_permissions_for",
]
