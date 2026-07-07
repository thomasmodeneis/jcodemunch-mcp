"""Tests for get_parity_map — correspondence-aware migration parity.

Covers:
  - exact-name ported detection (body may differ under 'signature' policy)
  - rename detection via structural+behavioral similarity
  - ported_diverged when a matched counterpart's signature changed
  - added (target-only) and orphaned (no in-scope caller) classification
  - parity_pct math
  - port plan: topological order (leaf before dependent), unblocked/blocking_deps
  - SCC cycle grouping in the port plan
  - name_only divergence suppresses ported_diverged
  - rename=False falls back to exact-only
  - honest errors (same scope, unindexed repo, empty scope)
  - read-only (idempotent)
"""

from pathlib import Path

from jcodemunch_mcp.tools.get_parity_map import get_parity_map
from jcodemunch_mcp.tools.index_folder import index_folder


# Source tree (legacy/) ported to target tree (v2/).
#   db_lookup      exact match, identical body            -> ported
#   helper         exact match, body differs (sig same)   -> ported (signature policy)
#   get_user_by_id renamed to fetch_user (similar)        -> ported (renamed_similar)
#   compute_total  exact name, signature changed          -> ported_diverged
#   caller_a       unmatched, nothing calls it            -> orphaned
#   caller_b       unmatched, called by caller_a          -> unported
#   cyc_x / cyc_y  unmatched, mutually recursive          -> unported + one scc_group
#   only_in_legacy unmatched, nothing calls it            -> orphaned
# Target-only:
#   brand_new                                             -> added
_LEGACY = (
    "def db_lookup(uid: int) -> dict:\n"
    "    return {'id': uid}\n"
    "\n"
    "def helper() -> int:\n"
    "    return db_lookup(0)['id']\n"
    "\n"
    "def get_user_by_id(uid: int) -> dict:\n"
    "    row = db_lookup(uid)\n"
    "    return row\n"
    "\n"
    "def compute_total(items: list, tax: float) -> float:\n"
    "    return sum(items) + tax\n"
    "\n"
    "def caller_a() -> int:\n"
    "    return caller_b()\n"
    "\n"
    "def caller_b() -> int:\n"
    "    return db_lookup(1)['id']\n"
    "\n"
    "def cyc_x() -> int:\n"
    "    return cyc_y()\n"
    "\n"
    "def cyc_y() -> int:\n"
    "    return cyc_x()\n"
    "\n"
    "def only_in_legacy() -> int:\n"
    "    return 42\n"
)

_V2 = (
    "def db_lookup(uid: int) -> dict:\n"
    "    return {'id': uid}\n"
    "\n"
    "def helper() -> int:\n"
    "    return 1\n"
    "\n"
    "def fetch_user(uid: int) -> dict:\n"
    "    row = db_lookup(uid)\n"
    "    return row\n"
    "\n"
    "def compute_total(items: list) -> float:\n"
    "    return sum(items)\n"
    "\n"
    "def brand_new() -> int:\n"
    "    return 7\n"
)


def _make_repo(tmp_path: Path) -> tuple[str, str]:
    (tmp_path / "legacy").mkdir(parents=True, exist_ok=True)
    (tmp_path / "v2").mkdir(parents=True, exist_ok=True)
    (tmp_path / "legacy" / "mod.py").write_text(_LEGACY, encoding="utf-8")
    (tmp_path / "v2" / "mod.py").write_text(_V2, encoding="utf-8")
    storage = str(tmp_path / ".index")
    result = index_folder(str(tmp_path), use_ai_summaries=False, storage_path=storage)
    return result.get("repo", str(tmp_path)), storage


def _run(repo, storage, **kw):
    kw.setdefault("source_path", "legacy")
    kw.setdefault("target_path", "v2")
    return get_parity_map(source_repo=repo, target_repo=repo, storage_path=storage, **kw)


def _status_of(out, name):
    for s in out["symbols"]:
        if s["name"] == name:
            return s
    return None


class TestParityClassification:
    def test_summary_counts(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = _run(repo, storage)
        assert "error" not in out, out.get("error")
        summ = out["summary"]
        # 3 clean ported (db_lookup, helper, get_user_by_id->fetch_user)
        assert summ["ported"] == 3, out["symbols"]
        assert summ["ported_diverged"] == 1  # compute_total
        assert summ["unported"] == 3         # caller_b, cyc_x, cyc_y
        assert summ["orphaned"] == 2         # caller_a, only_in_legacy
        assert summ["added"] == 1            # brand_new
        assert summ["estimate"] is True
        # parity_pct = 3 / (3+1+3+2) = 33.3
        assert summ["parity_pct"] == 33.3

    def test_exact_ported_ignores_body_under_signature_policy(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = _run(repo, storage)
        helper = _status_of(out, "helper")
        assert helper["status"] == "ported"  # body differs, signature identical
        assert helper["match"]["match_basis"] == "exact_name"

    def test_rename_detected(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = _run(repo, storage)
        gu = _status_of(out, "get_user_by_id")
        assert gu["status"] == "ported"
        assert gu["match"]["match_basis"] == "renamed_similar"
        assert gu["match"]["target_name"] == "fetch_user"
        assert gu["match"]["confidence"] >= 0.75

    def test_diverged_on_signature_change(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = _run(repo, storage)
        ct = _status_of(out, "compute_total")
        assert ct["status"] == "ported_diverged"
        assert ct["divergence"]["signature_changed"] is True

    def test_orphaned_vs_unported(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = _run(repo, storage)
        assert _status_of(out, "caller_a")["status"] == "orphaned"
        assert _status_of(out, "only_in_legacy")["status"] == "orphaned"
        assert _status_of(out, "caller_b")["status"] == "unported"

    def test_added_surface(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = _run(repo, storage)
        # brand_new is target-only; it must not appear as a source symbol row.
        assert _status_of(out, "brand_new") is None
        assert out["summary"]["added"] == 1


class TestPortPlan:
    def test_plan_spans_all_unmatched(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = _run(repo, storage)
        names = {p["name"] for p in out["port_plan"]}
        assert names == {"caller_a", "caller_b", "cyc_x", "cyc_y", "only_in_legacy"}

    def test_leaf_ordered_before_dependent(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = _run(repo, storage)
        order = {p["name"]: p["order_index"] for p in out["port_plan"]}
        # caller_b is called by caller_a => must be ported first.
        assert order["caller_b"] < order["caller_a"]

    def test_unblocked_and_blocking_deps(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = _run(repo, storage)
        plan = {p["name"]: p for p in out["port_plan"]}
        assert plan["caller_b"]["unblocked"] is True
        assert plan["caller_b"]["blocking_deps"] == []
        assert plan["caller_a"]["unblocked"] is False
        assert "caller_b" in plan["caller_a"]["blocking_deps"]

    def test_cycle_grouped(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = _run(repo, storage)
        plan = {p["name"]: p for p in out["port_plan"]}
        gx = plan["cyc_x"]["scc_group"]
        gy = plan["cyc_y"]["scc_group"]
        assert gx is not None and gx == gy
        # Mutually-recursive peers are not each other's blockers.
        assert "cyc_y" not in plan["cyc_x"]["blocking_deps"]


class TestPolicies:
    def test_name_only_suppresses_divergence(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = _run(repo, storage, divergence="name_only")
        ct = _status_of(out, "compute_total")
        assert ct["status"] == "ported"  # no divergence check
        assert out["summary"]["ported_diverged"] == 0

    def test_rename_disabled_falls_back_to_exact(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = _run(repo, storage, rename=False)
        gu = _status_of(out, "get_user_by_id")
        # No exact counterpart => not ported; fetch_user becomes added surface.
        assert gu["status"] in {"unported", "orphaned"}
        assert out["summary"]["added"] == 2  # brand_new + fetch_user


class TestErrorsAndReadOnly:
    def test_identical_scope_errors(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = get_parity_map(source_repo=repo, target_repo=repo,
                             source_path="legacy", target_path="legacy",
                             storage_path=storage)
        assert "error" in out

    def test_unindexed_repo_errors(self, tmp_path):
        _, storage = _make_repo(tmp_path)
        out = get_parity_map(source_repo="local/does-not-exist",
                             target_repo="local/does-not-exist",
                             source_path="a", target_path="b",
                             storage_path=storage)
        assert "error" in out

    def test_empty_source_scope_errors(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = get_parity_map(source_repo=repo, target_repo=repo,
                             source_path="nonexistent-dir", target_path="v2",
                             storage_path=storage)
        assert "error" in out

    def test_bad_divergence_errors(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = _run(repo, storage, divergence="bogus")
        assert "error" in out

    def test_idempotent_read_only(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        a = _run(repo, storage)
        b = _run(repo, storage)
        assert a["summary"] == b["summary"]
        assert [s["status"] for s in a["symbols"]] == [s["status"] for s in b["symbols"]]
