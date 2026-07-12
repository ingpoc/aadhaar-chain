"""Unit tests for durable Google OAuth state."""
from __future__ import annotations

import time

from app.oauth_state import is_allowed_return_url, mint_oauth_state, parse_oauth_state


def test_mint_and_parse_oauth_state_roundtrip():
    state = mint_oauth_state(return_url="http://127.0.0.1:43102/search", aud="ondcbuyer")
    meta = parse_oauth_state(state)
    assert meta["return_url"] == "http://127.0.0.1:43102/search"
    assert meta["aud"] == "ondcbuyer"
    assert meta["exp"] > time.time()


def test_oauth_state_rejects_tamper():
    state = mint_oauth_state(return_url="http://127.0.0.1:43102/cart", aud="ondcbuyer")
    bad = state[:-4] + ("abcd" if not state.endswith("abcd") else "efgh")
    try:
        parse_oauth_state(bad)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_allowed_return_url_localhost_portfolio():
    assert is_allowed_return_url("http://127.0.0.1:43102/search")
    assert is_allowed_return_url("http://localhost:43102/search")
    assert is_allowed_return_url("http://127.0.0.1:43103/dashboard")
    assert is_allowed_return_url("http://localhost:43103/dashboard")
    assert not is_allowed_return_url("https://evil.example/phish")


def test_allowed_return_url_deployed_fqdns():
    assert is_allowed_return_url("https://ondcbuyer.aadharcha.in/")
    assert is_allowed_return_url("https://ondcbuyer.aadharcha.in/search")
    assert is_allowed_return_url("https://ondcseller.aadharcha.in/dashboard")
    assert not is_allowed_return_url("https://ondcbuyer.aadharchain.in/")
