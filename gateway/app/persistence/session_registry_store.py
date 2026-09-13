"""PostgreSQL access for the gateway session revoke/list registry."""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row


async def upsert_session(connection: Any, record: dict[str, Any]) -> None:
    await connection.execute(
        """
        INSERT INTO auth_sessions (
            sid, principal_id, aud, identity_provider, iat, exp, device_label, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (sid) DO UPDATE SET
            principal_id = EXCLUDED.principal_id,
            aud = EXCLUDED.aud,
            identity_provider = EXCLUDED.identity_provider,
            iat = EXCLUDED.iat,
            exp = EXCLUDED.exp,
            device_label = EXCLUDED.device_label,
            updated_at = NOW()
        """,
        (
            record["sid"],
            record["principal_id"],
            record.get("aud"),
            record.get("identity_provider"),
            int(record["iat"]),
            record.get("exp"),
            record.get("device_label") or "session",
        ),
    )


async def delete_session(connection: Any, sid: str) -> None:
    await connection.execute("DELETE FROM auth_sessions WHERE sid = %s", (sid,))


async def insert_revoked_sid(
    connection: Any, sid: str, principal_id: str | None
) -> None:
    await connection.execute(
        """
        INSERT INTO auth_revoked_sids (sid, principal_id)
        VALUES (%s, %s)
        ON CONFLICT (sid) DO UPDATE SET
            principal_id = COALESCE(EXCLUDED.principal_id, auth_revoked_sids.principal_id),
            revoked_at = NOW()
        """,
        (sid, principal_id),
    )


async def upsert_revoked_principal(
    connection: Any, principal_id: str, revoked_before: int
) -> None:
    await connection.execute(
        """
        INSERT INTO auth_revoked_principals (principal_id, revoked_before)
        VALUES (%s, %s)
        ON CONFLICT (principal_id) DO UPDATE SET
            revoked_before = EXCLUDED.revoked_before,
            revoked_at = NOW()
        """,
        (principal_id, revoked_before),
    )


async def load_registry(
    connection: Any, *, now: int
) -> tuple[dict[str, dict[str, Any]], set[str], dict[str, int]]:
    sessions: dict[str, dict[str, Any]] = {}
    async with connection.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            """
            SELECT sid, principal_id, aud, identity_provider, iat, exp, device_label
            FROM auth_sessions
            WHERE exp IS NULL OR exp >= %s
            """,
            (now,),
        )
        for row in await cursor.fetchall():
            sessions[str(row["sid"])] = {
                "sid": row["sid"],
                "principal_id": row["principal_id"],
                "aud": row.get("aud"),
                "identity_provider": row.get("identity_provider"),
                "iat": int(row["iat"]),
                "exp": row.get("exp"),
                "device_label": row.get("device_label") or "session",
            }

        await cursor.execute("SELECT sid FROM auth_revoked_sids")
        revoked_sids = {str(row["sid"]) for row in await cursor.fetchall()}

        await cursor.execute(
            "SELECT principal_id, revoked_before FROM auth_revoked_principals"
        )
        revoked_principals = {
            str(row["principal_id"]): int(row["revoked_before"])
            for row in await cursor.fetchall()
        }
    return sessions, revoked_sids, revoked_principals
