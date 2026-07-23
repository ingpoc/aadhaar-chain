from __future__ import annotations

import pytest

from app.domain_state_machines import (
    CONTRACT_VERSION,
    DuplicateTransition,
    STATE_MACHINES,
    StaleTransition,
    TransitionError,
    require_transition,
    transition_manifest,
)


def test_contract_owns_every_cf0_state_machine_family() -> None:
    assert CONTRACT_VERSION == "cf0.v1"
    assert {machine.family for machine in STATE_MACHINES.values()} == {
        "order",
        "payment_refund",
        "return",
        "issue_grievance",
        "approval",
    }
    assert set(STATE_MACHINES) == {
        "order",
        "payment",
        "refund",
        "return",
        "issue",
        "approval",
    }


@pytest.mark.parametrize(
    ("machine_name", "source", "target"),
    [
        (name, source, target)
        for name, machine in STATE_MACHINES.items()
        for source, targets in machine.transitions.items()
        for target in targets
    ],
)
def test_every_declared_transition_is_legal(
    machine_name: str, source: str, target: str
) -> None:
    assert (
        require_transition(
            machine_name,
            source,
            target,
            current_version=7,
            expected_version=7,
        )
        == 8
    )


@pytest.mark.parametrize("machine_name", sorted(STATE_MACHINES))
def test_every_undeclared_edge_is_illegal(machine_name: str) -> None:
    machine = STATE_MACHINES[machine_name]
    for source in machine.states:
        for target in machine.states:
            if source == target or target in machine.transitions.get(source, frozenset()):
                continue
            with pytest.raises(TransitionError, match="illegal"):
                require_transition(machine_name, source, target)


@pytest.mark.parametrize("machine_name", sorted(STATE_MACHINES))
def test_duplicate_requires_an_idempotent_replay_binding(machine_name: str) -> None:
    initial = STATE_MACHINES[machine_name].initial
    with pytest.raises(DuplicateTransition):
        require_transition(machine_name, initial, initial, current_version=2)
    assert (
        require_transition(
            machine_name,
            initial,
            initial,
            current_version=2,
            expected_version=2,
            allow_idempotent_replay=True,
        )
        == 2
    )


def test_stale_and_concurrent_writers_cannot_advance_the_same_version() -> None:
    assert require_transition(
        "order",
        "payment_pending",
        "paid",
        current_version=4,
        expected_version=4,
    ) == 5
    with pytest.raises(StaleTransition, match="version 4 is stale"):
        require_transition(
            "order",
            "paid",
            "accepted",
            current_version=5,
            expected_version=4,
        )


@pytest.mark.parametrize(
    ("machine_name", "source", "target"),
    [
        ("order", "payment_unknown", "paid"),
        ("payment", "unknown", "reconciled"),
        ("refund", "unknown", "reconciled"),
        ("return", "refund_pending", "completed"),
        ("issue", "escalated", "resolution_proposed"),
        ("approval", "issued", "revoked"),
    ],
)
def test_recovery_and_invalidation_edges_are_explicit(
    machine_name: str, source: str, target: str
) -> None:
    require_transition(machine_name, source, target)


def test_terminal_states_have_no_outgoing_transitions() -> None:
    for machine in STATE_MACHINES.values():
        for terminal in machine.terminal:
            assert not machine.transitions.get(terminal)


def test_manifest_is_versioned_and_serializable() -> None:
    manifest = transition_manifest()
    assert manifest["contract_version"] == CONTRACT_VERSION
    assert manifest["machines"]["approval"]["terminal"] == [
        "consumed",
        "expired",
        "revoked",
    ]
