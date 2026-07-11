"""Tests for the Solana identity-registry bridge."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from app.solana_bridge import (
    SolanaBridge,
    get_solana_bridge,
    reset_solana_bridge,
    verification_bit,
)
from config import settings


@pytest.fixture(autouse=True)
def reset_bridge_singleton() -> None:
    reset_solana_bridge()
    yield
    reset_solana_bridge()


def test_verification_bit_mapping() -> None:
    assert verification_bit("aadhaar") == 1
    assert verification_bit("pan") == 2


def test_instruction_discriminators_loaded_from_idl() -> None:
    bridge = SolanaBridge()
    create_disc = bridge._instruction_discriminator("create_identity")
    update_disc = bridge._instruction_discriminator("update_verification_status")
    idl = json.loads((Path(__file__).resolve().parents[1] / "idl" / "identity_registry.json").read_text())
    by_name = {ix["name"]: bytes(ix["discriminator"]) for ix in idl["instructions"]}
    assert create_disc == by_name["create_identity"]
    assert update_disc == by_name["update_verification_status"]


@pytest.mark.asyncio
async def test_build_create_identity_transaction_returns_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = SolanaBridge()
    wallet = Keypair().pubkey()
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.get_latest_blockhash.return_value = MagicMock(
        value=MagicMock(
            blockhash="EkSnNWid2scguJCkytRxgKv6MWCdR6fWf32y77i3YzrS",
        )
    )

    with patch("app.solana_bridge.AsyncClient", return_value=mock_client):
        encoded = await bridge.build_create_identity_transaction(
            str(wallet),
            f"did:solana:{wallet}",
            "aadhaarchain://commitment/test",
        )

    raw = base64.b64decode(encoded)
    assert len(raw) > 0


@pytest.mark.asyncio
async def test_submit_approved_verification_disabled_by_default() -> None:
    bridge = get_solana_bridge()
    assert bridge.is_enabled is False
    assert await bridge.submit_approved_verification("wallet", "aadhaar") is None


@pytest.mark.asyncio
async def test_submit_approved_verification_invokes_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    oracle = Keypair()
    monkeypatch.setattr(settings, "solana_on_chain_enabled", True)
    monkeypatch.setattr(settings, "oracle_private_key", "test")
    reset_solana_bridge()

    bridge = SolanaBridge()
    bridge._oracle_keypair = oracle  # noqa: SLF001

    handler = AsyncMock()
    bridge.set_on_chain_approved_handler(handler)

    with patch.object(
        bridge,
        "update_verification_status",
        AsyncMock(return_value="sig123"),
    ) as update_mock:
        signature = await bridge.submit_approved_verification("wallet123", "aadhaar")

    assert signature == "sig123"
    update_mock.assert_awaited_once_with("wallet123", "aadhaar")
    handler.assert_awaited_once_with("wallet123", "aadhaar", "sig123")
