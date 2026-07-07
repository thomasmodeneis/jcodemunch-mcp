"""get_decorator_census — repo-wide census of decorators / annotations / attributes.

Answers "where is every `@app.route` / `@Injectable` / `@pytest.fixture` /
`[Serializable]` in this repo, and how many?" in one read-only call, over the
decorators the index already stores on each symbol — so it's cross-language by
construction (Python decorators, TS/Java annotations, C# attributes: whatever the
extractor captured).

Sharper than a flat string histogram: forms are **normalized** (the leading `@`,
call-arguments, and `[...]` brackets stripped) so `@app.route('/a')` and
`@app.route('/b')` count under one `app.route` bucket instead of scattering. Each
bucket keeps the distinct `raw_forms` it collapsed, a per-decorator symbol-kind
breakdown, and a file count; `include_sites` lists the exact symbols.

Pairs with the framework-aware tools (get_signal_chains / get_endpoint_impact):
this surfaces the decorator surface; those resolve what it wires together.

Read-only. Aggregation, not retrieval — it deliberately does NOT report a
tokens-saved estimate (there is no "full-file read" alternative to attribute
honestly, matching get_delivery_metrics / get_hotspots).
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from typing import Optional

from ..storage import IndexStore
from ._utils import index_status_to_tool_error, resolve_repo

logger = logging.getLogger(__name__)


def _normalize_decorator(raw: str) -> str:
    """Collapse a raw decorator string to its grouping key.

    ``@app.route('/x')`` -> ``app.route``; ``@dataclass(frozen=True)`` ->
    ``dataclass``; ``@Override`` -> ``Override``; ``[Serializable]`` ->
    ``Serializable``. Keeps the dotted path (the useful discriminator); drops the
    leading ``@``, any call arguments, and C#-style brackets.
    """
    s = (raw or "").strip()
    s = s.lstrip("@").strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1].strip()
    paren = s.find("(")
    if paren != -1:
        s = s[:paren]
    return s.strip()


def _short_raw(raw: str, cap: int = 100) -> str:
    """Flatten a raw decorator to one line and cap its length.

    Keeps the shape visible without dumping multi-line argument tables (e.g. a
    big ``@pytest.mark.parametrize([...])``).
    """
    s = " ".join((raw or "").split())
    return s if len(s) <= cap else s[:cap] + "..."


def get_decorator_census(
    repo: str,
    name_filter: Optional[str] = None,
    scope_path: Optional[str] = None,
    kind: Optional[str] = None,
    include_sites: bool = False,
    max_decorators: int = 100,
    max_sites_per: int = 50,
    storage_path: Optional[str] = None,
) -> dict:
    """Census of decorators/annotations/attributes across an indexed repo.

    Args:
        repo:           Repository identifier.
        name_filter:    Case-insensitive substring on the NORMALIZED decorator
                        name (e.g. 'route', 'fixture', 'inject'). None = all.
        scope_path:     Optional subtree prefix (matched against each symbol's
                        file path) to restrict the census to a monorepo package.
        kind:           Optional symbol-kind filter (function/method/class/...).
        include_sites:  When True, each bucket lists its decorated symbols
                        (id/name/kind/file/line/raw), capped at max_sites_per.
        max_decorators: Cap on histogram rows (default 100).
        max_sites_per:  Cap on sites listed per decorator (default 50).
        storage_path:   Optional index storage override.

    Returns:
        ``{repo, summary, decorators, _meta}`` or ``{error}``.
    """
    t0 = time.perf_counter()

    if max_decorators < 1:
        return {"error": "max_decorators must be >= 1"}
    if max_sites_per < 0:
        return {"error": "max_sites_per must be >= 0"}

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)
    if not index:
        return index_status_to_tool_error(store.inspect_index(owner, name))

    norm_scope = (scope_path or "").strip().replace("\\", "/").rstrip("/")
    nf = name_filter.lower() if name_filter else None

    buckets: dict[str, dict] = {}
    decorated_symbols = 0
    total_uses = 0
    by_language: Counter = Counter()

    for sym in index.symbols:
        decs = sym.get("decorators") or []
        if not decs:
            continue
        f = (sym.get("file", "") or "").replace("\\", "/")
        if not f:
            continue
        if norm_scope and not (f == norm_scope or f.startswith(norm_scope + "/")):
            continue
        if kind and sym.get("kind") != kind:
            continue

        counted_this_symbol = False
        for raw in decs:
            norm = _normalize_decorator(raw)
            if not norm:
                continue
            if nf and nf not in norm.lower():
                continue
            b = buckets.get(norm)
            if b is None:
                b = {"count": 0, "raw": set(), "kinds": Counter(), "files": set(), "sites": []}
                buckets[norm] = b
            b["count"] += 1
            b["raw"].add(_short_raw(raw))
            b["kinds"][sym.get("kind", "")] += 1
            b["files"].add(f)
            total_uses += 1
            by_language[sym.get("language") or "unknown"] += 1
            if include_sites and len(b["sites"]) < max_sites_per:
                b["sites"].append({
                    "id": sym.get("id"),
                    "name": sym.get("name"),
                    "kind": sym.get("kind"),
                    "file": f,
                    "line": sym.get("line"),
                    "raw": _short_raw(raw, cap=160),
                })
            counted_this_symbol = True

        if counted_this_symbol:
            decorated_symbols += 1

    ranked = sorted(buckets.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
    rows: list[dict] = []
    for norm, b in ranked[:max_decorators]:
        row = {
            "decorator": norm,
            "count": b["count"],
            "raw_forms": sorted(b["raw"])[:10],
            "symbol_kinds": dict(b["kinds"].most_common()),
            "files": len(b["files"]),
        }
        if include_sites:
            row["sites"] = b["sites"]
        rows.append(row)

    distinct = len(buckets)
    note = (
        "Histogram groups by normalized decorator name (leading @, call-args, and "
        "[] brackets stripped); raw_forms shows the distinct source variants "
        "collapsed into each bucket. Aggregates the decorators/annotations/"
        "attributes the index stored, across languages. Read-only."
    )
    if not rows:
        note = (
            "No decorated symbols matched"
            + (f" name_filter={name_filter!r}" if name_filter else "")
            + (f" scope_path={scope_path!r}" if scope_path else "")
            + (f" kind={kind!r}" if kind else "")
            + ". "
            + note
        )

    return {
        "repo": repo,
        "summary": {
            "decorated_symbols": decorated_symbols,
            "total_decorator_uses": total_uses,
            "distinct_decorators": distinct,
            "by_language": dict(by_language.most_common()),
        },
        "decorators": rows,
        "_meta": {
            "timing_ms": round((time.perf_counter() - t0) * 1000, 1),
            "name_filter": name_filter,
            "scope_path": scope_path,
            "kind": kind,
            "include_sites": include_sites,
            "decorators_truncated": distinct > max_decorators,
            "note": note,
        },
    }
