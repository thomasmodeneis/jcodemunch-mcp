"""v1.108.70 — bounded-source mode for get_symbol_source (#340).

Covers the contract from the issue's acceptance comment:

1. default (no bounds) is byte-for-byte the full-source response;
2. max_source_lines / max_source_bytes bound the body and author truncation
   metadata;
3. explicit absolute line ranges clamp to the symbol body;
4. batch max_total_source_bytes returns partial entries instead of dropping
   oversized symbols;
5. verify=True still verifies the full indexed body while the returned source
   is clearly marked a bounded view;
6. context_lines cannot silently defeat the bound (rejected).

Pure-logic cases exercise the slicing / byte arithmetic directly; integration
cases run the real index → get_symbol_source path.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from jcodemunch_mcp.tools.get_symbol import (
    _bound_source,
    _utf8_safe_truncate,
    get_symbol_source,
)


# --------------------------------------------------------------------------- #
# Pure-logic unit tests                                                        #
# --------------------------------------------------------------------------- #

# A symbol spanning absolute file lines 10..19 (10 lines: "line0".."line9").
_SRC = "\n".join(f"line{i}" for i in range(10))
_LINE = 10
_END = 19


def _bound(**kw):
    base = dict(
        source=_SRC,
        symbol_line=_LINE,
        symbol_end_line=_END,
        source_start_line=None,
        source_end_line=None,
        max_source_lines=None,
        max_source_bytes=None,
        remaining_total_bytes=None,
    )
    base.update(kw)
    return _bound_source(**base)


class TestUtf8SafeTruncate:
    def test_returns_full_when_under_cap(self):
        assert _utf8_safe_truncate("hello", 100) == "hello"

    def test_zero_bytes_is_empty(self):
        assert _utf8_safe_truncate("hello", 0) == ""

    def test_never_splits_a_multibyte_char(self):
        # "é" is two UTF-8 bytes; a 2-byte cap that lands mid-char drops it.
        text = "héllo"  # h(1) é(2) l(1) l(1) o(1) = 6 bytes
        assert _utf8_safe_truncate(text, 2) == "h"
        assert _utf8_safe_truncate(text, 3) == "hé"
        # Result is always valid UTF-8 and within the cap.
        for cap in range(0, 8):
            out = _utf8_safe_truncate(text, cap)
            assert len(out.encode("utf-8")) <= cap


class TestBoundSourceLogic:
    def test_no_bounds_returns_full_untruncated(self):
        b = _bound()
        assert b["text"] == _SRC
        assert b["truncated"] is False
        assert b["reason"] is None
        assert b["range"] == {"start_line": 10, "end_line": 19}
        assert b["total_range"] == {"start_line": 10, "end_line": 19}
        assert b["total_lines"] == 10

    def test_max_source_lines_keeps_first_n(self):
        b = _bound(max_source_lines=3)
        assert b["text"] == "line0\nline1\nline2"
        assert b["truncated"] is True
        assert b["reason"] == "max_source_lines"
        assert b["range"] == {"start_line": 10, "end_line": 12}
        assert b["total_lines"] == 10

    def test_explicit_range_clamps_and_slices(self):
        b = _bound(source_start_line=12, source_end_line=14)
        assert b["text"] == "line2\nline3\nline4"
        assert b["reason"] == "source_range"
        assert b["range"] == {"start_line": 12, "end_line": 14}

    def test_range_covering_whole_symbol_is_not_truncated(self):
        # Below-start clamps to the body start, above-end clamps to the end.
        b = _bound(source_start_line=1, source_end_line=999)
        assert b["text"] == _SRC
        assert b["truncated"] is False
        assert b["reason"] is None

    def test_open_ended_start_runs_to_symbol_end(self):
        b = _bound(source_start_line=18)  # abs 18 -> rel 8 -> "line8","line9"
        assert b["text"] == "line8\nline9"
        assert b["range"] == {"start_line": 18, "end_line": 19}
        assert b["reason"] == "source_range"

    def test_max_source_bytes_is_utf8_safe_and_capped(self):
        b = _bound(max_source_bytes=11)  # "line0\nline1" == 11 bytes
        assert len(b["text"].encode("utf-8")) <= 11
        assert b["truncated"] is True
        assert b["reason"] == "max_source_bytes"

    def test_batch_remaining_total_bytes_caps_text(self):
        b = _bound(remaining_total_bytes=8)
        assert len(b["text"].encode("utf-8")) <= 8
        assert b["truncated"] is True
        assert b["reason"] == "max_total_source_bytes"

    def test_tighter_later_bound_wins_the_reason(self):
        # Line cap then a tighter byte cap: the byte cap is the binding reason.
        b = _bound(max_source_lines=5, max_source_bytes=8)
        assert len(b["text"].encode("utf-8")) <= 8
        assert b["reason"] == "max_source_bytes"

    def test_total_bytes_overrides_per_symbol_byte_reason(self):
        b = _bound(max_source_bytes=20, remaining_total_bytes=6)
        assert len(b["text"].encode("utf-8")) <= 6
        assert b["reason"] == "max_total_source_bytes"


# --------------------------------------------------------------------------- #
# Integration tests over a real index                                          #
# --------------------------------------------------------------------------- #

def _find_symbol_id(tmp_path: Path, repo: str, name: str) -> str:
    from jcodemunch_mcp.storage import IndexStore

    owner, repo_name = repo.split("/", 1)
    store = IndexStore(base_path=str(tmp_path))
    db_path = store._sqlite._db_path(owner, repo_name)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id FROM symbols WHERE name = ?", (name,)).fetchone()
    conn.close()
    assert row, f"symbol {name!r} not indexed"
    return row["id"]


@pytest.fixture
def indexed(tmp_path):
    """Index a file holding two multi-line functions; return (repo, ids)."""
    big_body = "\n".join(f"    x{i} = {i}" for i in range(30))
    other_body = "\n".join(f"    y{i} = {i}" for i in range(30))
    (tmp_path / "big.py").write_text(
        f"def big_function():\n{big_body}\n    return x0\n\n"
        f"def other_function():\n{other_body}\n    return y0\n",
        encoding="utf-8",
    )
    from jcodemunch_mcp.tools.index_folder import index_folder

    result = index_folder(path=str(tmp_path), use_ai_summaries=False, storage_path=str(tmp_path))
    assert result.get("success")
    repo = result["repo"]
    return {
        "repo": repo,
        "storage": str(tmp_path),
        "big": _find_symbol_id(tmp_path, repo, "big_function"),
        "other": _find_symbol_id(tmp_path, repo, "other_function"),
    }


class TestBoundedSourceIntegration:
    def test_default_is_unchanged(self, indexed):
        out = get_symbol_source(
            repo=indexed["repo"], symbol_id=indexed["big"], storage_path=indexed["storage"]
        )
        assert out.get("error") is None
        # Full body present, no bounded-mode fields leaked into the default path.
        assert "x29 = 29" in out["source"]
        for k in ("source_truncated", "source_range", "source_is_bounded_view",
                  "source_total_lines", "source_total_bytes", "source_truncated_reason"):
            assert k not in out

    def test_max_source_lines_bounds_and_labels(self, indexed):
        out = get_symbol_source(
            repo=indexed["repo"], symbol_id=indexed["big"],
            storage_path=indexed["storage"], max_source_lines=3,
        )
        assert out["source_truncated"] is True
        assert out["source_is_bounded_view"] is True
        assert out["source_truncated_reason"] == "max_source_lines"
        assert out["source"].count("\n") == 2  # exactly 3 lines
        assert "x29 = 29" not in out["source"]  # tail dropped
        # Absolute line frame matches `line`/`end_line`.
        assert out["source_range"]["start_line"] == out["line"]
        assert out["source_total_range"]["start_line"] == out["line"]
        assert out["source_total_range"]["end_line"] == out["end_line"]
        assert out["source_total_lines"] >= 30

    def test_explicit_range_clamps_to_symbol_body(self, indexed):
        # Range entirely covering the symbol → not truncated.
        wide = get_symbol_source(
            repo=indexed["repo"], symbol_id=indexed["big"], storage_path=indexed["storage"],
            source_start_line=1, source_end_line=10_000,
        )
        assert wide["source_truncated"] is False
        assert "x29 = 29" in wide["source"]

        # A narrow window inside the body → a clamped slice.
        line = wide["line"]
        narrow = get_symbol_source(
            repo=indexed["repo"], symbol_id=indexed["big"], storage_path=indexed["storage"],
            source_start_line=line + 1, source_end_line=line + 3,
        )
        assert narrow["source_truncated"] is True
        assert narrow["source_truncated_reason"] == "source_range"
        assert narrow["source_range"]["start_line"] == line + 1
        assert narrow["source_range"]["end_line"] == line + 3

    def test_verify_still_checks_full_body(self, indexed):
        out = get_symbol_source(
            repo=indexed["repo"], symbol_id=indexed["big"], storage_path=indexed["storage"],
            verify=True, max_source_lines=2,
        )
        # Hash check is against the full indexed body, so it passes...
        assert out["content_verified"] is True
        # ...while the returned source is explicitly a bounded view.
        assert out["source_is_bounded_view"] is True
        assert out["source"].count("\n") == 1  # 2 lines only

    def test_context_lines_with_bound_is_rejected(self, indexed):
        out = get_symbol_source(
            repo=indexed["repo"], symbol_id=indexed["big"], storage_path=indexed["storage"],
            context_lines=5, max_source_lines=3,
        )
        assert "error" in out
        assert "context_lines" in out["error"]

    def test_batch_total_cap_returns_partial_not_dropped(self, indexed):
        out = get_symbol_source(
            repo=indexed["repo"],
            symbol_ids=[indexed["big"], indexed["other"]],
            storage_path=indexed["storage"],
            max_total_source_bytes=120,
        )
        syms = out["symbols"]
        # Both symbols are present (oversized ones come back partial, not dropped).
        assert len(syms) == 2
        assert {s["name"] for s in syms} == {"big_function", "other_function"}
        # Total returned source bytes respect the batch cap.
        total = sum(len(s["source"].encode("utf-8")) for s in syms)
        assert total <= 120
        # At least one entry had to be truncated to fit.
        assert any(s.get("source_truncated") for s in syms)

    def test_invalid_bounds_rejected(self, indexed):
        bad = get_symbol_source(
            repo=indexed["repo"], symbol_id=indexed["big"], storage_path=indexed["storage"],
            max_source_lines=0,
        )
        assert "error" in bad
        inverted = get_symbol_source(
            repo=indexed["repo"], symbol_id=indexed["big"], storage_path=indexed["storage"],
            source_start_line=50, source_end_line=10,
        )
        assert "error" in inverted
