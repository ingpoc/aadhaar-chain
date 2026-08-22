"""Buyer SPA cart/billing sessions keyed by ondc-session-id."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from app import commerce_demo
from app.persistence.commerce_repository import CommerceRepository
from app.persistence.connection import live_connection_pool
from app.persistence.transaction import UnitOfWork


def _pool(request: Request):
    return live_connection_pool(getattr(request.app.state, "persistence_pool", None))


async def load_session(request: Request, session_id: str) -> dict[str, Any]:
    pool = _pool(request)
    if pool is None:
        return commerce_demo.get_cart_session(session_id)
    async with UnitOfWork(pool) as unit_of_work:
        payload = await CommerceRepository(unit_of_work).get_buyer_session(session_id)
    if not payload:
        return commerce_demo.empty_cart_session(session_id)
    return {**commerce_demo.empty_cart_session(session_id), **payload, "id": session_id}


async def save_session(request: Request, session: dict[str, Any]) -> dict[str, Any]:
    pool = _pool(request)
    session_id = str(session.get("id") or "").strip()
    session["id"] = session_id
    if pool is None:
        return commerce_demo.save_cart_session(session)
    async with UnitOfWork(pool) as unit_of_work:
        return await CommerceRepository(unit_of_work).upsert_buyer_session(
            session_id, session
        )


async def upsert_buyer(
    request: Request, session_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    session = await load_session(request, session_id)
    commerce_demo.apply_cart_buyer(session, body)
    return await save_session(request, session)


async def add_item(
    request: Request, session_id: str, item: dict[str, Any], quantity: int
) -> dict[str, Any]:
    session = await load_session(request, session_id)
    commerce_demo.apply_cart_item(session, item, quantity)
    return await save_session(request, session)


async def set_item_quantity(
    request: Request, session_id: str, item_id: str, quantity: int
) -> dict[str, Any]:
    session = await load_session(request, session_id)
    commerce_demo.set_cart_item_quantity(session, item_id, quantity)
    return await save_session(request, session)
