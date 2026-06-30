"""Tests for Antigravity (Google) integration — P1: MCP registration + skill bundle.

Antigravity follows the Gemini CLI contract: MCP servers in
``~/.gemini/settings.json`` (registered via the shared json_patch client path),
agent skills under ``~/.gemini/antigravity/skills/``. End-to-end confirmation
that the live agent consumes the config requires a real Antigravity install and
is out of scope for unit tests (see docs/prd-antigravity-hooks.md).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jcodemunch_mcp.cli import antigravity as ag
from jcodemunch_mcp.cli import init as initmod
from jcodemunch_mcp.cli import skills as skillsmod


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Point Path.home() at a tmp dir and silence Claude-Code CLI detection so
    _detect_clients is deterministic and file-based."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(initmod, "_find_executable", lambda name: None)
    return tmp_path


# --- paths / presence ------------------------------------------------------

def test_paths(fake_home):
    assert ag.gemini_dir() == fake_home / ".gemini"
    assert ag.gemini_settings_path() == fake_home / ".gemini" / "settings.json"
    assert ag.antigravity_skills_dir() == fake_home / ".gemini" / "antigravity" / "skills"


def test_present_detection(fake_home):
    assert ag.antigravity_present() is False
    (fake_home / ".gemini").mkdir()
    assert ag.antigravity_present() is True


# --- skill bundle ----------------------------------------------------------

def test_install_skill_writes_marker(fake_home):
    (fake_home / ".gemini").mkdir()
    msg = ag.install_antigravity_skill()
    p = fake_home / ".gemini" / "antigravity" / "skills" / "jcodemunch" / "SKILL.md"
    assert p.exists()
    assert "wrote" in msg
    assert skillsmod._SKILL_MARKER in p.read_text(encoding="utf-8")


def test_install_skill_idempotent(fake_home):
    (fake_home / ".gemini").mkdir()
    ag.install_antigravity_skill()
    assert "already present" in ag.install_antigravity_skill()


def test_install_skill_dry_run(fake_home):
    msg = ag.install_antigravity_skill(dry_run=True)
    assert "would write" in msg
    assert not (fake_home / ".gemini" / "antigravity").exists()


def test_status_reflects_presence(fake_home):
    assert ag.antigravity_skill_status()["present"] is False
    (fake_home / ".gemini").mkdir()
    ag.install_antigravity_skill()
    st = ag.antigravity_skill_status()
    assert st["present"] is True
    assert st["path"].endswith("SKILL.md")


def test_uninstall_removes_and_cleans(fake_home):
    (fake_home / ".gemini").mkdir()
    ag.install_antigravity_skill()
    msg = ag.uninstall_antigravity_skill()
    assert "removed" in msg
    assert not (fake_home / ".gemini" / "antigravity" / "skills" / "jcodemunch").exists()


def test_uninstall_preserves_foreign_skill(fake_home):
    d = fake_home / ".gemini" / "antigravity" / "skills" / "jcodemunch"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("# someone else's skill", encoding="utf-8")
    msg = ag.uninstall_antigravity_skill()
    assert "not a jcodemunch skill" in msg
    assert (d / "SKILL.md").exists()


# --- MCP registration via the shared client path ---------------------------

def test_detect_includes_antigravity_when_present(fake_home):
    (fake_home / ".gemini").mkdir()
    clients = initmod._detect_clients()
    matches = [c for c in clients if c.name == "Antigravity"]
    assert len(matches) == 1
    c = matches[0]
    assert c.method == "json_patch"
    assert c.config_path == fake_home / ".gemini" / "settings.json"


def test_detect_excludes_antigravity_when_absent(fake_home):
    assert not any(c.name == "Antigravity" for c in initmod._detect_clients())


def test_mcp_registration_patches_gemini_settings(fake_home):
    (fake_home / ".gemini").mkdir()
    client = next(c for c in initmod._detect_clients() if c.name == "Antigravity")
    msg = initmod.configure_client(client)
    data = json.loads((fake_home / ".gemini" / "settings.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["jcodemunch"] == initmod._MCP_ENTRY
    assert "added" in msg


def test_antigravity_alias_registered():
    assert initmod._AGENT_ALIASES.get("antigravity") == "Antigravity"


def test_install_status_reports_antigravity_skill(fake_home):
    (fake_home / ".gemini").mkdir()
    report = initmod.install_status()
    assert "antigravity" in report["skills"]
    assert report["skills"]["antigravity"]["present"] is False
    # And the Antigravity client shows up unconfigured before registration.
    assert any(c["name"] == "Antigravity" and not c["configured"] for c in report["clients"])
