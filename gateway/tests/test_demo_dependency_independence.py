"""AgentGuard demo paths must work without regulated or blockchain integrations."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest
from fastapi.testclient import TestClient

from config import settings
from main import app


def test_gateway_imports_when_solana_packages_are_unavailable(tmp_path: Path) -> None:
    script = textwrap.dedent(
        """
        import builtins

        original_import = builtins.__import__

        def reject_solana(name, *args, **kwargs):
            if name.split('.', 1)[0] in {'solana', 'solders'}:
                raise ModuleNotFoundError(f'blocked optional dependency: {name}')
            return original_import(name, *args, **kwargs)

        builtins.__import__ = reject_solana
        from main import app
        assert app.title
        """
    )
    env = {
        **os.environ,
        "DATA_DIR": str(tmp_path),
        "SOLANA_ON_CHAIN_ENABLED": "false",
        "SETU_EKYC_ENABLED": "false",
        "ONDC_ENABLED": "false",
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_demo_core_works_with_optional_integrations_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "aadhaar_chain_env", "demo")
    monkeypatch.setattr(settings, "solana_on_chain_enabled", False)
    monkeypatch.setattr(settings, "setu_ekyc_enabled", False)
    monkeypatch.setattr(settings, "ondc_enabled", False)
    monkeypatch.setattr(settings, "auth_demo_continue", True)

    client = TestClient(app)
    assert client.get("/health").status_code == 200

    login = client.post("/api/auth/demo-continue", json={"audience": "ondcbuyer"})
    assert login.status_code == 200
    agent = client.post(
        "/api/agentguard/agents/ensure",
        json={"role": "buyer"},
        cookies=login.cookies,
    )
    assert agent.status_code == 200
    assert agent.json()["data"]["agent"]["principal_id"].startswith("principal:demo:")

    item = client.post(
        "/api/demo-commerce/test-fixtures/seller/items",
        json={
            "title": "Dependency-free Atta",
            "price_inr": 120,
            "inventory": 2,
            "seller_name": "Independent Foods",
        },
    ).json()["data"]["item"]
    published = client.post(f"/api/demo-commerce/test-fixtures/seller/items/{item['item_id']}/publish")
    assert published.status_code == 200
    search = client.get("/api/demo-commerce/buyer/search", params={"q": "grocery"})
    assert search.json()["data"]["count"] == 1

    order = client.post(
        "/api/demo-commerce/test-fixtures/buyer/orders",
        json={
            "idempotency_key": "dependency-free-order-1",
            "item_id": item["item_id"],
            "quantity": 1,
            "buyer_id": agent.json()["data"]["agent"]["principal_id"],
        },
    )
    assert order.status_code == 200
    assert order.json()["data"]["order"]["payment"]["adapter"] == "simulated_payment_v1"
    assert order.json()["data"]["order"]["status"] == "paid"
