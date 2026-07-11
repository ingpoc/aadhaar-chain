"""Solana identity-registry bridge for the AadhaarChain gateway.

Instruction discriminators are loaded from the vendored Anchor IDL JSON so they
stay aligned with deployed programs (Anchor 0.31+ IDL is not yet supported by
anchorpy's dynamic client). RPC uses solana-py; transaction primitives use solders
per the solders / solana-py documentation split.
"""
from __future__ import annotations

import base64
import json
import logging
import struct
from pathlib import Path
from typing import Any, Callable, Optional

import base58
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts
from solana.rpc.commitment import Confirmed
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.transaction import Transaction

from config import settings

logger = logging.getLogger(__name__)

VERIFICATION_TYPE_BY_DOCUMENT: dict[str, int] = {
    "aadhaar": 0,
    "pan": 1,
}

DEFAULT_PROGRAM_ID = "DPW1Ji3XhNb4zAnL9SLq5ZBjmG7ePPegWuocY5VeJLdm"
DEFAULT_IDL_PATH = Path(__file__).resolve().parents[1] / "idl" / "identity_registry.json"

OnChainApprovedHandler = Callable[[str, str, Optional[str]], Any]


def verification_bit(document_type: str) -> int:
    """Return the local/off-chain bitmap bit for a document type."""
    if document_type not in VERIFICATION_TYPE_BY_DOCUMENT:
        raise ValueError(f"Unsupported document type for on-chain bitmap: {document_type}")
    return 1 << VERIFICATION_TYPE_BY_DOCUMENT[document_type]


def _coerce_blockhash(blockhash: Hash | str) -> Hash:
    if isinstance(blockhash, Hash):
        return blockhash
    return Hash.from_string(blockhash)


def _encode_anchor_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack("<I", len(encoded)) + encoded


def _encode_pubkey_vec(keys: list[Pubkey]) -> bytes:
    payload = struct.pack("<I", len(keys))
    for key in keys:
        payload += bytes(key)
    return payload


class SolanaBridge:
    """Gateway bridge to the identity-registry Anchor program."""

    def __init__(self) -> None:
        self._idl = self._load_idl()
        self._program_id = Pubkey.from_string(
            settings.identity_registry_program_id or DEFAULT_PROGRAM_ID
        )
        self._rpc_url = settings.solana_rpc_url
        self._commitment = Confirmed
        self._oracle_keypair = self._load_oracle_keypair()
        self._on_chain_approved: Optional[OnChainApprovedHandler] = None

    @property
    def is_enabled(self) -> bool:
        return bool(settings.solana_on_chain_enabled and self._oracle_keypair is not None)

    @property
    def oracle_public_key(self) -> Optional[Pubkey]:
        if self._oracle_keypair is None:
            return None
        return self._oracle_keypair.pubkey()

    def set_on_chain_approved_handler(self, handler: Optional[OnChainApprovedHandler]) -> None:
        self._on_chain_approved = handler

    def _load_idl(self) -> dict[str, Any]:
        idl_path = Path(settings.solana_idl_path) if settings.solana_idl_path else DEFAULT_IDL_PATH
        if not idl_path.is_file():
            raise FileNotFoundError(f"Identity registry IDL not found at {idl_path}")
        return json.loads(idl_path.read_text())

    def _instruction_discriminator(self, name: str) -> bytes:
        for instruction in self._idl.get("instructions", []):
            if instruction.get("name") == name:
                return bytes(instruction["discriminator"])
        raise KeyError(f"Instruction {name!r} not found in identity registry IDL")

    def _load_oracle_keypair(self) -> Optional[Keypair]:
        secret = settings.oracle_private_key
        if not secret:
            return None
        try:
            raw = base58.b58decode(secret)
            return Keypair.from_bytes(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load oracle keypair: %s", exc)
            return None

    def _identity_pda(self, authority: Pubkey) -> Pubkey:
        pda, _ = Pubkey.find_program_address([b"identity", bytes(authority)], self._program_id)
        return pda

    def _config_pda(self) -> Pubkey:
        pda, _ = Pubkey.find_program_address([b"config"], self._program_id)
        return pda

    async def config_account_exists(self) -> bool:
        async with AsyncClient(self._rpc_url) as client:
            response = await client.get_account_info(self._config_pda(), commitment=self._commitment)
            return response.value is not None

    async def identity_account_exists(self, wallet_address: str) -> bool:
        authority = Pubkey.from_string(wallet_address)
        async with AsyncClient(self._rpc_url) as client:
            response = await client.get_account_info(
                self._identity_pda(authority),
                commitment=self._commitment,
            )
            return response.value is not None

    async def build_create_identity_transaction(
        self,
        wallet_address: str,
        did: str,
        metadata_uri: str,
    ) -> str:
        """Return a base64-encoded unsigned legacy transaction for wallet signing."""
        authority = Pubkey.from_string(wallet_address)
        identity_pda = self._identity_pda(authority)
        data = (
            self._instruction_discriminator("create_identity")
            + _encode_anchor_string(did)
            + _encode_anchor_string(metadata_uri)
            + _encode_pubkey_vec([])
        )
        instruction = Instruction(
            program_id=self._program_id,
            accounts=[
                AccountMeta(identity_pda, is_signer=False, is_writable=True),
                AccountMeta(authority, is_signer=True, is_writable=True),
                AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
            ],
            data=data,
        )
        async with AsyncClient(self._rpc_url) as client:
            blockhash_response = await client.get_latest_blockhash(self._commitment)
            blockhash = _coerce_blockhash(blockhash_response.value.blockhash)
            message = Message.new_with_blockhash([instruction], authority, blockhash)
            transaction = Transaction.new_unsigned(message)
            return base64.b64encode(bytes(transaction)).decode("ascii")

    async def update_verification_status(self, wallet_address: str, document_type: str) -> str:
        """Oracle-signed on-chain verification bitmap update. Returns transaction signature."""
        if not self.is_enabled:
            raise RuntimeError("Solana on-chain bridge is disabled or oracle keypair is missing")
        if document_type not in VERIFICATION_TYPE_BY_DOCUMENT:
            raise ValueError(f"Unsupported document type: {document_type}")

        authority = Pubkey.from_string(wallet_address)
        verification_type = VERIFICATION_TYPE_BY_DOCUMENT[document_type]
        data = (
            self._instruction_discriminator("update_verification_status")
            + struct.pack("<B", verification_type)
            + struct.pack("<B", 1)
        )
        oracle_pubkey = self._oracle_keypair.pubkey()  # type: ignore[union-attr]
        instruction = Instruction(
            program_id=self._program_id,
            accounts=[
                AccountMeta(self._identity_pda(authority), is_signer=False, is_writable=True),
                AccountMeta(oracle_pubkey, is_signer=True, is_writable=False),
                AccountMeta(self._config_pda(), is_signer=False, is_writable=False),
            ],
            data=data,
        )

        async with AsyncClient(self._rpc_url) as client:
            blockhash_response = await client.get_latest_blockhash(self._commitment)
            blockhash = _coerce_blockhash(blockhash_response.value.blockhash)
            last_valid_block_height = blockhash_response.value.last_valid_block_height
            message = Message.new_with_blockhash([instruction], oracle_pubkey, blockhash)
            transaction = Transaction.new_unsigned(message)
            transaction.sign([self._oracle_keypair], blockhash)  # type: ignore[list-item]
            send_response = await client.send_transaction(
                transaction,
                opts=TxOpts(skip_preflight=True),
            )
            signature = send_response.value
            await client.confirm_transaction(
                signature,
                commitment=self._commitment,
                last_valid_block_height=last_valid_block_height,
            )
            return str(signature)

    async def submit_approved_verification(
        self,
        wallet_address: str,
        document_type: str,
    ) -> Optional[str]:
        """Submit on-chain verification update and notify the approved handler."""
        if not self.is_enabled:
            return None
        signature = await self.update_verification_status(wallet_address, document_type)
        if self._on_chain_approved is not None:
            result = self._on_chain_approved(wallet_address, document_type, signature)
            if hasattr(result, "__await__"):
                await result
        return signature


_bridge: Optional[SolanaBridge] = None


def get_solana_bridge() -> SolanaBridge:
    global _bridge
    if _bridge is None:
        _bridge = SolanaBridge()
    return _bridge


def reset_solana_bridge() -> None:
    """Test helper to rebuild the singleton."""
    global _bridge
    _bridge = None
