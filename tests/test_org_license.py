"""Tests for the org-rollup license gate (team SKU).

Scope check: this gate covers ONLY org-rollup. The validation backend is mocked
via ``_check_server`` so tests never touch the network.
"""

from __future__ import annotations

import time

import pytest

from jcodemunch_mcp.org import license as lic


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # All state under a temp CODE_INDEX_PATH; no real env key bleed-through.
    monkeypatch.setenv("CODE_INDEX_PATH", str(tmp_path))
    monkeypatch.delenv("JCODEMUNCH_LICENSE_KEY", raising=False)
    # Config fallback returns empty unless a test sets the env key.
    monkeypatch.setattr(lic, "_license_key", lambda: __import__("os").environ.get("JCODEMUNCH_LICENSE_KEY", "").strip())
    yield


def _server(monkeypatch, answer):
    monkeypatch.setattr(lic, "_check_server", lambda key: answer)


def test_mask_key():
    assert lic.mask_key("ABCD1234EFGH") == "ABCD…EFGH"
    assert lic.mask_key("short") == "*****"
    assert lic.mask_key("") == ""


@pytest.mark.parametrize("tier", ["studio", "platform"])
def test_multiseat_tier_is_licensed(monkeypatch, tier):
    monkeypatch.setenv("JCODEMUNCH_LICENSE_KEY", "VALIDKEY0001")
    _server(monkeypatch, {"valid": True, "tier": tier, "error": None})
    gate = lic.check_gate()
    assert gate["allowed"] is True
    assert gate["mode"] == "licensed"
    assert gate["tier"] == tier
    assert gate["key_masked"] == "VALI…0001"


def test_builder_tier_does_not_unlock(monkeypatch):
    # A valid single-seat Builder license is NOT entitled to org-rollup.
    monkeypatch.setenv("JCODEMUNCH_LICENSE_KEY", "BUILDERKEY01")
    _server(monkeypatch, {"valid": True, "tier": "builder", "error": None})
    # Within grace: allowed, but flagged as a tier-upgrade case (real tier shown).
    gate = lic.check_gate()
    assert gate["allowed"] is True and gate["mode"] == "grace"
    assert "upgrade" in gate["reason"].lower()
    assert gate["tier"] == "builder"
    # After grace: blocked, with a Studio/Platform upsell (not a generic "get a license").
    state = lic._load_state()
    state["grace_started_at"] = time.time() - (lic.GRACE_SECONDS + 10)
    lic._save_state(state)
    gate2 = lic.check_gate()
    assert gate2["allowed"] is False and gate2["mode"] == "blocked"
    assert "studio or platform" in gate2["reason"].lower()
    assert gate2["tier"] == "builder"


def test_no_key_starts_grace(monkeypatch):
    _server(monkeypatch, None)  # never consulted (no key)
    gate = lic.check_gate()
    assert gate["allowed"] is True
    assert gate["mode"] == "grace"
    assert 1 <= gate["grace_days_left"] <= 14
    assert gate["get_license"]


def test_grace_expires_then_blocks(monkeypatch):
    # First call starts the clock; rewind it past the window.
    lic.check_gate()
    state = lic._load_state()
    state["grace_started_at"] = time.time() - (lic.GRACE_SECONDS + 10)
    lic._save_state(state)
    gate = lic.check_gate()
    assert gate["allowed"] is False
    assert gate["mode"] == "blocked"
    assert gate["grace_days_left"] == 0
    assert "license" in gate["reason"].lower()


def test_invalid_key_in_grace_allows_but_after_grace_blocks(monkeypatch):
    monkeypatch.setenv("JCODEMUNCH_LICENSE_KEY", "BADKEY000001")
    _server(monkeypatch, {"valid": False, "tier": None, "error": "not found"})
    # Within grace: allowed, but the reason surfaces the key error.
    gate = lic.check_gate()
    assert gate["allowed"] is True and gate["mode"] == "grace"
    assert "not found" in gate["reason"]
    # Age out the grace clock → blocked.
    state = lic._load_state()
    state["grace_started_at"] = time.time() - (lic.GRACE_SECONDS + 10)
    lic._save_state(state)
    gate2 = lic.check_gate()
    assert gate2["allowed"] is False and gate2["mode"] == "blocked"


def test_sticky_offline_keeps_confirmed_key(monkeypatch):
    key = "STICKYKEY001"
    monkeypatch.setenv("JCODEMUNCH_LICENSE_KEY", key)
    # First check confirms valid and caches it.
    _server(monkeypatch, {"valid": True, "tier": "studio", "error": None})
    assert lic.check_gate()["mode"] == "licensed"
    # Force a re-check window, then make the server unreachable.
    state = lic._load_state()
    state["checked_at"] = time.time() - (lic.RECHECK_SECONDS + 10)
    lic._save_state(state)
    _server(monkeypatch, None)  # unreachable
    gate = lic.check_gate()
    assert gate["allowed"] is True and gate["mode"] == "licensed"  # sticky


def test_revocation_blocks_even_with_old_grace(monkeypatch):
    key = "REVOKED00001"
    monkeypatch.setenv("JCODEMUNCH_LICENSE_KEY", key)
    # Was valid (Studio) and cached.
    _server(monkeypatch, {"valid": True, "tier": "studio", "error": None})
    lic.check_gate()
    # Server now explicitly revokes; force a recheck.
    state = lic._load_state()
    state["checked_at"] = time.time() - (lic.RECHECK_SECONDS + 10)
    # An existing customer's grace clock is long past.
    state["grace_started_at"] = time.time() - (lic.GRACE_SECONDS + 10)
    lic._save_state(state)
    _server(monkeypatch, {"valid": False, "tier": None, "error": "revoked"})
    gate = lic.check_gate()
    assert gate["allowed"] is False and gate["mode"] == "blocked"
    assert "revoked" in gate["reason"]
