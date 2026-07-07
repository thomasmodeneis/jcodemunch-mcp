"""`jcodemunch-mcp parity` — migration parity between two symbol trees.

Thin CLI over ``tools.get_parity_map``: reports, for each source symbol, whether
an equivalent counterpart exists in the target (ported), exists but drifted
(ported_diverged), is missing (unported), is a possible intentional drop
(orphaned), or is target-only (added) — plus a dependency-ordered port plan for
what's left. Read-only and plan-only: it never ports anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional


def _resolve_repo_arg(repo_arg: str, storage_path: Optional[str]) -> str:
    """Path-like argument -> indexed repo id (digest/receipt ergonomics)."""
    if repo_arg in (".", "..") or "/" in repo_arg or "\\" in repo_arg:
        path = Path(repo_arg).resolve()
        if path.exists():
            try:
                from ..tools.resolve_repo import resolve_repo as _resolve
                resolved = _resolve(str(path), storage_path)
                if resolved.get("indexed") and resolved.get("repo"):
                    return resolved["repo"]
            except Exception as e:  # noqa: BLE001
                print(f"error resolving '{repo_arg}': {e}", file=sys.stderr)
    return repo_arg


def _render_human(out: dict) -> str:
    if out.get("error"):
        return f"error: {out['error']}"

    src = out.get("source", {})
    tgt = out.get("target", {})
    summ = out.get("summary", {})
    lines: list[str] = []
    lines.append(
        f"# Parity — {src.get('repo')}{('/' + src.get('path')) if src.get('path') else ''} "
        f"-> {tgt.get('repo')}{('/' + tgt.get('path')) if tgt.get('path') else ''}"
    )
    lines.append("")
    lines.append(f"  parity           {summ.get('parity_pct', 0)}%  (estimate)")
    lines.append(f"  ported           {summ.get('ported', 0)}")
    lines.append(f"  ported-diverged  {summ.get('ported_diverged', 0)}  (counterpart drifted)")
    lines.append(f"  unported         {summ.get('unported', 0)}")
    lines.append(f"  orphaned         {summ.get('orphaned', 0)}  (no migrated caller)")
    lines.append(f"  added            {summ.get('added', 0)}  (target-only)")
    lines.append(f"  source symbols   {src.get('symbol_count', 0)}")
    lines.append(f"  target symbols   {tgt.get('symbol_count', 0)}")

    diverged = [s for s in out.get("symbols", []) if s.get("status") == "ported_diverged"]
    if diverged:
        lines.append("")
        lines.append("  diverged (looks ported, isn't equivalent):")
        for s in diverged[:15]:
            m = s.get("match") or {}
            lines.append(f"    {s.get('qualified_name')} -> {m.get('target_name')} "
                         f"[{m.get('match_basis')}]")

    plan = out.get("port_plan") or []
    if plan:
        ready = [p for p in plan if p.get("unblocked")]
        lines.append("")
        lines.append(f"  port plan: {len(plan)} unported, {len(ready)} unblocked now")
        for p in plan[:15]:
            mark = "*" if p.get("unblocked") else " "
            blockers = p.get("blocking_deps") or []
            btxt = "" if not blockers else f"  waits on: {', '.join(blockers[:4])}"
            scc = "" if p.get("scc_group") is None else f"  [cycle {p['scc_group']}]"
            lines.append(f"    {mark} [{p.get('order_index')}] {p.get('name')}{scc}{btxt}")
        if len(plan) > 15:
            lines.append(f"    ... {len(plan) - 15} more")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Map migration parity between two symbol trees, with a port plan.",
    )
    parser.add_argument("source",
        help="Source repo id (ported FROM); a path, owner/name, or bare display name.")
    parser.add_argument("target",
        help="Target repo id (ported TO); may equal source when comparing two subpaths.")
    parser.add_argument("--source-path", default=None,
        help="Optional subtree within the source repo (file-path prefix).")
    parser.add_argument("--target-path", default=None,
        help="Optional subtree within the target repo (file-path prefix).")
    parser.add_argument("--match-threshold", type=float, default=0.75,
        help="Similarity floor (0-1) for rename matching (default 0.75).")
    parser.add_argument("--divergence", default="signature",
        choices=["signature", "signature+body", "name_only"],
        help="Divergence policy (default 'signature').")
    parser.add_argument("--no-rename", action="store_true",
        help="Disable rename matching (exact-name only).")
    parser.add_argument("--no-port-plan", action="store_true",
        help="Skip the dependency-ordered port plan.")
    parser.add_argument("--json", action="store_true",
        help="Emit the structured payload as JSON instead of the human report.")
    parser.add_argument("--storage-path", default=None,
        help="Override index storage location.")
    args = parser.parse_args(argv)

    source = _resolve_repo_arg(args.source, args.storage_path)
    target = _resolve_repo_arg(args.target, args.storage_path)

    from ..tools.get_parity_map import get_parity_map
    out = get_parity_map(
        source_repo=source,
        target_repo=target,
        source_path=args.source_path,
        target_path=args.target_path,
        match_threshold=args.match_threshold,
        divergence=args.divergence,
        rename=not args.no_rename,
        include_port_plan=not args.no_port_plan,
        storage_path=args.storage_path,
    )

    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        text = _render_human(out)
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.write(text.encode(enc, errors="backslashreplace").decode(enc))
        sys.stdout.write("\n")
    return 0 if not out.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())
