"""Versioned CF0 lifecycle contracts shared by commerce and AgentGuard runtimes."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


CONTRACT_VERSION = "cf0.v1"


class TransitionError(ValueError):
    """A requested transition is not admitted by the canonical contract."""


class StaleTransition(TransitionError):
    """The caller attempted to mutate an older aggregate version."""


class DuplicateTransition(TransitionError):
    """The caller repeated a transition without an idempotent replay binding."""


@dataclass(frozen=True)
class StateMachine:
    family: str
    version: str
    initial: str
    transitions: Mapping[str, frozenset[str]]
    terminal: frozenset[str]

    @property
    def states(self) -> frozenset[str]:
        targets = {
            target
            for allowed_targets in self.transitions.values()
            for target in allowed_targets
        }
        return frozenset(self.transitions) | frozenset(targets) | self.terminal


def _machine(
    *,
    family: str,
    initial: str,
    transitions: Mapping[str, set[str]],
    terminal: set[str],
) -> StateMachine:
    frozen = MappingProxyType(
        {source: frozenset(targets) for source, targets in transitions.items()}
    )
    machine = StateMachine(
        family=family,
        version="1",
        initial=initial,
        transitions=frozen,
        terminal=frozenset(terminal),
    )
    if initial not in machine.states:
        raise RuntimeError(f"{family} initial state is not declared")
    if any(machine.transitions.get(state) for state in machine.terminal):
        raise RuntimeError(f"{family} terminal states must not have outgoing edges")
    return machine


STATE_MACHINES: Mapping[str, StateMachine] = MappingProxyType(
    {
        "order": _machine(
            family="order",
            initial="prepared",
            transitions={
                "prepared": {"payment_pending", "cancelled"},
                "payment_pending": {
                    "paid",
                    "payment_failed",
                    "payment_unknown",
                    "cancelled",
                },
                "payment_unknown": {"paid", "payment_failed", "cancelled"},
                "paid": {"accepted", "rejected", "confirmed", "cancelled"},
                "confirmed": {"preparing", "cancelled"},
                "accepted": {"fulfilled", "cancelled"},
                "preparing": {"shipped", "cancelled"},
                "fulfilled": {"closed"},
                "shipped": {"delivered"},
            },
            terminal={
                "payment_failed",
                "rejected",
                "delivered",
                "closed",
                "cancelled",
            },
        ),
        "payment": _machine(
            family="payment_refund",
            initial="pending",
            transitions={
                "pending": {"succeeded", "failed", "unknown"},
                "unknown": {"reconciled", "failed"},
            },
            terminal={"succeeded", "failed", "reconciled"},
        ),
        "refund": _machine(
            family="payment_refund",
            initial="pending",
            transitions={
                "pending": {"succeeded", "failed", "unknown"},
                "unknown": {"reconciled", "failed"},
            },
            terminal={"succeeded", "failed", "reconciled"},
        ),
        "return": _machine(
            family="return",
            initial="requested",
            transitions={
                "requested": {"approved", "rejected", "cancelled"},
                "approved": {"in_transit", "cancelled"},
                "in_transit": {"received"},
                "received": {"refund_pending", "replacement_pending"},
                "refund_pending": {"completed", "failed"},
                "replacement_pending": {"completed", "failed"},
            },
            terminal={"rejected", "cancelled", "completed", "failed"},
        ),
        "issue": _machine(
            family="issue_grievance",
            initial="open",
            transitions={
                "open": {"acknowledged"},
                "acknowledged": {"resolution_proposed", "escalated"},
                "escalated": {"resolution_proposed"},
                "resolution_proposed": {"accepted", "rejected"},
                "rejected": {"acknowledged", "escalated"},
                "accepted": {"closed"},
            },
            terminal={"closed"},
        ),
        "approval": _machine(
            family="approval",
            initial="issued",
            transitions={
                "issued": {"consumed", "expired", "revoked"},
            },
            terminal={"consumed", "expired", "revoked"},
        ),
    }
)

PAYMENT_ORDER_TARGETS: Mapping[str, str] = MappingProxyType(
    {
        "pending": "payment_pending",
        "succeeded": "paid",
        "failed": "payment_failed",
        "unknown": "payment_unknown",
        "reconciled": "paid",
    }
)


def require_transition(
    machine_name: str,
    current: str,
    target: str,
    *,
    current_version: int | None = None,
    expected_version: int | None = None,
    allow_idempotent_replay: bool = False,
) -> int | None:
    """Validate one transition and return the next aggregate version."""
    try:
        machine = STATE_MACHINES[machine_name]
    except KeyError as exc:
        raise TransitionError(f"unknown state machine: {machine_name}") from exc

    if current_version is not None and current_version < 1:
        raise TransitionError("current version must be positive")
    if expected_version is not None:
        if current_version is None:
            raise TransitionError("expected version requires a current version")
        if expected_version != current_version:
            raise StaleTransition(
                f"{machine_name} version {expected_version} is stale; "
                f"current version is {current_version}"
            )
    if current not in machine.states:
        raise TransitionError(f"unknown {machine_name} state: {current}")
    if target not in machine.states:
        raise TransitionError(f"unknown {machine_name} state: {target}")
    if current == target:
        if allow_idempotent_replay:
            return current_version
        raise DuplicateTransition(
            f"duplicate {machine_name} transition {current} -> {target}"
        )
    if target not in machine.transitions.get(current, frozenset()):
        raise TransitionError(
            f"illegal {machine_name} transition {current} -> {target}"
        )
    return None if current_version is None else current_version + 1


def transition_manifest() -> dict[str, object]:
    """Serializable contract evidence for docs, inventories, and tests."""
    return {
        "contract_version": CONTRACT_VERSION,
        "payment_order_targets": dict(PAYMENT_ORDER_TARGETS),
        "machines": {
            name: {
                "family": machine.family,
                "version": machine.version,
                "initial": machine.initial,
                "terminal": sorted(machine.terminal),
                "transitions": {
                    source: sorted(targets)
                    for source, targets in machine.transitions.items()
                },
            }
            for name, machine in STATE_MACHINES.items()
        },
    }


__all__ = [
    "CONTRACT_VERSION",
    "DuplicateTransition",
    "PAYMENT_ORDER_TARGETS",
    "STATE_MACHINES",
    "StaleTransition",
    "StateMachine",
    "TransitionError",
    "require_transition",
    "transition_manifest",
]
