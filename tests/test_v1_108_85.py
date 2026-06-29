"""Tests for v1.108.85 — install-mechanism-aware `watch` hint + migration de-spam (#357).

Follow-up to #357 (reported by @zakblacki on a pipx install):
  1. The "watchfiles is required" hint suggested a bare `pip install
     'jcodemunch-mcp[watch]'`, which fails on pipx/uv installs — same blind spot
     fixed for `upgrade`. `watch_extra_install_command()` now matches the
     install mechanism.
  2. A stale/corrupt JSON index (e.g. `.pack/<id>`) re-warned on every
     load/eager-migrate path — 8x in a single `watch-all`. The migration
     schema-validation warning now fires at most once per (owner, name).
"""

from __future__ import annotations

import json
from unittest import mock

from jcodemunch_mcp.cli import upgrade as up
from jcodemunch_mcp import watcher


# ---------------------------------------------------------------------------
# watch_extra_install_command — install-mechanism aware
# ---------------------------------------------------------------------------


class TestWatchExtraInstallCommand:
    def test_pipx_uses_inject(self):
        with mock.patch.object(up, "detect_install_mechanism", return_value=("pipx", "x")):
            assert up.watch_extra_install_command() == "pipx inject jcodemunch-mcp watchfiles"

    def test_uv_tool_reinstalls_with_extra(self):
        with mock.patch.object(up, "detect_install_mechanism", return_value=("uv", "x")):
            assert up.watch_extra_install_command() == (
                "uv tool install --force 'jcodemunch-mcp[watch]'"
            )

    def test_uvx_uses_with(self):
        with mock.patch.object(up, "detect_install_mechanism", return_value=("uvx", "x")):
            assert up.watch_extra_install_command() == (
                "uvx --with watchfiles jcodemunch-mcp <command>"
            )

    def test_pip_falls_back_to_extra(self):
        with mock.patch.object(up, "detect_install_mechanism", return_value=("pip", None)):
            assert up.watch_extra_install_command() == "pip install 'jcodemunch-mcp[watch]'"

    def test_venv_falls_back_to_extra(self):
        with mock.patch.object(up, "detect_install_mechanism", return_value=("venv", None)):
            assert up.watch_extra_install_command() == "pip install 'jcodemunch-mcp[watch]'"


# ---------------------------------------------------------------------------
# watcher._watchfiles_missing_msg — uses the mechanism-aware hint, safely
# ---------------------------------------------------------------------------


class TestWatchfilesMissingMsg:
    def test_embeds_mechanism_command(self):
        with mock.patch.object(up, "detect_install_mechanism", return_value=("pipx", "x")):
            msg = watcher._watchfiles_missing_msg()
        assert "watchfiles is required" in msg
        assert "pipx inject jcodemunch-mcp watchfiles" in msg

    def test_falls_back_when_helper_unavailable(self):
        with mock.patch.object(
            up, "watch_extra_install_command", side_effect=RuntimeError("boom")
        ):
            msg = watcher._watchfiles_missing_msg()
        # Never raises; degrades to the canonical pip form.
        assert "pip install 'jcodemunch-mcp[watch]'" in msg


# ---------------------------------------------------------------------------
# Migration schema-validation warning is de-duplicated per (owner, name)
# ---------------------------------------------------------------------------


class TestMigrationWarningDedup:
    def _write_bad_json(self, tmp_path):
        # Missing the required `indexed_at` field → fails schema validation.
        p = tmp_path / "bad.json"
        p.write_text(json.dumps({"repo": "local/.pack-nodejs"}), encoding="utf-8")
        return p

    def test_warns_once_across_repeated_migrations(self, tmp_path, caplog):
        from jcodemunch_mcp.storage import sqlite_store as ss

        ss._MIGRATION_SCHEMA_WARNED.discard((".pack", "nodejs"))
        store = ss.SQLiteIndexStore(base_path=str(tmp_path))
        bad = self._write_bad_json(tmp_path)

        with caplog.at_level("WARNING", logger="jcodemunch_mcp.storage.sqlite_store"):
            r1 = store.migrate_from_json(bad, ".pack", "nodejs")
            r2 = store.migrate_from_json(bad, ".pack", "nodejs")
            r3 = store.migrate_from_json(bad, ".pack", "nodejs")

        assert r1 is None and r2 is None and r3 is None
        hits = [
            rec for rec in caplog.records
            if "Migration schema validation failed" in rec.getMessage()
        ]
        assert len(hits) == 1, f"expected one warning, got {len(hits)}"
        # The single warning is actionable.
        assert "delete-index" in hits[0].getMessage()

    def test_distinct_repos_each_warn_once(self, tmp_path, caplog):
        from jcodemunch_mcp.storage import sqlite_store as ss

        ss._MIGRATION_SCHEMA_WARNED.discard((".pack", "nodejs"))
        ss._MIGRATION_SCHEMA_WARNED.discard((".pack", "python"))
        store = ss.SQLiteIndexStore(base_path=str(tmp_path))
        bad = self._write_bad_json(tmp_path)

        with caplog.at_level("WARNING", logger="jcodemunch_mcp.storage.sqlite_store"):
            store.migrate_from_json(bad, ".pack", "nodejs")
            store.migrate_from_json(bad, ".pack", "python")

        hits = [
            rec for rec in caplog.records
            if "Migration schema validation failed" in rec.getMessage()
        ]
        assert len(hits) == 2
