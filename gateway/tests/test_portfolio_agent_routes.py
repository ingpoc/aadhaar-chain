"""Portfolio agent control-plane routes (FQDN Cursor handoff)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from main import app


client = TestClient(app)


def test_portfolio_runtime_requires_user_header() -> None:
    response = client.get("/api/agent/runtime?app=ondc-buyer")
    assert response.status_code == 401


def test_portfolio_runtime_snapshot_shape(monkeypatch) -> None:
    from app import portfolio_agent_routes

    class _Policy:
        runtime_available = True
        auth_mode = "api_key"
        model = "composer-2.5"
        blocked_reason = None
        provider = "cursor"

    monkeypatch.setattr(
        portfolio_agent_routes,
        "resolve_runtime_policy",
        lambda: _Policy(),
    )

    response = client.get(
        "/api/agent/runtime?app=ondc-buyer",
        headers={"X-User-Id": "principal:demo:test"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["runtime_available"] is True
    assert body["agent_access"] is True
    assert body["control_plane"] == "gateway"
    assert body["app_id"] == "ondc-buyer"
