"""Tests for ONDC crypto + BAP adapter (PreProd wiring)."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi.testclient import TestClient

from app.ondc_crypto import (
    create_authorization_header,
    minify_json,
    verify_authorization_header,
)
from config import settings


def test_lbnp_onboarding_uses_dedicated_identity_and_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    signing = Ed25519PrivateKey.generate()
    encryption = X25519PrivateKey.generate()
    for name, key in (
        ("signing_private.pem", signing),
        ("encryption_private.pem", encryption),
    ):
        (tmp_path / name).write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    (tmp_path / "request_id.txt").write_text("lbnp-test-request\n", encoding="utf-8")
    (tmp_path / "public_metadata.json").write_text(
        json.dumps(
            {
                "source": "local",
                "encryption_public_key_format": "asn1_der_spki_b64",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "ondc_lbnp_keys_dir", str(tmp_path))
    monkeypatch.setattr(settings, "ondc_registry_env", "preprod")

    from app import ondc_onboard_routes
    from main import app

    client = TestClient(app)
    status = client.get("/ondc/np/lbnp/status").json()["data"]
    assert status["subscriber_id"] == "ondclbnp.aadharcha.in"
    assert status["callback_url"] == "https://ondclbnp.aadharcha.in/ondc"
    assert status["keys_dir"] == str(tmp_path)
    assert status["contract"] == {
        "domain": "ONDC:LOG10",
        "participant_role": "Logistics Buyer NP / LBNP / BAP",
        "protocol_target": "1.2.5",
        "accepted_response_versions": ["1.2.5", "1.2.0"],
        "response_version_rule": "Accept 1.2.0 only when the selected LSP advertises it",
        "initial_scope": "Immediate Delivery, P2P, forward lifecycle only",
        "counterparty_role": "External LSP / BPP",
        "fleet_owner": "external LSP",
    }
    verification = client.get(
        "/ondc-site-verification.html",
        headers={"host": "ondclbnp.aadharcha.in"},
    )
    assert verification.status_code == 200
    assert 'name="ondc-site-verification"' in verification.text

    ondc_public = serialization.load_der_public_key(
        base64.b64decode(ondc_onboard_routes.ONDC_ENC_PUBLIC_KEYS["preprod"])
    )
    shared = encryption.exchange(ondc_public)
    plain = b"lbnp-challenge"
    pad_len = 16 - (len(plain) % 16)
    encryptor = Cipher(algorithms.AES(shared), modes.ECB()).encryptor()
    challenge = base64.b64encode(
        encryptor.update(plain + bytes([pad_len]) * pad_len) + encryptor.finalize()
    ).decode("ascii")
    callback = client.post(
        "/ondc/on_subscribe",
        headers={"host": "ondclbnp.aadharcha.in"},
        json={"subscriber_id": "ondclbnp.aadharcha.in", "challenge": challenge},
    )
    assert callback.status_code == 200
    assert callback.json() == {"answer": "lbnp-challenge"}


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
    (tmp_path / "unique_key_id.txt").write_text("test-uk-id\n", encoding="utf-8")
    (tmp_path / "encryption_private.pem").write_bytes(pem)  # unused placeholder
    return tmp_path


def test_minify_and_auth_header_stable(ed25519_pem: Path):
    key = serialization.load_pem_private_key(
        (ed25519_pem / "signing_private.pem").read_bytes(), password=None
    )
    body = {"a": 1, "b": [2, 3]}
    header = create_authorization_header(
        body,
        subscriber_id="ondcbuyer.aadharcha.in",
        unique_key_id="test-uk-id",
        private_key=key,
        created=1700000000,
        expires=1700003600,
    )
    assert header.startswith("Signature keyId=")
    assert "ondcbuyer.aadharcha.in|test-uk-id|ed25519" in header
    assert 'algorithm="ed25519"' in header
    assert minify_json(body) == '{"a":1,"b":[2,3]}'
    public_b64 = base64.b64encode(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode("ascii")
    assert verify_authorization_header(
        body,
        header,
        signing_public_key_b64=public_b64,
        expected_subscriber_id="ondcbuyer.aadharcha.in",
        expected_unique_key_id="test-uk-id",
        now=1700000001,
    )
    assert not verify_authorization_header(
        {"hello": "tampered"},
        header,
        signing_public_key_b64=public_b64,
        expected_subscriber_id="ondcbuyer.aadharcha.in",
        expected_unique_key_id="test-uk-id",
        now=1700000001,
    )


def test_ondc_search_dispatches_when_configured(
    tmp_path: Path, ed25519_pem: Path, monkeypatch
):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "ondc_enabled", True)
    monkeypatch.setattr(settings, "ondc_subscriber_id", "ondcbuyer.aadharcha.in")
    monkeypatch.setattr(settings, "ondc_bap_id", "ondcbuyer.aadharcha.in")
    monkeypatch.setattr(settings, "ondc_bap_uri", "https://ondcbuyer.aadharcha.in/ondc")
    monkeypatch.setattr(settings, "ondc_unique_key_id", "test-uk-id")
    monkeypatch.setattr(
        settings,
        "ondc_signing_private_key_path",
        str(ed25519_pem / "signing_private.pem"),
    )
    monkeypatch.setattr(
        settings, "ondc_gateway_url", "https://preprod.gateway.ondc.org/search"
    )
    monkeypatch.setattr(settings, "ondc_buyer_keys_dir", str(ed25519_pem))

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.json = lambda: {"message": {"ack": {"status": "ACK"}}}
    mock_resp.text = '{"message":{"ack":{"status":"ACK"}}}'

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=mock_resp)

    from main import app

    with patch("app.ondc_routes.httpx.AsyncClient", return_value=mock_client):
        client = TestClient(app)
        res = client.post(
            "/api/ondc/search", json={"query": "banana", "city": "std:080"}
        )
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["dispatched"] is True
    assert data["ack"] == "ACK"
    assert data["transaction_id"]
    mock_client.post.assert_awaited()
    call_kwargs = mock_client.post.await_args
    assert "Authorization" in call_kwargs.kwargs["headers"]
    sent = json.loads(call_kwargs.kwargs["content"].decode("utf-8"))
    assert sent["context"]["action"] == "search"
    assert sent["context"]["domain"] == "ONDC:RET10"
    assert sent["message"]["intent"]["item"]["descriptor"]["name"] == "banana"
    assert sent["message"]["intent"]["payment"] == {
        "@ondc/org/buyer_app_finder_fee_type": "Percent",
        "@ondc/org/buyer_app_finder_fee_amount": "0",
    }


def test_ondc_search_can_also_dispatch_to_configured_bpp(
    tmp_path: Path, ed25519_pem: Path, monkeypatch
):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "ondc_enabled", True)
    monkeypatch.setattr(settings, "ondc_subscriber_id", "ondcbuyer.aadharcha.in")
    monkeypatch.setattr(settings, "ondc_bap_id", "ondcbuyer.aadharcha.in")
    monkeypatch.setattr(settings, "ondc_bap_uri", "https://ondcbuyer.aadharcha.in/ondc")
    monkeypatch.setattr(
        settings, "ondc_bpp_uri", "https://ondcseller.aadharcha.in/ondc"
    )
    monkeypatch.setattr(settings, "ondc_unique_key_id", "test-uk-id")
    monkeypatch.setattr(
        settings,
        "ondc_signing_private_key_path",
        str(ed25519_pem / "signing_private.pem"),
    )
    monkeypatch.setattr(
        settings, "ondc_gateway_url", "https://preprod.gateway.ondc.org/search"
    )
    monkeypatch.setattr(settings, "ondc_buyer_keys_dir", str(ed25519_pem))

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.json = lambda: {"message": {"ack": {"status": "ACK"}}}
    mock_resp.text = '{"message":{"ack":{"status":"ACK"}}}'
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=mock_resp)

    from main import app

    with patch("app.ondc_routes.httpx.AsyncClient", return_value=mock_client):
        client = TestClient(app)
        res = client.post(
            "/api/ondc/search",
            json={"query": "atta", "include_configured_bpp": True},
        )

    assert res.status_code == 200
    assert res.json()["data"]["direct_bpp"] == {
        "bpp_uri": "https://ondcseller.aadharcha.in/ondc",
        "http_status": 200,
        "ack": "ACK",
        "ok": True,
    }
    assert [call.args[0] for call in mock_client.post.await_args_list] == [
        "https://preprod.gateway.ondc.org/search",
        "https://ondcseller.aadharcha.in/ondc/search",
    ]


def test_on_search_inbox_and_catalogs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    from main import app

    client = TestClient(app)
    txn = "txn-test-1"
    payload = {
        "context": {
            "action": "on_search",
            "transaction_id": txn,
            "message_id": "msg-1",
            "bpp_id": "seller.example",
            "bpp_uri": "https://seller.example/ondc",
        },
        "message": {
            "catalog": {
                "providers": [
                    {
                        "id": "p1",
                        "descriptor": {"name": "Demo Store"},
                        "items": [
                            {
                                "id": "sku-1",
                                "descriptor": {"name": "Robusta Bananas"},
                                "price": {"currency": "INR", "value": "40"},
                                "delivery_areas": ["Pune", "411001"],
                            }
                        ],
                    }
                ]
            }
        },
    }
    ack = client.post("/ondc/on_search", json=payload)
    assert ack.status_code == 200
    assert ack.json()["message"]["ack"]["status"] == "ACK"
    catalogs = client.get(f"/api/ondc/catalogs?transaction_id={txn}")
    assert catalogs.status_code == 200
    items = catalogs.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["name"] == "Robusta Bananas"
    assert items[0]["bpp_id"] == "seller.example"
    assert items[0]["delivery_areas"] == ["Pune", "411001"]


def test_ondc_status_disabled_by_default(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "ondc_enabled", False)
    from main import app

    client = TestClient(app)
    res = client.get("/api/ondc/status")
    assert res.status_code == 200
    assert res.json()["data"]["enabled"] is False
    assert res.json()["data"]["configured"] is False


def test_ondc_select_init_confirm_dispatch(
    tmp_path: Path, ed25519_pem: Path, monkeypatch
):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "ondc_enabled", True)
    monkeypatch.setattr(settings, "ondc_subscriber_id", "ondcbuyer.aadharcha.in")
    monkeypatch.setattr(settings, "ondc_bap_id", "ondcbuyer.aadharcha.in")
    monkeypatch.setattr(settings, "ondc_bap_uri", "https://ondcbuyer.aadharcha.in/ondc")
    monkeypatch.setattr(settings, "ondc_unique_key_id", "test-uk-id")
    monkeypatch.setattr(
        settings,
        "ondc_signing_private_key_path",
        str(ed25519_pem / "signing_private.pem"),
    )
    monkeypatch.setattr(settings, "ondc_buyer_keys_dir", str(ed25519_pem))

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.json = lambda: {"message": {"ack": {"status": "ACK"}}}
    mock_resp.text = '{"message":{"ack":{"status":"ACK"}}}'
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=mock_resp)

    from main import app

    body = {
        "transaction_id": "txn-order-bap",
        "bpp_id": "ondcseller.aadharcha.in",
        "bpp_uri": "https://ondcseller.aadharcha.in/ondc",
        "order": {"items": [{"id": "item_atta", "quantity": {"count": "1"}}]},
    }
    with patch("app.ondc_routes.httpx.AsyncClient", return_value=mock_client):
        client = TestClient(app)
        for action in ("select", "init", "confirm"):
            res = client.post(f"/api/ondc/{action}", json=body)
            assert res.status_code == 200, res.text
            data = res.json()["data"]
            assert data["dispatched"] is True
            assert data["ack"] == "ACK"
            assert data["bpp_uri"] == "https://ondcseller.aadharcha.in/ondc"

    targets = [c.args[0] for c in mock_client.post.await_args_list]
    assert any(t.endswith("/select") for t in targets)
    assert any(t.endswith("/init") for t in targets)
    assert any(t.endswith("/confirm") for t in targets)
    sent = json.loads(
        mock_client.post.await_args_list[0].kwargs["content"].decode("utf-8")
    )
    assert sent["context"]["bpp_id"] == "ondcseller.aadharcha.in"
    assert "Authorization" in mock_client.post.await_args_list[0].kwargs["headers"]


def test_logistics_routes_use_only_the_dedicated_lbnp_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.ondc_routes import _signing_role_for_envelope

    signing = Ed25519PrivateKey.generate()
    (tmp_path / "signing_private.pem").write_bytes(
        signing.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    (tmp_path / "encryption_private.pem").write_bytes(
        X25519PrivateKey.generate().private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    (tmp_path / "unique_key_id.txt").write_text("lbnp-test-uk\n", encoding="utf-8")
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "ondc_enabled", True)
    monkeypatch.setattr(settings, "ondc_lbnp_keys_dir", str(tmp_path))
    monkeypatch.setattr(settings, "ondc_lbnp_subscriber_id", "ondclbnp.aadharcha.in")

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.json = lambda: {"message": {"ack": {"status": "ACK"}}}
    mock_resp.text = '{"message":{"ack":{"status":"ACK"}}}'
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=mock_resp)

    from main import app

    search = {
        "transaction_id": "txn-log10",
        "message_id": "msg-search",
        "intent": {
            "category": {"id": "Immediate Delivery"},
            "fulfillment": {
                "type": "Delivery",
                "start": {"location": {"gps": "12.4535,77.9283"}},
                "end": {"location": {"gps": "12.9715987,77.5945627"}},
            },
        },
    }
    order_message = {
        "order": {
            "id": "LO1",
            "fulfillments": [{"id": "F1", "type": "Delivery"}],
        }
    }
    with patch("app.ondc_routes.httpx.AsyncClient", return_value=mock_client):
        client = TestClient(app)
        response = client.post("/api/ondc/logistics/search", json=search)
        assert response.status_code == 200, response.text
        for action in ("init", "confirm", "update"):
            response = client.post(
                f"/api/ondc/logistics/{action}",
                json={
                    "transaction_id": "txn-log10",
                    "message_id": f"msg-{action}",
                    "bpp_id": "ondc.bringg.space",
                    "bpp_uri": "https://ondc.bringg.space",
                    "message": order_message,
                },
            )
            assert response.status_code == 200, response.text

    sent = [
        json.loads(call.kwargs["content"].decode("utf-8"))
        for call in mock_client.post.await_args_list
    ]
    assert [payload["context"]["action"] for payload in sent] == [
        "search",
        "init",
        "confirm",
        "update",
    ]
    assert all(payload["context"]["domain"] == "ONDC:LOG10" for payload in sent)
    assert all(payload["context"]["core_version"] == "1.2.5" for payload in sent)
    assert all(_signing_role_for_envelope(payload) == "lbnp" for payload in sent)
    assert all(
        payload["context"]["bap_id"] == "ondclbnp.aadharcha.in" for payload in sent
    )
    assert all(
        "ondclbnp.aadharcha.in|lbnp-test-uk|ed25519"
        in call.kwargs["headers"]["Authorization"]
        for call in mock_client.post.await_args_list
    )
    assert client.post("/api/ondc/logistics/select", json={}).status_code == 404
    bad = {
        **search,
        "intent": {**search["intent"], "category": {"id": "Standard Delivery"}},
    }
    assert client.post("/api/ondc/logistics/search", json=bad).status_code == 422


def test_third_party_lookup_does_not_send_the_callers_key_id(
    ed25519_pem: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "ondc_enabled", True)
    monkeypatch.setattr(
        settings,
        "ondc_signing_private_key_path",
        str(ed25519_pem / "signing_private.pem"),
    )
    monkeypatch.setattr(settings, "ondc_unique_key_id", "buyer-own-key")
    monkeypatch.setattr(settings, "ondc_subscriber_id", "ondcbuyer.aadharcha.in")

    from main import app

    signed_post = AsyncMock(return_value=(200, [], "{}"))
    with patch("app.ondc_routes._signed_post", signed_post):
        response = TestClient(app).post(
            "/api/ondc/lookup",
            json={
                "subscriber_id": "ondc.bringg.space",
                "domain": "ONDC:LOG10",
                "type": "BPP",
            },
        )
    assert response.status_code == 200
    assert "ukId" not in signed_post.await_args.args[1]


def test_logistics_callback_requires_a_registry_matched_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    lbnp_signing = Ed25519PrivateKey.generate()
    provider_signing = Ed25519PrivateKey.generate()
    (tmp_path / "signing_private.pem").write_bytes(
        lbnp_signing.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    (tmp_path / "encryption_private.pem").write_bytes(
        X25519PrivateKey.generate().private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    (tmp_path / "unique_key_id.txt").write_text("lbnp-test-uk\n", encoding="utf-8")
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "ondc_enabled", True)
    monkeypatch.setattr(settings, "ondc_lbnp_keys_dir", str(tmp_path))
    monkeypatch.setattr(settings, "ondc_registry_url", "https://registry.test/lookup")

    provider_public = base64.b64encode(
        provider_signing.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode("ascii")
    registry_response = AsyncMock()
    registry_response.status_code = 200
    registry_response.json = lambda: [
        {
            "subscriber_id": "ondc.bringg.space",
            "domain": "ONDC:LOG10",
            "type": "BPP",
            "status": "SUBSCRIBED",
            "ukId": "provider-uk",
            "signing_public_key": provider_public,
        }
    ]
    registry_response.text = "[]"
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=registry_response)
    callback = {
        "context": {
            "domain": "ONDC:LOG10",
            "action": "on_search",
            "core_version": "1.2.5",
            "bap_id": "ondclbnp.aadharcha.in",
            "bpp_id": "ondc.bringg.space",
            "transaction_id": "txn-log10-callback",
            "message_id": "msg-log10-callback",
        },
        "message": {"catalog": {"bpp/providers": []}},
    }
    authorization = create_authorization_header(
        callback,
        subscriber_id="ondc.bringg.space",
        unique_key_id="provider-uk",
        private_key=provider_signing,
    )

    from main import app

    with patch("app.ondc_routes.httpx.AsyncClient", return_value=mock_client):
        client = TestClient(app)
        good = client.post(
            "/ondc/on_search",
            json=callback,
            headers={"Authorization": authorization},
        )
        assert good.status_code == 200
        assert good.json()["message"]["ack"]["status"] == "ACK"
        bad = client.post(
            "/ondc/on_search",
            json={
                **callback,
                "context": {**callback["context"], "message_id": "tampered"},
            },
            headers={"Authorization": authorization},
        )
        assert bad.status_code == 401
        assert bad.json()["message"]["ack"]["status"] == "NACK"
