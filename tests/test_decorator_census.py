"""Tests for get_decorator_census — repo-wide decorator/annotation census.

Covers:
  - normalization collapses call-forms + dotted paths + brackets into one bucket
  - histogram counts, raw_forms, symbol_kinds, file counts
  - name_filter (substring on the normalized name)
  - scope_path narrows to a subtree
  - kind filter
  - include_sites lists decorated symbols (capped)
  - honest empty result (no match) is not an error
  - validation errors
  - read-only (idempotent)
"""

from pathlib import Path

from jcodemunch_mcp.tools.get_decorator_census import (
    _normalize_decorator,
    get_decorator_census,
)
from jcodemunch_mcp.tools.index_folder import index_folder


_APP = (
    "import functools\n"
    "\n"
    "def route(path):\n"
    "    def deco(fn):\n"
    "        return fn\n"
    "    return deco\n"
    "\n"
    "def fixture(fn):\n"
    "    return fn\n"
    "\n"
    "@route('/users')\n"
    "def list_users():\n"
    "    return []\n"
    "\n"
    "@route('/orders')\n"
    "def list_orders():\n"
    "    return []\n"
    "\n"
    "@fixture\n"
    "def db():\n"
    "    return None\n"
    "\n"
    "@property\n"
    "def name(self):\n"
    "    return self._n\n"
)

_LIB = (
    "from dataclasses import dataclass\n"
    "\n"
    "@dataclass\n"
    "class Plain:\n"
    "    x: int = 0\n"
    "\n"
    "@dataclass(frozen=True)\n"
    "class Frozen:\n"
    "    y: int = 0\n"
)


def _make_repo(tmp_path: Path) -> tuple[str, str]:
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "lib").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "views.py").write_text(_APP, encoding="utf-8")
    (tmp_path / "lib" / "models.py").write_text(_LIB, encoding="utf-8")
    storage = str(tmp_path / ".index")
    result = index_folder(str(tmp_path), use_ai_summaries=False, storage_path=storage)
    return result.get("repo", str(tmp_path)), storage


def _bucket(out, decorator):
    for d in out["decorators"]:
        if d["decorator"] == decorator:
            return d
    return None


class TestNormalization:
    def test_call_forms_and_brackets(self):
        assert _normalize_decorator("@app.route('/x')") == "app.route"
        assert _normalize_decorator("@dataclass(frozen=True)") == "dataclass"
        assert _normalize_decorator("@pytest.fixture(autouse=True)") == "pytest.fixture"
        assert _normalize_decorator("@Override") == "Override"
        assert _normalize_decorator("[Serializable]") == "Serializable"
        assert _normalize_decorator("  @property  ") == "property"


class TestCensus:
    def test_histogram_groups_call_forms(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = get_decorator_census(repo, storage_path=storage)
        assert "error" not in out, out.get("error")
        route = _bucket(out, "route")
        assert route is not None
        assert route["count"] == 2  # @route('/users') + @route('/orders')
        # Two distinct call-forms collapsed into the one bucket.
        assert set(route["raw_forms"]) == {"@route('/users')", "@route('/orders')"}
        assert route["files"] == 1
        assert route["symbol_kinds"].get("function") == 2

    def test_dataclass_variants_collapse(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = get_decorator_census(repo, storage_path=storage)
        dc = _bucket(out, "dataclass")
        assert dc is not None
        assert dc["count"] == 2
        assert set(dc["raw_forms"]) == {"@dataclass", "@dataclass(frozen=True)"}
        assert dc["symbol_kinds"].get("class") == 2

    def test_summary(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = get_decorator_census(repo, storage_path=storage)
        s = out["summary"]
        # route x2, fixture x1, property x1, dataclass x2 = 6 uses
        assert s["total_decorator_uses"] == 6
        assert s["distinct_decorators"] == 4  # route, fixture, property, dataclass
        assert s["decorated_symbols"] == 6
        assert s["by_language"].get("python") == 6

    def test_name_filter(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = get_decorator_census(repo, name_filter="route", storage_path=storage)
        assert {d["decorator"] for d in out["decorators"]} == {"route"}

    def test_scope_path(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = get_decorator_census(repo, scope_path="lib", storage_path=storage)
        names = {d["decorator"] for d in out["decorators"]}
        assert names == {"dataclass"}  # only lib/models.py

    def test_kind_filter(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = get_decorator_census(repo, kind="class", storage_path=storage)
        names = {d["decorator"] for d in out["decorators"]}
        assert names == {"dataclass"}  # only the class decorators

    def test_include_sites(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = get_decorator_census(repo, name_filter="route", include_sites=True,
                                   storage_path=storage)
        route = _bucket(out, "route")
        assert "sites" in route
        site_names = {s["name"] for s in route["sites"]}
        assert site_names == {"list_users", "list_orders"}
        assert all("file" in s and "line" in s and "raw" in s for s in route["sites"])

    def test_empty_match_is_not_error(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = get_decorator_census(repo, name_filter="nonexistent-xyz", storage_path=storage)
        assert "error" not in out
        assert out["decorators"] == []
        assert out["summary"]["total_decorator_uses"] == 0
        assert "No decorated symbols matched" in out["_meta"]["note"]


class TestErrorsAndReadOnly:
    def test_bad_max_decorators(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        assert "error" in get_decorator_census(repo, max_decorators=0, storage_path=storage)

    def test_unindexed_repo(self, tmp_path):
        _, storage = _make_repo(tmp_path)
        out = get_decorator_census("local/does-not-exist", storage_path=storage)
        assert "error" in out

    def test_idempotent(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        a = get_decorator_census(repo, storage_path=storage)
        b = get_decorator_census(repo, storage_path=storage)
        assert a["summary"] == b["summary"]
        assert a["decorators"] == b["decorators"]
