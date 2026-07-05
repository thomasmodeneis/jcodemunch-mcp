"""v1.108.0 — explicit-paths indexing (Change A) + workspace-scoped project
intel (Change C).

Change A adds `paths=[...]` to `index_folder` plus a `--paths-from FILE | -`
CLI flag on `jcodemunch-mcp index` so an agent can index exactly the files
git just touched without paying the cost of a full tree walk.

Change C adds a `scope_path` kwarg to `get_project_intel` and a new
`list_workspaces` tool that enumerates monorepo members from
pnpm/yarn/npm/turborepo/lerna/rush/Go/Cargo manifests.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest


# --------------------------------------------------------------------------- #
# Change A — explicit-paths indexing                                          #
# --------------------------------------------------------------------------- #

class TestExplicitPaths:
    def test_only_listed_files_indexed(self, tmp_path: Path):
        (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
        (tmp_path / "b.py").write_text("def beta():\n    return 2\n")
        (tmp_path / "c.py").write_text("def gamma():\n    return 3\n")

        from jcodemunch_mcp.tools.index_folder import index_folder
        result = index_folder(
            path=str(tmp_path),
            paths=["a.py", "b.py"],
            use_ai_summaries=False,
            incremental=False,
        )
        assert result.get("success") is True, result
        # The fast-path response uses 'symbol_count'; full-path uses 'symbol_count'
        # via the index. Either way, gamma should NOT appear.
        # Hit the repo index and confirm.
        from jcodemunch_mcp.storage import IndexStore
        store = IndexStore()
        owner, repo_name = result["repo"].split("/", 1)
        idx = store.load_index(owner, repo_name)
        assert idx is not None
        sym_names = {(s.name if hasattr(s, "name") else s["name"]) for s in idx.symbols}
        assert "alpha" in sym_names
        assert "beta" in sym_names
        assert "gamma" not in sym_names

    def test_directory_in_paths_recurses(self, tmp_path: Path):
        sub = tmp_path / "pkg"
        sub.mkdir()
        (sub / "x.py").write_text("def x():\n    return 1\n")
        (sub / "y.py").write_text("def y():\n    return 2\n")
        (tmp_path / "outside.py").write_text("def outside():\n    return 3\n")

        from jcodemunch_mcp.tools.index_folder import index_folder
        result = index_folder(
            path=str(tmp_path),
            paths=["pkg"],
            use_ai_summaries=False,
            incremental=False,
        )
        assert result.get("success") is True, result
        from jcodemunch_mcp.storage import IndexStore
        store = IndexStore()
        owner, repo_name = result["repo"].split("/", 1)
        idx = store.load_index(owner, repo_name)
        sym_names = {(s.name if hasattr(s, "name") else s["name"]) for s in idx.symbols}
        assert "x" in sym_names
        assert "y" in sym_names
        assert "outside" not in sym_names

    def test_outside_root_rejected(self, tmp_path: Path):
        (tmp_path / "a.py").write_text("def a():\n    return 1\n")
        elsewhere = tmp_path.parent  # ancestor — definitely outside

        from jcodemunch_mcp.tools.index_folder import index_folder
        result = index_folder(
            path=str(tmp_path),
            paths=[str(elsewhere / "evil.py")],
            use_ai_summaries=False,
            incremental=False,
        )
        # Either success with warnings, or graceful no-source-files error.
        warnings = result.get("warnings") or []
        assert any("outside" in str(w).lower() or "non-existent" in str(w).lower() for w in warnings) \
            or result.get("error", "").startswith("No source files")

    def test_unsupported_extension_skipped(self, tmp_path: Path):
        (tmp_path / "ok.py").write_text("def ok():\n    return 1\n")
        (tmp_path / "junk.bin").write_bytes(b"\x00\x01")

        from jcodemunch_mcp.tools.index_folder import index_folder
        result = index_folder(
            path=str(tmp_path),
            paths=["ok.py", "junk.bin"],
            use_ai_summaries=False,
            incremental=False,
        )
        assert result.get("success") is True
        warnings = result.get("warnings") or []
        assert any("junk.bin" in str(w) and "unsupported" in str(w).lower() for w in warnings)

    def test_secret_file_in_paths_rejected(self, tmp_path: Path):
        """An explicitly-listed credential file must be refused, matching the
        full walk. Regression for the paths=[...] secret-filter bypass — the
        explicit branch checked only symlink/extension/size, so a caller naming
        a .env / secrets/*.yaml / credentials.json indexed it and it was then
        served unredacted by the source-dump tools.
        """
        from jcodemunch_mcp.tools.index_folder import (
            resolve_explicit_paths,
            discover_local_files,
        )
        from jcodemunch_mcp.security import is_secret_file

        (tmp_path / "config" / "secrets").mkdir(parents=True)
        (tmp_path / "config" / "secrets" / "database.yaml").write_text(
            "aws_secret_access_key: AKIAIOSFODNN7EXAMPLE\npassword: hunter2\n"
        )
        (tmp_path / "credentials.json").write_text(
            '{"aws_secret_access_key": "AKIAIOSFODNN7EXAMPLE"}\n'
        )
        (tmp_path / "app.py").write_text("def app():\n    return 1\n")

        # Sanity: the classifier flags both.
        assert is_secret_file("config/secrets/database.yaml")
        assert is_secret_file("credentials.json")

        files, warnings, skip_counts, _req = resolve_explicit_paths(
            tmp_path,
            ["config/secrets/database.yaml", "credentials.json", "app.py"],
            max_files=100,
        )
        names = sorted(f.name for f in files)
        assert names == ["app.py"], names
        assert skip_counts.get("secret") == 2, skip_counts
        assert any("secret" in w.lower() for w in warnings), warnings

        # Parity with the full walk: it refuses the same two files.
        walk_files, _ww, walk_skip = discover_local_files(tmp_path, max_files=100)
        assert sorted(f.name for f in walk_files) == ["app.py"]
        assert walk_skip.get("secret") == 2, walk_skip

    def test_secret_symbols_never_reach_index_via_paths(self, tmp_path: Path):
        """End-to-end: index_folder(paths=[secret]) must not persist the
        credential file's contents into the index."""
        (tmp_path / "ok.py").write_text("def ok():\n    return 1\n")
        (tmp_path / ".env").write_text("AWS_SECRET_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE\n")

        from jcodemunch_mcp.tools.index_folder import index_folder
        result = index_folder(
            path=str(tmp_path),
            paths=["ok.py", ".env"],
            use_ai_summaries=False,
            incremental=False,
        )
        assert result.get("success") is True, result
        from jcodemunch_mcp.storage import IndexStore
        store = IndexStore()
        owner, repo_name = result["repo"].split("/", 1)
        idx = store.load_index(owner, repo_name)
        indexed_files = {
            (s.file if hasattr(s, "file") else s.get("file")) for s in idx.symbols
        }
        assert not any(str(f).endswith(".env") for f in indexed_files if f), indexed_files

    def test_paths_omitted_does_full_walk(self, tmp_path: Path):
        (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
        (tmp_path / "b.py").write_text("def beta():\n    return 2\n")

        from jcodemunch_mcp.tools.index_folder import index_folder
        result = index_folder(
            path=str(tmp_path),
            use_ai_summaries=False,
            incremental=False,
        )
        assert result.get("success") is True
        from jcodemunch_mcp.storage import IndexStore
        store = IndexStore()
        owner, repo_name = result["repo"].split("/", 1)
        idx = store.load_index(owner, repo_name)
        sym_names = {(s.name if hasattr(s, "name") else s["name"]) for s in idx.symbols}
        # Both got indexed via the full walk
        assert "alpha" in sym_names
        assert "beta" in sym_names


class TestLoadIndexPathsFromArg:
    """Unit-test the CLI's --paths-from file/stdin reader helper."""

    def test_reads_file_strips_blanks_and_comments(self, tmp_path: Path):
        from jcodemunch_mcp.server import _load_index_paths_from_arg
        f = tmp_path / "p.txt"
        f.write_text("a.py\n\n# comment\n  b.py  \nsubdir/c.py\n", encoding="utf-8")
        paths, err = _load_index_paths_from_arg(str(f))
        assert err is None
        assert paths == ["a.py", "b.py", "subdir/c.py"]

    def test_reads_stdin(self, monkeypatch):
        from jcodemunch_mcp.server import _load_index_paths_from_arg
        monkeypatch.setattr("sys.stdin", io.StringIO("x.py\ny.py\n"))
        paths, err = _load_index_paths_from_arg("-")
        assert err is None
        assert paths == ["x.py", "y.py"]

    def test_empty_returns_error(self, tmp_path: Path):
        from jcodemunch_mcp.server import _load_index_paths_from_arg
        f = tmp_path / "empty.txt"
        f.write_text("\n# nothing useful\n", encoding="utf-8")
        paths, err = _load_index_paths_from_arg(str(f))
        assert paths is None
        assert err is not None
        assert "no usable paths" in err.lower()

    def test_missing_file_returns_error(self, tmp_path: Path):
        from jcodemunch_mcp.server import _load_index_paths_from_arg
        paths, err = _load_index_paths_from_arg(str(tmp_path / "missing.txt"))
        assert paths is None
        assert "cannot read" in (err or "").lower()


# --------------------------------------------------------------------------- #
# Change C — list_workspaces + get_project_intel(scope_path=)                 #
# --------------------------------------------------------------------------- #

@pytest.fixture
def pnpm_monorepo(tmp_path: Path) -> Path:
    """A small pnpm-style monorepo: packages/api + packages/web."""
    (tmp_path / "pnpm-workspace.yaml").write_text(
        "packages:\n  - 'packages/*'\n",
        encoding="utf-8",
    )
    pkg_api = tmp_path / "packages" / "api"
    pkg_api.mkdir(parents=True)
    (pkg_api / "package.json").write_text(json.dumps({"name": "@acme/api"}))
    (pkg_api / "Dockerfile").write_text("FROM node:20\nEXPOSE 3000\n")
    (pkg_api / "index.js").write_text("function main(){return 1}\n")

    pkg_web = tmp_path / "packages" / "web"
    pkg_web.mkdir(parents=True)
    (pkg_web / "package.json").write_text(json.dumps({"name": "@acme/web"}))
    (pkg_web / "index.js").write_text("function root(){return 2}\n")

    return tmp_path


@pytest.fixture
def pnpm_indexed(pnpm_monorepo: Path):
    from jcodemunch_mcp.tools.index_folder import index_folder
    result = index_folder(
        path=str(pnpm_monorepo),
        use_ai_summaries=False,
        incremental=False,
    )
    assert result.get("success") is True, result
    return result["repo"]


class TestListWorkspaces:
    def test_pnpm_detected(self, pnpm_indexed):
        from jcodemunch_mcp.tools.list_workspaces import list_workspaces
        out = list_workspaces(repo=pnpm_indexed)
        assert "error" not in out, out
        ws = out["result"]["workspaces"]
        paths = {w["path"] for w in ws}
        assert "packages/api" in paths
        assert "packages/web" in paths
        names = {w["package_name"] for w in ws}
        assert "@acme/api" in names
        assert "@acme/web" in names
        assert "pnpm" in out["result"]["managers"]
        assert out["result"]["is_monorepo"] is True

    def test_non_monorepo_returns_empty_list(self, tmp_path: Path):
        # Index a flat folder
        (tmp_path / "a.py").write_text("def a():\n    return 1\n")
        from jcodemunch_mcp.tools.index_folder import index_folder
        idx_res = index_folder(
            path=str(tmp_path), use_ai_summaries=False, incremental=False,
        )
        assert idx_res.get("success") is True

        from jcodemunch_mcp.tools.list_workspaces import list_workspaces
        out = list_workspaces(repo=idx_res["repo"])
        assert "error" not in out, out
        assert out["result"]["workspaces"] == []
        assert out["result"]["is_monorepo"] is False

    def test_cargo_workspace(self, tmp_path: Path):
        (tmp_path / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crates/foo", "crates/bar"]\n',
            encoding="utf-8",
        )
        foo = tmp_path / "crates" / "foo"
        foo.mkdir(parents=True)
        (foo / "Cargo.toml").write_text('[package]\nname = "foo"\nversion = "0.1.0"\n')
        (foo / "src").mkdir()
        (foo / "src" / "lib.rs").write_text("pub fn hello() {}\n")

        bar = tmp_path / "crates" / "bar"
        bar.mkdir(parents=True)
        (bar / "Cargo.toml").write_text('[package]\nname = "bar"\nversion = "0.1.0"\n')
        (bar / "src").mkdir()
        (bar / "src" / "lib.rs").write_text("pub fn there() {}\n")

        from jcodemunch_mcp.tools.index_folder import index_folder
        idx_res = index_folder(path=str(tmp_path), use_ai_summaries=False, incremental=False)
        assert idx_res.get("success") is True

        from jcodemunch_mcp.tools.list_workspaces import list_workspaces
        out = list_workspaces(repo=idx_res["repo"])
        ws = out["result"]["workspaces"]
        paths = {w["path"] for w in ws}
        names = {w["package_name"] for w in ws}
        assert "crates/foo" in paths
        assert "crates/bar" in paths
        assert "foo" in names
        assert "bar" in names
        assert "cargo" in out["result"]["managers"]


class TestScopedProjectIntel:
    def test_scope_path_restricts_to_subtree(self, pnpm_indexed):
        from jcodemunch_mcp.tools.get_project_intel import get_project_intel
        # Repo-wide: should find the per-package Dockerfile too
        full = get_project_intel(repo=pnpm_indexed, category="infra")
        assert "error" not in full
        # Scoped: should still find the Dockerfile (it's under packages/api)
        scoped = get_project_intel(
            repo=pnpm_indexed, category="infra", scope_path="packages/api",
        )
        assert "error" not in scoped, scoped
        assert scoped.get("scope_path") == "packages/api"
        # The scoped query found at least one Dockerfile
        api_infra = scoped["categories"].get("infra", {})
        assert len(api_infra.get("dockerfiles") or []) >= 1

    def test_scope_path_excludes_other_packages(self, pnpm_indexed):
        from jcodemunch_mcp.tools.get_project_intel import get_project_intel
        scoped_web = get_project_intel(
            repo=pnpm_indexed, category="deps", scope_path="packages/web",
        )
        assert "error" not in scoped_web
        deps = scoped_web["categories"].get("deps", {})
        # When scoped to packages/web, the api package.json should NOT be found
        pkg_jsons = deps.get("npm_packages") or deps.get("package_json") or []
        # Either it's there with just web's package.json, or there's a scripts dict
        # The most important assertion: api's package.json isn't surfaced
        flat_str = json.dumps(deps)
        assert "@acme/api" not in flat_str

    def test_invalid_scope_path_errors(self, pnpm_indexed):
        from jcodemunch_mcp.tools.get_project_intel import get_project_intel
        out = get_project_intel(repo=pnpm_indexed, scope_path="does/not/exist")
        assert "error" in out
        assert "not a directory" in out["error"].lower()

    def test_scope_path_traversal_rejected(self, pnpm_indexed):
        from jcodemunch_mcp.tools.get_project_intel import get_project_intel
        out = get_project_intel(repo=pnpm_indexed, scope_path="../etc")
        assert "error" in out
