"""Durable, single-seller commerce application service.

This module models a simulated payment saga.  It deliberately does not call or
claim a real payment provider: prepare, result recording, and reconciliation are
separate durable operations.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .persistence.commerce_repository import CommerceRepository
from .persistence.connection import ConnectionPool
from .persistence.repositories import IdempotencyConflict, IdempotencyRepository
from .persistence.transaction import UnitOfWork


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


class CommerceV1:
    """Application boundary for durable INR/paise single-seller commerce."""

    operation = "commerce.checkout.prepare.v1"

    def __init__(self, pool: ConnectionPool, *, clock: Clock = _utcnow) -> None:
        self.pool = pool
        self.clock = clock

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
                if payment["status"] != "unknown":
                    raise CommerceConflict("only unknown payments can be reconciled")
                persisted_status = "reconciled" if outcome == "succeeded" else "failed"
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
]
