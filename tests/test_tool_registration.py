"""Consolidated tool-registration consistency guard.

Adding a tool touches several surfaces: the builder (`_build_tools_list`), the
dispatch, `_CANONICAL_TOOL_NAMES`, the config tier bundles, and the categorized
`claude-md --generate` snippet (`_SNIPPET_TOOL_CATEGORIES`). Those last two are
hand-maintained lists that silently drift, and the failure is whack-a-mole —
the snippet check can't even fail until the canonical list is complete, so a
missing entry surfaces one CI round at a time.

This single test enumerates every surface and reports ALL gaps at once, so the
next tool addition fails here, immediately, with the complete list of what's
missing where — instead of three separate red runs.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from jcodemunch_mcp import config as config_module
from jcodemunch_mcp.server import (
    _CANONICAL_TOOL_NAMES,
    _SNIPPET_TOOL_CATEGORIES,
    _build_tools_list,
)


@pytest.fixture
def _restore_config():
    orig = config_module._GLOBAL_CONFIG.copy()
    yield
    config_module._GLOBAL_CONFIG.clear()
    config_module._GLOBAL_CONFIG.update(orig)


def _snippet_tools() -> set[str]:
    return {t for _, lst in _SNIPPET_TOOL_CATEGORIES for t in lst}


def _full_built_names() -> set[str]:
    """Every tool the builder can emit: full profile, nothing disabled, front
    door hidden (counter surface is a separate mode, not a real catalog tool)."""
    cfg = deepcopy(config_module.DEFAULTS)
    cfg["tool_profile"] = "full"
    cfg["disabled_tools"] = []
    cfg["compact_schemas"] = False
    cfg["languages"] = None  # keep search_columns in the list (SQL gate off)
    config_module._GLOBAL_CONFIG.clear()
    config_module._GLOBAL_CONFIG.update(cfg)
    return {t.name for t in _build_tools_list()}


def test_every_built_tool_is_registered_on_all_surfaces(_restore_config):
    built = _full_built_names()
    canonical = set(_CANONICAL_TOOL_NAMES)
    snippet = _snippet_tools()

    gaps: list[str] = []
    for tool in sorted(built):
        missing = []
        if tool not in canonical:
            missing.append("_CANONICAL_TOOL_NAMES")
        if tool not in snippet:
            missing.append("_SNIPPET_TOOL_CATEGORIES (claude-md snippet)")
        if missing:
            gaps.append(f"  {tool}: missing from {', '.join(missing)}")

    assert not gaps, (
        "Tool(s) emitted by _build_tools_list() are not registered on every "
        "surface. Add each to the listed structure(s):\n" + "\n".join(gaps)
    )


def test_canonical_and_snippet_lists_agree():
    """The two hand-maintained static surfaces must be identical — neither may
    list a tool the other omits."""
    canonical = set(_CANONICAL_TOOL_NAMES)
    snippet = _snippet_tools()
    only_canonical = canonical - snippet
    only_snippet = snippet - canonical
    assert not only_canonical and not only_snippet, (
        f"_CANONICAL_TOOL_NAMES and _SNIPPET_TOOL_CATEGORIES disagree.\n"
        f"  only in canonical: {sorted(only_canonical)}\n"
        f"  only in snippet:   {sorted(only_snippet)}"
    )


def test_no_duplicate_entries_in_static_lists():
    """A copy-paste into the wrong category shouldn't double-list a tool."""
    canonical = list(_CANONICAL_TOOL_NAMES)
    snippet = [t for _, lst in _SNIPPET_TOOL_CATEGORIES for t in lst]
    assert len(canonical) == len(set(canonical)), "duplicate in _CANONICAL_TOOL_NAMES"
    assert len(snippet) == len(set(snippet)), "duplicate in _SNIPPET_TOOL_CATEGORIES"
