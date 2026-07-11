"""AgentGuard store + HTTP API tests."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import agentguard
from config import settings
from main import app

# Valid-length base58-ish wallet stubs for Field min_length=32
WALLET = "AgentGuardTestWallet1111111111111111111"


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    yield


def test_ensure_agent_and_policy_roundtrip() -> None:
    agent, policy = agentguard.ensure_seller_ops_agent(WALLET)
    assert agent.name == "Store Operations Assistant"
    assert agent.status == "active"
    assert policy.refund_auto_max_inr == 5000
    agent2, policy2 = agentguard.ensure_seller_ops_agent(WALLET)
    assert agent2.agent_id == agent.agent_id
    assert policy2.policy_id == policy.policy_id


def test_evaluate_allow_need_approval_and_pause() -> None:
    allow = agentguard.evaluate_action(
        wallet_address=WALLET,
        action="refund",
        amount_inr=3000,
        resource_id="order-1",
    )
    assert allow["decision"] == "allow"
    assert allow["receipt"]["outcome"] == "allowed"

    need = agentguard.evaluate_action(
        wallet_address=WALLET,
        action="refund",
        amount_inr=7500,
        resource_id="order-2",
    )
    assert need["decision"] == "need_approval"
    assert need["approval"]["approval_id"]

    agent_id = allow["agent"]["agent_id"]
    agentguard.pause_agent(agent_id)
    denied = agentguard.evaluate_action(
        wallet_address=WALLET,
        action="refund",
        amount_inr=1000,
        resource_id="order-3",
    )
    assert denied["decision"] == "deny"
    assert denied["receipt"]["outcome"] == "paused"


def test_consume_approval_once_replay_conflicts() -> None:
    need = agentguard.evaluate_action(
        wallet_address=WALLET,
        action="refund",
        amount_inr=7500,
        resource_id="order-replay",
    )
    approval_id = need["approval"]["approval_id"]
    first = agentguard.consume_approval(
        approval_id=approval_id,
        wallet_address=WALLET,
    )
    assert first["receipt"]["outcome"] == "approved"
    with pytest.raises(agentguard.ConflictError):
        agentguard.consume_approval(
            approval_id=approval_id,
            wallet_address=WALLET,
        )


def test_http_agentguard_flow() -> None:
    client = TestClient(app)

    ensure = client.post(
        "/api/agentguard/agents/ensure",
        json={"wallet_address": WALLET},
    )
    assert ensure.status_code == 200
    assert ensure.json()["success"] is True
    agent_id = ensure.json()["data"]["agent"]["agent_id"]
    assert ensure.json()["data"]["policy"]["refund_auto_max_inr"] == 5000

    status = client.get(f"/api/agentguard/wallets/{WALLET}")
    assert status.status_code == 200
    assert status.json()["data"]["policy"]["refund_auto_max_inr"] == 5000

    ok = client.post(
        "/api/agentguard/actions/evaluate",
        json={
            "wallet_address": WALLET,
            "action": "refund",
            "amount_inr": 3000,
            "resource_id": "ord-a",
        },
    )
    assert ok.json()["data"]["decision"] == "allow"

    need = client.post(
        "/api/agentguard/actions/evaluate",
        json={
            "wallet_address": WALLET,
            "action": "refund",
            "amount_inr": 7500,
            "resource_id": "ord-b",
        },
    )
    approval_id = need.json()["data"]["approval"]["approval_id"]

    consume = client.post(
        "/api/agentguard/approvals/consume",
        json={"wallet_address": WALLET, "approval_id": approval_id},
    )
    assert consume.status_code == 200
    receipt_id = consume.json()["data"]["receipt"]["receipt_id"]

    replay = client.post(
        "/api/agentguard/approvals/consume",
        json={"wallet_address": WALLET, "approval_id": approval_id},
    )
    assert replay.status_code == 409

    pause = client.post(
        f"/api/agentguard/agents/{agent_id}/pause",
        json={"wallet_address": WALLET},
    )
    assert pause.status_code == 200
    assert pause.json()["data"]["agent"]["status"] == "paused"

    blocked = client.post(
        "/api/agentguard/actions/evaluate",
        json={
            "wallet_address": WALLET,
            "action": "refund",
            "amount_inr": 1000,
            "resource_id": "ord-c",
        },
    )
    assert blocked.json()["data"]["decision"] == "deny"

    receipt = client.get(f"/api/agentguard/receipts/{receipt_id}")
    assert receipt.status_code == 200
    body = receipt.json()["data"]["receipt"]
    assert "aadhaar" not in str(body).lower()
    assert body["amount_inr"] == 7500


def test_http_agentguard_checkout_action() -> None:
    client = TestClient(app)
    allow = client.post(
        "/api/agentguard/actions/evaluate",
        json={
            "wallet_address": WALLET,
            "action": "checkout",
            "amount_inr": 5000,
            "resource_id": "session-1",
        },
    )
    assert allow.json()["data"]["decision"] == "allow"

    need = client.post(
        "/api/agentguard/actions/evaluate",
        json={
            "wallet_address": WALLET,
            "action": "checkout",
            "amount_inr": 15000,
            "resource_id": "session-2",
        },
    )
    assert need.json()["data"]["decision"] == "need_approval"
    approval_id = need.json()["data"]["approval"]["approval_id"]
    consume = client.post(
        "/api/agentguard/approvals/consume",
        json={"wallet_address": WALLET, "approval_id": approval_id},
    )
    assert consume.status_code == 200
    replay = client.post(
        "/api/agentguard/approvals/consume",
        json={"wallet_address": WALLET, "approval_id": approval_id},
    )
    assert replay.status_code == 409
