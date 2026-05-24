"""Focused Astro regression tests."""

from pathlib import Path

from jcodemunch_mcp.parser import parse_file
from jcodemunch_mcp.parser.astro_shared import mask_html_comments_keep_offsets, split_astro_frontmatter
from jcodemunch_mcp.parser.imports import extract_imports
from jcodemunch_mcp.parser.languages import LANGUAGE_EXTENSIONS, LANGUAGE_REGISTRY, get_language_for_path


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "astro"


def _read_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def test_astro_extension_and_registry_present():
    assert LANGUAGE_EXTENSIONS.get(".astro") == "astro"
    assert "astro" in LANGUAGE_REGISTRY
    assert get_language_for_path("src/pages/home.astro") == "astro"


def test_astro_component_symbol_created():
    symbols = parse_file("---\nconst x = 1;\n---\n<div />", "src/Button.astro", "astro")
    assert any(s.kind == "class" and s.name == "Button" for s in symbols)


def test_astro_bom_crlf_frontmatter_parses_with_line_offsets():
    content = "\ufeff" + _read_fixture("sample_bom_crlf.astro").replace("\n", "\r\n")
    symbols = parse_file(content, "src/BomCase.astro", "astro")
    by_name = {s.name: s for s in symbols}

    assert "Props" in by_name
    assert "frontmatterHelper" in by_name
    assert by_name["frontmatterHelper"].line == 5
    assert any(s.name == "hero-banner" and s.kind == "constant" for s in symbols)


def test_astro_template_id_extraction_ignores_comments():
    content = _read_fixture("sample_malformed_frontmatter.astro")
    symbols = parse_file(content, "src/Malformed.astro", "astro")
    names = {s.name for s in symbols}

    assert "Malformed" in names
    assert "content-root" in names
    assert "comment-should-not-extract" not in names


def test_astro_multi_script_parsing_infers_ts_and_skips_json_script():
    content = _read_fixture("sample_multi_script.astro")
    symbols = parse_file(content, "src/Multi.astro", "astro")
    by_name = {s.name: s for s in symbols}

    assert "CounterState" in by_name
    assert by_name["CounterState"].kind == "type"
    assert "increment" in by_name
    assert "hydrate" in by_name
    assert "shouldSkip" not in by_name


def test_astro_imports_include_frontmatter_and_synthetic_template_components():
    content = _read_fixture("sample_multi_script.astro")
    imports = extract_imports(content, "src/Multi.astro", "astro")

    specifiers = {edge["specifier"] for edge in imports}
    assert "./UserCard.astro" in specifiers
    assert "NavBar" in specifiers

    navbar = [edge for edge in imports if edge["specifier"] == "NavBar"]
    assert len(navbar) == 1
    assert navbar[0] == {"specifier": "NavBar", "names": ["NavBar"]}


def test_astro_synthetic_component_edges_are_deduplicated():
    content = "<main><NavBar /><nav-bar /><NavBar /></main>"
    imports = extract_imports(content, "src/Dedupe.astro", "astro")
    navbar = [edge for edge in imports if edge["specifier"] == "NavBar"]
    assert len(navbar) == 1


def test_astro_no_frontmatter_and_malformed_frontmatter_do_not_crash():
    no_frontmatter = "<section id='simple'><h1>Hello</h1></section>"
    malformed = _read_fixture("sample_malformed_frontmatter.astro")

    no_fm_symbols = parse_file(no_frontmatter, "src/NoFrontmatter.astro", "astro")
    malformed_symbols = parse_file(malformed, "src/Malformed.astro", "astro")

    assert any(s.name == "NoFrontmatter" and s.kind == "class" for s in no_fm_symbols)
    assert any(s.name == "Malformed" and s.kind == "class" for s in malformed_symbols)


def test_astro_shared_helpers_preserve_offsets_and_frontmatter_split():
    content = "\ufeff---\nconst x = 1;\n---\n<div>\n<!-- hidden -->\n<p id='ok' />\n</div>\n"
    frontmatter, template, fm_start, template_start = split_astro_frontmatter(content)

    assert frontmatter == "const x = 1;\n"
    assert fm_start == 2
    assert template_start == 4
    masked = mask_html_comments_keep_offsets(template)
    assert len(masked) == len(template)
    assert masked.count("\n") == template.count("\n")
    assert "hidden" not in masked
    assert "id='ok'" in masked
