"""Executable CF0 inventory for every non-safe HTTP route.

The inventory deliberately includes command-shaped POSTs that do not persist
state.  That makes the completeness check fail whenever a new write-capable
surface is added without an explicit authority and audit classification.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable


INVENTORY_VERSION = "cf0.write-risk.v1"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
RISK_TIERS = frozenset({"read_only", "low", "medium", "high", "critical"})


@dataclass(frozen=True)
class MutationRecord:
    route_id: str
    method: str
    path: str
    handler: str
    mutation_effect: str
    resource_owner: str
    source_of_truth: str
    risk_tier: str
    authority_path: str
    agentguard_action: str
    executor: str
    idempotency: str
    audit_receipt: str
    negative_test: str


def _record(method: str, path: str, handler: str) -> MutationRecord:
    route_id = f"{method} {path}"
    policy_family = ""
    effect = "persistent_write"
    owner = "gateway"
    source = "PostgreSQL"
    risk = "medium"
    authority = "authenticated_session"
    action = "not_applicable"
    executor = "gateway_command_handler"
    idempotency = "route_specific_or_transactional"
    audit = "domain_row_and_gateway_log"
    negative = "reject_unauthenticated_or_foreign_principal"

    if path.startswith("/api/agentguard/"):
        policy_family = "agentguard"
        owner = "AgentGuard"
        source = "PostgreSQL_or_exclusive_local_fallback"
        authority = "AgentGuard_session_principal"
        audit = "AgentGuard_decision_approval_intent_receipt"
        action = "control_plane"
        if "/actions/evaluate" in path or "/receipts/verify" in path:
            effect = "none"
            risk = "read_only"
            idempotency = "not_required"
            negative = "reject_invalid_contract_or_foreign_principal"
        elif "/actions/execute" in path or "/approvals/" in path:
            risk = "high"
            action = "request_bound_approval_or_mandate"
            negative = "reject_replay_stale_approval_or_request_hash_mismatch"
    elif path.startswith("/api/commerce/v1/"):
        policy_family = "commerce_v1"
        owner = "CommerceV1"
        action = "buyer_commerce_command"
        authority = "session_principal_plus_AgentGuard_at_checkout_effect"
        audit = "commerce_version_idempotency_ledger_and_AgentGuard_receipt"
        risk = "high" if "checkout" in path else "medium"
        negative = "reject_stale_version_foreign_principal_or_idempotency_conflict"
    elif path.startswith("/api/demo-commerce/test-fixtures/"):
        policy_family = "fixture_compatibility"
        owner = "CommerceCompatibilityAdapter"
        authority = "fixture_mode_plus_session_principal"
        action = "compatibility_fixture_only"
        risk = "high"
        audit = "fixture_domain_row_and_idempotency_record"
        negative = "reject_when_fixture_mode_disabled_or_principal_mismatch"
    elif path.startswith("/api/demo-commerce/"):
        policy_family = "commerce_compatibility"
        owner = "CommerceCompatibilityAdapter"
        authority = "authenticated_session_principal"
        action = "buyer_issue_create"
        risk = "low"
        audit = "versioned_commerce_issue_row"
        negative = "reject_unauthenticated_or_foreign_order_principal"
    elif path.startswith("/api/ondc/") or path.startswith("/ondc/"):
        policy_family = "ondc_protocol"
        owner = "ONDC_message_runtime"
        source = "PostgreSQL_inbox_outbox"
        authority = "ONDC_signature_or_operator_service_authority"
        action = "signed_protocol_command"
        executor = "ONDC_inbox_outbox_worker"
        idempotency = "message_id_deduplication"
        audit = "signed_envelope_inbox_outbox_dead_letter"
        negative = "reject_bad_signature_duplicate_or_correlation_mismatch"
        risk = "critical" if "dead-letter" in path or "outbox/drain" in path else "high"
    elif path.startswith("/api/identity/"):
        policy_family = "legacy_identity"
        owner = "legacy_identity_hangar"
        authority = "session_operator_webhook_or_dev_fixture_gate"
        action = "out_of_scope_legacy_identity_write"
        audit = "identity_domain_row_and_audit_event"
        negative = "reject_foreign_principal_unsigned_webhook_or_disabled_fixture"
        risk = "critical" if "decision" in path or "revoke" in path else "high"
        if "/proof-token/verify" in path:
            effect = "none"
            risk = "read_only"
            idempotency = "not_required"
    elif path.startswith("/api/auth/"):
        policy_family = "host_identity"
        owner = "host_identity"
        source = "signed_session_cookie"
        authority = "Auth0_or_local_demo_gate"
        action = "session_lifecycle"
        executor = "auth_adapter"
        idempotency = "session_replacement"
        audit = "auth_provider_and_gateway_log"
        negative = "reject_demo_continue_outside_allowed_environment"
        risk = "medium"
    elif path.startswith("/api/agent/"):
        policy_family = "agent_runtime"
        owner = "agent_runtime"
        authority = "session_principal_plus_AgentGuard_tool_boundary"
        action = "runtime_tool_delegation"
        executor = "Cursor_runtime_agent"
        audit = "runtime_event_stream_and_AgentGuard_tool_receipts"
        negative = "reject_unauthenticated_or_unauthorized_tool_effect"
        risk = "high"
    elif path.startswith("/api/realtime/client-secret"):
        policy_family = "realtime_broker"
        owner = "realtime_session_broker"
        source = "ephemeral_provider_credential"
        effect = "external_ephemeral_session"
        authority = "authenticated_session"
        action = "voice_session_create"
        executor = "gateway_provider_adapter"
        idempotency = "not_required"
        audit = "gateway_redacted_log"
        negative = "reject_unauthenticated_or_misconfigured_provider"
        risk = "high"
    elif path.startswith("/api/realtime/transcripts/"):
        policy_family = "realtime_transcript"
        owner = "realtime_transcript_store"
        authority = "authenticated_realtime_session"
        action = "transcript_event_persist"
        audit = "transcript_event_row"
        negative = "reject_foreign_session_or_invalid_event"
        risk = "medium"

    if not policy_family:
        raise ValueError(f"unclassified non-safe route: {route_id}")

    return MutationRecord(
        route_id=route_id,
        method=method,
        path=path,
        handler=handler,
        mutation_effect=effect,
        resource_owner=owner,
        source_of_truth=source,
        risk_tier=risk,
        authority_path=authority,
        agentguard_action=action,
        executor=executor,
        idempotency=idempotency,
        audit_receipt=audit,
        negative_test=negative,
    )


def _effective_routes(
    routes: Iterable[Any], inherited_prefix: str = ""
) -> Iterable[tuple[Any, str]]:
    """Yield leaf routes across FastAPI's lazy included-router wrappers."""
    for route in routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            include_context = getattr(route, "include_context", None)
            include_prefix = str(getattr(include_context, "prefix", "") or "")
            yield from _effective_routes(
                original_router.routes, f"{inherited_prefix}{include_prefix}"
            )
            continue
        path = str(getattr(route, "path", "") or "")
        yield route, f"{inherited_prefix}{path}"


def inventory_for_routes(routes: Iterable[Any]) -> list[MutationRecord]:
    inventory: list[MutationRecord] = []
    for route, path in _effective_routes(routes):
        handler = getattr(route, "name", "")
        for method in sorted(set(getattr(route, "methods", set())) - SAFE_METHODS):
            inventory.append(_record(method, path, handler))
    return sorted(inventory, key=lambda item: item.route_id)


def inventory_manifest(routes: Iterable[Any]) -> dict[str, Any]:
    records = inventory_for_routes(routes)
    return {
        "version": INVENTORY_VERSION,
        "route_count": len(records),
        "risk_tiers": sorted(RISK_TIERS),
        "records": [asdict(record) for record in records],
    }


__all__ = [
    "INVENTORY_VERSION",
    "MutationRecord",
    "RISK_TIERS",
    "SAFE_METHODS",
    "inventory_for_routes",
    "inventory_manifest",
]
