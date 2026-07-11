#!/usr/bin/env python3
"""One-time localnet setup for the identity-registry global config.

Registers the gateway oracle public key as verification_oracle so approved
verifications can call update_verification_status on-chain.

Usage:
  cd aadharchain/gateway
  ORACLE_PRIVATE_KEY=<base58> SOLANA_ON_CHAIN_ENABLED=true \\
    .venv/bin/python scripts/init_identity_registry_config.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import struct
import sys
from pathlib import Path

import base58
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.transaction import Transaction

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402

DEFAULT_IDL = ROOT / "idl" / "identity_registry.json"


def _coerce_blockhash(blockhash: Hash | str) -> Hash:
    if isinstance(blockhash, Hash):
        return blockhash
    return Hash.from_string(str(blockhash))


def _discriminator(idl: dict, name: str) -> bytes:
    for instruction in idl["instructions"]:
        if instruction["name"] == name:
            return bytes(instruction["discriminator"])
    raise KeyError(name)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize identity-registry config on localnet")
    parser.add_argument("--admin-key", help="Base58 admin secret (defaults to ~/.config/solana/id.json)")
    args = parser.parse_args()

    if not settings.oracle_private_key:
        raise SystemExit("ORACLE_PRIVATE_KEY is required in gateway .env")

    oracle = Keypair.from_bytes(base58.b58decode(settings.oracle_private_key))
    if args.admin_key:
        admin = Keypair.from_bytes(base58.b58decode(args.admin_key))
    else:
        wallet_path = Path.home() / ".config" / "solana" / "id.json"
        admin = Keypair.from_bytes(bytes(json.loads(wallet_path.read_text())))

    program_id = Pubkey.from_string(settings.identity_registry_program_id)
    config_pda, _ = Pubkey.find_program_address([b"config"], program_id)
    idl = json.loads(DEFAULT_IDL.read_text())

    data = (
        _discriminator(idl, "initialize_config")
        + bytes(oracle.pubkey())
        + bytes(Pubkey.from_string(settings.credential_manager_program_id))
        + bytes(Pubkey.from_string(settings.reputation_engine_program_id))
        + bytes(Pubkey.from_string(settings.staking_manager_program_id))
    )

    instruction = Instruction(
        program_id=program_id,
        accounts=[
            AccountMeta(config_pda, is_signer=False, is_writable=True),
            AccountMeta(admin.pubkey(), is_signer=True, is_writable=True),
            AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ],
        data=data,
    )

    async with AsyncClient(settings.solana_rpc_url) as client:
        existing = await client.get_account_info(config_pda, commitment=Confirmed)
        if existing.value is not None:
            print(f"Config already initialized at {config_pda}")
            print(f"Oracle pubkey for verification updates: {oracle.pubkey()}")
            return

        blockhash_resp = await client.get_latest_blockhash(Confirmed)
        blockhash = _coerce_blockhash(blockhash_resp.value.blockhash)
        last_valid_block_height = blockhash_resp.value.last_valid_block_height
        message = Message.new_with_blockhash([instruction], admin.pubkey(), blockhash)
        transaction = Transaction.new_unsigned(message)
        transaction.sign([admin], blockhash)
        # Localnet preflight often returns BlockhashNotFound despite a fresh blockhash.
        send_resp = await client.send_transaction(
            transaction,
            opts=TxOpts(skip_preflight=True),
        )
        await client.confirm_transaction(
            send_resp.value,
            commitment=Confirmed,
            last_valid_block_height=last_valid_block_height,
        )
        print(f"Initialized config at {config_pda}")
        print(f"verification_oracle set to {oracle.pubkey()}")
        print(f"Transaction signature: {send_resp.value}")


if __name__ == "__main__":
    asyncio.run(main())
