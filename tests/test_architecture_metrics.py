"""Tests for get_architecture_metrics — Gini concentration, Lakos depth, DSM modularity.

Covers:
  - Gini coefficient math (even -> 0, concentrated -> high)
  - fan-in concentration surfaces the hub file as top concentrator
  - dependency depth: longest chain + max_depth over a known import chain
  - modularity: cycle detection (back-edges, cyclic_files, cluster split)
  - summary picks the most-concentrated metric
  - honest errors (unindexed, bad top_n)
  - read-only (idempotent)
"""

from pathlib import Path

from jcodemunch_mcp.tools.get_architecture_metrics import _gini, get_architecture_metrics
from jcodemunch_mcp.tools.index_folder import index_folder


# base.py is a leaf imported by mid + three consumers (fan-in 4 = the hub).
# top -> mid -> base is a depth-2 chain. cyc_a <-> cyc_b is an import cycle.
_FILES = {
    "base.py": "def base():\n    return 1\n",
    "mid.py": "from base import base\n\ndef mid():\n    return base()\n",
    "top.py": "from mid import mid\n\ndef top():\n    return mid()\n",
    "u1.py": "from base import base\n\ndef u1():\n    return base()\n",
    "u2.py": "from base import base\n\ndef u2():\n    return base()\n",
    "u3.py": "from base import base\n\ndef u3():\n    return base()\n",
    "cyc_a.py": "from cyc_b import cb\n\ndef ca():\n    return cb()\n",
    "cyc_b.py": "from cyc_a import ca\n\ndef cb():\n    return ca()\n",
}


def _make_repo(tmp_path: Path) -> tuple[str, str]:
    for rel, content in _FILES.items():
        (tmp_path / rel).write_text(content, encoding="utf-8")
    storage = str(tmp_path / ".index")
    result = index_folder(str(tmp_path), use_ai_summaries=False, storage_path=storage)
    return result.get("repo", str(tmp_path)), storage


class TestGini:
    def test_even_is_zero(self):
        assert _gini([1, 1, 1, 1]) == 0.0

    def test_concentrated_is_high(self):
        assert _gini([0, 0, 0, 10]) > 0.7

    def test_empty_and_zero(self):
        assert _gini([]) == 0.0
        assert _gini([0, 0, 0]) == 0.0


class TestConcentration:
    def test_fanin_hub_is_top_concentrator(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = get_architecture_metrics(repo, storage_path=storage)
        assert "error" not in out, out.get("error")
        top_in = out["concentration"]["top_concentrators"]["fan_in"]
        assert top_in, "expected fan-in concentrators"
        assert top_in[0]["file"].endswith("base.py")
        assert top_in[0]["value"] == 4  # mid + u1 + u2 + u3
        # fan-in is heavily concentrated on one file -> higher Gini than fan-out.
        g = out["concentration"]["gini"]
        assert g["fan_in"] > g["fan_out"]

    def test_summary_most_concentrated(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = get_architecture_metrics(repo, storage_path=storage)
        assert out["summary"]["most_concentrated_metric"] == "fan_in"


class TestDepth:
    def test_longest_chain(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = get_architecture_metrics(repo, storage_path=storage)
        depth = out["depth"]
        assert depth["max_depth"] == 2  # top -> mid -> base
        assert depth["longest_chain"][0].endswith("top.py")
        assert depth["longest_chain"][-1].endswith("base.py")
        assert len(depth["longest_chain"]) == 3
        assert depth["available"] is True


class TestModularity:
    def test_cycle_detected(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        out = get_architecture_metrics(repo, storage_path=storage)
        mod = out["modularity"]
        assert mod["cycle_count"] == 1
        assert mod["cyclic_files"] == 2  # cyc_a + cyc_b
        assert out["depth"]["back_edge_count"] >= 1
        # The cycle pair is a separate cluster from the base/mid/top chain.
        assert mod["clusters"] >= 2


class TestErrorsAndReadOnly:
    def test_unindexed_repo(self, tmp_path):
        _, storage = _make_repo(tmp_path)
        assert "error" in get_architecture_metrics("local/nope-xyz", storage_path=storage)

    def test_bad_top_n(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        assert "error" in get_architecture_metrics(repo, top_n=0, storage_path=storage)

    def test_idempotent(self, tmp_path):
        repo, storage = _make_repo(tmp_path)
        a = get_architecture_metrics(repo, storage_path=storage)
        b = get_architecture_metrics(repo, storage_path=storage)
        assert a["concentration"]["gini"] == b["concentration"]["gini"]
        assert a["depth"] == b["depth"]
        assert a["modularity"] == b["modularity"]
