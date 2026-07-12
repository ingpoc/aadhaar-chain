"""Session cookie flags for local vs cross-site (Render gateway) deploy."""
from __future__ import annotations

from app import session_auth


def test_local_demo_cookie_is_lax_host_only(monkeypatch):
    monkeypatch.setattr(session_auth.settings, "public_gateway_url", "http://127.0.0.1:43101")
    monkeypatch.setattr(session_auth, "get_runtime_mode", lambda: "demo")
    assert session_auth.cookie_secure_flag() is False
    assert session_auth.cookie_samesite() == "lax"
    assert session_auth.cookie_domain() is None


def test_render_production_cookie_is_none_secure_host_only(monkeypatch):
    monkeypatch.setattr(
        session_auth.settings,
        "public_gateway_url",
        "https://identity-aadhar-gateway-main.onrender.com",
    )
    monkeypatch.setattr(session_auth, "get_runtime_mode", lambda: "production")
    assert session_auth.cookie_secure_flag() is True
    assert session_auth.cookie_samesite() == "none"
    # onrender.com must not claim Domain=.aadharcha.in
    assert session_auth.cookie_domain() is None


def test_aadharcha_gateway_host_sets_parent_domain(monkeypatch):
    monkeypatch.setattr(
        session_auth.settings,
        "public_gateway_url",
        "https://gateway.aadharcha.in",
    )
    monkeypatch.setattr(session_auth, "get_runtime_mode", lambda: "production")
    assert session_auth.cookie_domain() == ".aadharcha.in"
