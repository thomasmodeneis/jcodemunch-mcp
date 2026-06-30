"""Tests for get_endpoint_impact — endpoint -> handler -> blast radius + views.

Builds real fixture repos (same pattern as test_v1_108_58) and exercises both
route sources: decorator-bound (Flask @app.get) and string-dispatched (Django
path()).
"""

from jcodemunch_mcp.tools.index_folder import index_folder
from jcodemunch_mcp.tools.get_endpoint_impact import (
    get_endpoint_impact,
    _parse_endpoint_query,
    _norm_path,
    _match_endpoints,
)


def _index(src, store):
    result = index_folder(str(src), use_ai_summaries=False, storage_path=str(store))
    assert result["success"] is True
    return result["repo"], str(store)


def _flask_repo(tmp_path):
    src = tmp_path / "src"
    store = tmp_path / "store"
    src.mkdir()
    store.mkdir()
    (src / "app.py").write_text(
        "from flask import Flask, render_template\n"
        "app = Flask(__name__)\n\n"
        "@app.get('/users')\n"
        "def list_users():\n"
        "    return render_template('users.html')\n\n"
        "def trigger():\n"
        "    return list_users()\n\n"
        "def unrelated():\n"
        "    return 42\n"
    )
    return _index(src, store)


def _django_repo(tmp_path):
    src = tmp_path / "src"
    store = tmp_path / "store"
    src.mkdir()
    store.mkdir()
    (src / "views.py").write_text(
        "def list_users(request):\n"
        "    return 1\n"
    )
    (src / "urls.py").write_text(
        "from views import list_users\n"
        "from django.urls import path\n\n"
        "urlpatterns = [path('users/', list_users)]\n"
    )
    return _index(src, store)


# --- pure helpers ----------------------------------------------------------

def test_parse_endpoint_query():
    assert _parse_endpoint_query("GET /users") == ("GET", "/users")
    assert _parse_endpoint_query("/users") == (None, "/users")
    assert _parse_endpoint_query("post /a/b/") == ("POST", "/a/b")


def test_norm_path():
    assert _norm_path("users/") == "/users"
    assert _norm_path("/Users") == "/users"
    assert _norm_path("/") == "/"


def test_match_exact_then_loose():
    eps = [
        {"verb": "GET", "path": "/api/users"},
        {"verb": "POST", "path": "/users"},
    ]
    # exact verb+path
    assert _match_endpoints(eps, "POST", "/users") == [eps[1]]
    # loose: query /users matches /api/users by suffix
    got = _match_endpoints(eps, "GET", "/users")
    assert eps[0] in got
    # verb filter excludes
    assert _match_endpoints(eps, "DELETE", "/users") == []


# --- decorator route (Flask) ----------------------------------------------

def test_flask_decorator_endpoint_impact(tmp_path):
    repo, store = _flask_repo(tmp_path)
    res = get_endpoint_impact(repo, endpoint="GET /users", call_depth=2, storage_path=store)
    assert "error" not in res
    assert res["matched_endpoints"], res
    handlers = {m["handler_name"] for m in res["matched_endpoints"]}
    assert "list_users" in handlers
    imp = next(i for i in res["impacts"] if i["handler"]["name"] == "list_users")
    assert imp["source"] == "decorator"
    # renders users.html
    templates = {v["template"] for v in imp["rendered_views"]}
    assert any("users.html" in (t or "") for t in templates)
    # trigger() calls list_users -> shows up as a caller at call_depth=2
    caller_names = {c.get("name") for c in imp["callers"]}
    assert "trigger" in caller_names


def test_flask_path_only_query(tmp_path):
    repo, store = _flask_repo(tmp_path)
    res = get_endpoint_impact(repo, endpoint="/users", storage_path=store)
    assert res["matched_endpoints"]
    assert any(m["handler_name"] == "list_users" for m in res["matched_endpoints"])


# --- string-dispatch route (Django) ---------------------------------------

def test_django_string_dispatch_endpoint(tmp_path):
    repo, store = _django_repo(tmp_path)
    res = get_endpoint_impact(repo, endpoint="/users", storage_path=store)
    assert res["matched_endpoints"], res
    m = res["matched_endpoints"][0]
    assert m["handler_name"] == "list_users"
    assert m["source"].startswith("flow_edge")


# --- handler_symbol_id + no-match + arg validation -------------------------

def test_handler_symbol_id(tmp_path):
    repo, store = _flask_repo(tmp_path)
    # discover the handler id first
    disc = get_endpoint_impact(repo, endpoint="GET /users", storage_path=store)
    hid = disc["matched_endpoints"][0]["handler_id"]
    res = get_endpoint_impact(repo, handler_symbol_id=hid, storage_path=store)
    assert res["matched_endpoints"][0]["handler_id"] == hid
    assert res["impacts"][0]["handler"]["id"] == hid


def test_no_match_returns_hint(tmp_path):
    repo, store = _flask_repo(tmp_path)
    res = get_endpoint_impact(repo, endpoint="GET /nonexistent-route-xyz", storage_path=store)
    assert res["matched_endpoints"] == []
    assert "hint" in res
    assert res["_meta"]["endpoints_known"] >= 1


def test_requires_an_argument(tmp_path):
    repo, store = _flask_repo(tmp_path)
    res = get_endpoint_impact(repo, storage_path=store)
    assert "error" in res


def test_unknown_repo():
    res = get_endpoint_impact("does/not-exist", endpoint="GET /x", storage_path=None)
    assert "error" in res
