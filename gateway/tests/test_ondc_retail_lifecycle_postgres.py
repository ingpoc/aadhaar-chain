"""Signed RET10 callbacks persist and reconcile into CommerceV1."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.commerce_v1 import CommerceV1
from app.ondc_crypto import create_authorization_header
from app.ondc_routes import _process_inbox_record, router as ondc_router
from app.persistence import ConnectionPool, MigrationRunner
from config import settings


DATABASE_URL = os.getenv("DATABASE_URL")
MIGRATIONS = Path(__file__).parents[1] / "migrations"

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not DATABASE_URL,
        reason="DATABASE_URL is required for PostgreSQL integration tests",
    ),
]


@pytest_asyncio.fixture
async def postgres_url() -> AsyncIterator[str]:
    assert DATABASE_URL is not None
    schema = f"retail_lifecycle_{uuid4().hex}"
    admin = await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)
    try:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        yield make_conninfo(DATABASE_URL, options=f"-csearch_path={schema},public")
    finally:
        await admin.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
        )
        await admin.close()


@pytest_asyncio.fixture
async def commerce(
    postgres_url: str,
) -> AsyncIterator[tuple[CommerceV1, ConnectionPool]]:
    pool = ConnectionPool(postgres_url, min_size=0, max_size=8)
    await pool.open()
    await MigrationRunner(pool, MIGRATIONS).apply()
    try:
        yield CommerceV1(pool), pool
    finally:
        await pool.close()


async def _paid_order(service: CommerceV1) -> dict:
    await service.upsert_inventory(
        seller_id="seller-1",
        sku="atta-2kg",
        title="Atta 2kg",
        unit_price_paise=12_500,
        available_quantity=10,
    )
    cart = await service.create_cart(principal_id="principal:buyer-1", seller_id="seller-1")
    cart = await service.set_cart_line(
        principal_id="principal:buyer-1",
        cart_id=cart["cart_id"],
        sku="atta-2kg",
        quantity=2,
        expected_version=cart["version"],
    )
    quote = await service.preview_checkout(
        principal_id="principal:buyer-1",
        cart_id=cart["cart_id"],
        expected_version=cart["version"],
    )
    prepared = await service.prepare_checkout(
        principal_id="principal:buyer-1",
        quote_id=quote["quote_id"],
        idempotency_key="retail-lifecycle-checkout",
    )
    paid = await service.record_payment_result(
        principal_id="principal:buyer-1",
        payment_attempt_id=prepared["payment_attempt"]["payment_attempt_id"],
        status="succeeded",
    )
    return paid["order"]


def _seller_key(pem_dir: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(
        (pem_dir / "signing_private.pem").read_bytes(),
        password=None,
    )
    assert isinstance(key, Ed25519PrivateKey)
    return key


def _enable_retail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    key = Ed25519PrivateKey.generate()
    pem_dir = tmp_path / "keys"
    pem_dir.mkdir()
    (pem_dir / "signing_private.pem").write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    (pem_dir / "unique_key_id.txt").write_text("seller-uk\n", encoding="utf-8")
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "ondc_enabled", True)
    monkeypatch.setattr(settings, "ondc_subscriber_id", "ondcbuyer.aadharcha.in")
    monkeypatch.setattr(settings, "ondc_bap_id", "ondcbuyer.aadharcha.in")
    monkeypatch.setattr(settings, "ondc_bap_uri", "https://ondcbuyer.aadharcha.in/ondc")
    monkeypatch.setattr(settings, "ondc_unique_key_id", "buyer-uk")
    monkeypatch.setattr(
        settings, "ondc_signing_private_key_path", str(pem_dir / "signing_private.pem")
    )
    monkeypatch.setattr(settings, "ondc_buyer_keys_dir", str(pem_dir))
    monkeypatch.setattr(settings, "ondc_seller_keys_dir", str(pem_dir))
    monkeypatch.setattr(settings, "ondc_bpp_id", "ondcseller.aadharcha.in")
    monkeypatch.setattr(settings, "ondc_bpp_uri", "https://ondcseller.aadharcha.in/ondc")
    monkeypatch.setattr(settings, "ondc_seller_unique_key_id", "seller-uk")
    monkeypatch.setattr(
        settings,
        "ondc_seller_signing_private_key_path",
        str(pem_dir / "signing_private.pem"),
    )
    return pem_dir


def _envelope(
    *,
    action: str,
    transaction_id: str,
    message_id: str,
    timestamp: str,
    message: dict,
) -> dict:
    return {
        "context": {
            "domain": "ONDC:RET10",
            "action": action,
            "core_version": "1.2.0",
            "bap_id": "ondcbuyer.aadharcha.in",
            "bap_uri": "https://ondcbuyer.aadharcha.in/ondc",
            "bpp_id": "ondcseller.aadharcha.in",
            "bpp_uri": "https://ondcseller.aadharcha.in/ondc",
            "transaction_id": transaction_id,
            "message_id": message_id,
            "timestamp": timestamp,
            "ttl": "PT30S",
        },
        "message": message,
    }


async def test_apply_retail_update_persists_select_to_settlement(
    commerce: tuple[CommerceV1, ConnectionPool],
) -> None:
    service, _pool = commerce
    order = await _paid_order(service)
    bound = await service.bind_retail_protocol(
        order_id=str(order["order_id"]),
        retail={
            "transaction_id": "txn-retail-1",
            "bpp_id": "ondcseller.aadharcha.in",
            "core_version": "1.2.0",
            "signature_verified": True,
        },
    )
    assert bound["fulfilment"]["retail"]["transaction_id"] == "txn-retail-1"
    replay_bind = await service.bind_retail_protocol(
        order_id=str(order["order_id"]),
        retail={
            "transaction_id": "txn-retail-1",
            "bpp_id": "ondcseller.aadharcha.in",
            "core_version": "1.2.0",
            "signature_verified": True,
        },
    )
    assert int(replay_bind["version"]) == int(bound["version"])

    steps = (
        (
            "a" * 64,
            {
                "action": "on_select",
                "message_id": "msg-select",
                "bpp_id": "ondcseller.aadharcha.in",
                "provider_timestamp": "2026-08-19T10:00:00Z",
                "quote": {"price": {"currency": "INR", "value": "250.00"}},
            },
            "paid",
        ),
        (
            "b" * 64,
            {
                "action": "on_init",
                "message_id": "msg-init",
                "bpp_id": "ondcseller.aadharcha.in",
                "provider_timestamp": "2026-08-19T10:01:00Z",
                "billing": {"name": "Buyer"},
            },
            "paid",
        ),
        (
            "c" * 64,
            {
                "action": "on_confirm",
                "message_id": "msg-confirm",
                "bpp_id": "ondcseller.aadharcha.in",
                "provider_timestamp": "2026-08-19T10:02:00Z",
                "provider_status": "Accepted",
                "target_status": "confirmed",
                "protocol_order_id": "ord_retail_1",
            },
            "confirmed",
        ),
        (
            "d" * 64,
            {
                "action": "on_status",
                "message_id": "msg-packed",
                "bpp_id": "ondcseller.aadharcha.in",
                "provider_timestamp": "2026-08-19T10:03:00Z",
                "provider_status": "Packed",
                "target_status": "preparing",
            },
            "preparing",
        ),
        (
            "e" * 64,
            {
                "action": "on_track",
                "message_id": "msg-track",
                "bpp_id": "ondcseller.aadharcha.in",
                "provider_timestamp": "2026-08-19T10:04:00Z",
                "tracking_id": "AWB-RET-1",
                "tracking_url": "https://ondcseller.aadharcha.in/ondc/track/AWB-RET-1",
            },
            "preparing",
        ),
        (
            "f" * 64,
            {
                "action": "on_status",
                "message_id": "msg-picked",
                "bpp_id": "ondcseller.aadharcha.in",
                "provider_timestamp": "2026-08-19T10:05:00Z",
                "provider_status": "Order-picked-up",
                "target_status": "shipped",
            },
            "shipped",
        ),
        (
            "1" * 64,
            {
                "action": "on_status",
                "message_id": "msg-delivered",
                "bpp_id": "ondcseller.aadharcha.in",
                "provider_timestamp": "2026-08-19T10:06:00Z",
                "provider_status": "Order-delivered",
                "target_status": "delivered",
            },
            "delivered",
        ),
    )
    last = None
    for commitment, update, expected_status in steps:
        last = await service.apply_retail_update(
            transaction_id="txn-retail-1",
            event_commitment=commitment,
            update=update,
        )
        assert last["duplicate"] is False
        assert last["order"]["status"] == expected_status
    assert last is not None
    replay = await service.apply_retail_update(
        transaction_id="txn-retail-1",
        event_commitment="1" * 64,
        update=steps[-1][1],
    )
    assert replay["duplicate"] is True
    assert int(replay["order"]["version"]) == int(last["order"]["version"])

    returned = await service.apply_retail_update(
        transaction_id="txn-retail-1",
        event_commitment="2" * 64,
        update={
            "action": "on_update",
            "message_id": "msg-return",
            "bpp_id": "ondcseller.aadharcha.in",
            "provider_timestamp": "2026-08-19T10:07:00Z",
            "return_status": "requested",
            "return_reason": "Return_Initiated",
        },
    )
    assert returned["return"]["status"] == "requested"
    settled = await service.apply_retail_update(
        transaction_id="txn-retail-1",
        event_commitment="3" * 64,
        update={
            "action": "on_update",
            "message_id": "msg-settle",
            "bpp_id": "ondcseller.aadharcha.in",
            "provider_timestamp": "2026-08-19T10:08:00Z",
            "return_status": "completed",
            "refund_amount_paise": 25_000,
            "settlement": {"settlement_amount": {"currency": "INR", "value": "250.00"}},
        },
    )
    assert settled["order"]["status"] == "delivered"
    assert settled["return"]["status"] == "completed"
    assert settled["refund"]["status"] == "succeeded"
    assert settled["refund"]["amount_paise"] == 25_000
    retail = settled["order"]["fulfilment"]["retail"]
    assert retail["protocol_order_id"] == "ord_retail_1"
    assert retail["quote"]["price"]["value"] == "250.00"
    assert settled["order"]["fulfilment"]["tracking_id"] == "AWB-RET-1"
    assert len(retail["processed_callbacks"]) == 9


async def test_signed_http_callbacks_reconcile_into_commerce_v1(
    commerce: tuple[CommerceV1, ConnectionPool],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, pool = commerce
    pem_dir = _enable_retail(tmp_path, monkeypatch)
    order = await _paid_order(service)
    await service.bind_retail_protocol(
        order_id=str(order["order_id"]),
        retail={
            "transaction_id": "txn-retail-http",
            "bpp_id": "ondcseller.aadharcha.in",
            "core_version": "1.2.0",
            "signature_verified": True,
        },
    )
    api = FastAPI()
    api.state.persistence_pool = pool
    api.include_router(ondc_router)

    callbacks = [
        (
            "on_select",
            "2026-08-19T12:00:00.000Z",
            {"order": {"quote": {"price": {"currency": "INR", "value": "250.00"}}}},
        ),
        (
            "on_init",
            "2026-08-19T12:01:00.000Z",
            {"order": {"billing": {"name": "Buyer"}}},
        ),
        (
            "on_confirm",
            "2026-08-19T12:02:00.000Z",
            {"order": {"id": "ord_http_1", "state": "Accepted"}},
        ),
        (
            "on_status",
            "2026-08-19T12:03:00.000Z",
            {
                "order": {
                    "id": "ord_http_1",
                    "state": "In-progress",
                    "fulfillments": [
                        {"state": {"descriptor": {"code": "Packed"}}}
                    ],
                }
            },
        ),
        (
            "on_track",
            "2026-08-19T12:04:00.000Z",
            {
                "tracking": {
                    "id": "AWB-HTTP",
                    "url": "https://ondcseller.aadharcha.in/ondc/track/AWB-HTTP",
                    "status": "active",
                }
            },
        ),
        (
            "on_status",
            "2026-08-19T12:05:00.000Z",
            {
                "order": {
                    "id": "ord_http_1",
                    "fulfillments": [
                        {"state": {"descriptor": {"code": "Order-picked-up"}}}
                    ],
                }
            },
        ),
        (
            "on_status",
            "2026-08-19T12:06:00.000Z",
            {
                "order": {
                    "id": "ord_http_1",
                    "fulfillments": [
                        {"state": {"descriptor": {"code": "Order-delivered"}}}
                    ],
                }
            },
        ),
        (
            "on_update",
            "2026-08-19T12:07:00.000Z",
            {
                "order": {
                    "id": "ord_http_1",
                    "quote": {"price": {"currency": "INR", "value": "250.00"}},
                    "fulfillments": [
                        {
                            "type": "Return",
                            "state": {"descriptor": {"code": "Liquidated"}},
                        }
                    ],
                    "payment": {
                        "@ondc/org/settlement_details": [
                            {
                                "settlement_amount": {
                                    "currency": "INR",
                                    "value": "250.00",
                                }
                            }
                        ]
                    },
                }
            },
        ),
    ]
    async with AsyncClient(
        transport=ASGITransport(app=api), base_url="http://test"
    ) as client:
        for index, (action, timestamp, message) in enumerate(callbacks):
            envelope = _envelope(
                action=action,
                transaction_id="txn-retail-http",
                message_id=f"msg-http-{index}",
                timestamp=timestamp,
                message=message,
            )
            authorization = create_authorization_header(
                envelope,
                subscriber_id="ondcseller.aadharcha.in",
                unique_key_id="seller-uk",
                private_key=_seller_key(pem_dir),
            )
            response = await client.post(
                f"/ondc/{action}",
                json=envelope,
                headers={"Authorization": authorization},
            )
            assert response.status_code == 200, response.text
            assert response.json()["message"]["ack"]["status"] == "ACK"
            async with pool.connection() as connection:
                result = await connection.execute(
                    """
                    SELECT inbox_id, state FROM ondc_inbox
                    WHERE transaction_id = %s AND message_id = %s
                    """,
                    ("txn-retail-http", f"msg-http-{index}"),
                )
                inbox_id, inbox_state = await result.fetchone()
            if inbox_state != "delivered":
                reconciled = await _process_inbox_record(pool, inbox_id)
                assert reconciled is not None
                assert reconciled["state"] == "delivered"

    current = await service.get_retail_binding("txn-retail-http")
    retail = current["fulfilment"]["retail"]
    assert current["status"] == "delivered"
    assert retail["protocol_order_id"] == "ord_http_1"
    assert current["fulfilment"]["tracking_id"] == "AWB-HTTP"
    assert retail["return_status"] == "refund_pending"
    assert retail["refund_status"] == "succeeded"
    assert len(retail["processed_callbacks"]) == 8
