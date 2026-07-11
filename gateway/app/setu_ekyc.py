"""Setu.co Aadhaar eKYC client (sandbox + production).

Docs: https://docs.setu.co/data/ekyc/quickstart
Sandbox: https://dg-sandbox.setu.co
Production: https://dg.setu.co
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import settings

MAP_FILE = "setu-ekyc-map.json"


def setu_ekyc_configured() -> bool:
    return bool(
        settings.setu_ekyc_enabled
        and settings.setu_ekyc_client_id
        and settings.setu_ekyc_client_secret
        and settings.setu_ekyc_product_instance_id
    )


def _headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "x-client-id": settings.setu_ekyc_client_id or "",
        "x-client-secret": settings.setu_ekyc_client_secret or "",
        "x-product-instance-id": settings.setu_ekyc_product_instance_id or "",
    }


def _base() -> str:
    return (settings.setu_ekyc_base_url or "https://dg-sandbox.setu.co").rstrip("/")


def _request(method: str, path: str, body: Optional[dict] = None) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = Request(
        f"{_base()}{path}",
        data=data,
        headers=_headers(),
        method=method,
    )
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Setu eKYC HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Setu eKYC unreachable: {exc}") from exc


def create_ekyc_request(*, webhook_url: str, redirection_url: str) -> dict[str, Any]:
    """POST /api/ekyc/ → id, status, kycURL."""
    return _request(
        "POST",
        "/api/ekyc/",
        {
            "webhook_url": webhook_url,
            "redirection_url": redirection_url,
        },
    )


def get_ekyc_request(request_id: str) -> dict[str, Any]:
    """GET /api/ekyc/:id."""
    return _request("GET", f"/api/ekyc/{request_id}")


def _map_path() -> Path:
    return Path(settings.data_dir).expanduser() / MAP_FILE


def save_ekyc_link(*, setu_id: str, wallet_address: str, verification_id: str) -> None:
    path = _map_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    data[setu_id] = {
        "wallet_address": wallet_address,
        "verification_id": verification_id,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_ekyc_link(setu_id: str) -> Optional[dict[str, str]]:
    path = _map_path()
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    row = data.get(setu_id)
    return row if isinstance(row, dict) else None
