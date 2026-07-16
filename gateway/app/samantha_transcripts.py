"""Authenticated, file-backed Samantha diagnostic transcript events."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from config import settings

TRANSCRIPT_FILE = "samantha-transcripts.jsonl"
ALLOWED_ROLES = {"buyer", "seller"}
ALLOWED_EVENT_TYPES = {
    "session_started", "session_stopped", "user_text", "user_voice_transcript",
    "assistant_text", "tool_call", "tool_result", "error",
}
MAX_CONTENT_CHARS = 4_000
MAX_METADATA_BYTES = 8_000
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{8,160}$")
_lock = Lock()


def _path() -> Path:
    return Path(settings.data_dir).expanduser() / TRANSCRIPT_FILE


def _clean_metadata(value: Any, depth: int = 0) -> Any:
    if depth > 3:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(key)[:80]: _clean_metadata(item, depth + 1)
            for key, item in list(value.items())[:40]
            if str(key).lower() not in {"authorization", "cookie", "client_secret", "api_key", "audio"}
        }
    if isinstance(value, list):
        return [_clean_metadata(item, depth + 1) for item in value[:40]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1_000]


def append_event(
    *, principal_id: str, role: str, session_id: str, event_type: str,
    content: str = "", metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if role not in ALLOWED_ROLES:
        raise ValueError("role must be buyer or seller")
    if event_type not in ALLOWED_EVENT_TYPES:
        raise ValueError("unsupported transcript event type")
    if not _SAFE_ID.fullmatch(session_id):
        raise ValueError("invalid Samantha session id")
    clean_metadata = _clean_metadata(metadata or {})
    encoded_metadata = json.dumps(clean_metadata, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(encoded_metadata) > MAX_METADATA_BYTES:
        clean_metadata = {"truncated": True}
    event = {
        "at": datetime.now(timezone.utc).isoformat(),
        "principal_id": principal_id,
        "role": role,
        "session_id": session_id,
        "event_type": event_type,
        "content": (content or "")[:MAX_CONTENT_CHARS],
        "metadata": clean_metadata,
    }
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n")
    return event


def list_events(*, principal_id: str, role: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    path = _path()
    if not path.exists():
        return []
    matches: list[dict[str, Any]] = []
    with _lock:
        lines = path.read_text(encoding="utf-8").splitlines()
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("principal_id") != principal_id or (role and event.get("role") != role):
            continue
        matches.append(event)
        if len(matches) >= max(1, min(limit, 500)):
            break
    matches.reverse()
    return matches
