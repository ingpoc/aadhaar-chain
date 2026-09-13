"""Durable session registry: active sessions plus revoke deny-list.

Cookie clear alone is not a stolen-cookie response. ``parse_session_token``
consults this registry. PostgreSQL is the owner (same gateway DATABASE_URL as
AgentGuard/commerce). Process memory is a cache loaded at bind and written
through on mutate. Staging/production, or any postgres backend, fail closed if
the store is unready or a write cannot complete.
"""
from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock
from typing import Any, Optional

from app.persistence.connection import ConnectionPool
from app.persistence.session_registry_store import (
    delete_session,
    insert_revoked_sid,
    load_registry,
    upsert_revoked_principal,
    upsert_session,
)

_lock = RLock()
_sessions: dict[str, dict[str, Any]] = {}
_revoked_sids: set[str] = set()
_revoked_principals: dict[str, int] = {}
_pool: ConnectionPool | None = None
_require_durable = False
_ready = True


class SessionRegistryUnavailable(RuntimeError):
    """Durable session registry required but PostgreSQL is unusable."""


def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def bind_session_store(
    pool: ConnectionPool | None, *, require_durable: bool
) -> None:
    """Attach the existing gateway pool. Does not load until ``open_session_store``."""
    global _pool, _require_durable, _ready
    _pool = pool
    _require_durable = require_durable
    _ready = not require_durable and pool is None


def unbind_session_store() -> None:
    """Detach the pool and return to memory-only (tests / shutdown)."""
    global _pool, _require_durable, _ready
    _pool = None
    _require_durable = False
    _ready = True


def reset_session_registry() -> None:
    """Test helper — empty the in-process cache. Does not TRUNCATE PostgreSQL."""
    with _lock:
        _sessions.clear()
        _revoked_sids.clear()
        _revoked_principals.clear()


def _fail_closed() -> bool:
    return (_require_durable or _pool is not None) and not _ready


async def open_session_store() -> None:
    """Load durable rows into memory. Fail closed if PostgreSQL is required and down."""
    global _ready
    if _pool is None:
        if _require_durable:
            raise SessionRegistryUnavailable(
                "PostgreSQL session registry required in this runtime"
            )
        _ready = True
        return
    try:
        async with _pool.connection() as connection:
            sessions, revoked_sids, revoked_principals = await load_registry(
                connection, now=_now()
            )
    except SessionRegistryUnavailable:
        raise
    except Exception as exc:
        raise SessionRegistryUnavailable("session registry load failed") from exc
    with _lock:
        _sessions.clear()
        _sessions.update(sessions)
        _revoked_sids.clear()
        _revoked_sids.update(revoked_sids)
        _revoked_principals.clear()
        _revoked_principals.update(revoked_principals)
    _ready = True


async def _persist(write) -> None:
    if _pool is None:
        if _require_durable:
            raise SessionRegistryUnavailable(
                "PostgreSQL session registry required in this runtime"
            )
        return
    try:
        async with _pool.connection() as connection:
            async with connection.transaction():
                await write(connection)
    except SessionRegistryUnavailable:
        raise
    except Exception as exc:
        raise SessionRegistryUnavailable("session registry write failed") from exc


def register_session(
    payload: dict[str, Any], *, device_label: Optional[str] = None
) -> None:
    sid = payload.get("sid")
    principal_id = payload.get("principal_id")
    if not isinstance(sid, str) or not sid:
        return
    if not isinstance(principal_id, str) or not principal_id:
        return
    record = {
        "sid": sid,
        "principal_id": principal_id,
        "aud": payload.get("aud"),
        "identity_provider": payload.get("identity_provider"),
        "iat": payload.get("iat") or _now(),
        "exp": payload.get("exp"),
        "device_label": device_label or payload.get("device_label") or "session",
    }
    with _lock:
        _revoked_sids.discard(sid)
        _sessions[sid] = record


async def persist_registered_session(
    payload: dict[str, Any], *, device_label: Optional[str] = None
) -> None:
    register_session(payload, device_label=device_label)
    record = get_session(str(payload.get("sid") or ""))
    if record is None:
        return
    await _persist(lambda connection: upsert_session(connection, record))


def is_revoked(payload: dict[str, Any]) -> bool:
    if _fail_closed():
        return True
    sid = payload.get("sid")
    principal_id = payload.get("principal_id")
    iat = payload.get("iat")
    issued = iat if isinstance(iat, int) else 0
    with _lock:
        if isinstance(sid, str) and sid in _revoked_sids:
            return True
        if isinstance(principal_id, str) and principal_id in _revoked_principals:
            return issued < _revoked_principals[principal_id]
    return False


def revoke_sid(sid: str) -> bool:
    if not sid:
        return False
    with _lock:
        _revoked_sids.add(sid)
        _sessions.pop(sid, None)
        return True


async def revoke_sid_durable(sid: str) -> bool:
    if not sid:
        return False
    record = get_session(sid)
    principal_id = (
        str(record["principal_id"])
        if record and isinstance(record.get("principal_id"), str)
        else None
    )
    existed = revoke_sid(sid)

    async def write(connection: Any) -> None:
        await insert_revoked_sid(connection, sid, principal_id)
        await delete_session(connection, sid)

    await _persist(write)
    return existed


def revoke_principal(principal_id: str) -> int:
    ts = _now()
    dropped = 0
    with _lock:
        _revoked_principals[principal_id] = ts
        stale = [
            sid
            for sid, record in _sessions.items()
            if record.get("principal_id") == principal_id
        ]
        for sid in stale:
            _revoked_sids.add(sid)
            _sessions.pop(sid, None)
            dropped += 1
    return dropped


async def revoke_principal_durable(principal_id: str) -> int:
    ts = _now()
    with _lock:
        _revoked_principals[principal_id] = ts
        stale = [
            sid
            for sid, record in list(_sessions.items())
            if record.get("principal_id") == principal_id
        ]
        for sid in stale:
            _revoked_sids.add(sid)
            _sessions.pop(sid, None)
    dropped = len(stale)

    async def write(connection: Any) -> None:
        await upsert_revoked_principal(connection, principal_id, ts)
        for sid in stale:
            await insert_revoked_sid(connection, sid, principal_id)
            await delete_session(connection, sid)

    await _persist(write)
    return dropped


def get_session(sid: str) -> Optional[dict[str, Any]]:
    with _lock:
        record = _sessions.get(sid)
        return dict(record) if record else None


def list_principal_sessions(principal_id: str) -> list[dict[str, Any]]:
    now = _now()
    with _lock:
        return [
            dict(record)
            for record in _sessions.values()
            if record.get("principal_id") == principal_id
            and int(record.get("exp") or 0) >= now
            and record.get("sid") not in _revoked_sids
        ]
