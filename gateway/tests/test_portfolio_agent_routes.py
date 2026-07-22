"""Portfolio agent control-plane routes (FQDN Cursor handoff)."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from main import app
from cursor_agent_runtime.outcome import (
    RuntimeOutcomeError,
    parse_verified_runtime_outcome,
)


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


def test_runtime_outcome_rejects_narrative_only_success() -> None:
    with pytest.raises(RuntimeOutcomeError, match="narrative text"):
        parse_verified_runtime_outcome(
            "I triaged 17 orders and completed the task.",
            observed_completed_tools=("shell",),
        )


def test_runtime_outcome_rejects_unobserved_tool_claim() -> None:
    content = (
        '{"status":"completed","summary":"Triaged 17 orders.",'
        '"executed_tools":["commerce_api"],'
        '"postcondition":{"verified":true,"evidence":"Queue now has one follow-up."}}'
    )
    with pytest.raises(RuntimeOutcomeError, match="completed SDK tool calls"):
        parse_verified_runtime_outcome(
            content,
            observed_completed_tools=("read_file",),
        )


def test_runtime_outcome_accepts_observed_tool_and_verified_postcondition() -> None:
    content = (
        '{"status":"completed","summary":"Created the weekly basket.",'
        '"executed_tools":["commerce_api"],'
        '"postcondition":{"verified":true,"evidence":"Read-back returned basket weekly-1."}}'
    )
    outcome = parse_verified_runtime_outcome(
        content,
        observed_completed_tools=("commerce_api", "commerce_api"),
    )
    assert outcome.summary == "Created the weekly basket."
    assert outcome.executed_tools == ("commerce_api",)
    assert outcome.postcondition_evidence == "Read-back returned basket weekly-1."


def _install_fake_cursor_run(monkeypatch, *, result_text: str, tool_name: str = "commerce_api") -> None:
    from app import portfolio_agent_routes
    import cursor_sdk

    class _Policy:
        runtime_available = True
        auth_mode = "cursor_api_key"
        model = "composer-2.5"
        blocked_reason = None

    class _Run:
        def messages(self):
            return [
                SimpleNamespace(
                    type="tool_call",
                    status="completed",
                    name=tool_name,
                )
            ]

        def wait(self):
            return SimpleNamespace(status="success", result=result_text, id="run-test")

    class _AgentContext:
        agent_id = "sdk-test"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def send(self, _prompt):
            return _Run()

    class _Agent:
        @staticmethod
        def create(_options):
            return _AgentContext()

        @staticmethod
        def resume(_agent_id, _options):
            return _AgentContext()

    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    monkeypatch.setattr(portfolio_agent_routes, "resolve_runtime_policy", lambda: _Policy())
    monkeypatch.setattr(cursor_sdk, "Agent", _Agent)
    monkeypatch.setattr(cursor_sdk, "AgentOptions", lambda **kwargs: kwargs)
    monkeypatch.setattr(cursor_sdk, "LocalAgentOptions", lambda **kwargs: kwargs)
    portfolio_agent_routes._SESSIONS.clear()


def test_portfolio_agent_stream_fails_closed_on_narrative_result(monkeypatch) -> None:
    _install_fake_cursor_run(
        monkeypatch,
        result_text="Triaged 17 orders: 16 resolved and 1 needs follow-up.",
    )
    response = client.post(
        "/api/agent/seller",
        headers={"X-User-Id": "principal:demo:seller"},
        json={"prompt": "Triage my orders", "sessionId": "fail-closed", "context": {}},
    )
    assert response.status_code == 200
    assert '"type": "error"' in response.text
    assert '"type": "result"' not in response.text


def test_portfolio_agent_stream_emits_verified_outcome(monkeypatch) -> None:
    _install_fake_cursor_run(
        monkeypatch,
        result_text=(
            '{"status":"completed","summary":"No orders require follow-up.",'
            '"executed_tools":["commerce_api"],'
            '"postcondition":{"verified":true,'
            '"evidence":"Read-back returned zero open orders."}}'
        ),
    )
    response = client.post(
        "/api/agent/seller",
        headers={"X-User-Id": "principal:demo:seller"},
        json={"prompt": "Triage my orders", "sessionId": "verified", "context": {}},
    )
    assert response.status_code == 200
    assert '"type": "result"' in response.text
    assert '"executed_tools": ["commerce_api"]' in response.text
    assert '"verified": true' in response.text
