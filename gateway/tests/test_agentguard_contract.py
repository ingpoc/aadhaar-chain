"""Golden compatibility proof for the shared AgentGuard contract."""
from __future__ import annotations

import json
from pathlib import Path

from app.agentguard_contract import DecisionV2, canonicalize, sha256_hex


WORKSPACE_FIXTURES = (
    Path(__file__).resolve().parents[3] / "shared" / "agentguard-contract" / "fixtures"
)
LOCAL_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agentguard-contract"
FIXTURES = WORKSPACE_FIXTURES if WORKSPACE_FIXTURES.is_dir() else LOCAL_FIXTURES


def test_python_canonicalizes_and_hashes_shared_golden_action_request() -> None:
    action_request = json.loads((FIXTURES / "golden-action-request.json").read_text(encoding="utf-8"))
    expected = (FIXTURES / "golden-action-request.canonical.txt").read_text(encoding="utf-8").strip()

    canonical = canonicalize(action_request)

    assert canonical == expected
    assert sha256_hex(canonical) == "b1845e24832e79a73abc2f3502a3130f9d947caf5b1c89e3c2cf8e74fa9ebab2"
    assert canonicalize(dict(reversed(list(action_request.items())))) == expected


def test_python_validates_shared_golden_decision_v2() -> None:
    decision = DecisionV2.model_validate_json(
        (FIXTURES / "golden-decision-v2.json").read_text(encoding="utf-8")
    )

    assert decision.schema_version == "2"
    assert decision.decision == "need_approval"
    assert decision.required_action == "review"
    assert decision.risk_level == "medium"
