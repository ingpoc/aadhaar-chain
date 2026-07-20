"""Golden compatibility proof for the shared AgentGuard contract."""
from __future__ import annotations

import json
from pathlib import Path

from app.agentguard_contract import canonicalize, sha256_hex


FIXTURES = Path(__file__).resolve().parents[3] / "shared" / "agentguard-contract" / "fixtures"


def test_python_canonicalizes_and_hashes_shared_golden_action_request() -> None:
    action_request = json.loads((FIXTURES / "golden-action-request.json").read_text(encoding="utf-8"))
    expected = (FIXTURES / "golden-action-request.canonical.txt").read_text(encoding="utf-8").strip()

    canonical = canonicalize(action_request)

    assert canonical == expected
    assert sha256_hex(canonical) == "b1845e24832e79a73abc2f3502a3130f9d947caf5b1c89e3c2cf8e74fa9ebab2"
    assert canonicalize(dict(reversed(list(action_request.items())))) == expected
