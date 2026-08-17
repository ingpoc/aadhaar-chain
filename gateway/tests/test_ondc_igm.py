"""Signed Retail IGM request, callback correlation, and failure recovery."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app.ondc_crypto import create_authorization_header
from config import settings


@pytest.fixture()
def ed25519_pem(tmp_path: Path) -> Path:
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "signing_private.pem"
    path.write_bytes(pem)
    (tmp_path / "unique_key_id.txt").write_text("seller-uk\n", encoding="utf-8")
    return tmp_path


def _enable_retail_igm(
    tmp_path: Path,
    ed25519_pem: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "ondc_enabled", True)
    monkeypatch.setattr(settings, "ondc_subscriber_id", "ondcbuyer.aadharcha.in")
    monkeypatch.setattr(settings, "ondc_bap_id", "ondcbuyer.aadharcha.in")
    monkeypatch.setattr(settings, "ondc_bap_uri", "https://ondcbuyer.aadharcha.in/ondc")
    monkeypatch.setattr(settings, "ondc_unique_key_id", "buyer-uk")
    monkeypatch.setattr(
        settings,
        "ondc_signing_private_key_path",
        str(ed25519_pem / "signing_private.pem"),
    )
    monkeypatch.setattr(settings, "ondc_buyer_keys_dir", str(ed25519_pem))
    monkeypatch.setattr(settings, "ondc_seller_keys_dir", str(ed25519_pem))
    monkeypatch.setattr(settings, "ondc_bpp_id", "ondcseller.aadharcha.in")
    monkeypatch.setattr(
        settings, "ondc_bpp_uri", "https://ondcseller.aadharcha.in/ondc"
    )
    monkeypatch.setattr(settings, "ondc_seller_unique_key_id", "seller-uk")
    monkeypatch.setattr(
        settings,
        "ondc_seller_signing_private_key_path",
        str(ed25519_pem / "signing_private.pem"),
    )


def _create_local_issue(client: TestClient) -> dict[str, str]:
    item = client.post(
        "/api/demo-commerce/test-fixtures/seller/items",
        json={"title": "IGM item", "price_inr": 50, "inventory": 1},
    ).json()["data"]["item"]
    client.post(f"/api/demo-commerce/test-fixtures/seller/items/{item['item_id']}/publish")
    order = client.post(
        "/api/demo-commerce/test-fixtures/buyer/orders",
        json={"item_id": item["item_id"], "quantity": 1, "buyer_id": "igm-buyer"},
    ).json()["data"]["order"]
    issue = client.post(
        f"/api/demo-commerce/test-fixtures/buyer/orders/{order['order_id']}/issues",
        json={"reason": "fulfillment", "description": "Package stalled"},
    ).json()["data"]["issue"]
    return {
        "order_id": order["order_id"],
        "issue_id": issue["issue_id"],
    }


def _seller_key(ed25519_pem: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(
        (ed25519_pem / "signing_private.pem").read_bytes(),
        password=None,
    )
    assert isinstance(key, Ed25519PrivateKey)
    return key


def _on_issue_envelope(*, issue_id: str, transaction_id: str, message_id: str) -> dict:
    return {
        "context": {
            "domain": "ONDC:RET10",
            "action": "on_issue",
            "core_version": "1.2.0",
            "bap_id": "ondcbuyer.aadharcha.in",
            "bap_uri": "https://ondcbuyer.aadharcha.in/ondc",
            "bpp_id": "ondcseller.aadharcha.in",
            "bpp_uri": "https://ondcseller.aadharcha.in/ondc",
            "transaction_id": transaction_id,
            "message_id": message_id,
            "timestamp": "2026-08-17T09:00:00.000Z",
            "ttl": "PT30S",
        },
        "message": {
            "issue": {
                "id": issue_id,
                "status": "PROCESSING",
                "issue_type": "ISSUE",
            }
        },
    }


def test_signed_igm_issue_request_uses_retail_scope_and_authorization(
    tmp_path: Path, ed25519_pem: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_retail_igm(tmp_path, ed25519_pem, monkeypatch)
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.json = lambda: {"message": {"ack": {"status": "ACK"}}}
    mock_resp.text = '{"message":{"ack":{"status":"ACK"}}}'
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=mock_resp)

    from main import app

    app.state.persistence_pool = None
    with patch("app.ondc_routes.httpx.AsyncClient", return_value=mock_client):
        client = TestClient(app)
        created = _create_local_issue(client)
        response = client.post(
            "/api/ondc/issue",
            json={
                "issue_id": created["issue_id"],
                "bpp_id": "ondcseller.aadharcha.in",
                "bpp_uri": "https://ondcseller.aadharcha.in/ondc",
                "transaction_id": "txn-igm-sign",
                "message_id": "msg-igm-sign",
            },
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["dispatched"] is True
    assert data["ack"] == "ACK"
    assert data["issue_id"] == created["issue_id"]
    assert data["target"] == "https://ondcseller.aadharcha.in/ondc/issue"
    mock_client.post.assert_awaited()
    call = mock_client.post.await_args
    assert call.args[0].endswith("/issue")
    assert "Authorization" in call.kwargs["headers"]
    sent = json.loads(call.kwargs["content"].decode("utf-8"))
    assert sent["context"]["action"] == "issue"
    assert sent["context"]["domain"] == "ONDC:RET10"
    assert sent["context"]["core_version"] == "1.2.0"
    assert sent["message"]["issue"]["id"] == created["issue_id"]
    assert sent["message"]["issue"]["category"] == "FULFILLMENT"
    assert sent["message"]["issue"]["issue_type"] == "ISSUE"

    issues = client.get(
        "/api/demo-commerce/test-fixtures/buyer/issues"
    ).json()["data"]["issues"]
    correlated = next(row for row in issues if row["issue_id"] == created["issue_id"])
    assert correlated["igm_transaction_id"] == "txn-igm-sign"
    assert any(
        (event.get("network") or {}).get("transaction_id") == "txn-igm-sign"
        for event in correlated["history"]
    )


def test_igm_rejects_non_retail_domain_and_unknown_issue(
    tmp_path: Path, ed25519_pem: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_retail_igm(tmp_path, ed25519_pem, monkeypatch)
    from main import app

    app.state.persistence_pool = None
    client = TestClient(app)
    created = _create_local_issue(client)
    unknown = client.post(
        "/api/ondc/issue",
        json={"issue_id": "issue_missing", "bpp_uri": "https://ondcseller.aadharcha.in/ondc"},
    )
    logistics = client.post(
        "/api/ondc/issue",
        json={
            "issue_id": created["issue_id"],
            "domain": "ONDC:LOG10",
            "bpp_uri": "https://ondcseller.aadharcha.in/ondc",
        },
    )
    assert unknown.status_code == 404
    assert logistics.status_code == 422
    assert "ONDC:RET10" in logistics.json()["detail"]


def test_on_issue_callback_correlates_and_rejects_invalid_signatures(
    tmp_path: Path, ed25519_pem: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_retail_igm(tmp_path, ed25519_pem, monkeypatch)
    from main import app

    app.state.persistence_pool = None
    client = TestClient(app)
    created = _create_local_issue(client)
    envelope = _on_issue_envelope(
        issue_id=created["issue_id"],
        transaction_id="txn-igm-callback",
        message_id="msg-igm-callback",
    )
    authorization = create_authorization_header(
        envelope,
        subscriber_id="ondcseller.aadharcha.in",
        unique_key_id="seller-uk",
        private_key=_seller_key(ed25519_pem),
    )

    good = client.post(
        "/ondc/on_issue",
        json=envelope,
        headers={"Authorization": authorization},
    )
    assert good.status_code == 200
    assert good.json()["message"]["ack"]["status"] == "ACK"

    issues = client.get(
        "/api/demo-commerce/test-fixtures/buyer/issues"
    ).json()["data"]["issues"]
    correlated = next(row for row in issues if row["issue_id"] == created["issue_id"])
    network_events = [
        event for event in correlated["history"] if event.get("network")
    ]
    assert network_events
    assert network_events[-1]["network"]["action"] == "on_issue"
    assert network_events[-1]["network"]["transaction_id"] == "txn-igm-callback"
    assert network_events[-1]["network"]["signature_verified"] is True
    assert correlated["status"] == "acknowledged"
    assert correlated["igm_transaction_id"] == "txn-igm-callback"

    replay = client.post(
        "/ondc/on_issue",
        json=envelope,
        headers={"Authorization": authorization},
    )
    assert replay.status_code == 200
    assert replay.json()["message"]["ack"]["status"] == "ACK"

    tampered = client.post(
        "/ondc/on_issue",
        json={
            **envelope,
            "context": {**envelope["context"], "message_id": "tampered"},
        },
        headers={"Authorization": authorization},
    )
    assert tampered.status_code == 401
    assert tampered.json()["message"]["ack"]["status"] == "NACK"

    unsigned = client.post("/ondc/on_issue", json=envelope)
    assert unsigned.status_code == 401
    assert "missing ONDC Authorization" in unsigned.json()["error"]["message"]

    wrong_domain = client.post(
        "/ondc/on_issue",
        json={
            **envelope,
            "context": {
                **envelope["context"],
                "domain": "ONDC:LOG10",
                "message_id": "wrong-domain",
            },
        },
        headers={"Authorization": authorization},
    )
    assert wrong_domain.status_code == 401
    assert "ONDC:RET10" in wrong_domain.json()["error"]["message"]


def test_igm_dispatch_failure_stays_retryable(
    tmp_path: Path, ed25519_pem: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_retail_igm(tmp_path, ed25519_pem, monkeypatch)
    from main import app

    app.state.persistence_pool = None
    with patch(
        "app.ondc_routes._signed_post",
        AsyncMock(side_effect=RuntimeError("network unavailable")),
    ):
        client = TestClient(app)
        created = _create_local_issue(client)
        response = client.post(
            "/api/ondc/issue",
            json={
                "issue_id": created["issue_id"],
                "bpp_id": "ondcseller.aadharcha.in",
                "bpp_uri": "https://ondcseller.aadharcha.in/ondc",
                "transaction_id": "txn-igm-retry",
                "message_id": "msg-igm-retry",
            },
        )
    assert response.status_code == 502
    outbox = client.get("/api/ondc/outbox").json()["data"]["items"]
    failed = next(row for row in outbox if row.get("transaction_id") == "txn-igm-retry")
    assert failed["status"] == "error"
    assert "network unavailable" in str(failed.get("error") or "")

    issues = client.get(
        "/api/demo-commerce/test-fixtures/buyer/issues"
    ).json()["data"]["issues"]
    correlated = next(row for row in issues if row["issue_id"] == created["issue_id"])
    assert any(
        "dispatch failed" in str(event.get("note") or "")
        for event in correlated["history"]
    )


def test_seller_bpp_issue_acks_and_posts_signed_on_issue(
    tmp_path: Path, ed25519_pem: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_retail_igm(tmp_path, ed25519_pem, monkeypatch)
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.json = lambda: {"message": {"ack": {"status": "ACK"}}}
    mock_resp.text = '{"message":{"ack":{"status":"ACK"}}}'
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=mock_resp)

    from main import app

    app.state.persistence_pool = None
    inbound = _on_issue_envelope(
        issue_id="issue_from_buyer",
        transaction_id="txn-bpp-igm",
        message_id="msg-bpp-igm",
    )
    inbound["context"]["action"] = "issue"
    inbound["message"]["issue"]["status"] = "OPEN"

    with patch("app.ondc_bpp.httpx.AsyncClient", return_value=mock_client):
        client = TestClient(app)
        ack = client.post("/ondc/issue", json=inbound)

    assert ack.status_code == 200
    assert ack.json()["message"]["ack"]["status"] == "ACK"
    mock_client.post.assert_awaited()
    call = mock_client.post.await_args
    assert call.args[0].endswith("/on_issue")
    assert "Authorization" in call.kwargs["headers"]
    sent = json.loads(call.kwargs["content"].decode("utf-8"))
    assert sent["context"]["action"] == "on_issue"
    assert sent["context"]["domain"] == "ONDC:RET10"
    assert sent["context"]["message_id"] == "msg-bpp-igm"
    assert sent["message"]["issue"]["id"] == "issue_from_buyer"
    assert sent["message"]["issue"]["status"] == "PROCESSING"


def test_igm_dispatch_binds_confirmed_order_without_local_issue(
    tmp_path: Path, ed25519_pem: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_retail_igm(tmp_path, ed25519_pem, monkeypatch)
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.json = lambda: {"message": {"ack": {"status": "ACK"}}}
    mock_resp.text = '{"message":{"ack":{"status":"ACK"}}}'
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=mock_resp)

    from main import app

    app.state.persistence_pool = None
    order_id = "B5f876d453"
    transaction_id = "f876d453-6c33-4297-952e-7ee54ed50551"
    with patch("app.ondc_routes.httpx.AsyncClient", return_value=mock_client):
        client = TestClient(app)
        confirm = client.post(
            "/api/ondc/confirm",
            json={
                "order": {"id": order_id},
                "bpp_id": "ondcseller.aadharcha.in",
                "bpp_uri": "https://ondcseller.aadharcha.in/ondc",
                "transaction_id": transaction_id,
                "message_id": "msg-confirm-igm-bind",
            },
        )
        assert confirm.status_code == 200, confirm.text
        unknown_order = client.post(
            "/api/ondc/issue",
            json={
                "order_id": "B5missing",
                "bpp_uri": "https://ondcseller.aadharcha.in/ondc",
                "transaction_id": "txn-unknown-order",
            },
        )
        unknown_issue = client.post(
            "/api/ondc/issue",
            json={
                "issue_id": "issue_missing",
                "bpp_uri": "https://ondcseller.aadharcha.in/ondc",
            },
        )
        created = client.post(
            "/api/ondc/issue",
            json={
                "issue_id": order_id,
                "order_id": order_id,
                "bpp_id": "ondcseller.aadharcha.in",
                "bpp_uri": "https://ondcseller.aadharcha.in/ondc",
                "transaction_id": transaction_id,
                "message_id": "msg-igm-bind",
            },
        )
        assert created.status_code == 200, created.text
        bound_id = created.json()["data"]["issue_id"]
        correlated = client.post(
            "/api/ondc/issue",
            json={
                "issue_id": bound_id,
                "bpp_id": "ondcseller.aadharcha.in",
                "bpp_uri": "https://ondcseller.aadharcha.in/ondc",
                "transaction_id": transaction_id,
                "message_id": "msg-igm-bind-existing",
            },
        )

    assert unknown_order.status_code == 404
    assert unknown_order.json()["detail"] == "Unknown order"
    assert unknown_issue.status_code == 404
    assert unknown_issue.json()["detail"] == "Unknown issue"
    assert bound_id != order_id
    sent = json.loads(mock_client.post.await_args_list[-2].kwargs["content"].decode("utf-8"))
    assert sent["message"]["issue"]["id"] == bound_id
    assert sent["message"]["issue"]["order_details"]["id"] == order_id
    assert correlated.status_code == 200, correlated.text
    assert correlated.json()["data"]["issue_id"] == bound_id
    issues = client.get("/api/demo-commerce/test-fixtures/buyer/issues").json()["data"]["issues"]
    matching = [row for row in issues if row["issue_id"] == bound_id]
    assert len(matching) == 1
    assert matching[0]["order_id"] == order_id
