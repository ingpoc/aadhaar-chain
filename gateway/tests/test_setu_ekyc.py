"""Unit tests for Setu eKYC map + config gate."""
from __future__ import annotations

from pathlib import Path

import pytest

from app import setu_ekyc
from config import settings


def test_setu_ekyc_configured_requires_all_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "setu_ekyc_enabled", True)
    monkeypatch.setattr(settings, "setu_ekyc_client_id", "cid")
    monkeypatch.setattr(settings, "setu_ekyc_client_secret", "secret")
    monkeypatch.setattr(settings, "setu_ekyc_product_instance_id", "prod")
    assert setu_ekyc.setu_ekyc_configured() is True

    monkeypatch.setattr(settings, "setu_ekyc_client_secret", None)
    assert setu_ekyc.setu_ekyc_configured() is False


def test_setu_ekyc_link_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    setu_ekyc.save_ekyc_link(
        setu_id="req-123",
        wallet_address="Wallet111",
        verification_id="aadhaar_Wallet111",
    )
    link = setu_ekyc.load_ekyc_link("req-123")
    assert link == {
        "wallet_address": "Wallet111",
        "verification_id": "aadhaar_Wallet111",
    }
    assert setu_ekyc.load_ekyc_link("missing") is None
