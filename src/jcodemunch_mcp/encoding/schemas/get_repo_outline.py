"""Compact encoder for get_repo_outline — repo topology overview.

The tool emits its directory and symbol-kind breakdowns as **dicts**
(`{path: count}` / `{kind: count}`) and the most-imported / most-central
lists as lists of dicts, plus a few scalars. Those structured fields are
carried as JSON blobs so the payload round-trips losslessly.

Earlier versions of this schema declared a `files` table the tool never
produces (which re-materialised as a phantom `files: []` on decode) and
modelled `directories` as a list-of-dict table though the tool returns a
dict (which decoded to an empty list), while `symbol_kinds`,
`most_imported_files`, and `most_central_symbols` were absent from the
schema entirely and dropped. Under the default `server_output=adaptive`
path the lossy compact form shipped precisely because discarding that data
cleared the savings gate. Carrying the real fields as JSON blobs keeps the
payload complete; when the compact form no longer beats JSON by the gate
threshold the dispatcher simply falls back to JSON, which is correct.
"""

from .. import schema_driven as sd

TOOLS = ("get_repo_outline",)
ENCODING_ID = "ro1"

# No tables: directories/symbol_kinds are dicts (not list-of-dict), and the
# two list fields are optional, so JSON blobs preserve both shape and
# optional-key presence exactly (a table would inject `key: []` when absent).
_TABLES: list = []
_SCALARS = (
    "repo", "indexed_at", "file_count", "symbol_count", "staleness_warning",
)
_META = (
    "timing_ms", "tokens_saved", "total_tokens_saved", "is_stale",
)
_JSON = (
    "languages", "directories", "symbol_kinds",
    "most_imported_files", "most_central_symbols",
)


def encode(tool: str, response: dict) -> tuple[str, str]:
    return sd.encode(
        tool, response, ENCODING_ID, _TABLES, _SCALARS,
        meta_keys=_META, json_blobs=_JSON,
    )


def decode(payload: str) -> dict:
    return sd.decode(
        payload, _TABLES, _SCALARS, meta_keys=_META, json_blobs=_JSON,
    )
