"""get_endpoint_impact — "what breaks if I change this HTTP endpoint?"

Maps an endpoint (HTTP method + URL) to its handler symbol, then composes the
existing impact primitives into one read-only answer:

  * **blast radius** — importers + (optionally) callers of the handler, via
    :func:`get_blast_radius`.
  * **rendered views** — templates the handler renders, via the ``render``
    flow edges from :mod:`flow_edges`.

Endpoint resolution is built on the route coverage the index already exposes,
unifying two sources into one endpoint table:

  * **string-dispatched routes** via :func:`flow_edges.resolve_flow_edges`
    (Django ``path()``, Express ``router.get(p, h)``, Flask ``add_url_rule``,
    Rails ``to:``) — these are invisible to the call graph.
  * **decorator-bound routes** via the same gateway classification
    :mod:`get_signal_chains` uses (Flask / FastAPI ``@app.get``, Spring
    ``@GetMapping``), reusing ``_classify_gateway`` / ``_extract_label``.

Read-only; nothing is persisted. Deeper framework path resolution — FastAPI
``APIRouter(prefix=...)`` / ``include_router`` composition and Spring class-level
``@RequestMapping`` inheritance — is a follow-on that will enrich this same
endpoint table; until then those routes resolve by their local (un-prefixed)
path or via ``handler_symbol_id``.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from ..storage import IndexStore
from ._utils import resolve_repo
from .flow_edges import resolve_flow_edges
from .get_signal_chains import _classify_gateway, _extract_label
from .get_blast_radius import get_blast_radius

logger = logging.getLogger(__name__)

# Verbs that match any requested method (Django path() carries none; add_url_rule
# defaults to ANY; an unspecified query verb matches everything).
_WILDCARD_VERBS = frozenset({"ANY", "PATH", ""})

# Decorator gateway labels are "VERB /path" (see get_signal_chains._extract_label);
# the http:<name> fallback (no extractable path) is intentionally not matched.
_LABEL_RE = re.compile(r"^([A-Z]+)\s+(\S+)")


def _norm_path(p: str) -> str:
    """Normalize a URL path for comparison: leading slash, no trailing slash, lower."""
    if not p:
        return ""
    p = p.strip()
    if not p.startswith("/"):
        p = "/" + p
    if len(p) > 1:
        p = p.rstrip("/")
    return p.lower()


def _parse_endpoint_query(endpoint: str) -> tuple[Optional[str], str]:
    """'GET /users' -> ('GET', '/users'); '/users' -> (None, '/users')."""
    parts = endpoint.strip().split(None, 1)
    if len(parts) == 2 and parts[0].isalpha():
        return parts[0].upper(), _norm_path(parts[1])
    return None, _norm_path(endpoint.strip())


def _collect_endpoints(index, store, owner: str, name: str) -> list[dict]:
    """Unify string-dispatch route edges + decorator gateways into endpoint records."""
    endpoints: list[dict] = []
    seen: set = set()

    # 1) string-dispatched routes (flow edges)
    try:
        edges = resolve_flow_edges(index, store, owner, name, kinds=("route",))
    except Exception:  # pragma: no cover - resolver is best-effort
        logger.debug("resolve_flow_edges(route) failed", exc_info=True)
        edges = []
    for e in edges:
        if e.get("type") != "route->handler":
            continue
        verb = (e.get("verb") or "ANY").upper()
        key = (verb, _norm_path(e.get("path", "")), e.get("dst_id"), e.get("dst_name"))
        if key in seen:
            continue
        seen.add(key)
        endpoints.append({
            "verb": verb,
            "path": e.get("path", ""),
            "handler_id": e.get("dst_id"),
            "handler_name": e.get("dst_name"),
            "handler_file": e.get("dst_file"),
            "source": "flow_edge:" + (e.get("framework_shape") or "route"),
            "resolution": e.get("resolution", "unresolved"),
        })

    # 2) decorator-bound routes (gateway classification)
    for sym in index.symbols:
        if _classify_gateway(sym, None) != "http":
            continue
        m = _LABEL_RE.match(_extract_label(sym, "http") or "")
        if not m:
            continue
        verb, path = m.group(1).upper(), m.group(2)
        key = (verb, _norm_path(path), sym.get("id"), sym.get("name"))
        if key in seen:
            continue
        seen.add(key)
        endpoints.append({
            "verb": verb,
            "path": path,
            "handler_id": sym.get("id"),
            "handler_name": sym.get("name"),
            "handler_file": sym.get("file"),
            "source": "decorator",
            "resolution": "resolved",
        })

    return endpoints


def _match_endpoints(endpoints: list[dict], verb: Optional[str], path: str) -> list[dict]:
    """Match query (verb, normalized path) against the endpoint table.

    Exact path match first; if none, fall back to suffix/containment so a query
    for ``/users`` finds a route registered as ``/api/users`` (and vice versa).
    """
    def _verb_ok(ev: str) -> bool:
        return not verb or ev in _WILDCARD_VERBS or ev == verb

    exact = [e for e in endpoints if _verb_ok(e["verb"]) and _norm_path(e["path"]) == path]
    if exact:
        return exact
    if not path:
        return []
    loose = []
    for e in endpoints:
        if not _verb_ok(e["verb"]):
            continue
        en = _norm_path(e["path"])
        if en and (en.endswith(path) or path.endswith(en) or path in en or en in path):
            loose.append(e)
    return loose


def _impact_for_handler(
    repo: str, handler: dict, render_edges: list[dict], *,
    depth: int, call_depth: int, storage_path: Optional[str],
) -> dict:
    """Compose blast radius + rendered views for one handler symbol."""
    hid = handler.get("handler_id")
    br = get_blast_radius(
        repo, symbol=hid, depth=depth, call_depth=call_depth, storage_path=storage_path,
    )
    if not isinstance(br, dict) or "error" in br:
        br = {}
    views = [
        {"template": r.get("dst_name"), "file": r.get("dst_file")}
        for r in render_edges if r.get("src_id") == hid
    ]
    label = f'{handler.get("verb", "ANY")} {handler.get("path", "")}'.strip()
    return {
        "endpoint": label,
        "handler": {
            "id": hid,
            "name": handler.get("handler_name"),
            "file": handler.get("handler_file"),
        },
        "source": handler.get("source"),
        "affected_files": br.get("confirmed", []),
        "affected_file_count": br.get("confirmed_count", len(br.get("confirmed", []))),
        "callers": br.get("callers", []),
        "caller_count": br.get("caller_count", 0),
        "rendered_views": views,
    }


def get_endpoint_impact(
    repo: str,
    endpoint: Optional[str] = None,
    handler_symbol_id: Optional[str] = None,
    depth: int = 1,
    call_depth: int = 2,
    storage_path: Optional[str] = None,
) -> dict:
    """Endpoint-centric impact analysis. Read-only.

    Args:
        repo:              Repository identifier (owner/repo or just repo name).
        endpoint:          HTTP endpoint, e.g. ``"GET /users"`` or ``"/users"``
                           (verb optional). Matched against the resolved route
                           table.
        handler_symbol_id: Alternative to ``endpoint`` — analyse a handler symbol
                           directly (use when a route's full path isn't yet
                           resolvable, e.g. prefixed FastAPI/Spring routes).
        depth:             Import hops for blast radius (1 = direct importers).
        call_depth:        Call-graph hops for caller detection (0 disables).
        storage_path:      Custom storage path.

    Returns:
        ``{repo, query, matched_endpoints, impacts, _meta}`` — one ``impacts``
        entry per distinct handler. Honest empty result + hint when nothing
        matches.
    """
    try:
        owner, name = resolve_repo(repo, storage_path)
    except Exception as e:
        return {"error": str(e)}
    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)
    if index is None:
        return {"error": f"No index found for {repo!r}. Run index_folder first."}
    if not endpoint and not handler_symbol_id:
        return {"error": "Provide either 'endpoint' (e.g. 'GET /users') or 'handler_symbol_id'."}

    endpoints = _collect_endpoints(index, store, owner, name)

    if handler_symbol_id:
        matched = [e for e in endpoints if e.get("handler_id") == handler_symbol_id]
        if not matched:
            sym = next((s for s in index.symbols if s.get("id") == handler_symbol_id), None)
            if sym is None:
                return {
                    "error": f"No symbol {handler_symbol_id!r} in index.",
                    "matched_endpoints": [],
                }
            matched = [{
                "verb": "ANY", "path": "",
                "handler_id": sym.get("id"), "handler_name": sym.get("name"),
                "handler_file": sym.get("file"),
                "source": "handler_symbol_id", "resolution": "resolved",
            }]
        query = {"handler_symbol_id": handler_symbol_id}
    else:
        verb, path = _parse_endpoint_query(endpoint)
        matched = _match_endpoints(endpoints, verb, path)
        query = {"endpoint": endpoint}
        if not matched:
            return {
                "repo": f"{owner}/{name}",
                "query": query,
                "matched_endpoints": [],
                "hint": (
                    "No route matched. Resolution covers string-dispatch "
                    "(Django/Express/Flask/Rails) + decorator routes "
                    "(Flask/FastAPI/Spring local path). FastAPI APIRouter prefix "
                    "composition and Spring class-level mappings are not yet "
                    "resolved — try the handler directly via handler_symbol_id, "
                    "or query by a path suffix."
                ),
                "_meta": {"endpoints_known": len(endpoints)},
            }

    try:
        render_edges = resolve_flow_edges(index, store, owner, name, kinds=("render",))
    except Exception:  # pragma: no cover
        logger.debug("resolve_flow_edges(render) failed", exc_info=True)
        render_edges = []

    impacts: list[dict] = []
    seen_handlers: set = set()
    for e in matched:
        hid = e.get("handler_id")
        if not hid or hid in seen_handlers:
            continue
        seen_handlers.add(hid)
        impacts.append(_impact_for_handler(
            repo, e, render_edges, depth=depth, call_depth=call_depth,
            storage_path=storage_path,
        ))

    return {
        "repo": f"{owner}/{name}",
        "query": query,
        "matched_endpoints": [
            {k: e.get(k) for k in
             ("verb", "path", "handler_id", "handler_name", "handler_file", "source", "resolution")}
            for e in matched
        ],
        "impacts": impacts,
        "_meta": {"endpoints_known": len(endpoints), "handler_count": len(impacts)},
    }
