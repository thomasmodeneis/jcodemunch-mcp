"""v1.108.104 — close the index_dependency gap in the counter's state-change gate.

PR #361 (readOnlyHint annotations) surfaced that `index_dependency` — a real
write tool (dependency snapshot + reindex) — was missing from
`counter.STATE_CHANGING_ACTIONS`. That set gates the counter's `order`
dispatcher, so `order("index_dependency", ...)` ran without the
`allow_state_change=true` opt-in the read-only front door requires. The PR
compensated in the annotation layer; this closes the gap at the source so the
`order` gate and the annotations derive from one list.
"""

from __future__ import annotations

import jcodemunch_mcp.server as server
from jcodemunch_mcp import counter


def test_index_dependency_is_state_changing():
    assert counter.is_state_changing("index_dependency") is True


def test_order_gate_blocks_index_dependency_without_optin():
    catalog = server._catalog_names()
    assert "index_dependency" in catalog  # reachable via order
    reason = counter.order_gate("index_dependency", catalog, allow_state_change=False)
    assert reason is not None
    assert "allow_state_change=true" in reason


def test_order_gate_allows_index_dependency_with_optin():
    catalog = server._catalog_names()
    reason = counter.order_gate("index_dependency", catalog, allow_state_change=True)
    assert reason is None


def test_annotation_writeset_no_longer_special_cases_index_dependency():
    # index_dependency is marked mutating purely because it now lives in the
    # authoritative set, not via a hand-added name in server._NON_READONLY_TOOLS.
    assert "index_dependency" in counter.STATE_CHANGING_ACTIONS
    assert "index_dependency" not in server._ANNOTATION_ONLY_WRITERS
    # The annotation write-set is the authoritative state-changing set + the counter
    # front door + the dual-mode annotation-only writers (v1.108.110). index_dependency
    # must ride in via STATE_CHANGING_ACTIONS, never the annotation-only escape hatch.
    assert server._NON_READONLY_TOOLS == (
        counter.STATE_CHANGING_ACTIONS | {"order", "route"} | server._ANNOTATION_ONLY_WRITERS
    )
