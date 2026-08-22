"""PostgreSQL repository for the durable single-seller commerce domain."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Any, Iterable
from uuid import UUID, uuid5

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.domain_state_machines import PAYMENT_ORDER_TARGETS

from .transaction import UnitOfWork


class CommerceRepository:
    """All commerce SQL, scoped to one explicit unit of work."""

    def __init__(self, unit_of_work: UnitOfWork) -> None:
        if unit_of_work.connection is None:
            raise RuntimeError("CommerceRepository requires an active UnitOfWork")
        self.connection = unit_of_work.connection

    async def upsert_inventory(
        self,
        seller_id: str,
        sku: str,
        title: str,
        unit_price_paise: int,
        available_quantity: int,
    ) -> dict[str, Any]:
        result = await self.connection.execute(
            """
            INSERT INTO commerce_inventory (
                seller_id, sku, title, unit_price_paise, available_quantity
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (seller_id, sku) DO UPDATE SET
                title = EXCLUDED.title,
                unit_price_paise = EXCLUDED.unit_price_paise,
                available_quantity = EXCLUDED.available_quantity,
                version = commerce_inventory.version + 1,
                updated_at = NOW()
            WHERE EXCLUDED.available_quantity >= commerce_inventory.reserved_quantity
            RETURNING *
            """,
            (seller_id, sku, title, unit_price_paise, available_quantity),
        )
        row = await result.fetchone()
        if row is None:
            raise ValueError(
                "available quantity cannot be lower than reserved quantity"
            )
        return await self._dict_row(
            "commerce_inventory", "seller_id = %s AND sku = %s", (seller_id, sku)
        )

    async def create_cart(
        self, cart_id: UUID, principal_id: str, seller_id: str
    ) -> dict[str, Any]:
        await self.connection.execute(
            """
            INSERT INTO commerce_carts (cart_id, principal_id, seller_id)
            VALUES (%s, %s, %s)
            """,
            (cart_id, principal_id, seller_id),
        )
        return await self.get_cart(cart_id, principal_id)

    async def get_cart(
        self, cart_id: UUID, principal_id: str, *, lock: bool = False
    ) -> dict[str, Any]:
        suffix = " FOR UPDATE" if lock else ""
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"""
                SELECT * FROM commerce_carts
                WHERE cart_id = %s AND principal_id = %s{suffix}
                """,
                (cart_id, principal_id),
            )
            row = await cursor.fetchone()
        if row is None:
            raise LookupError("cart not found")
        return row

    async def set_cart_line(
        self,
        cart_id: UUID,
        principal_id: str,
        sku: str,
        quantity: int,
        expected_version: int,
    ) -> dict[str, Any]:
        cart = await self.get_cart(cart_id, principal_id, lock=True)
        if cart["status"] != "open":
            raise ValueError("cart is not open")
        if cart["version"] != expected_version:
            raise RuntimeError("stale cart version")
        inventory = await self.get_inventory(cart["seller_id"], sku)
        if quantity > inventory["available_quantity"] - inventory["reserved_quantity"]:
            raise ValueError("insufficient inventory")
        if quantity == 0:
            await self.connection.execute(
                "DELETE FROM commerce_cart_lines WHERE cart_id = %s AND sku = %s",
                (cart_id, sku),
            )
        else:
            await self.connection.execute(
                """
                INSERT INTO commerce_cart_lines (cart_id, sku, quantity)
                VALUES (%s, %s, %s)
                ON CONFLICT (cart_id, sku) DO UPDATE SET quantity = EXCLUDED.quantity
                """,
                (cart_id, sku, quantity),
            )
        await self.connection.execute(
            """
            UPDATE commerce_carts SET version = version + 1, updated_at = NOW()
            WHERE cart_id = %s
            """,
            (cart_id,),
        )
        return await self.get_cart_with_lines(cart_id, principal_id)

    async def get_cart_with_lines(
        self, cart_id: UUID, principal_id: str, *, lock: bool = False
    ) -> dict[str, Any]:
        cart = await self.get_cart(cart_id, principal_id, lock=lock)
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT l.sku, l.quantity, i.title, i.unit_price_paise,
                       i.available_quantity, i.reserved_quantity, i.version AS inventory_version
                FROM commerce_cart_lines l
                JOIN commerce_inventory i
                  ON i.seller_id = %s AND i.sku = l.sku
                WHERE l.cart_id = %s
                ORDER BY l.sku
                """,
                (cart["seller_id"], cart_id),
            )
            lines = await cursor.fetchall()
        return {**cart, "lines": lines}

    async def get_inventory(
        self, seller_id: str, sku: str, *, lock: bool = False
    ) -> dict[str, Any]:
        suffix = " FOR UPDATE" if lock else ""
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"SELECT * FROM commerce_inventory WHERE seller_id = %s AND sku = %s{suffix}",
                (seller_id, sku),
            )
            row = await cursor.fetchone()
        if row is None:
            raise LookupError("inventory item not found")
        return row

    async def create_quote(
        self,
        quote_id: UUID,
        cart: dict[str, Any],
        landed_total_paise: int,
        expires_at: datetime,
    ) -> dict[str, Any]:
        snapshots: list[dict[str, Any]] = []
        subtotal = 0
        if not cart["lines"]:
            raise ValueError("cart is empty")
        for line in cart["lines"]:
            inventory = await self.get_inventory(
                cart["seller_id"], line["sku"], lock=True
            )
            free = inventory["available_quantity"] - inventory["reserved_quantity"]
            if line["quantity"] > free:
                raise ValueError("insufficient inventory")
            line_total = line["quantity"] * inventory["unit_price_paise"]
            subtotal += line_total
            snapshots.append(
                {
                    "sku": line["sku"],
                    "title": inventory["title"],
                    "quantity": line["quantity"],
                    "unit_price_paise": inventory["unit_price_paise"],
                    "inventory_version": inventory["version"],
                    "line_total_paise": line_total,
                }
            )
        if landed_total_paise < subtotal:
            raise ValueError("landed total cannot be below subtotal")
        await self.connection.execute(
            """
            INSERT INTO commerce_quotes (
                quote_id, cart_id, principal_id, seller_id, cart_version,
                subtotal_paise, landed_total_paise, line_snapshot, expires_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                quote_id,
                cart["cart_id"],
                cart["principal_id"],
                cart["seller_id"],
                cart["version"],
                subtotal,
                landed_total_paise,
                Jsonb(snapshots),
                expires_at,
            ),
        )
        for line in snapshots:
            reservation_id = uuid5(quote_id, line["sku"])
            await self.connection.execute(
                """
                UPDATE commerce_inventory
                SET reserved_quantity = reserved_quantity + %s, updated_at = NOW()
                WHERE seller_id = %s AND sku = %s
                """,
                (line["quantity"], cart["seller_id"], line["sku"]),
            )
            await self.connection.execute(
                """
                INSERT INTO commerce_inventory_reservations (
                    reservation_id, quote_id, seller_id, sku, quantity
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    reservation_id,
                    quote_id,
                    cart["seller_id"],
                    line["sku"],
                    line["quantity"],
                ),
            )
        return await self.get_quote(quote_id, cart["principal_id"])

    async def get_quote(
        self, quote_id: UUID, principal_id: str, *, lock: bool = False
    ) -> dict[str, Any]:
        suffix = " FOR UPDATE" if lock else ""
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"SELECT * FROM commerce_quotes WHERE quote_id = %s AND principal_id = %s{suffix}",
                (quote_id, principal_id),
            )
            row = await cursor.fetchone()
        if row is None:
            raise LookupError("quote not found")
        return row

    async def release_quote(self, quote_id: UUID, status: str = "released") -> None:
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT seller_id, sku, quantity
                FROM commerce_inventory_reservations
                WHERE quote_id = %s AND status = 'held'
                FOR UPDATE
                """,
                (quote_id,),
            )
            reservations = await cursor.fetchall()
        for reservation in reservations:
            await self.connection.execute(
                """
                UPDATE commerce_inventory
                SET reserved_quantity = reserved_quantity - %s, updated_at = NOW()
                WHERE seller_id = %s AND sku = %s
                """,
                (reservation["quantity"], reservation["seller_id"], reservation["sku"]),
            )
        await self.connection.execute(
            """
            UPDATE commerce_inventory_reservations
            SET status = 'released', released_at = NOW()
            WHERE quote_id = %s AND status = 'held'
            """,
            (quote_id,),
        )
        await self.connection.execute(
            "UPDATE commerce_quotes SET status = %s WHERE quote_id = %s AND status = 'open'",
            (status, quote_id),
        )

    async def release_expired_quotes(self, now: datetime) -> int:
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT quote_id
                FROM commerce_quotes
                WHERE status = 'open' AND expires_at <= %s
                ORDER BY expires_at, quote_id
                FOR UPDATE SKIP LOCKED
                """,
                (now,),
            )
            expired = await cursor.fetchall()
        for quote in expired:
            await self.release_quote(quote["quote_id"], "expired")
        return len(expired)

    async def create_order_and_payment(
        self,
        order_id: UUID,
        payment_attempt_id: UUID,
        quote: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        await self.connection.execute(
            """
            INSERT INTO commerce_orders (
                order_id, principal_id, seller_id, cart_id, quote_id,
                landed_total_paise, status
            ) VALUES (%s, %s, %s, %s, %s, %s, 'payment_pending')
            """,
            (
                order_id,
                quote["principal_id"],
                quote["seller_id"],
                quote["cart_id"],
                quote["quote_id"],
                quote["landed_total_paise"],
            ),
        )
        await self.connection.execute(
            """
            INSERT INTO commerce_payment_attempts (
                payment_attempt_id, order_id, principal_id, amount_paise, status
            ) VALUES (%s, %s, %s, %s, 'pending')
            """,
            (
                payment_attempt_id,
                order_id,
                quote["principal_id"],
                quote["landed_total_paise"],
            ),
        )
        await self.connection.execute(
            """
            UPDATE commerce_inventory_reservations
            SET order_id = %s WHERE quote_id = %s AND status = 'held'
            """,
            (order_id, quote["quote_id"]),
        )
        await self.connection.execute(
            """
            UPDATE commerce_quotes SET status = 'consumed', consumed_at = NOW()
            WHERE quote_id = %s
            """,
            (quote["quote_id"],),
        )
        await self.connection.execute(
            """
            UPDATE commerce_carts SET status = 'checked_out', updated_at = NOW()
            WHERE cart_id = %s
            """,
            (quote["cart_id"],),
        )
        return await self.get_order(order_id), await self.get_payment(
            payment_attempt_id
        )

    async def get_order(self, order_id: UUID, *, lock: bool = False) -> dict[str, Any]:
        return await self._dict_row(
            "commerce_orders", "order_id = %s", (order_id,), lock=lock
        )

    async def get_order_by_logistics_transaction(
        self, transaction_id: str, *, lock: bool = False
    ) -> dict[str, Any]:
        suffix = " FOR UPDATE" if lock else ""
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"""
                SELECT * FROM commerce_orders
                WHERE fulfilment->'logistics'->>'transaction_id' = %s
                ORDER BY created_at
                LIMIT 2{suffix}
                """,
                (transaction_id,),
            )
            rows = list(await cursor.fetchall())
        if not rows:
            raise LookupError("logistics transaction is not bound to an order")
        if len(rows) != 1:
            raise ValueError("logistics transaction is bound to multiple orders")
        return rows[0]

    async def get_order_by_retail_transaction(
        self, transaction_id: str, *, lock: bool = False
    ) -> dict[str, Any]:
        suffix = " FOR UPDATE" if lock else ""
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"""
                SELECT * FROM commerce_orders
                WHERE fulfilment->'retail'->>'transaction_id' = %s
                ORDER BY created_at
                LIMIT 2{suffix}
                """,
                (transaction_id,),
            )
            rows = list(await cursor.fetchall())
        if not rows:
            raise LookupError("retail transaction is not bound to an order")
        if len(rows) != 1:
            raise ValueError("retail transaction is bound to multiple orders")
        return rows[0]

    async def get_return_for_order(
        self, order_id: UUID, *, lock: bool = False
    ) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if lock else ""
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"""
                SELECT * FROM commerce_returns
                WHERE order_id = %s
                ORDER BY created_at
                LIMIT 1{suffix}
                """,
                (order_id,),
            )
            return await cursor.fetchone()

    async def create_or_get_return(
        self,
        *,
        return_id: UUID,
        order_id: UUID,
        principal_id: str,
        seller_id: str,
        reason: str,
    ) -> tuple[dict[str, Any], bool]:
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                INSERT INTO commerce_returns (
                    return_id, order_id, principal_id, seller_id, reason
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (order_id) DO NOTHING
                RETURNING *
                """,
                (return_id, order_id, principal_id, seller_id, reason),
            )
            created_row = await cursor.fetchone()
            if created_row is not None:
                return created_row, True
            await cursor.execute(
                """
                SELECT * FROM commerce_returns
                WHERE order_id = %s
                ORDER BY created_at
                LIMIT 1
                """,
                (order_id,),
            )
            existing = await cursor.fetchone()
        if existing is None:
            raise RuntimeError("return insert conflicted without an existing row")
        return existing, False

    async def set_return_status(
        self,
        return_id: UUID,
        *,
        expected_version: int,
        status: str,
        resolution: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                UPDATE commerce_returns
                SET status = %s,
                    version = version + 1,
                    resolution = COALESCE(%s, resolution),
                    updated_at = NOW()
                WHERE return_id = %s AND version = %s
                RETURNING *
                """,
                (status, Jsonb(resolution) if resolution is not None else None, return_id, expected_version),
            )
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("stale return transition")
        return row

    async def update_order_fulfilment(
        self,
        order_id: UUID,
        *,
        expected_version: int,
        status: str,
        fulfilment: dict[str, Any],
    ) -> dict[str, Any]:
        result = await self.connection.execute(
            """
            UPDATE commerce_orders
            SET status = %s, version = version + 1, fulfilment = %s, updated_at = NOW()
            WHERE order_id = %s AND version = %s
            RETURNING *
            """,
            (status, Jsonb(fulfilment), order_id, expected_version),
        )
        row = await result.fetchone()
        if row is None:
            raise RuntimeError("stale order fulfilment update")
        return await self.get_order(order_id)

    async def get_payment(
        self, payment_attempt_id: UUID, *, lock: bool = False
    ) -> dict[str, Any]:
        return await self._dict_row(
            "commerce_payment_attempts",
            "payment_attempt_id = %s",
            (payment_attempt_id,),
            lock=lock,
        )

    async def get_payment_for_order(
        self, order_id: UUID, *, lock: bool = False
    ) -> dict[str, Any]:
        return await self._dict_row(
            "commerce_payment_attempts",
            "order_id = %s",
            (order_id,),
            lock=lock,
        )

    async def get_payment_by_provider_reference(
        self, provider_reference: str, *, lock: bool = False
    ) -> dict[str, Any]:
        return await self._dict_row(
            "commerce_payment_attempts",
            "provider_reference = %s",
            (provider_reference,),
            lock=lock,
        )

    async def attach_provider_order(
        self,
        payment_attempt_id: UUID,
        *,
        provider: str,
        provider_reference: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        payment = await self.get_payment(payment_attempt_id, lock=True)
        merged = {**(payment.get("result") or {}), **result}
        current_ref = payment.get("provider_reference")
        if current_ref and current_ref != provider_reference:
            raise ValueError("payment attempt is already bound to a different provider order")
        await self.connection.execute(
            """
            UPDATE commerce_payment_attempts
            SET provider = %s, provider_reference = %s, result = %s, updated_at = NOW()
            WHERE payment_attempt_id = %s AND status = 'pending'
              AND (provider_reference IS NULL OR provider_reference = %s)
            """,
            (
                provider,
                provider_reference,
                Jsonb(merged),
                payment_attempt_id,
                provider_reference,
            ),
        )
        return await self.get_payment(payment_attempt_id)

    async def set_payment_status(
        self,
        payment_attempt_id: UUID,
        status: str,
        result: dict[str, Any],
        provider_reference: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        payment = await self.get_payment(payment_attempt_id, lock=True)
        await self.connection.execute(
            """
            UPDATE commerce_payment_attempts
            SET status = %s, result = %s, provider_reference = COALESCE(%s, provider_reference),
                updated_at = NOW()
            WHERE payment_attempt_id = %s
            """,
            (status, Jsonb(result), provider_reference, payment_attempt_id),
        )
        order_status = PAYMENT_ORDER_TARGETS[status]
        await self.connection.execute(
            """
            UPDATE commerce_orders SET status = %s, version = version + 1, updated_at = NOW()
            WHERE order_id = %s
            """,
            (order_status, payment["order_id"]),
        )
        return await self.get_order(payment["order_id"]), await self.get_payment(
            payment_attempt_id
        )

    async def consume_reservations(self, order_id: UUID) -> None:
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT seller_id, sku, quantity FROM commerce_inventory_reservations
                WHERE order_id = %s AND status = 'held' FOR UPDATE
                """,
                (order_id,),
            )
            reservations = await cursor.fetchall()
        for reservation in reservations:
            await self.connection.execute(
                """
                UPDATE commerce_inventory
                SET available_quantity = available_quantity - %s,
                    reserved_quantity = reserved_quantity - %s,
                    version = version + 1, updated_at = NOW()
                WHERE seller_id = %s AND sku = %s
                """,
                (
                    reservation["quantity"],
                    reservation["quantity"],
                    reservation["seller_id"],
                    reservation["sku"],
                ),
            )
        await self.connection.execute(
            "UPDATE commerce_inventory_reservations SET status = 'consumed' WHERE order_id = %s AND status = 'held'",
            (order_id,),
        )

    async def release_order_reservations(self, order_id: UUID) -> None:
        result = await self.connection.execute(
            "SELECT quote_id FROM commerce_orders WHERE order_id = %s",
            (order_id,),
        )
        row = await result.fetchone()
        if row is not None:
            await self.release_quote(row[0])

    async def post_balanced_ledger(
        self,
        ledger_transaction_id: UUID,
        order_id: UUID,
        payment_attempt_id: UUID,
        posting_type: str,
        amount_paise: int,
        entries: Iterable[tuple[UUID, str, str]],
    ) -> None:
        await self.connection.execute(
            """
            INSERT INTO commerce_ledger_transactions (
                ledger_transaction_id, order_id, payment_attempt_id, posting_type
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (payment_attempt_id, posting_type) DO NOTHING
            """,
            (ledger_transaction_id, order_id, payment_attempt_id, posting_type),
        )
        for entry_id, account, side in entries:
            await self.connection.execute(
                """
                INSERT INTO commerce_ledger_entries (
                    ledger_entry_id, ledger_transaction_id, account, side, amount_paise
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (ledger_entry_id) DO NOTHING
                """,
                (entry_id, ledger_transaction_id, account, side, amount_paise),
            )

    async def get_refundable_order(
        self, order_id: UUID, seller_id: str, *, lock: bool = False
    ) -> dict[str, Any]:
        suffix = " FOR UPDATE OF orders, payment" if lock else ""
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"""
                SELECT
                    orders.*, payment.payment_attempt_id, payment.status AS payment_status,
                    payment.amount_paise AS payment_amount_paise,
                    payment.provider AS payment_provider,
                    payment.provider_reference AS payment_provider_reference,
                    payment.result AS payment_result
                FROM commerce_orders AS orders
                JOIN commerce_payment_attempts AS payment ON payment.order_id = orders.order_id
                WHERE orders.order_id = %s AND orders.seller_id = %s{suffix}
                """,
                (order_id, seller_id),
            )
            row = await cursor.fetchone()
        if row is None:
            raise LookupError("refundable order not found")
        return row

    async def create_or_get_refund(
        self,
        *,
        refund_id: UUID,
        order_id: UUID,
        payment_attempt_id: UUID,
        seller_id: str,
        principal_id: str,
        amount_paise: int,
        idempotency_key: str,
        correlation_id: str,
    ) -> tuple[dict[str, Any], bool]:
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                INSERT INTO commerce_refunds (
                    refund_id, order_id, payment_attempt_id, seller_id, principal_id,
                    amount_paise, status, idempotency_key, correlation_id
                ) VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                (
                    refund_id,
                    order_id,
                    payment_attempt_id,
                    seller_id,
                    principal_id,
                    amount_paise,
                    idempotency_key,
                    correlation_id,
                ),
            )
            refund = await cursor.fetchone()
            created = refund is not None
            if refund is None:
                await cursor.execute(
                    """
                    SELECT * FROM commerce_refunds
                    WHERE seller_id = %s AND idempotency_key = %s
                    """,
                    (seller_id, idempotency_key),
                )
                refund = await cursor.fetchone()
        if refund is None:
            raise ValueError("order already has a refund under another idempotency key")
        if (
            refund["order_id"] != order_id
            or refund["amount_paise"] != amount_paise
            or refund["correlation_id"] != correlation_id
        ):
            raise ValueError("idempotent refund replay changed the bound request")
        return refund, created

    async def set_refund_status(
        self, refund_id: UUID, current_status: str, status: str
    ) -> dict[str, Any]:
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                UPDATE commerce_refunds SET status = %s
                WHERE refund_id = %s AND status = %s
                RETURNING *
                """,
                (status, refund_id, current_status),
            )
            refund = await cursor.fetchone()
        if refund is None:
            raise RuntimeError("stale refund transition")
        return refund

    async def get_store(self, seller_id: str) -> dict[str, Any] | None:
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                "SELECT * FROM commerce_seller_stores WHERE seller_id = %s",
                (seller_id,),
            )
            return await cursor.fetchone()

    async def upsert_store(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                INSERT INTO commerce_seller_stores (
                    seller_id, store_name, city, state, pin, serviceability_tokens,
                    fulfilment_sla_hours, return_window_days, support_hours, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (seller_id) DO UPDATE SET
                    store_name = EXCLUDED.store_name,
                    city = EXCLUDED.city,
                    state = EXCLUDED.state,
                    pin = EXCLUDED.pin,
                    serviceability_tokens = EXCLUDED.serviceability_tokens,
                    fulfilment_sla_hours = EXCLUDED.fulfilment_sla_hours,
                    return_window_days = EXCLUDED.return_window_days,
                    support_hours = EXCLUDED.support_hours,
                    status = EXCLUDED.status,
                    version = commerce_seller_stores.version + 1,
                    updated_at = NOW()
                RETURNING *
                """,
                (
                    payload["seller_id"],
                    payload["store_name"],
                    payload["city"],
                    payload["state"],
                    payload["pin"],
                    payload["serviceability_tokens"],
                    payload["fulfilment_sla_hours"],
                    payload["return_window_days"],
                    payload["support_hours"],
                    payload["status"],
                ),
            )
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("store upsert returned no row")
        return row

    async def get_staff(self, seller_id: str, staff_id: str) -> dict[str, Any] | None:
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT * FROM commerce_seller_staff
                WHERE seller_id = %s AND staff_id = %s
                """,
                (seller_id, staff_id),
            )
            return await cursor.fetchone()

    async def list_staff(self, seller_id: str) -> list[dict[str, Any]]:
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT * FROM commerce_seller_staff
                WHERE seller_id = %s
                ORDER BY
                    CASE role
                        WHEN 'owner' THEN 0
                        WHEN 'manager' THEN 1
                        WHEN 'fulfilment' THEN 2
                        WHEN 'support' THEN 3
                        ELSE 4
                    END,
                    updated_at DESC
                """,
                (seller_id,),
            )
            return list(await cursor.fetchall())

    async def list_staff_memberships(
        self, member_principal_id: str
    ) -> list[dict[str, Any]]:
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT * FROM commerce_seller_staff
                WHERE member_principal_id = %s
                  AND status IN ('active', 'invited')
                ORDER BY updated_at DESC
                """,
                (member_principal_id,),
            )
            return list(await cursor.fetchall())

    async def upsert_staff_member(
        self, payload: dict[str, Any], *, staff_id: str | None = None
    ) -> dict[str, Any]:
        assigned_id = str(
            staff_id
            or payload.get("staff_id")
            or "staff_"
            + sha256(
                f"{payload['seller_id']}:{payload['member_principal_id']}".encode()
            ).hexdigest()[:16]
        )
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                INSERT INTO commerce_seller_staff (
                    staff_id, seller_id, member_principal_id, display_name, email,
                    role, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (seller_id, member_principal_id) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    email = EXCLUDED.email,
                    role = CASE
                        WHEN commerce_seller_staff.role = 'owner'
                            THEN commerce_seller_staff.role
                        ELSE EXCLUDED.role
                    END,
                    status = CASE
                        WHEN commerce_seller_staff.role = 'owner'
                            THEN commerce_seller_staff.status
                        ELSE EXCLUDED.status
                    END,
                    version = commerce_seller_staff.version + 1,
                    updated_at = NOW()
                RETURNING *
                """,
                (
                    assigned_id,
                    payload["seller_id"],
                    payload["member_principal_id"],
                    payload.get("display_name") or "",
                    payload.get("email") or "",
                    payload.get("role") or "viewer",
                    payload.get("status") or "invited",
                ),
            )
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("staff upsert returned no row")
        return row

    async def get_buyer_session(self, session_id: str) -> dict[str, Any] | None:
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                "SELECT payload FROM commerce_buyer_sessions WHERE session_id = %s",
                (session_id,),
            )
            row = await cursor.fetchone()
        return dict(row["payload"]) if row and row.get("payload") is not None else None

    async def upsert_buyer_session(
        self, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                INSERT INTO commerce_buyer_sessions (session_id, payload)
                VALUES (%s, %s)
                ON CONFLICT (session_id) DO UPDATE SET
                    payload = EXCLUDED.payload,
                    updated_at = NOW()
                RETURNING payload
                """,
                (session_id, Jsonb(payload)),
            )
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("buyer session upsert returned no row")
        return dict(row["payload"])

    async def ensure_owner_staff(self, seller_id: str) -> dict[str, Any]:
        digest = sha256(seller_id.encode("utf-8")).hexdigest()[:16]
        return await self.upsert_staff_member(
            {
                "staff_id": f"staff_owner_{digest}",
                "seller_id": seller_id,
                "member_principal_id": seller_id,
                "display_name": "Store owner",
                "email": "",
                "role": "owner",
                "status": "active",
            },
            staff_id=f"staff_owner_{digest}",
        )

    async def _dict_row(
        self,
        table: str,
        where: str,
        params: tuple[Any, ...],
        *,
        lock: bool = False,
    ) -> dict[str, Any]:
        suffix = " FOR UPDATE" if lock else ""
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(f"SELECT * FROM {table} WHERE {where}{suffix}", params)
            row = await cursor.fetchone()
        if row is None:
            raise LookupError(f"{table} row not found")
        return row
