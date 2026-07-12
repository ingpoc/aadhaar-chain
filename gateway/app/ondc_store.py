"""Durable ONDC outbox/inbox under DATA_DIR (Render Free /tmp OK)."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

from config import settings

_LOCK = threading.Lock()
_MAX_ITEMS = 500


def _root() -> Path:
    root = Path(settings.data_dir).expanduser() / "ondc"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _path(name: str) -> Path:
    return _root() / name


def _read_list(name: str) -> list[dict[str, Any]]:
    path = _path(name)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _write_list(name: str, items: list[dict[str, Any]]) -> None:
    path = _path(name)
    trimmed = items[-_MAX_ITEMS:]
    path.write_text(json.dumps(trimmed, indent=2, default=str) + "\n", encoding="utf-8")


def append_outbox(entry: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        items = _read_list("outbox.json")
        items.append(entry)
        _write_list("outbox.json", items)
    return entry


def update_outbox(entry_id: str, **fields: Any) -> Optional[dict[str, Any]]:
    with _LOCK:
        items = _read_list("outbox.json")
        for item in items:
            if item.get("id") == entry_id:
                item.update(fields)
                item["updated_at"] = int(time.time())
                _write_list("outbox.json", items)
                return item
    return None


def list_outbox(limit: int = 50) -> list[dict[str, Any]]:
    with _LOCK:
        items = _read_list("outbox.json")
    return list(reversed(items[-limit:]))


def append_inbox(entry: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        items = _read_list("inbox.json")
        items.append(entry)
        _write_list("inbox.json", items)
    return entry


def list_inbox(limit: int = 50, *, action: Optional[str] = None) -> list[dict[str, Any]]:
    with _LOCK:
        items = _read_list("inbox.json")
    if action:
        items = [i for i in items if i.get("action") == action]
    return list(reversed(items[-limit:]))


def catalogs_for_transaction(transaction_id: str) -> list[dict[str, Any]]:
    """Extract provider/item catalogs from on_search payloads for a transaction."""
    results: list[dict[str, Any]] = []
    for entry in list_inbox(limit=200, action="on_search"):
        payload = entry.get("payload") or {}
        ctx = payload.get("context") or {}
        if ctx.get("transaction_id") != transaction_id:
            continue
        message = payload.get("message") or {}
        catalog = message.get("catalog") or {}
        providers = (
            catalog.get("providers")
            or catalog.get("bpp/providers")
            or catalog.get("bpp_providers")
            or []
        )
        if not providers and catalog:
            providers = [catalog]
        bpp_id = ctx.get("bpp_id")
        bpp_uri = ctx.get("bpp_uri")
        for provider in providers:
            descriptor = provider.get("descriptor") or {}
            for item in provider.get("items") or []:
                item_desc = item.get("descriptor") or {}
                price = item.get("price") or {}
                results.append(
                    {
                        "id": item.get("id"),
                        "name": item_desc.get("name") or item.get("id"),
                        "description": item_desc.get("long_desc") or item_desc.get("short_desc"),
                        "price_inr": price.get("value"),
                        "currency": price.get("currency") or "INR",
                        "provider_id": provider.get("id"),
                        "provider_name": descriptor.get("name"),
                        "bpp_id": bpp_id,
                        "bpp_uri": bpp_uri,
                        "transaction_id": transaction_id,
                        "inbox_id": entry.get("id"),
                        "raw_item": item,
                    }
                )
    return results
