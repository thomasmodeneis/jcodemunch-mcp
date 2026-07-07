"""get_parity_map — correspondence-aware migration parity between two symbol trees.

Answers the two questions a port/migration keeps re-deriving by hand:

  1. What's left?  For each symbol in the SOURCE scope, is there an equivalent
     counterpart in the TARGET scope (ported), none (unported), or one that exists
     but has silently drifted (ported_diverged)?  The diverged verdict is the
     headline: it's the failure a name-only "does it exist in both trees" check
     reports as done.
  2. What next?  The remaining unported symbols are ordered by the source
     dependency graph so you never port a symbol before the things it calls;
     cycles are grouped so you port them together.

Sharper than a presence check on two counts:
  * Rename-aware — a ported-and-renamed symbol (``getUserById`` -> ``fetch_user``)
    is matched via the structural+behavioral similarity signals, not reported as a
    false unported+added pair.
  * Divergence-aware — a matched counterpart whose signature (or body) changed is
    surfaced, not smoothed over.

Read-only and plan-only: this MAPS the migration and ORDERS the work. It never
edits, moves, or ports anything — that stays the caller's keystroke.

Honesty gates: ``parity_pct`` is a labelled estimate; rename matches carry a
confidence; degenerate inputs (same scope twice, an unindexed/empty scope) return
an honest error, never a fabricated 100%.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from ..storage import IndexStore
from ._utils import index_status_to_tool_error, resolve_repo
from .find_similar_symbols import (
    _byte_ratio,
    _callee_set,
    _is_test_file,
    _jaccard,
    _looks_generated,
    _signature_tokens,
)
from .get_dependency_cycles import _find_cycles

logger = logging.getLogger(__name__)

# Similarity blend for rename detection (structural + behavioral; no embedding
# dependency so P1 is deterministic and test-stable). Mirrors the non-embedding
# signals find_similar_symbols uses.
_SIG_WEIGHT = 0.50
_CALLEE_WEIGHT = 0.35
_SIZE_WEIGHT = 0.15

# Bound the rename pass: skip it (fall back to exact-only) past this many
# candidate comparisons and say so, rather than blow up on huge trees.
_RENAME_PAIR_BUDGET = 200_000

_DEFAULT_KINDS = frozenset({"function", "method", "class"})
_VALID_DIVERGENCE = frozenset({"signature", "signature+body", "name_only"})


def _qualname(sym: dict, id_to_name: dict[str, str]) -> str:
    """One-level container-qualified name for collision disambiguation.

    ``save`` on two different classes becomes ``ClassA.save`` / ``ClassB.save``.
    """
    name = sym.get("name", "") or ""
    parent = sym.get("parent")
    if parent and parent in id_to_name:
        return f"{id_to_name[parent]}.{name}"
    return name


def _collect_scope(
    index,
    path: Optional[str],
    kinds: frozenset,
) -> list[dict]:
    """Return the index's symbols under *path*, filtered to *kinds*.

    *path* is a subtree prefix (or exact file) matched against ``sym['file']``;
    None/'' means the whole index. Tests and generated files are skipped so the
    map reflects hand-written surface, matching find_similar_symbols.
    """
    norm = (path or "").strip().replace("\\", "/").rstrip("/")
    out: list[dict] = []
    for sym in index.symbols:
        if sym.get("kind") not in kinds:
            continue
        f = (sym.get("file", "") or "").replace("\\", "/")
        if not f:
            continue
        if norm and not (f == norm or f.startswith(norm + "/")):
            continue
        if _is_test_file(f) or _looks_generated(f):
            continue
        # Shallow copy so the added `_qual` key never mutates the cached index.
        out.append(dict(sym))
    return out


def _id_name_map(index) -> dict[str, str]:
    return {s.get("id"): s.get("name", "") for s in index.symbols if s.get("id")}


def _similarity(a: dict, b: dict) -> float:
    """Structural+behavioral similarity in [0, 1] between two symbols.

    Signature-token Jaccard + callee-name Jaccard + byte-length ratio, weighted.
    Signals with no evidence on either side are dropped and the weight
    redistributed, so a tiny signature-less pair isn't penalised to zero.
    """
    signals: list[tuple[float, float]] = []  # (value, weight)

    sa, sb = _signature_tokens(a), _signature_tokens(b)
    if sa or sb:
        signals.append((_jaccard(sa, sb), _SIG_WEIGHT))

    ca, cb = _callee_set(a), _callee_set(b)
    if ca or cb:
        signals.append((_jaccard(ca, cb), _CALLEE_WEIGHT))

    la = int(a.get("byte_length", 0) or 0)
    lb = int(b.get("byte_length", 0) or 0)
    if la or lb:
        signals.append((_byte_ratio(la, lb), _SIZE_WEIGHT))

    if not signals:
        return 0.0
    total_w = sum(w for _, w in signals)
    return sum(v * w for v, w in signals) / total_w if total_w else 0.0


def _diverged(src: dict, tgt: dict, policy: str) -> dict:
    """Divergence breakdown between a source symbol and its matched counterpart.

    ``signature_changed`` compares parameter shape (param_count) and the
    name-stripped signature-token bags, so a rename alone is NOT divergence.
    ``body_changed`` compares content_hash and is only consulted under the
    ``signature+body`` policy.
    """
    result = {"signature_changed": False, "body_changed": False}
    if policy == "name_only":
        return result

    pc = int(src.get("param_count", 0) or 0)
    pt = int(tgt.get("param_count", 0) or 0)
    # Strip both symbols' own names from the token bags so a pure rename doesn't
    # read as a signature change.
    names = {(src.get("name") or "").lower(), (tgt.get("name") or "").lower()}
    ts = {t for t in _signature_tokens(src) if t not in names}
    tt = {t for t in _signature_tokens(tgt) if t not in names}
    sig_changed = (pc != pt) or ((ts or tt) and _jaccard(ts, tt) < 0.9)
    result["signature_changed"] = bool(sig_changed)

    if policy == "signature+body":
        hs = src.get("content_hash")
        ht = tgt.get("content_hash")
        result["body_changed"] = bool(hs and ht and hs != ht)
    return result


def _is_diverged(div: dict, policy: str) -> bool:
    if policy == "name_only":
        return False
    if policy == "signature+body":
        return div["signature_changed"] or div["body_changed"]
    return div["signature_changed"]


def _build_port_plan(unported: list[dict]) -> list[dict]:
    """Dependency-ordered plan over the unported symbols.

    Edge A->B when A calls B and B is also unported. Cycles are collapsed to an
    ``scc_group`` (ported together). Order = topological level over the
    cycle-condensed graph, so leaves (nothing unported to wait on) come first.
    """
    by_qual = {s["_qual"]: s for s in unported}
    name_to_quals: dict[str, list[str]] = {}
    for s in unported:
        name_to_quals.setdefault((s.get("name") or "").lower(), []).append(s["_qual"])

    # adjacency: qual -> set of unported quals it depends on (calls)
    adj: dict[str, set[str]] = {q: set() for q in by_qual}
    for s in unported:
        for callee in _callee_set(s):
            for tq in name_to_quals.get(callee, []):
                if tq != s["_qual"]:
                    adj[s["_qual"]].add(tq)

    # SCC grouping (Kosaraju, size > 1) over the same adjacency.
    sccs = _find_cycles({k: sorted(v) for k, v in adj.items()})
    scc_of: dict[str, int] = {}
    for i, comp in enumerate(sccs):
        for member in comp:
            if member in by_qual:
                scc_of[member] = i

    # Condense to units (each SCC is one unit; every other symbol its own unit).
    unit_of: dict[str, str] = {}
    units: dict[str, set[str]] = {}
    for q in by_qual:
        uid = f"scc{scc_of[q]}" if q in scc_of else q
        unit_of[q] = uid
        units.setdefault(uid, set()).add(q)

    unit_deps: dict[str, set[str]] = {}
    for uid, members in units.items():
        deps: set[str] = set()
        for q in members:
            for d in adj[q]:
                du = unit_of.get(d)
                if du and du != uid:
                    deps.add(du)
        unit_deps[uid] = deps

    # Kahn over the condensed DAG -> topological level per unit.
    order: dict[str, int] = {}
    emitted_units: set[str] = set()
    remaining = set(units)
    level = 0
    while remaining:
        ready = [u for u in remaining if unit_deps[u] <= emitted_units]
        if not ready:  # residual cross-unit cycle (defensive) — emit the rest
            ready = list(remaining)
        for u in sorted(ready):
            for q in units[u]:
                order[q] = level
            emitted_units.add(u)
            remaining.discard(u)
        level += 1

    plan: list[dict] = []
    for s in unported:
        q = s["_qual"]
        # blocking deps = unported symbols this one calls, excluding same-cycle peers
        blocking = sorted(
            {
                by_qual[d].get("name", d)
                for d in adj[q]
                if scc_of.get(d) is None or scc_of.get(d) != scc_of.get(q)
            }
        )
        entry = {
            "name": s.get("name"),
            "qualified_name": q,
            "file": s.get("file"),
            "order_index": order[q],
            "unblocked": len(blocking) == 0,
            "blocking_deps": blocking,
            "scc_group": scc_of.get(q),
        }
        plan.append(entry)
    plan.sort(key=lambda e: (e["order_index"], str(e["name"])))
    return plan


def get_parity_map(
    source_repo: str,
    target_repo: str,
    source_path: Optional[str] = None,
    target_path: Optional[str] = None,
    match_threshold: float = 0.75,
    divergence: str = "signature",
    rename: bool = True,
    include_port_plan: bool = True,
    storage_path: Optional[str] = None,
) -> dict:
    """Map migration parity between a source and target symbol tree.

    Args:
        source_repo:  Repo id of the tree being ported FROM.
        target_repo:  Repo id of the tree being ported TO (may equal source_repo).
        source_path:  Optional subtree within source_repo (prefix of file paths).
        target_path:  Optional subtree within target_repo.
        match_threshold: Similarity floor for rename matching (0-1, default 0.75).
        divergence:   'signature' (default) | 'signature+body' | 'name_only'.
        rename:       When True (default), unmatched source symbols are matched to
                      target symbols by structural+behavioral similarity (rename
                      detection). Auto-disabled with a note past the pair budget.
        include_port_plan: Emit the dependency-ordered plan over unported symbols.
        storage_path: Optional index storage override.

    Returns:
        ``{source, target, summary, symbols, port_plan, parity_axes, _meta}`` or
        ``{error}``.
    """
    t0 = time.perf_counter()

    if not (0.0 <= match_threshold <= 1.0):
        return {"error": "match_threshold must be in [0.0, 1.0]"}
    if divergence not in _VALID_DIVERGENCE:
        return {"error": f"divergence must be one of {sorted(_VALID_DIVERGENCE)}"}

    same_repo = source_repo == target_repo
    src_norm = (source_path or "").strip().replace("\\", "/").rstrip("/")
    tgt_norm = (target_path or "").strip().replace("\\", "/").rstrip("/")
    if same_repo and src_norm == tgt_norm:
        return {
            "error": (
                "source and target scope are identical; give two different repos "
                "or two different paths to compare."
            )
        }

    try:
        s_owner, s_name = resolve_repo(source_repo, storage_path)
    except ValueError as e:
        return {"error": f"source_repo: {e}"}
    try:
        t_owner, t_name = resolve_repo(target_repo, storage_path)
    except ValueError as e:
        return {"error": f"target_repo: {e}"}

    store = IndexStore(base_path=storage_path)
    s_index = store.load_index(s_owner, s_name)
    if not s_index:
        return index_status_to_tool_error(store.inspect_index(s_owner, s_name))
    if same_repo:
        t_index = s_index
    else:
        t_index = store.load_index(t_owner, t_name)
        if not t_index:
            return index_status_to_tool_error(store.inspect_index(t_owner, t_name))

    kinds = _DEFAULT_KINDS
    source_syms = _collect_scope(s_index, source_path, kinds)
    target_syms = _collect_scope(t_index, target_path, kinds)

    if not source_syms:
        return {
            "error": (
                f"No source symbols under {source_path or '(repo root)'!r} in "
                f"{source_repo!r}. Nothing to map."
            )
        }

    s_id_names = _id_name_map(s_index)
    t_id_names = _id_name_map(t_index)
    for s in source_syms:
        s["_qual"] = _qualname(s, s_id_names)
    for s in target_syms:
        s["_qual"] = _qualname(s, t_id_names)

    # Target lookup by qualified name (first wins on the rare collision).
    target_by_qual: dict[str, dict] = {}
    for s in target_syms:
        target_by_qual.setdefault(s["_qual"], s)

    matched_target_ids: set[str] = set()
    records: list[dict] = []  # per source symbol

    # ---- Pass 1: exact qualified-name match ---------------------------------
    unmatched_source: list[dict] = []
    for s in source_syms:
        tgt = target_by_qual.get(s["_qual"])
        if tgt is not None and tgt.get("id") not in matched_target_ids:
            matched_target_ids.add(tgt.get("id"))
            records.append({"src": s, "tgt": tgt, "basis": "exact_name", "confidence": 1.0})
        else:
            unmatched_source.append(s)

    # ---- Pass 2: rename matching (structural+behavioral similarity) ---------
    rename_disabled_reason = None
    if rename and unmatched_source:
        free_targets = [t for t in target_syms if t.get("id") not in matched_target_ids]
        # Same-kind candidates only; bound the total comparison count.
        pair_estimate = len(unmatched_source) * max(1, len(free_targets))
        if pair_estimate > _RENAME_PAIR_BUDGET:
            rename_disabled_reason = (
                f"rename matching skipped: {pair_estimate} candidate comparisons "
                f"exceed the {_RENAME_PAIR_BUDGET} budget (scope your paths to enable it)"
            )
        else:
            targets_by_kind: dict[str, list[dict]] = {}
            for t in free_targets:
                targets_by_kind.setdefault(t.get("kind"), []).append(t)
            still_unmatched: list[dict] = []
            for s in unmatched_source:
                best, best_sim = None, 0.0
                for t in targets_by_kind.get(s.get("kind"), []):
                    if t.get("id") in matched_target_ids:
                        continue
                    sim = _similarity(s, t)
                    if sim > best_sim:
                        best, best_sim = t, sim
                if best is not None and best_sim >= match_threshold:
                    matched_target_ids.add(best.get("id"))
                    records.append(
                        {"src": s, "tgt": best, "basis": "renamed_similar",
                         "confidence": round(best_sim, 3)}
                    )
                else:
                    still_unmatched.append(s)
            unmatched_source = still_unmatched

    # ---- Classify -----------------------------------------------------------
    # Which source symbols got a counterpart (for orphan detection).
    ported_names_lower = {(r["src"].get("name") or "").lower() for r in records}

    symbols_out: list[dict] = []
    ported = diverged = unported = orphaned = 0
    pending_syms: list[dict] = []  # all unmatched source (unported + orphaned)

    for r in records:
        s, tgt = r["src"], r["tgt"]
        div = _diverged(s, tgt, divergence)
        is_div = _is_diverged(div, divergence)
        status = "ported_diverged" if is_div else "ported"
        if is_div:
            diverged += 1
        else:
            ported += 1
        entry = {
            "name": s.get("name"),
            "qualified_name": s["_qual"],
            "kind": s.get("kind"),
            "source_file": s.get("file"),
            "status": status,
            "match": {
                "target_name": tgt.get("name"),
                "target_file": tgt.get("file"),
                "match_basis": r["basis"],
                "confidence": r["confidence"],
            },
        }
        if divergence != "name_only":
            entry["divergence"] = div
        symbols_out.append(entry)

    # Unmatched source symbols are all pending work (=> the port plan). Split the
    # label: `orphaned` = nothing else in the source scope calls it (an entry point
    # or a leftover the migration may be silently dropping — worth a look);
    # `unported` = still referenced by other source code, so clearly part of the
    # pending graph. Both go into the port plan.
    source_referenced: set[str] = set()
    for s in source_syms:
        source_referenced |= _callee_set(s)
    for s in unmatched_source:
        pending_syms.append(s)
        referenced = (s.get("name") or "").lower() in source_referenced
        if referenced:
            status = "unported"
            unported += 1
        else:
            status = "orphaned"
            orphaned += 1
        symbols_out.append({
            "name": s.get("name"),
            "qualified_name": s["_qual"],
            "kind": s.get("kind"),
            "source_file": s.get("file"),
            "status": status,
            "match": None,
        })

    # Target-only symbols => added surface.
    added = 0
    for t in target_syms:
        if t.get("id") not in matched_target_ids:
            added += 1

    denom = ported + diverged + unported + orphaned
    parity_pct = round(100.0 * ported / denom, 1) if denom else 100.0

    port_plan: list[dict] = []
    if include_port_plan and pending_syms:
        port_plan = _build_port_plan(pending_syms)

    honest_note = (
        "parity_pct = ported / (ported + ported_diverged + unported + orphaned); "
        "estimate. orphaned = unmatched source with no in-scope caller (entry point "
        "or a possible intentional drop); unported = still referenced by source code. "
        "added = target-only surface (excluded from parity). Port plan spans all "
        "unmatched source symbols."
    )
    if rename_disabled_reason:
        honest_note += f" {rename_disabled_reason}."

    return {
        "source": {"repo": source_repo, "path": source_path, "symbol_count": len(source_syms)},
        "target": {"repo": target_repo, "path": target_path, "symbol_count": len(target_syms)},
        "summary": {
            "parity_pct": parity_pct,
            "ported": ported,
            "ported_diverged": diverged,
            "unported": unported,
            "added": added,
            "orphaned": orphaned,
            "estimate": True,
        },
        "symbols": symbols_out,
        "port_plan": port_plan,
        "parity_axes": {},  # reserved: P3 jdoc doc-parity / jdata schema-parity
        "_meta": {
            "timing_ms": round((time.perf_counter() - t0) * 1000, 1),
            "match_threshold": match_threshold,
            "divergence": divergence,
            "rename": rename and rename_disabled_reason is None,
            "kinds": sorted(kinds),
            "honest_note": honest_note,
        },
    }
