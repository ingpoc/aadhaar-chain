"""PostgreSQL repository for the durable single-seller commerce domain."""

from __future__ import annotations

from datetime import datetime
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

    async def get_payment(
        self, payment_attempt_id: UUID, *, lock: bool = False
    ) -> dict[str, Any]:
        return await self._dict_row(
            "commerce_payment_attempts",
            "payment_attempt_id = %s",
            (payment_attempt_id,),
            lock=lock,
        )

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
                    payment.amount_paise AS payment_amount_paise
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
