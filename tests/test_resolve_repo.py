"""Tests for resolve_repo tool."""

import hashlib
import sqlite3

import pytest

from jcodemunch_mcp.storage import INDEX_VERSION, IndexStore
from jcodemunch_mcp.storage.sqlite_store import _cache_evict
from jcodemunch_mcp.tools.resolve_repo import (
    resolve_repo,
    _compute_repo_id,
    _git_common_dir_cheap,
)
from jcodemunch_mcp.watcher import _local_repo_id
from jcodemunch_mcp.tools.index_folder import index_folder


class TestComputeRepoId:
    def test_deterministic_id_matches_local_repo_id(self, tmp_path):
        """_compute_repo_id must produce the same ID as _local_repo_id."""
        folder = tmp_path / "my-project"
        folder.mkdir()
        from pathlib import Path
        assert _compute_repo_id(Path(folder)) == _local_repo_id(str(folder))

    def test_different_paths_produce_different_ids(self, tmp_path):
        left = tmp_path / "left" / "shared"
        right = tmp_path / "right" / "shared"
        left.mkdir(parents=True)
        right.mkdir(parents=True)
        from pathlib import Path
        assert _compute_repo_id(Path(left)) != _compute_repo_id(Path(right))


class TestResolveRepo:
    def _index_project(self, tmp_path, name="loadability"):
        project = tmp_path / name
        project.mkdir()
        (project / "main.py").write_text("def hello(): pass\n")
        store_path = str(tmp_path / "store")

        index_folder(str(project), use_ai_summaries=False, storage_path=store_path, identity_mode="local")
        repo_id = _compute_repo_id(project)
        owner, repo_name = repo_id.split("/", 1)
        return project, store_path, owner, repo_name

    def _mutate_sqlite_meta(self, store_path, owner, name, sql, params=()):
        store = IndexStore(base_path=store_path)
        db_path = store._sqlite._db_path(owner, name)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()
        _cache_evict(owner, name)

    def test_resolve_exact_indexed_root(self, tmp_path):
        """Resolving an indexed root returns indexed: true with metadata."""
        project = tmp_path / "myproject"
        project.mkdir()
        (project / "main.py").write_text("def hello(): pass\n")
        store_path = str(tmp_path / "store")

        index_folder(str(project), use_ai_summaries=False, storage_path=store_path, identity_mode="local")

        result = resolve_repo(str(project), storage_path=store_path)
        assert result["found"] is True
        assert result["indexed"] is True
        assert result["repo"].startswith("local/myproject-")
        assert result["symbol_count"] >= 1
        assert result["file_count"] >= 1
        assert "hint" not in result

    def test_resolve_future_version_index_reports_unloadable(self, tmp_path):
        """A present but future-version SQLite index is not queryable."""
        project, store_path, owner, name = self._index_project(tmp_path, "futurever")
        self._mutate_sqlite_meta(
            store_path,
            owner,
            name,
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("index_version", str(INDEX_VERSION + 100)),
        )

        result = resolve_repo(str(project), storage_path=store_path)

        assert result["found"] is True
        assert result["index_present"] is True
        assert result["indexed"] is False
        assert result["loadable"] is False
        assert result["status"] == "sqlite_future_version"
        assert result["load_error"] == "sqlite_future_version"
        assert result["hint"]

    def test_resolve_missing_meta_index_reports_unloadable(self, tmp_path):
        """A present SQLite index without metadata is not queryable."""
        project, store_path, owner, name = self._index_project(tmp_path, "missingmeta")
        self._mutate_sqlite_meta(store_path, owner, name, "DELETE FROM meta")

        result = resolve_repo(str(project), storage_path=store_path)

        assert result["found"] is True
        assert result["index_present"] is True
        assert result["indexed"] is False
        assert result["loadable"] is False
        assert result["status"] == "sqlite_missing_meta"
        assert result["load_error"] == "sqlite_missing_meta"
        assert result["hint"]

    def test_resolve_subdirectory_via_git(self, tmp_path, monkeypatch):
        """Resolving a subdirectory finds the repo via git root."""
        import subprocess
        project = tmp_path / "gitrepo"
        project.mkdir()
        subprocess.run(["git", "init"], cwd=str(project), capture_output=True)
        subdir = project / "src" / "pkg"
        subdir.mkdir(parents=True)
        (project / "main.py").write_text("def top(): pass\n")
        store_path = str(tmp_path / "store")

        index_folder(str(project), use_ai_summaries=False, storage_path=store_path, identity_mode="local")

        result = resolve_repo(str(subdir), storage_path=store_path)
        assert result["found"] is True
        assert result["indexed"] is True
        assert result["repo"].startswith("local/gitrepo-")

    def test_resolve_non_indexed_path(self, tmp_path):
        """Non-indexed path returns indexed: false with hint."""
        project = tmp_path / "unindexed"
        project.mkdir()
        store_path = str(tmp_path / "store")

        result = resolve_repo(str(project), storage_path=store_path)
        assert result["found"] is True
        assert result["indexed"] is False
        assert "repo" in result
        assert result["hint"] == "call index_folder to index this path"

    def test_resolve_nonexistent_path(self, tmp_path):
        """Nonexistent path returns found: false with error."""
        result = resolve_repo(str(tmp_path / "does-not-exist"))
        assert result["found"] is False
        assert result["indexed"] is False
        assert "error" in result

    def test_resolve_file_uses_parent(self, tmp_path):
        """Resolving a file path uses its parent directory."""
        project = tmp_path / "filetest"
        project.mkdir()
        pyfile = project / "app.py"
        pyfile.write_text("def run(): pass\n")
        store_path = str(tmp_path / "store")

        index_folder(str(project), use_ai_summaries=False, storage_path=store_path)

        result = resolve_repo(str(pyfile), storage_path=store_path)
        assert result["found"] is True
        assert result["indexed"] is True
        assert result["repo"].startswith("local/filetest-")

    def test_result_has_timing(self, tmp_path):
        """Result always includes _meta with timing_ms."""
        result = resolve_repo(str(tmp_path))
        assert "_meta" in result
        assert "timing_ms" in result["_meta"]


class TestWorktreeCanonicalCandidates:
    """Issue #277 — when a path is a Git worktree of an already-indexed
    canonical checkout, surface the canonical repo as a candidate instead
    of treating the worktree as a fresh unindexed target.
    """

    def _git(self, *args, cwd):
        import subprocess
        env = {
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            env={**__import__("os").environ, **env},
        )
        assert result.returncode == 0, f"git {args} failed: {result.stderr}"
        return result.stdout

    def test_worktree_surfaces_canonical_candidate(self, tmp_path):
        """A worktree of an already-indexed repo lists the canonical as a candidate."""
        canonical = tmp_path / "canonical"
        canonical.mkdir()
        self._git("init", "-b", "main", cwd=canonical)
        (canonical / "main.py").write_text("def hello(): return 1\n", encoding="utf-8")
        self._git("add", "main.py", cwd=canonical)
        self._git("commit", "-m", "initial", cwd=canonical)

        store_path = str(tmp_path / "store")
        index_folder(str(canonical), use_ai_summaries=False, storage_path=store_path, identity_mode="local")

        # Create a linked worktree on a new branch (sibling path).
        worktree = tmp_path / "wt-feature"
        self._git(
            "worktree", "add", "-b", "feature", str(worktree), cwd=canonical
        )

        result = resolve_repo(str(worktree), storage_path=store_path)
        assert result["found"] is True
        assert result["indexed"] is False, (
            "worktree path itself isn't indexed — that's the whole point"
        )
        assert "canonical_candidates" in result, (
            f"expected canonical_candidates in {result}"
        )
        assert len(result["canonical_candidates"]) == 1
        cand = result["canonical_candidates"][0]
        assert cand["repo"].startswith("local/canonical-")
        assert cand["rationale"] == "shared --git-common-dir"
        assert "Git worktree" in result["hint"]

    def test_unrelated_unindexed_path_has_no_candidates(self, tmp_path):
        """A non-Git, non-worktree path stays on the original hint with no candidates."""
        canonical = tmp_path / "canonical"
        canonical.mkdir()
        self._git("init", cwd=canonical)
        (canonical / "main.py").write_text("def hello(): return 1\n", encoding="utf-8")
        self._git("add", "main.py", cwd=canonical)
        self._git("commit", "-m", "initial", cwd=canonical)

        store_path = str(tmp_path / "store")
        index_folder(str(canonical), use_ai_summaries=False, storage_path=store_path, identity_mode="local")

        # An unrelated empty directory — not a worktree, not indexed.
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir()

        result = resolve_repo(str(unrelated), storage_path=store_path)
        assert result["indexed"] is False
        assert "canonical_candidates" not in result
        assert result["hint"] == "call index_folder to index this path"


class TestGitCommonDirCheap:
    """Regression: jcm#303 — filesystem-only common-dir resolution so that
    canonical-candidate discovery scales without an O(indexes) git subprocess
    storm in large worktree environments.
    """

    def test_main_checkout_returns_dot_git(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        assert _git_common_dir_cheap(repo) == (repo / ".git").resolve()

    def test_linked_worktree_follows_gitdir_and_commondir(self, tmp_path):
        # Simulate the standard `git worktree add` layout without invoking git.
        canonical = tmp_path / "canonical"
        canonical.mkdir()
        canonical_git = canonical / ".git"
        canonical_git.mkdir()
        worktrees_dir = canonical_git / "worktrees" / "feature"
        worktrees_dir.mkdir(parents=True)
        # commondir is a relative path back to the canonical .git
        (worktrees_dir / "commondir").write_text("../..\n", encoding="utf-8")

        worktree = tmp_path / "wt"
        worktree.mkdir()
        # .git as a pointer file with absolute gitdir, matching real git behaviour
        (worktree / ".git").write_text(
            f"gitdir: {worktrees_dir}\n", encoding="utf-8"
        )

        assert _git_common_dir_cheap(worktree) == canonical_git.resolve()
        # And the main checkout resolves to the same common-dir, which is the
        # invariant canonical-candidate matching relies on.
        assert _git_common_dir_cheap(canonical) == canonical_git.resolve()

    def test_submodule_layout_returns_gitdir(self, tmp_path):
        # Submodule: .git is a pointer file but the gitdir has no commondir file.
        parent_git = tmp_path / "parent" / ".git"
        modules = parent_git / "modules" / "sub"
        modules.mkdir(parents=True)

        submodule = tmp_path / "parent" / "sub"
        submodule.mkdir(parents=True)
        (submodule / ".git").write_text(
            f"gitdir: {modules}\n", encoding="utf-8"
        )

        assert _git_common_dir_cheap(submodule) == modules.resolve()

    def test_non_git_path_returns_none(self, tmp_path):
        assert _git_common_dir_cheap(tmp_path) is None

    def test_malformed_pointer_file_returns_none(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").write_text("not-a-gitdir-pointer\n", encoding="utf-8")
        assert _git_common_dir_cheap(repo) is None

    def test_empty_pointer_file_returns_none(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").write_text("gitdir:\n", encoding="utf-8")
        assert _git_common_dir_cheap(repo) is None


class TestResolveRepoFastPaths:
    """Regression: jcm#303 — exact source_root and source_root containment
    must hit before the legacy compute-then-inspect path so that large index
    stores don't time out resolve_repo.
    """

    def test_exact_source_root_match_path_meta(self, tmp_path):
        canonical = tmp_path / "exactsrc"
        canonical.mkdir()
        (canonical / "main.py").write_text("def f(): return 1\n", encoding="utf-8")

        store_path = str(tmp_path / "store")
        index_folder(
            str(canonical),
            use_ai_summaries=False,
            storage_path=store_path,
            identity_mode="local",
        )

        result = resolve_repo(str(canonical), storage_path=store_path)
        assert result["found"] is True
        assert result["indexed"] is True
        assert result["_meta"]["match_path"] == "exact_source_root"

    def test_source_root_containment_subdirectory(self, tmp_path):
        canonical = tmp_path / "containment"
        canonical.mkdir()
        sub = canonical / "src" / "deep"
        sub.mkdir(parents=True)
        (sub / "main.py").write_text("def f(): return 1\n", encoding="utf-8")

        store_path = str(tmp_path / "store")
        index_folder(
            str(canonical),
            use_ai_summaries=False,
            storage_path=store_path,
            identity_mode="local",
        )

        # Resolving the subdirectory should hit the containment fast path
        # and return the indexed parent.
        result = resolve_repo(str(sub), storage_path=store_path)
        assert result["found"] is True
        assert result["indexed"] is True
        assert result["_meta"]["match_path"] == "source_root_containment"

    def test_not_indexed_path_marks_match_path(self, tmp_path):
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir()
        store_path = str(tmp_path / "store")

        result = resolve_repo(str(unrelated), storage_path=store_path)
        assert result["found"] is True
        assert result["indexed"] is False
        assert result["_meta"]["match_path"] == "not_indexed"

    def test_fast_path_does_not_spawn_subprocess(self, tmp_path, monkeypatch):
        """At scale, the exact-source-root fast path must not invoke git at all.
        Catches regressions where someone reintroduces a subprocess call in the
        hot path."""
        canonical = tmp_path / "nosub"
        canonical.mkdir()
        (canonical / "main.py").write_text("def f(): return 1\n", encoding="utf-8")
        store_path = str(tmp_path / "store")
        index_folder(
            str(canonical),
            use_ai_summaries=False,
            storage_path=store_path,
            identity_mode="local",
        )

        import subprocess as _sp
        original_run = _sp.run
        calls = []

        def tracking_run(*args, **kwargs):
            calls.append(args[0] if args else kwargs.get("args"))
            return original_run(*args, **kwargs)

        monkeypatch.setattr(_sp, "run", tracking_run)
        result = resolve_repo(str(canonical), storage_path=store_path)
        assert result["indexed"] is True
        # The exact-source-root fast path returned without consulting git.
        git_calls = [c for c in calls if c and len(c) > 0 and "git" in str(c[0]).lower()]
        assert git_calls == [], (
            f"expected no git subprocess in fast path, got: {git_calls}"
        )


class TestResolveRepoCanonicalCandidateFastPath:
    """Regression: jcm#303 follow-up (reported by @rknighton against v1.108.15).
    v1.108.14 fixed the O(N) common-dir scan, but the provisional repo_id
    computation in the not-indexed branch still routed through
    `resolve_index_identity` → `detect_git_root` → `_read_origin_url`, which
    spawns `git config --get remote.origin.url`. In large-worktree environments
    under `git_root_identity=true`, that subprocess hung. Fix: discover
    canonical candidates BEFORE the slow path, return immediately with a
    cheap local provisional repo_id.
    """

    def _git(self, *args, cwd):
        import subprocess
        env = {
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            env={**__import__("os").environ, **env},
        )
        assert result.returncode == 0, f"git {args} failed: {result.stderr}"

    def test_worktree_resolve_returns_via_fast_path(self, tmp_path):
        """A worktree of an indexed repo hits canonical_candidate_fast,
        not the legacy compute_repo_id path."""
        canonical = tmp_path / "canonical"
        canonical.mkdir()
        self._git("init", "-b", "main", cwd=canonical)
        (canonical / "main.py").write_text("def hello(): return 1\n", encoding="utf-8")
        self._git("add", "main.py", cwd=canonical)
        self._git("commit", "-m", "initial", cwd=canonical)

        store_path = str(tmp_path / "store")
        index_folder(
            str(canonical),
            use_ai_summaries=False,
            storage_path=store_path,
            identity_mode="local",
        )

        worktree = tmp_path / "wt"
        self._git("worktree", "add", "-b", "feature", str(worktree), cwd=canonical)

        result = resolve_repo(str(worktree), storage_path=store_path)
        assert result["found"] is True
        assert result["indexed"] is False
        assert "canonical_candidates" in result
        assert result["_meta"]["match_path"] == "canonical_candidate_fast", (
            f"expected canonical_candidate_fast match_path, got: {result['_meta']}"
        )

    def test_worktree_resolve_does_not_invoke_read_origin_url(
        self, tmp_path, monkeypatch
    ):
        """Validates the actual reporter symptom: resolving a worktree path
        must not call `git config --get remote.origin.url` (the call that
        was hanging in the reporter's 130-worktree environment)."""
        canonical = tmp_path / "canonical"
        canonical.mkdir()
        self._git("init", "-b", "main", cwd=canonical)
        (canonical / "main.py").write_text("def hello(): return 1\n", encoding="utf-8")
        self._git("add", "main.py", cwd=canonical)
        self._git("commit", "-m", "initial", cwd=canonical)

        store_path = str(tmp_path / "store")
        index_folder(
            str(canonical),
            use_ai_summaries=False,
            storage_path=store_path,
            identity_mode="local",
        )

        worktree = tmp_path / "wt"
        self._git("worktree", "add", "-b", "feature", str(worktree), cwd=canonical)

        # Track subprocess.run AFTER the index is built (the indexer itself
        # may legitimately invoke git; we only care about resolve_repo).
        import subprocess as _sp
        original_run = _sp.run
        calls = []

        def tracking_run(*args, **kwargs):
            calls.append(args[0] if args else kwargs.get("args"))
            return original_run(*args, **kwargs)

        monkeypatch.setattr(_sp, "run", tracking_run)
        result = resolve_repo(str(worktree), storage_path=store_path)
        assert "canonical_candidates" in result

        origin_calls = [
            c for c in calls
            if c
            and len(c) >= 4
            and str(c[0]).lower().endswith(("git", "git.exe"))
            and "config" in c
            and "--get" in c
            and "remote.origin.url" in c
        ]
        assert origin_calls == [], (
            f"resolve_repo must not invoke `git config --get remote.origin.url`; "
            f"that call was the cold-start hang in the reporter's environment. "
            f"Got: {origin_calls}"
        )


class TestReadOriginUrlHardening:
    """Regression: jcm#303 follow-up. `_read_origin_url` previously lacked the
    defensive subprocess posture of `_git_toplevel` (no `stdin=DEVNULL`, no
    env neutralisation). On Windows under heavy worktree fan-out the missing
    stdin redirect was a likely contributor to the cold-start hang.
    """

    def test_read_origin_url_uses_devnull_and_neutralised_env(
        self, tmp_path, monkeypatch
    ):
        from jcodemunch_mcp.storage import git_root as _gr

        captured = {}

        def fake_run(*args, **kwargs):
            captured["args"] = args[0] if args else kwargs.get("args")
            captured["stdin"] = kwargs.get("stdin")
            captured["env"] = kwargs.get("env")
            import types
            return types.SimpleNamespace(returncode=0, stdout="git@github.com:foo/bar.git\n")

        monkeypatch.setattr("subprocess.run", fake_run)
        url = _gr._read_origin_url(tmp_path)

        assert url == "git@github.com:foo/bar.git"
        import subprocess
        assert captured["stdin"] == subprocess.DEVNULL, (
            "expected stdin=subprocess.DEVNULL to prevent blocking on stdin"
        )
        env = captured["env"]
        assert env is not None
        assert env.get("GIT_CONFIG_NOSYSTEM") == "1"
        assert env.get("GIT_TERMINAL_PROMPT") == "0"
        # GIT_CONFIG_GLOBAL should be devnull (platform-dependent value).
        import os
        assert env.get("GIT_CONFIG_GLOBAL") == os.devnull
