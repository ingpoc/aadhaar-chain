"""Legacy ``/api/demo-commerce`` shape backed exclusively by CommerceV1.

This adapter preserves the shipped Buyer/Seller response contract while the
durable CommerceV1 tables remain the only state owner in PostgreSQL mode.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.commerce_v1 import CommerceV1
from app.domain_state_machines import apply_igm_legal_path, require_transition
from app.persistence.connection import ConnectionPool
from app.persistence.transaction import UnitOfWork


def _iso(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _as_uuid(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def stamp_fulfilment_sla(
    fulfilment: dict[str, Any],
    *,
    sla_hours: int | None,
    accepted_at: datetime | None = None,
) -> dict[str, Any]:
    """Stamp fulfilment SLA due time once on accept. No-op when already set or hours missing."""
    if fulfilment.get("sla_due_at"):
        return fulfilment
    if sla_hours is None:
        return fulfilment
    hours = int(sla_hours)
    if not 1 <= hours <= 72:
        return fulfilment
    stamped_at = accepted_at or datetime.now(timezone.utc)
    if stamped_at.tzinfo is None:
        stamped_at = stamped_at.replace(tzinfo=timezone.utc)
    due_at = stamped_at + timedelta(hours=hours)
    fulfilment["accepted_at"] = stamped_at.isoformat()
    fulfilment["sla_hours"] = hours
    fulfilment["sla_due_at"] = due_at.isoformat()
    return fulfilment


class CommerceCompatibilityAdapter:
    """Translate the legacy single-item demo contract to CommerceV1 state."""

    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool
        self.commerce = CommerceV1(pool)

    @staticmethod
    def _item(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "item_id": row["sku"],
            "version": row["version"],
            "status": row["status"],
            "seller_id": row["seller_id"],
            "seller_name": row.get("seller_name"),
            "title": row["title"],
            "description": row.get("description") or "",
            "price_inr": row["unit_price_paise"] / 100,
            "inventory": row["available_quantity"] - row["reserved_quantity"],
            "category_id": row.get("category_id"),
            "delivery_estimate": row.get("delivery_estimate"),
            "return_policy": row.get("return_policy"),
            "image_url": row.get("image_url"),
            "image_caption": row.get("image_caption"),
            "delivery_areas": row.get("delivery_areas") or [],
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
        }

    @staticmethod
    def _order(row: dict[str, Any]) -> dict[str, Any]:
        lines = row.get("line_snapshot") or []
        line = lines[0] if lines else {}
        payment_status = row.get("payment_status")
        return {
            "order_id": str(row["order_id"]),
            "transaction_id": str(row["order_id"]),
            "message_id": str(row["payment_attempt_id"]),
            "buyer_id": row["principal_id"],
            "seller_id": row["seller_id"],
            "seller_name": row.get("seller_name"),
            "item_id": line.get("sku") or "",
            "item_title": line.get("title") or "",
            "item_version": line.get("inventory_version") or 1,
            "quantity": line.get("quantity") or 0,
            "amount_inr": row["landed_total_paise"] / 100,
            "status": row["status"],
            "version": row["version"],
            "fulfilment": row.get("fulfilment") or {"history": []},
            "delivery_address": (row.get("fulfilment") or {}).get("delivery_address"),
            "payment": {
                "status": payment_status,
                "amount_inr": row["payment_amount_paise"] / 100,
                "reference_id": row.get("provider_reference"),
            },
            "refunded_amount_inr": (row.get("refunded_amount_paise") or 0) / 100,
            "refund_status": row.get("refund_status"),
            "refund_authorization": (
                {
                    "receipt_id": row.get("refund_authorization_receipt_id"),
                    "outcome": row.get("refund_authorization_outcome") or "succeeded",
                    "amount_inr": int(row.get("refund_authorization_amount_inr") or 0),
                    "recorded_at": _iso(row.get("refund_authorization_created_at")),
                }
                if row.get("refund_authorization_receipt_id")
                else None
            ),
            "authorization": (
                {
                    "decision": "allow",
                    "reason_code": row.get("authorization_outcome") or "executed",
                    "receipt_id": row.get("authorization_receipt_id"),
                    "approval_id": row.get("authorization_approval_id"),
                    "amount_inr": (
                        int(row.get("authorization_amount_paise") or 0) / 100
                    ),
                    "recorded_at": _iso(row.get("authorization_created_at")),
                }
                if row.get("authorization_receipt_id")
                else None
            ),
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
        }

    async def set_delivery_context(
        self,
        order_id: str,
        *,
        principal_id: str,
        delivery_context: dict[str, Any],
    ) -> None:
        async with UnitOfWork(self.pool) as unit_of_work:
            async with unit_of_work.connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT principal_id, fulfilment
                    FROM commerce_orders
                    WHERE order_id = %s
                    FOR UPDATE
                    """,
                    (UUID(order_id),),
                )
                current = await cursor.fetchone()
                if current is None or current["principal_id"] != principal_id:
                    raise KeyError("order not found")
                fulfilment = dict(current.get("fulfilment") or {})
                fulfilment["delivery_address"] = delivery_context
                await cursor.execute(
                    """
                    UPDATE commerce_orders
                    SET fulfilment = %s, updated_at = NOW()
                    WHERE order_id = %s
                    """,
                    (Jsonb(fulfilment), UUID(order_id)),
                )

    @staticmethod
    def _issue(row: dict[str, Any]) -> dict[str, Any]:
        protocol_order_id = str(row.get("protocol_order_id") or "").strip() or None
        local_order_id = row.get("order_id")
        return {
            "issue_id": str(row["issue_id"]),
            "order_id": str(local_order_id) if local_order_id else protocol_order_id,
            "protocol_order_id": protocol_order_id,
            "protocol_transaction_id": str(row.get("protocol_transaction_id") or "").strip()
            or None,
            "principal_id": row.get("principal_id"),
            "seller_id": row.get("seller_id"),
            "status": row["status"],
            "version": row["version"],
            "reason": row["reason"],
            "description": row["description"],
            "response": row.get("response"),
            "remedy": row.get("remedy"),
            "owner_id": row.get("owner_id"),
            "response_due_at": _iso(row["response_due_at"])
            if row.get("response_due_at")
            else None,
            "escalation_due_at": _iso(row["escalation_due_at"])
            if row.get("escalation_due_at")
            else None,
            "history": row.get("history") or [],
            "outcome_receipt": row.get("outcome_receipt"),
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
        }

    @staticmethod
    def _return(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "return_id": str(row["return_id"]),
            "order_id": str(row["order_id"]),
            "principal_id": row["principal_id"],
            "seller_id": row["seller_id"],
            "status": row["status"],
            "version": row["version"],
            "reason": row["reason"],
            "resolution": row.get("resolution"),
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
        }

    @staticmethod
    def _store(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "seller_id": row["seller_id"],
            "store_name": row.get("store_name") or "",
            "city": row.get("city") or "",
            "state": row.get("state") or "",
            "pin": row.get("pin") or "",
            "serviceability_tokens": list(row.get("serviceability_tokens") or []),
            "fulfilment_sla_hours": row.get("fulfilment_sla_hours"),
            "return_window_days": row.get("return_window_days"),
            "support_hours": row.get("support_hours") or "",
            "status": row.get("status") or "draft",
            "setup_required": row.get("status") != "ready",
            "version": row.get("version") or 1,
            "created_at": _iso(row["created_at"]) if row.get("created_at") else None,
            "updated_at": _iso(row["updated_at"]) if row.get("updated_at") else None,
        }

    async def get_store(self, seller_id: str) -> dict[str, Any] | None:
        row = await self.commerce.get_store(seller_id)
        return self._store(row)

    async def upsert_store(
        self, seller_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        row = await self.commerce.upsert_store(seller_id=seller_id, body=body)
        store = self._store(row)
        assert store is not None
        return {"store": store}

    def _staff(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        from app.commerce_v1 import staff_permissions_for

        role = str(row.get("role") or "viewer")
        return {
            "staff_id": row.get("staff_id"),
            "seller_id": row.get("seller_id"),
            "member_principal_id": row.get("member_principal_id"),
            "display_name": row.get("display_name") or "",
            "email": row.get("email") or "",
            "role": role,
            "status": row.get("status") or "invited",
            "version": row.get("version") or 1,
            "permissions": sorted(staff_permissions_for(role)),
            "created_at": _iso(row["created_at"]) if row.get("created_at") else None,
            "updated_at": _iso(row["updated_at"]) if row.get("updated_at") else None,
        }

    async def list_staff(self, seller_id: str) -> dict[str, Any]:
        store = await self.get_store(seller_id)
        if store is None or store.get("status") != "ready":
            raise ValueError("Complete store setup before managing staff.")
        rows = await self.commerce.list_staff(seller_id)
        members = [self._staff(row) for row in rows]
        return {"members": members, "count": len(members)}

    async def invite_staff(
        self, seller_id: str, actor_principal_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        store = await self.get_store(seller_id)
        if store is None or store.get("status") != "ready":
            raise ValueError("Complete store setup before managing staff.")
        if actor_principal_id != seller_id:
            raise PermissionError("Staff permission denied.")
        row = await self.commerce.invite_staff(
            seller_id=seller_id, actor_principal_id=actor_principal_id, body=body
        )
        member = self._staff(row)
        assert member is not None
        return {"member": member}

    async def update_staff(
        self,
        seller_id: str,
        staff_id: str,
        actor_principal_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        store = await self.get_store(seller_id)
        if store is None or store.get("status") != "ready":
            raise ValueError("Complete store setup before managing staff.")
        if actor_principal_id != seller_id:
            raise PermissionError("Staff permission denied.")
        row = await self.commerce.update_staff(
            seller_id=seller_id,
            staff_id=staff_id,
            actor_principal_id=actor_principal_id,
            body=body,
        )
        member = self._staff(row)
        assert member is not None
        return {"member": member}

    async def find_staff_membership(
        self, member_principal_id: str
    ) -> dict[str, Any] | None:
        row = await self.commerce.find_staff_membership(member_principal_id)
        return self._staff(row)

    async def import_catalog(
        self,
        seller_id: str,
        *,
        csv_text: str | None = None,
        items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        from app.commerce_v1 import (
            CommerceValidation,
            evaluate_catalog_import_row,
            parse_catalog_csv,
        )

        store = await self.get_store(seller_id)
        if store is None or store.get("status") != "ready":
            raise ValueError("Complete store setup before importing catalog.")
        rows = list(items or [])
        if csv_text is not None and str(csv_text).strip():
            rows = parse_catalog_csv(csv_text) + rows
        if not rows:
            raise CommerceValidation("Catalog import requires CSV rows or item objects.")
        imported: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        for index, row in enumerate(rows, start=1):
            payload, row_issues = evaluate_catalog_import_row(row, index=index)
            issues.extend(row_issues)
            if payload is None:
                continue
            created = await self.create_item({**payload, "seller_id": seller_id})
            imported.append(created["item"])
        return {
            "imported": imported,
            "issues": issues,
            "imported_count": len(imported),
            "issue_count": len(issues),
        }

    async def create_item(self, body: dict[str, Any]) -> dict[str, Any]:
        sku = str(body.get("item_id") or f"item_{uuid4().hex[:16]}")
        seller_id = str(body.get("seller_id") or "ondcseller")
        title = str(body.get("title") or body.get("name") or "Untitled item")
        await self.commerce.upsert_inventory(
            seller_id=seller_id,
            sku=sku,
            title=title,
            unit_price_paise=round(float(body.get("price_inr") or 0) * 100),
            available_quantity=int(body.get("inventory") or 0),
        )
        await self._update_item_metadata(seller_id, sku, body)
        item = await self.get_item(sku)
        return {"item": item, "inventory": item["inventory"]}

    async def update_item(self, item_id: str, body: dict[str, Any]) -> dict[str, Any]:
        current = await self._inventory(item_id)
        title = str(body.get("title") or body.get("name") or current["title"])
        price = body.get("price_inr")
        inventory = body.get("inventory")
        await self.commerce.upsert_inventory(
            seller_id=current["seller_id"],
            sku=item_id,
            title=title,
            unit_price_paise=(
                round(float(price) * 100)
                if price is not None
                else current["unit_price_paise"]
            ),
            available_quantity=(
                int(inventory)
                if inventory is not None
                else current["available_quantity"]
            ),
        )
        await self._update_item_metadata(current["seller_id"], item_id, body)
        item = await self.get_item(item_id)
        return {"item": item, "inventory": item["inventory"]}

    async def publish_item(
        self, item_id: str, status: str = "published"
    ) -> dict[str, Any]:
        if status not in {"draft", "published", "archived"}:
            raise ValueError("unsupported catalog status")
        async with UnitOfWork(self.pool) as unit_of_work:
            result = await unit_of_work.connection.execute(
                """
                UPDATE commerce_inventory
                SET status = %s, version = version + 1, updated_at = NOW()
                WHERE sku = %s RETURNING sku
                """,
                (status, item_id),
            )
            if await result.fetchone() is None:
                raise KeyError("item not found")
        item = await self.get_item(item_id)
        return {"item": item, "inventory": item["inventory"]}

    async def get_item(
        self, item_id: str, *, seller_id: str | None = None
    ) -> dict[str, Any]:
        row = await self._inventory(item_id, seller_id=seller_id)
        return self._item(row)

    async def list_items(
        self,
        *,
        seller_id: str | None = None,
        query: str | None = None,
        published_only: bool = False,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if seller_id is not None:
            clauses.append("seller_id = %s")
            parameters.append(seller_id)
        if published_only:
            clauses.append("status = 'published'")
        if query and query.strip():
            clauses.append("(title ILIKE %s OR description ILIKE %s)")
            needle = f"%{query.strip()}%"
            parameters.extend((needle, needle))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with UnitOfWork(self.pool) as unit_of_work:
            async with unit_of_work.connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    f"SELECT * FROM commerce_inventory {where} ORDER BY created_at DESC",
                    parameters,
                )
                rows = list(await cursor.fetchall())
        items = [self._item(row) for row in rows]
        return {"items": items, "count": len(items)}

    async def create_order(
        self, body: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        item = await self._inventory(str(body["item_id"]))
        principal_id = str(body.get("buyer_id") or "ondcbuyer")
        base = idempotency_key or f"fixture-order:{uuid4()}"
        cart = await self.commerce.create_cart(
            principal_id=principal_id,
            seller_id=item["seller_id"],
            idempotency_key=f"{base}:cart",
        )
        cart = await self.commerce.set_cart_line(
            principal_id=principal_id,
            cart_id=cart["cart_id"],
            sku=item["sku"],
            quantity=int(body.get("quantity") or 1),
            expected_version=cart["version"],
            idempotency_key=f"{base}:line",
        )
        quote = await self.commerce.preview_checkout(
            principal_id=principal_id,
            cart_id=cart["cart_id"],
            expected_version=cart["version"],
            idempotency_key=f"{base}:preview",
        )
        prepared = await self.commerce.prepare_checkout(
            principal_id=principal_id,
            quote_id=quote["quote_id"],
            idempotency_key=f"{base}:prepare",
            request={"source": "demo-commerce-compatibility"},
        )
        mode = str(body.get("payment_mode") or "success")
        status = {"success": "succeeded", "failure": "failed"}.get(mode, mode)
        if status not in {"succeeded", "failed", "unknown"}:
            status = "succeeded"
        payment_id = prepared["payment_attempt"]["payment_attempt_id"]
        current = await self.commerce.get_payment_state(
            principal_id=principal_id, payment_attempt_id=payment_id
        )
        current_status = current["payment_attempt"]["status"]
        if current_status == "pending":
            await self.commerce.record_payment_result(
                principal_id=principal_id,
                payment_attempt_id=payment_id,
                status=status,
                detail={"source": "demo-commerce-compatibility"},
            )
        elif current_status != status:
            raise ValueError(
                "idempotent order replay requested a different payment outcome"
            )
        order = await self.get_order(str(prepared["order"]["order_id"]))
        return {"order": order}

    async def get_order(
        self,
        order_id: str,
        *,
        principal_id: str | None = None,
        seller_id: str | None = None,
    ) -> dict[str, Any]:
        rows = await self._orders(
            order_id=order_id, principal_id=principal_id, seller_id=seller_id
        )
        if not rows:
            raise KeyError("order not found")
        return self._order(rows[0])

    async def issue_refund(
        self,
        order_id: str,
        *,
        seller_id: str,
        amount_inr: int,
        idempotency_key: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        return await self.commerce.issue_refund(
            seller_id=seller_id,
            order_id=order_id,
            amount_paise=amount_inr * 100,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
        )

    async def list_orders(
        self, *, principal_id: str | None = None, seller_id: str | None = None
    ) -> dict[str, Any]:
        orders = [
            self._order(row)
            for row in await self._orders(
                principal_id=principal_id, seller_id=seller_id
            )
        ]
        return {"orders": orders, "count": len(orders)}

    async def transition_order(
        self,
        order_id: str,
        status: str,
        *,
        expected_version: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = payload or {}
        async with UnitOfWork(self.pool) as unit_of_work:
            async with unit_of_work.connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT status, version, fulfilment, seller_id FROM commerce_orders
                    WHERE order_id = %s FOR UPDATE
                    """,
                    (UUID(order_id),),
                )
                current = await cursor.fetchone()
                if current is None:
                    raise KeyError("order not found")
                next_version = require_transition(
                    "order",
                    current["status"],
                    status,
                    current_version=current["version"],
                    expected_version=expected_version,
                )
                fulfilment = dict(current.get("fulfilment") or {})
                history = list(fulfilment.get("history") or [])
                recorded_at = datetime.now(timezone.utc)
                event = {
                    "status": status,
                    "recorded_at": recorded_at.isoformat(),
                }
                if payload.get("tracking_id"):
                    fulfilment["tracking_id"] = str(payload["tracking_id"])
                    event["tracking_id"] = str(payload["tracking_id"])
                if payload.get("provider_name"):
                    fulfilment["provider_name"] = str(payload["provider_name"])
                if payload.get("status_message"):
                    fulfilment["status_message"] = str(payload["status_message"])
                    event["status_message"] = str(payload["status_message"])
                if payload.get("logistics"):
                    logistics = dict(payload["logistics"])
                    fulfilment["logistics"] = logistics
                    event["logistics_transaction_id"] = logistics["transaction_id"]
                if status in {"confirmed", "accepted"} and not fulfilment.get("sla_due_at"):
                    await cursor.execute(
                        """
                        SELECT fulfilment_sla_hours
                        FROM commerce_seller_stores
                        WHERE seller_id = %s
                        """,
                        (str(current["seller_id"]),),
                    )
                    store = await cursor.fetchone()
                    sla_hours = (store or {}).get("fulfilment_sla_hours")
                    stamp_fulfilment_sla(
                        fulfilment,
                        sla_hours=int(sla_hours) if sla_hours is not None else None,
                        accepted_at=recorded_at,
                    )
                    if fulfilment.get("sla_due_at"):
                        event["sla_due_at"] = fulfilment["sla_due_at"]
                        event["sla_hours"] = fulfilment.get("sla_hours")
                fulfilment["status"] = status
                fulfilment["history"] = [*history, event]
                await cursor.execute(
                    """
                    UPDATE commerce_orders
                    SET status = %s, version = %s, fulfilment = %s, updated_at = NOW()
                    WHERE order_id = %s AND version = %s RETURNING order_id
                    """,
                    (
                        status,
                        next_version,
                        Jsonb(fulfilment),
                        UUID(order_id),
                        current["version"],
                    ),
                )
                if await cursor.fetchone() is None:
                    raise RuntimeError("stale order transition")
        return {"order": await self.get_order(order_id)}

    async def rebind_rejected_logistics_provider(
        self, order_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        await self.commerce.rebind_rejected_logistics_provider(
            order_id=order_id,
            logistics=dict(payload.get("logistics") or {}),
        )
        return {"order": await self.get_order(order_id)}

    async def create_issue(self, order_id: str, body: dict[str, Any]) -> dict[str, Any]:
        order = await self.get_order(order_id)
        issue_id = uuid4()
        async with UnitOfWork(self.pool) as unit_of_work:
            await unit_of_work.connection.execute(
                """
                INSERT INTO commerce_issues (
                    issue_id, order_id, principal_id, seller_id, reason, description,
                    history
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    issue_id,
                    UUID(order_id),
                    order["buyer_id"],
                    order["seller_id"],
                    str(body.get("reason") or "other"),
                    str(body.get("description") or body.get("reason") or "Issue"),
                    Jsonb(
                        [
                            {
                                "status": "open",
                                "actor_id": order["buyer_id"],
                                "note": "Customer issue created",
                                "at": datetime.now(timezone.utc).isoformat(),
                            }
                        ]
                    ),
                ),
            )
        return {"issue": (await self.list_issues(order_id=order_id))["issues"][0]}

    async def bind_protocol_issue(
        self,
        order_id: str,
        body: dict[str, Any],
        *,
        transaction_id: str | None = None,
    ) -> dict[str, Any]:
        """Create or reuse a commerce issue bound to an ONDC confirm order_id."""
        existing = await self.find_issue_for_protocol_order(order_id)
        if existing is not None:
            return {"issue": existing, "created": False}
        issue_id = uuid4()
        principal_id = str(body.get("principal_id") or "ondc-protocol").strip()
        seller_id = str(body.get("seller_id") or "ondc-bpp").strip()
        async with UnitOfWork(self.pool) as unit_of_work:
            await unit_of_work.connection.execute(
                """
                INSERT INTO commerce_issues (
                    issue_id, order_id, principal_id, seller_id, reason, description,
                    history, protocol_order_id, protocol_transaction_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    issue_id,
                    None,
                    principal_id,
                    seller_id,
                    str(body.get("reason") or "fulfillment"),
                    str(body.get("description") or "Protocol IGM issue"),
                    Jsonb(
                        [
                            {
                                "status": "open",
                                "actor_id": principal_id,
                                "note": "Protocol-bound IGM issue from confirmed ONDC order",
                                "at": datetime.now(timezone.utc).isoformat(),
                            }
                        ]
                    ),
                    order_id,
                    str(transaction_id or "").strip() or None,
                ),
            )
        bound = await self.find_issue_for_protocol_order(order_id)
        if bound is None:
            raise RuntimeError("protocol issue bind failed")
        return {"issue": bound, "created": True}

    async def find_issue_for_protocol_order(
        self, order_id: str
    ) -> dict[str, Any] | None:
        order_id = str(order_id or "").strip()
        if not order_id:
            return None
        async with UnitOfWork(self.pool) as unit_of_work:
            async with unit_of_work.connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT * FROM commerce_issues
                    WHERE protocol_order_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (order_id,),
                )
                row = await cursor.fetchone()
        return self._issue(row) if row else None

    async def create_return(
        self, order_id: str, *, principal_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        order = await self.get_order(order_id, principal_id=principal_id)
        if order["status"] not in {"delivered", "closed", "fulfilled"}:
            raise ValueError("return requires a completed delivery")
        return_id = uuid4()
        async with UnitOfWork(self.pool) as unit_of_work:
            try:
                await unit_of_work.connection.execute(
                    """
                    INSERT INTO commerce_returns (
                        return_id, order_id, principal_id, seller_id, reason
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        return_id,
                        UUID(order_id),
                        principal_id,
                        order["seller_id"],
                        str(body.get("reason") or "Buyer requested return"),
                    ),
                )
            except Exception as error:
                if "commerce_returns_one_per_order_idx" in str(error):
                    raise ValueError(
                        "return already requested for this order"
                    ) from None
                raise
        return {
            "return": (
                await self.list_returns(principal_id=principal_id, order_id=order_id)
            )["returns"][0]
        }

    async def list_returns(
        self,
        *,
        principal_id: str | None = None,
        seller_id: str | None = None,
        order_id: str | None = None,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        parameters: list[Any] = []
        for column, value in (
            ("principal_id", principal_id),
            ("seller_id", seller_id),
            ("order_id", UUID(order_id) if order_id else None),
        ):
            if value is not None:
                clauses.append(f"{column} = %s")
                parameters.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with UnitOfWork(self.pool) as unit_of_work:
            async with unit_of_work.connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    f"SELECT * FROM commerce_returns {where} ORDER BY created_at DESC",
                    parameters,
                )
                rows = list(await cursor.fetchall())
        returns = [self._return(row) for row in rows]
        return {"returns": returns, "count": len(returns)}

    async def list_issues(
        self,
        *,
        principal_id: str | None = None,
        seller_id: str | None = None,
        order_id: str | None = None,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        parameters: list[Any] = []
        for column, value in (
            ("principal_id", principal_id),
            ("seller_id", seller_id),
            ("order_id", UUID(order_id) if order_id else None),
        ):
            if value is not None:
                clauses.append(f"{column} = %s")
                parameters.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with UnitOfWork(self.pool) as unit_of_work:
            async with unit_of_work.connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    f"""
                    SELECT issue.*,
                           outcome.payload AS outcome_receipt
                    FROM commerce_issues AS issue
                    LEFT JOIN LATERAL (
                        SELECT receipt.payload
                        FROM agentguard_receipts AS receipt
                        WHERE receipt.principal_id = issue.principal_id
                          AND receipt.payload->>'action' = 'buyer.remedy.accept'
                          AND receipt.payload->'bound_action'->>'resource_id'
                              = issue.issue_id::text
                        ORDER BY receipt.created_at DESC
                        LIMIT 1
                    ) AS outcome ON TRUE
                    {where}
                    ORDER BY issue.created_at DESC
                    """,
                    parameters,
                )
                rows = list(await cursor.fetchall())
        issues = [self._issue(row) for row in rows]
        return {"issues": issues, "count": len(issues)}

    async def respond_issue(
        self, issue_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return await self.transition_issue(
            issue_id,
            {
                **body,
                "status": body.get("status") or "acknowledged",
                "actor_id": body.get("actor_id") or body.get("owner_id") or "seller",
                "owner_id": body.get("owner_id") or body.get("actor_id") or "seller",
                "response_target_minutes": body.get("response_target_minutes") or 240,
            },
        )

    async def transition_issue(
        self, issue_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._update_issue(
            issue_id,
            response=str(body.get("response") or body.get("message") or ""),
            status=str(body.get("status") or ""),
            actor_id=str(body.get("actor_id") or ""),
            owner_id=str(body.get("owner_id") or "") or None,
            response_target_minutes=(
                int(body["response_target_minutes"])
                if body.get("response_target_minutes") is not None
                else None
            ),
        )

    async def remedy_issue(self, issue_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._update_issue(
            issue_id,
            remedy=body,
            status=str(body.get("status") or "resolution_proposed"),
            actor_id=str(body.get("actor_id") or "seller"),
            owner_id=str(body.get("owner_id") or body.get("actor_id") or "seller"),
            response_target_minutes=int(body.get("response_target_minutes") or 240),
        )

    async def accept_remedy(self, issue_id: str) -> dict[str, Any]:
        async with UnitOfWork(self.pool) as unit_of_work:
            async with unit_of_work.connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    "SELECT * FROM commerce_issues WHERE issue_id = %s FOR UPDATE",
                    (UUID(issue_id),),
                )
                current = await cursor.fetchone()
                if current is None:
                    raise KeyError("issue not found")
                accepted_version = require_transition(
                    "issue",
                    current["status"],
                    "accepted",
                    current_version=current["version"],
                )
                closed_version = require_transition(
                    "issue",
                    "accepted",
                    "closed",
                    current_version=accepted_version,
                )
                actor_id = current["principal_id"]
                history = list(current.get("history") or [])
                now = datetime.now(timezone.utc).isoformat()
                history.extend(
                    [
                        {
                            "status": "accepted",
                            "actor_id": actor_id,
                            "note": "Buyer accepted remedy",
                            "at": now,
                        },
                        {
                            "status": "closed",
                            "actor_id": actor_id,
                            "note": "Issue closed",
                            "at": now,
                        },
                    ]
                )
                await cursor.execute(
                    """
                    UPDATE commerce_issues
                    SET status = 'closed', version = %s, history = %s,
                        updated_at = NOW()
                    WHERE issue_id = %s AND version = %s
                    RETURNING *
                    """,
                    (
                        closed_version,
                        Jsonb(history),
                        UUID(issue_id),
                        current["version"],
                    ),
                )
                row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("stale issue transition")
        return {"issue": self._issue(row)}

    async def cleanup(
        self, *, order_ids: set[str], item_ids: set[str]
    ) -> dict[str, Any]:
        removed_items = 0
        retained_items: list[str] = []
        async with UnitOfWork(self.pool) as unit_of_work:
            for item_id in item_ids:
                result = await unit_of_work.connection.execute(
                    """
                    DELETE FROM commerce_inventory i
                    WHERE i.sku = %s
                      AND NOT EXISTS (
                        SELECT 1 FROM commerce_inventory_reservations r
                        WHERE r.seller_id = i.seller_id AND r.sku = i.sku
                      )
                    """,
                    (item_id,),
                )
                removed_items += result.rowcount
                if result.rowcount == 0:
                    retained_items.append(item_id)
        return {
            "removed_orders": 0,
            "removed_items": removed_items,
            "retained_order_ids": sorted(order_ids),
            "retained_item_ids": sorted(retained_items),
            "note": "Durable CommerceV1 financial orders are not deleted by fixture cleanup.",
        }

    async def _inventory(
        self, item_id: str, *, seller_id: str | None = None
    ) -> dict[str, Any]:
        query = "SELECT * FROM commerce_inventory WHERE sku = %s"
        parameters: list[Any] = [item_id]
        if seller_id is not None:
            query += " AND seller_id = %s"
            parameters.append(seller_id)
        async with UnitOfWork(self.pool) as unit_of_work:
            async with unit_of_work.connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(query, parameters)
                row = await cursor.fetchone()
        if row is None:
            raise KeyError("item not found")
        return row

    async def _update_item_metadata(
        self, seller_id: str, sku: str, body: dict[str, Any]
    ) -> None:
        fields = {
            key: body[key]
            for key in (
                "description",
                "seller_name",
                "category_id",
                "delivery_estimate",
                "return_policy",
                "image_url",
                "image_caption",
                "delivery_areas",
            )
            if key in body and body[key] is not None
        }
        if not fields:
            return
        assignments = [f"{key} = %s" for key in fields]
        values = [
            Jsonb(value) if key == "delivery_areas" else value
            for key, value in fields.items()
        ]
        async with UnitOfWork(self.pool) as unit_of_work:
            await unit_of_work.connection.execute(
                f"""
                UPDATE commerce_inventory SET {", ".join(assignments)}, updated_at = NOW()
                WHERE seller_id = %s AND sku = %s
                """,
                (*values, seller_id, sku),
            )

    async def _orders(
        self,
        *,
        order_id: str | None = None,
        principal_id: str | None = None,
        seller_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if order_id is not None:
            parsed = _as_uuid(order_id)
            if parsed is None:
                return []
            clauses.append("o.order_id = %s")
            parameters.append(parsed)
        if principal_id is not None:
            clauses.append("o.principal_id = %s")
            parameters.append(principal_id)
        if seller_id is not None:
            clauses.append("o.seller_id = %s")
            parameters.append(seller_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with UnitOfWork(self.pool) as unit_of_work:
            async with unit_of_work.connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    f"""
                    SELECT o.*, q.line_snapshot, p.payment_attempt_id,
                           p.status AS payment_status,
                           p.amount_paise AS payment_amount_paise,
                           p.provider_reference,
                           (
                               SELECT inventory.seller_name
                               FROM commerce_inventory AS inventory
                               WHERE inventory.seller_id = o.seller_id
                                 AND inventory.sku = q.line_snapshot->0->>'sku'
                           ) AS seller_name,
                           auth_receipt.authorization_receipt_id,
                           auth_receipt.authorization_approval_id,
                           auth_receipt.authorization_outcome,
                           auth_receipt.authorization_amount_paise,
                           auth_receipt.authorization_created_at,
                           refund_receipt.refund_authorization_receipt_id,
                           refund_receipt.refund_authorization_outcome,
                           refund_receipt.refund_authorization_amount_inr,
                           refund_receipt.refund_authorization_created_at,
                           refund.refunded_amount_paise,
                           refund.refund_status
                    FROM commerce_orders o
                    JOIN commerce_quotes q ON q.quote_id = o.quote_id
                    JOIN commerce_payment_attempts p ON p.order_id = o.order_id
                    LEFT JOIN LATERAL (
                        SELECT
                            receipt.receipt_id AS authorization_receipt_id,
                            receipt.approval_id AS authorization_approval_id,
                            receipt.payload->>'outcome' AS authorization_outcome,
                            receipt.payload->'bound_action'->>'landed_total_paise'
                                AS authorization_amount_paise,
                            receipt.created_at AS authorization_created_at
                        FROM agentguard_receipts AS receipt
                        WHERE receipt.principal_id = o.principal_id
                          AND receipt.payload->>'action' = 'buyer.checkout.commit'
                          AND receipt.payload->'result'->'order'->>'order_id'
                              = o.order_id::text
                        ORDER BY receipt.created_at DESC
                        LIMIT 1
                    ) AS auth_receipt ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT
                            receipt.receipt_id AS refund_authorization_receipt_id,
                            receipt.payload->>'outcome' AS refund_authorization_outcome,
                            receipt.payload->'bound_action'->>'amount_inr'
                                AS refund_authorization_amount_inr,
                            COALESCE(
                                NULLIF(receipt.payload->>'created_at', '')::timestamptz,
                                receipt.created_at
                            ) AS refund_authorization_created_at
                        FROM agentguard_receipts AS receipt
                        WHERE receipt.principal_id = o.seller_id
                          AND receipt.payload->>'action' = 'seller.refund.issue'
                          AND receipt.payload->'bound_action'->>'resource_id'
                              = o.order_id::text
                        ORDER BY receipt.created_at DESC
                        LIMIT 1
                    ) AS refund_receipt ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT amount_paise AS refunded_amount_paise,
                               status AS refund_status
                        FROM commerce_refunds
                        WHERE order_id = o.order_id
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) AS refund ON TRUE
                    {where}
                    ORDER BY o.created_at DESC
                    """,
                    parameters,
                )
                return list(await cursor.fetchall())

    async def _update_issue(
        self,
        issue_id: str,
        *,
        response: str | None = None,
        remedy: dict[str, Any] | None = None,
        status: str,
        actor_id: str,
        owner_id: str | None = None,
        response_target_minutes: int | None = None,
    ) -> dict[str, Any]:
        async with UnitOfWork(self.pool) as unit_of_work:
            async with unit_of_work.connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT * FROM commerce_issues
                    WHERE issue_id = %s FOR UPDATE
                    """,
                    (UUID(issue_id),),
                )
                current = await cursor.fetchone()
                if current is None:
                    raise KeyError("issue not found")
                if not actor_id:
                    raise ValueError("Issue transition requires actor_id.")
                current_status = current["status"]
                current_version = current["version"]
                effective_owner = owner_id or current.get("owner_id")
                response_due_at = current.get("response_due_at")
                escalation_due_at = current.get("escalation_due_at")
                history = list(current.get("history") or [])
                now = datetime.now(timezone.utc)
                if current_status == "open" and status == "resolution_proposed":
                    current_version = require_transition(
                        "issue",
                        current_status,
                        "acknowledged",
                        current_version=current_version,
                    )
                    current_status = "acknowledged"
                    effective_owner = effective_owner or actor_id
                    response_target_minutes = response_target_minutes or 240
                    response_due_at = now + timedelta(minutes=response_target_minutes)
                    escalation_due_at = now + timedelta(
                        minutes=response_target_minutes * 2
                    )
                    history.append(
                        {
                            "status": "acknowledged",
                            "actor_id": actor_id,
                            "note": "Seller accepted ownership",
                            "at": now.isoformat(),
                        }
                    )
                if status == "acknowledged":
                    if not effective_owner:
                        raise ValueError("Issue acknowledgement requires owner_id.")
                    if (
                        response_target_minutes is None
                        or not 1 <= response_target_minutes <= 10_080
                    ):
                        raise ValueError(
                            "response_target_minutes must be between 1 and 10080."
                        )
                    response_due_at = now + timedelta(minutes=response_target_minutes)
                    escalation_due_at = now + timedelta(
                        minutes=response_target_minutes * 2
                    )
                elif not effective_owner:
                    raise ValueError(
                        "Issue escalation or rejection requires an assigned owner."
                    )
                next_version = require_transition(
                    "issue",
                    current_status,
                    status,
                    current_version=current_version,
                )
                history.append(
                    {
                        "status": status,
                        "actor_id": actor_id,
                        "note": response or str((remedy or {}).get("message") or ""),
                        "at": now.isoformat(),
                    }
                )
                await cursor.execute(
                    """
                    UPDATE commerce_issues
                    SET response = COALESCE(%s, response), remedy = COALESCE(%s, remedy),
                        owner_id = %s, response_due_at = %s, escalation_due_at = %s,
                        history = %s, status = %s, version = %s, updated_at = NOW()
                    WHERE issue_id = %s AND version = %s RETURNING *
                    """,
                    (
                        response,
                        Jsonb(remedy) if remedy is not None else None,
                        effective_owner,
                        response_due_at,
                        escalation_due_at,
                        Jsonb(history),
                        status,
                        next_version,
                        UUID(issue_id),
                        current["version"],
                    ),
                )
                row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("stale issue transition")
        return {"issue": self._issue(row)}

    async def record_igm_network_event(
        self,
        issue_id: str,
        *,
        action: str,
        transaction_id: str,
        message_id: str,
        network_status: str = "",
        note: str = "",
        signature_verified: bool = False,
        actor_id: str = "ondc-igm",
    ) -> dict[str, Any]:
        """Append a signed IGM correlation event; transition only on a legal path."""
        async with UnitOfWork(self.pool) as unit_of_work:
            async with unit_of_work.connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    "SELECT * FROM commerce_issues WHERE issue_id = %s FOR UPDATE",
                    (UUID(issue_id),),
                )
                current = await cursor.fetchone()
                if current is None:
                    raise KeyError("issue not found")
                current_status = str(current["status"] or "open")
                current_version = int(current["version"] or 1)
                next_status, next_version = apply_igm_legal_path(
                    current_status,
                    network_status,
                    current_version=current_version,
                )
                owner_id = current.get("owner_id") or current.get("seller_id")
                history = list(current.get("history") or [])
                history.append(
                    {
                        "status": next_status,
                        "actor_id": actor_id,
                        "note": note or f"IGM {action}",
                        "at": datetime.now(timezone.utc).isoformat(),
                        "network": {
                            "action": action,
                            "transaction_id": transaction_id,
                            "message_id": message_id,
                            "network_status": (
                                str(network_status or "").strip().upper() or None
                            ),
                            "signature_verified": bool(signature_verified),
                        },
                    }
                )
                await cursor.execute(
                    """
                    UPDATE commerce_issues
                    SET owner_id = COALESCE(owner_id, %s),
                        history = %s, status = %s, version = %s, updated_at = NOW()
                    WHERE issue_id = %s AND version = %s RETURNING *
                    """,
                    (
                        owner_id,
                        Jsonb(history),
                        next_status,
                        next_version,
                        UUID(issue_id),
                        current["version"],
                    ),
                )
                row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("stale IGM issue correlation")
        return {"issue": self._issue(row)}


__all__ = ["CommerceCompatibilityAdapter", "stamp_fulfilment_sla"]
