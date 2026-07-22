from datetime import datetime, timezone

from app.persistence.agentguard_repository import receipt_contract_view


def test_receipt_contract_view_flattens_signed_payload_for_activity_clients() -> None:
    created_at = datetime(2026, 7, 22, 12, 30, tzinfo=timezone.utc)
    view = receipt_contract_view(
        {
            "receipt_id": "receipt-1",
            "principal_id": "principal:auth0:test",
            "agent_id": "agent-1",
            "mandate_id": "mandate-1",
            "mandate_version": 2,
            "decision_id": "decision-1",
            "approval_id": "approval-1",
            "intent_id": "intent-1",
            "status": "executed",
            "created_at": created_at,
            "payload": {
                "receipt_id": "receipt-1",
                "schema_version": "2",
                "action": "buyer.checkout.commit",
                "outcome": "executed",
                "created_at": "2026-07-22T12:30:00+00:00",
                "bound_action": {
                    "action": "buyer.checkout.commit",
                    "resource_id": "order-1",
                    "amount_inr": 190,
                },
            },
        }
    )

    assert view["action"] == "buyer.checkout.commit"
    assert view["resource_id"] == "order-1"
    assert view["amount_inr"] == 190
    assert view["agent_id"] == "agent-1"
    assert view["created_at"] == "2026-07-22T12:30:00+00:00"
