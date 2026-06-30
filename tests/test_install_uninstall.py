"""Tests for install / uninstall / install-status round-trips."""

import json
from pathlib import Path

import pytest

from jcodemunch_mcp.cli.init import (
    _AGENT_ALIASES,
    _CLAUDE_MD_MARKER,
    _CLAUDE_MD_POLICY,
    _strip_jcm_hooks,
    _strip_policy_blocks,
    install_claude_md,
    install_cursor_rules,
    install_hooks,
    install_status,
    install_windsurf_rules,
    print_status,
    run_uninstall,
    uninstall_agents_md,
    uninstall_claude_md,
    uninstall_cursor_rules,
    uninstall_hooks,
    uninstall_windsurf_rules,
)


# ---------------------------------------------------------------------------
# _strip_policy_blocks
# ---------------------------------------------------------------------------

def test_strip_policy_blocks_no_marker():
    text = "# my notes\n\nNothing here.\n"
    new_text, changed = _strip_policy_blocks(text)
    assert not changed
    assert new_text == text


def test_strip_policy_blocks_only_policy():
    new_text, changed = _strip_policy_blocks(_CLAUDE_MD_POLICY)
    assert changed
    assert _CLAUDE_MD_MARKER not in new_text
    assert new_text.strip() == ""


def test_strip_policy_blocks_preserves_user_content_before():
    user_pre = "# Project notes\n\nSome custom guidance for the team.\n\n"
    combined = user_pre + _CLAUDE_MD_POLICY
    new_text, changed = _strip_policy_blocks(combined)
    assert changed
    assert "Project notes" in new_text
    assert "Some custom guidance" in new_text
    assert _CLAUDE_MD_MARKER not in new_text


def test_strip_policy_blocks_preserves_user_content_after():
    user_post = "\n\n## My Other Section\n\nCustom guidance.\n"
    combined = _CLAUDE_MD_POLICY + user_post
    new_text, changed = _strip_policy_blocks(combined)
    assert changed
    assert _CLAUDE_MD_MARKER not in new_text
    assert "## My Other Section" in new_text
    assert "Custom guidance" in new_text


def test_strip_policy_blocks_preserves_before_and_after():
    user_pre = "# Project notes\n\nPre-content.\n\n"
    user_post = "\n\n## My Other Section\n\nPost-content.\n"
    combined = user_pre + _CLAUDE_MD_POLICY + user_post
    new_text, changed = _strip_policy_blocks(combined)
    assert changed
    assert "Pre-content" in new_text
    assert "Post-content" in new_text
    assert _CLAUDE_MD_MARKER not in new_text


# ---------------------------------------------------------------------------
# _strip_jcm_hooks
# ---------------------------------------------------------------------------

def test_strip_jcm_hooks_removes_only_jcm_rules():
    data = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Read", "hooks": [{"type": "command", "command": "jcodemunch-mcp hook-pretooluse"}]},
                {"matcher": "Read", "hooks": [{"type": "command", "command": "/usr/local/bin/other-tool"}]},
            ],
            "PostToolUse": [
                {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "jcodemunch-mcp hook-posttooluse"}]},
            ],
        },
        "otherKey": "preserve me",
    }
    touched = _strip_jcm_hooks(data)
    assert "PreToolUse" in touched
    assert "PostToolUse" in touched
    # User's other-tool rule is preserved
    assert len(data["hooks"]["PreToolUse"]) == 1
    assert "other-tool" in data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    # PostToolUse had only jcm; event was removed entirely
    assert "PostToolUse" not in data["hooks"]
    # Unrelated top-level keys preserved
    assert data["otherKey"] == "preserve me"


def test_strip_jcm_hooks_empty_input():
    assert _strip_jcm_hooks({}) == []
    assert _strip_jcm_hooks({"hooks": {}}) == []


def test_strip_jcm_hooks_no_jcm():
    data = {"hooks": {"PreToolUse": [
        {"hooks": [{"type": "command", "command": "other-tool"}]},
    ]}}
    assert _strip_jcm_hooks(data) == []
    # Untouched
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "other-tool"


def test_strip_jcm_hooks_prunes_empty_hooks_dict():
    data = {"hooks": {"PreToolUse": [
        {"hooks": [{"type": "command", "command": "jcodemunch-mcp x"}]},
    ]}}
    _strip_jcm_hooks(data)
    assert "hooks" not in data


# ---------------------------------------------------------------------------
# CLAUDE.md install -> uninstall round-trip
# ---------------------------------------------------------------------------

def test_claude_md_round_trip(tmp_path, monkeypatch):
    target = tmp_path / "CLAUDE.md"
    monkeypatch.setattr("jcodemunch_mcp.cli.init._claude_md_path", lambda scope: target)

    install_claude_md("global", backup=False)
    assert target.exists()
    assert _CLAUDE_MD_MARKER in target.read_text(encoding="utf-8")

    uninstall_claude_md("global", backup=False)
    # File becomes empty -> removed entirely (we created it)
    assert not target.exists()


def test_claude_md_round_trip_preserves_pre_existing_content(tmp_path, monkeypatch):
    target = tmp_path / "CLAUDE.md"
    target.write_text("# My Project\n\nUser content.\n", encoding="utf-8")
    monkeypatch.setattr("jcodemunch_mcp.cli.init._claude_md_path", lambda scope: target)

    install_claude_md("global", backup=False)
    assert _CLAUDE_MD_MARKER in target.read_text(encoding="utf-8")

    uninstall_claude_md("global", backup=False)
    assert target.exists()
    surviving = target.read_text(encoding="utf-8")
    assert "# My Project" in surviving
    assert "User content" in surviving
    assert _CLAUDE_MD_MARKER not in surviving


def test_uninstall_claude_md_no_file(tmp_path, monkeypatch):
    target = tmp_path / "missing.md"
    monkeypatch.setattr("jcodemunch_mcp.cli.init._claude_md_path", lambda scope: target)
    msg = uninstall_claude_md("global", backup=False)
    assert "no file" in msg


def test_uninstall_claude_md_marker_absent(tmp_path, monkeypatch):
    target = tmp_path / "CLAUDE.md"
    target.write_text("# unrelated\n", encoding="utf-8")
    monkeypatch.setattr("jcodemunch_mcp.cli.init._claude_md_path", lambda scope: target)
    msg = uninstall_claude_md("global", backup=False)
    assert "not present" in msg
    assert target.read_text(encoding="utf-8") == "# unrelated\n"


# ---------------------------------------------------------------------------
# Cursor rules round-trip
# ---------------------------------------------------------------------------

def test_cursor_rules_round_trip(tmp_path, monkeypatch):
    target = tmp_path / ".cursor" / "rules" / "jcodemunch.mdc"
    monkeypatch.setattr("jcodemunch_mcp.cli.init._cursor_rules_path", lambda: target)
    install_cursor_rules(backup=False)
    assert target.exists()
    uninstall_cursor_rules(backup=False)
    assert not target.exists()


# ---------------------------------------------------------------------------
# Windsurf rules round-trip
# ---------------------------------------------------------------------------

def test_windsurf_rules_round_trip(tmp_path, monkeypatch):
    target = tmp_path / ".windsurfrules"
    monkeypatch.setattr("jcodemunch_mcp.cli.init._windsurf_rules_path", lambda: target)
    install_windsurf_rules(backup=False)
    assert target.exists()
    assert _CLAUDE_MD_MARKER in target.read_text(encoding="utf-8")
    uninstall_windsurf_rules(backup=False)
    # We created it -> removed entirely when empty after strip
    assert not target.exists()


def test_windsurf_rules_preserves_pre_existing(tmp_path, monkeypatch):
    target = tmp_path / ".windsurfrules"
    target.write_text("Custom team guidance.\nMore content.\n", encoding="utf-8")
    monkeypatch.setattr("jcodemunch_mcp.cli.init._windsurf_rules_path", lambda: target)
    install_windsurf_rules(backup=False)
    uninstall_windsurf_rules(backup=False)
    assert target.exists()
    surviving = target.read_text(encoding="utf-8")
    assert "Custom team guidance" in surviving
    assert _CLAUDE_MD_MARKER not in surviving


# ---------------------------------------------------------------------------
# Hooks round-trip
# ---------------------------------------------------------------------------

def test_hooks_round_trip(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setattr("jcodemunch_mcp.cli.init._settings_json_path", lambda: settings)
    install_hooks(backup=False)
    assert settings.exists()
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert "hooks" in data
    uninstall_hooks(backup=False)
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert "hooks" not in data


def test_uninstall_hooks_preserves_user_rules(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": "user-tool --do-thing"}]},
            ],
        },
        "model": "claude-sonnet",
    }), encoding="utf-8")
    monkeypatch.setattr("jcodemunch_mcp.cli.init._settings_json_path", lambda: settings)
    install_hooks(backup=False)
    uninstall_hooks(backup=False)
    data = json.loads(settings.read_text(encoding="utf-8"))
    # User's rule survived
    pre = data.get("hooks", {}).get("PreToolUse", [])
    assert any(
        "user-tool" in h.get("command", "")
        for r in pre
        for h in r.get("hooks", [])
    )
    # Top-level user settings untouched
    assert data.get("model") == "claude-sonnet"


# ---------------------------------------------------------------------------
# install_status / print_status
# ---------------------------------------------------------------------------

def test_install_status_shape():
    report = install_status()
    assert "clients" in report
    assert "policies" in report
    assert "hooks" in report
    assert isinstance(report["clients"], list)
    assert "claude_settings" in report["hooks"]
    assert "copilot" in report["hooks"]


def test_install_status_recognizes_custom_claude_launcher(monkeypatch):
    """Claude Code detection is launcher-agnostic and resolves the CLI via
    which() — a custom `jmunch-mcp --config jcodemunch.toml` registration in
    `claude mcp list` counts as configured (regression: bare ["claude"] raised
    FileNotFoundError on Windows and silently false-negatived)."""
    from jcodemunch_mcp.cli import init

    monkeypatch.setattr(init.shutil, "which", lambda name: "C:/fake/claude.CMD" if name == "claude" else None)

    class _Result:
        returncode = 0
        stdout = "jcodemunch: jmunch-mcp --config x/jcodemunch.toml - Connected\n"
        stderr = ""

    monkeypatch.setattr(init.subprocess, "run", lambda *a, **k: _Result())
    report = install_status()
    cc = next(c for c in report["clients"] if c["name"] == "Claude Code")
    assert cc["configured"] is True


def test_install_status_graceful_when_claude_cli_absent(monkeypatch):
    """No claude CLI -> no false positive. Claude Code is either dropped from
    the client list or listed as not-configured; never configured=True."""
    from jcodemunch_mcp.cli import init

    monkeypatch.setattr(init.shutil, "which", lambda name: None)

    class _Result:
        returncode = 0
        stdout = "jcodemunch: anything - Connected\n"
        stderr = ""

    monkeypatch.setattr(init.subprocess, "run", lambda *a, **k: _Result())
    report = install_status()
    cc = next((c for c in report["clients"] if c["name"] == "Claude Code"), None)
    assert cc is None or cc["configured"] is False


def test_print_status_runs_without_error(capsys):
    print_status({"clients": [], "policies": {}, "hooks": {
        "claude_settings": {"path": "/tmp/x", "events_with_jcm_rules": []},
        "copilot": {"path": "/tmp/y", "present": False},
    }})
    out = capsys.readouterr().out
    assert "install status" in out.lower()


def test_print_status_json(capsys):
    print_status({"clients": [], "policies": {}, "hooks": {
        "claude_settings": {"path": "/tmp/x", "events_with_jcm_rules": ["PreToolUse"]},
        "copilot": {"path": "/tmp/y", "present": True},
    }}, as_json=True)
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["hooks"]["copilot"]["present"] is True


# ---------------------------------------------------------------------------
# Agent aliases
# ---------------------------------------------------------------------------

def test_agent_aliases_complete():
    expected = {"claude-code", "claude-desktop", "cursor", "windsurf", "continue", "antigravity", "all"}
    assert set(_AGENT_ALIASES) == expected


# ---------------------------------------------------------------------------
# run_uninstall — end-to-end on an isolated tmp_path
# ---------------------------------------------------------------------------

def test_run_uninstall_dry_run_makes_no_changes(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setattr("jcodemunch_mcp.cli.init._settings_json_path", lambda: settings)
    monkeypatch.setattr("jcodemunch_mcp.cli.init._claude_md_path", lambda scope: tmp_path / f"CLAUDE-{scope}.md")
    monkeypatch.setattr("jcodemunch_mcp.cli.init._cursor_rules_path", lambda: tmp_path / "cursor.mdc")
    monkeypatch.setattr("jcodemunch_mcp.cli.init._windsurf_rules_path", lambda: tmp_path / ".windsurfrules")
    monkeypatch.setattr("jcodemunch_mcp.cli.init._detect_clients", lambda: [])
    monkeypatch.chdir(tmp_path)

    install_hooks(backup=False)
    install_claude_md("global", backup=False)
    snapshot_settings = settings.read_text(encoding="utf-8")
    snapshot_md = (tmp_path / "CLAUDE-global.md").read_text(encoding="utf-8")

    exit_code = run_uninstall(dry_run=True, no_backup=True, yes=True)
    assert exit_code == 0
    assert settings.read_text(encoding="utf-8") == snapshot_settings
    assert (tmp_path / "CLAUDE-global.md").read_text(encoding="utf-8") == snapshot_md
