"""Legacy ``/api/demo-commerce`` shape backed exclusively by CommerceV1.

This adapter preserves the shipped Buyer/Seller response contract while the
durable CommerceV1 tables remain the only state owner in PostgreSQL mode.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.commerce_v1 import CommerceV1
from app.domain_state_machines import require_transition
from app.persistence.connection import ConnectionPool
from app.persistence.transaction import UnitOfWork


def _iso(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


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
        return {
            "issue_id": str(row["issue_id"]),
            "order_id": str(row["order_id"]),
            "status": row["status"],
            "version": row["version"],
            "reason": row["reason"],
            "description": row["description"],
            "response": row.get("response"),
            "remedy": row.get("remedy"),
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
                    SELECT status, version, fulfilment FROM commerce_orders
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
                event = {
                    "status": status,
                    "recorded_at": datetime.now().astimezone().isoformat(),
                }
                if payload.get("tracking_id"):
                    fulfilment["tracking_id"] = str(payload["tracking_id"])
                    event["tracking_id"] = str(payload["tracking_id"])
                if payload.get("provider_name"):
                    fulfilment["provider_name"] = str(payload["provider_name"])
                if payload.get("status_message"):
                    fulfilment["status_message"] = str(payload["status_message"])
                    event["status_message"] = str(payload["status_message"])
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

    async def create_issue(self, order_id: str, body: dict[str, Any]) -> dict[str, Any]:
        order = await self.get_order(order_id)
        issue_id = uuid4()
        async with UnitOfWork(self.pool) as unit_of_work:
            await unit_of_work.connection.execute(
                """
                INSERT INTO commerce_issues (
                    issue_id, order_id, principal_id, seller_id, reason, description
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    issue_id,
                    UUID(order_id),
                    order["buyer_id"],
                    order["seller_id"],
                    str(body.get("reason") or "other"),
                    str(body.get("description") or body.get("reason") or "Issue"),
                ),
            )
        return {"issue": (await self.list_issues(order_id=order_id))["issues"][0]}

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
        return await self._update_issue(
            issue_id,
            response=str(body.get("response") or body.get("message") or ""),
            status=str(body.get("status") or "acknowledged"),
        )

    async def remedy_issue(self, issue_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._update_issue(
            issue_id,
            remedy=body,
            status=str(body.get("status") or "resolution_proposed"),
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
                await cursor.execute(
                    """
                    UPDATE commerce_issues
                    SET status = 'closed', version = %s, updated_at = NOW()
                    WHERE issue_id = %s AND version = %s
                    RETURNING *
                    """,
                    (closed_version, UUID(issue_id), current["version"]),
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
            clauses.append("o.order_id = %s")
            parameters.append(UUID(order_id))
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
                current_status = current["status"]
                current_version = current["version"]
                if current_status == "open" and status == "resolution_proposed":
                    current_version = require_transition(
                        "issue",
                        current_status,
                        "acknowledged",
                        current_version=current_version,
                    )
                    current_status = "acknowledged"
                next_version = require_transition(
                    "issue",
                    current_status,
                    status,
                    current_version=current_version,
                )
                await cursor.execute(
                    """
                    UPDATE commerce_issues
                    SET response = COALESCE(%s, response), remedy = COALESCE(%s, remedy),
                        status = %s, version = %s, updated_at = NOW()
                    WHERE issue_id = %s AND version = %s RETURNING *
                    """,
                    (
                        response,
                        Jsonb(remedy) if remedy is not None else None,
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


__all__ = ["CommerceCompatibilityAdapter"]
