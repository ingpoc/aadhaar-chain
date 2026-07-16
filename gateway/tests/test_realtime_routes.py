from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.session_auth import SESSION_COOKIE_NAME, create_principal_session_token
from config import settings
from main import app


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "openai_api_key", "test-samantha-key")


def _client(role: str | None = None, principal: str = "principal:auth0:test") -> TestClient:
    client = TestClient(app)
    if role:
        token = create_principal_session_token(
            principal_id=principal,
            audience=f"ondc{role}",
            identity_provider="auth0",
        )
        client.cookies.set(SESSION_COOKIE_NAME, token)
    return client


def test_samantha_requires_matching_authenticated_app_session() -> None:
    assert _client().post("/api/realtime/client-secret", json={"role": "buyer"}).status_code == 401
    assert _client("seller").post("/api/realtime/client-secret", json={"role": "buyer"}).status_code == 403
    assert _client().post(
        "/api/realtime/transcripts/events",
        json={"role": "buyer", "session_id": "samantha-buyer-12345678", "event_type": "user_text", "content": "atta"},
    ).status_code == 401


def test_authenticated_transcripts_are_principal_scoped_and_sanitized() -> None:
    client = _client("buyer")
    payload = {
        "role": "buyer",
        "session_id": "samantha-buyer-12345678",
        "event_type": "tool_call",
        "content": "add_to_cart",
        "metadata": {"arguments": {"item_id": "atta-1"}, "client_secret": "must-not-persist"},
    }
    saved = client.post("/api/realtime/transcripts/events", json=payload)
    assert saved.status_code == 200
    event = saved.json()["data"]["event"]
    assert event["principal_id"] == "principal:auth0:test"
    assert "client_secret" not in event["metadata"]

    listed = client.get("/api/realtime/transcripts?role=buyer")
    assert listed.status_code == 200
    assert listed.json()["data"]["count"] == 1
    assert _client("buyer", "principal:auth0:other").get(
        "/api/realtime/transcripts?role=buyer"
    ).json()["data"]["count"] == 0


def test_client_secret_primes_complete_buyer_cart_tool_contract() -> None:
    response = AsyncMock()
    response.status_code = 200
    response.json = lambda: {"value": "ephemeral-test", "expires_at": 123}
    response.text = "ok"
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=response)

    with patch("app.realtime_routes.httpx.AsyncClient", return_value=mock_client):
        result = _client("buyer").post("/api/realtime/client-secret", json={"role": "buyer"})
    assert result.status_code == 200
    data = result.json()["data"]
    assert data["client_secret"] == "ephemeral-test"
    assert "raw" not in data
    assert {"clear_cart", "remove_from_cart", "set_cart_quantity"}.issubset(data["tools_registered"])
