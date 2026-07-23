from app.mutation_inventory import RISK_TIERS, inventory_for_routes
from main import app


def test_every_non_safe_route_has_complete_write_risk_classification() -> None:
    records = inventory_for_routes(app.routes)
    discovered = {
        f"{method.upper()} {path}"
        for path, operations in app.openapi()["paths"].items()
        for method in operations
        if method.upper() not in {"GET", "HEAD", "OPTIONS", "PARAMETERS"}
    }

    assert len(records) == len(discovered)
    assert {record.route_id for record in records} == discovered
    for record in records:
        assert record.risk_tier in RISK_TIERS
        assert record.resource_owner
        assert record.source_of_truth
        assert record.authority_path
        assert record.agentguard_action
        assert record.executor
        assert record.idempotency
        assert record.audit_receipt
        assert record.negative_test


def test_fixture_and_high_consequence_routes_fail_closed_by_policy() -> None:
    records = {record.route_id: record for record in inventory_for_routes(app.routes)}

    for route_id, record in records.items():
        if "/test-fixtures/" in route_id:
            assert record.risk_tier == "high"
            assert "fixture_mode" in record.authority_path
        if "dead-letter" in route_id or "outbox/drain" in route_id:
            assert record.risk_tier == "critical"
        if "/api/agentguard/actions/execute" in route_id:
            assert record.risk_tier == "high"
            assert "approval" in record.agentguard_action
