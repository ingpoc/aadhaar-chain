import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from main import app
from app.routes import agent_manager


def test_create_aadhaar_verification_accepts_multipart_upload(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_create_verification(wallet_address: str, document_type: str, verification_data):
        captured["create_wallet_address"] = wallet_address
        captured["create_document_type"] = document_type
        captured["create_verification_data"] = verification_data
        return f"{document_type}_{wallet_address}"

    async def fake_orchestrate(wallet_address: str, document_type: str, document_data: bytes, verification_data, document_source):
        captured["orchestrate_wallet_address"] = wallet_address
        captured["orchestrate_document_type"] = document_type
        captured["document_data"] = document_data
        captured["verification_data"] = verification_data
        captured["document_source"] = document_source
        return None

    monkeypatch.setattr(agent_manager, "create_verification", fake_create_verification)
    monkeypatch.setattr(agent_manager, "orchestrate_verification", fake_orchestrate)

    client = TestClient(app)
    response = client.post(
        "/api/identity/wallet123/aadhaar",
        data={
            "name": "Alice Example",
            "dob": "1990-01-01",
            "uid": "123456789012",
            "consent_provided": "true",
        },
        files={
            "document": ("aadhaar.pdf", b"%PDF-1.4 test document", "application/pdf"),
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["verification_id"] == "aadhaar_wallet123"

    assert captured["create_wallet_address"] == "wallet123"
    assert captured["create_document_type"] == "aadhaar"
    assert captured["orchestrate_wallet_address"] == "wallet123"
    assert captured["orchestrate_document_type"] == "aadhaar"
    assert captured["document_data"] == b"%PDF-1.4 test document"

    verification_data = captured["verification_data"]
    assert getattr(verification_data, "uid") == "123456789012"
    assert getattr(verification_data, "consent_provided") is True

    document_source = captured["document_source"]
    assert getattr(document_source, "transport") == "upload"
    assert getattr(document_source, "file_name") == "aadhaar.pdf"
    assert getattr(document_source, "content_type") == "application/pdf"
    assert getattr(document_source, "size_bytes") == len(b"%PDF-1.4 test document")
    assert getattr(document_source, "sha256").startswith("sha256:")
