"""Tests for the parser module (Phase 1)."""

import pytest
from jcodemunch_mcp.parser import parse_file, Symbol


PYTHON_SOURCE = '''
class MyClass:
    """A sample class."""
    def method(self, x: int) -> str:
        """Do something."""
        return str(x)

def standalone(a, b):
    """Standalone function."""
    return a + b

MAX_SIZE = 100
'''


def test_parse_python():
    """Test Python parsing extracts expected symbols."""
    symbols = parse_file(PYTHON_SOURCE, "test.py", "python")
    
    # Should have class, method, function, constant
    assert len(symbols) >= 3
    
    # Check class
    class_syms = [s for s in symbols if s.kind == "class"]
    assert len(class_syms) == 1
    assert class_syms[0].name == "MyClass"
    assert "A sample class" in class_syms[0].docstring
    
    # Check method
    method_syms = [s for s in symbols if s.kind == "method"]
    assert len(method_syms) == 1
    assert method_syms[0].name == "method"
    assert method_syms[0].parent is not None
    
    # Check standalone function
    func_syms = [s for s in symbols if s.kind == "function" and s.name == "standalone"]
    assert len(func_syms) == 1
    assert "Standalone function" in func_syms[0].docstring
    
    # Check constant
    const_syms = [s for s in symbols if s.kind == "constant"]
    assert len(const_syms) == 1
    assert const_syms[0].name == "MAX_SIZE"


def test_symbol_id_format():
    """Test symbol ID generation."""
    from jcodemunch_mcp.parser import make_symbol_id

    assert make_symbol_id("src/main.py", "MyClass.method", "method") == "src/main.py::MyClass.method#method"
    assert make_symbol_id("test.py", "standalone", "function") == "test.py::standalone#function"
    # Without kind falls back to no suffix
    assert make_symbol_id("test.py", "foo") == "test.py::foo"


def test_unknown_language_returns_empty():
    """Test that unknown languages return empty list."""
    result = parse_file("some code", "test.unknown", "unknown")
    assert result == []


def test_symbol_byte_offsets():
    """Test that byte offsets are correct."""
    symbols = parse_file(PYTHON_SOURCE, "test.py", "python")

    for sym in symbols:
        # Byte offset should be non-negative
        assert sym.byte_offset >= 0
        assert sym.byte_length > 0

        # Line numbers should be positive
        assert sym.line > 0
        assert sym.end_line >= sym.line


LUA_SOURCE = """\
--- Initialise the addon
-- @param name string
local function init(name)
    return {name = name}
end

function MyAddon.OnLoad(self)
    print("loaded")
end

--- Handle combat log event
function MyAddon:OnCombatLogEvent(event, ...)
    self:process(event)
end
"""


def test_lua_local_function():
    symbols = parse_file(LUA_SOURCE, "addon.lua", "lua")
    names = {s.qualified_name for s in symbols}
    assert "init" in names
    sym = next(s for s in symbols if s.qualified_name == "init")
    assert sym.kind == "function"
    assert sym.parent is None
    assert "Initialise the addon" in sym.docstring


def test_lua_dot_method():
    symbols = parse_file(LUA_SOURCE, "addon.lua", "lua")
    sym = next(s for s in symbols if s.qualified_name == "MyAddon.OnLoad")
    assert sym.kind == "method"
    assert sym.parent == "MyAddon"
    assert sym.name == "OnLoad"


def test_lua_colon_method():
    symbols = parse_file(LUA_SOURCE, "addon.lua", "lua")
    sym = next(s for s in symbols if s.qualified_name == "MyAddon:OnCombatLogEvent")
    assert sym.kind == "method"
    assert sym.parent == "MyAddon"
    assert "Handle combat log event" in sym.docstring


def test_lua_extension_registered():
    from jcodemunch_mcp.parser.languages import LANGUAGE_EXTENSIONS
    assert LANGUAGE_EXTENSIONS.get(".lua") == "lua"


# ---------------------------------------------------------------------------
# JS/TS const extraction
# ---------------------------------------------------------------------------

_JS_CONST_SOURCE = """\
const MAX_RETRIES = 3;

export const BASE_URL = "https://api.example.com";

const config = { debug: false };

const onClick = () => console.log("click");

const handler = function() { return 42; };
"""

_TS_CONST_SOURCE = """\
const MAX_RETRIES: number = 3;

export const BASE_URL: string = "https://api.example.com";

const config = Object.freeze({ debug: false });

const format = (s: string): string => s.trim();
"""


def test_js_const_declarations_extracted_as_constants():
    symbols = parse_file(_JS_CONST_SOURCE, "util.js", "javascript")
    by_name = {s.name: s for s in symbols}
    # plain and exported consts should be indexed
    assert "MAX_RETRIES" in by_name
    assert "BASE_URL" in by_name
    assert "config" in by_name
    assert by_name["MAX_RETRIES"].kind == "constant"
    assert by_name["BASE_URL"].kind == "constant"
    assert by_name["config"].kind == "constant"
    # arrow function and function expression consts are NOT constants
    assert by_name.get("onClick", None) is None or by_name["onClick"].kind == "function"
    assert by_name.get("handler", None) is None or by_name["handler"].kind == "function"


def test_ts_const_declarations_extracted_as_constants():
    symbols = parse_file(_TS_CONST_SOURCE, "util.ts", "typescript")
    by_name = {s.name: s for s in symbols}
    assert "MAX_RETRIES" in by_name
    assert "BASE_URL" in by_name
    assert "config" in by_name
    assert by_name["MAX_RETRIES"].kind == "constant"
    assert by_name["BASE_URL"].kind == "constant"
    assert by_name["config"].kind == "constant"
    # arrow function const is not a constant
    assert by_name.get("format", None) is None or by_name["format"].kind == "function"


# ---------------------------------------------------------------------------
# Astro (.astro) parser tests
# ---------------------------------------------------------------------------

def test_astro_component_symbol():
    """File-level component symbol is emitted as a 'class' symbol."""
    content = "---\nconst x = 1;\n---\n<div>{x}</div>"
    syms = parse_file(content, "src/Button.astro", "astro")
    assert any(s.kind == "class" and s.name == "Button" for s in syms)


def test_astro_frontmatter_typescript():
    """TypeScript symbols declared in frontmatter are extracted."""
    content = "---\nfunction greet(name: string) { return name; }\n---\n<p/>"
    syms = parse_file(content, "src/Greet.astro", "astro")
    names = [s.name for s in syms]
    assert "greet" in names


def test_astro_props_interface():
    """interface Props in frontmatter is extracted as a 'type' symbol."""
    content = "---\nexport interface Props { title: string; }\n---\n<h1/>"
    syms = parse_file(content, "src/Card.astro", "astro")
    assert any(s.name == "Props" and s.kind == "type" for s in syms)


def test_astro_script_block():
    """Inline <script> block contents are extracted as JavaScript symbols."""
    content = "---\n---\n<div/>\n<script>\nfunction init() {}\n</script>"
    syms = parse_file(content, "src/Page.astro", "astro")
    assert any(s.name == "init" and s.kind == "function" for s in syms)


def test_astro_style_block():
    """<style> block is recorded as a constant symbol."""
    content = "---\n---\n<div/>\n<style>h1 { color: red; }</style>"
    syms = parse_file(content, "src/Styled.astro", "astro")
    assert any(s.kind == "constant" and "style" in s.name for s in syms)


def test_astro_import_extraction():
    """ESM imports from the frontmatter are extracted correctly."""
    from jcodemunch_mcp.parser.imports import extract_imports
    content = (
        "---\n"
        "import Button from './Button.astro';\n"
        "import { x } from 'mod';\n"
        "---\n"
        "<div/>"
    )
    imports = extract_imports(content, "src/Page.astro", "astro")
    specifiers = [i["specifier"] for i in imports]
    assert "./Button.astro" in specifiers
    assert "mod" in specifiers


def test_astro_line_offsets_are_correct():
    """Line numbers in extracted symbols account for the frontmatter offset."""
    # Line 1: ---
    # Line 2: (blank)
    # Line 3: function deep() {}
    # Line 4: ---
    content = "---\n\nfunction deep() {}\n---\n<p/>"
    syms = parse_file(content, "src/Lines.astro", "astro")
    deep = next((s for s in syms if s.name == "deep"), None)
    assert deep is not None, "symbol 'deep' was not extracted"
    assert deep.line == 3, f"expected line 3, got {deep.line}"


def test_astro_no_frontmatter():
    """Files without a frontmatter fence still produce a component symbol."""
    content = "<html><body><p>Hello</p></body></html>"
    syms = parse_file(content, "src/Static.astro", "astro")
    assert any(s.kind == "class" and s.name == "Static" for s in syms)


def test_astro_external_script_src():
    """<script src='...'> is extracted as a function symbol."""
    content = "---\n---\n<script src='/js/app.js'></script>"
    syms = parse_file(content, "src/Ext.astro", "astro")
    assert any(s.kind == "function" and "/js/app.js" in s.name for s in syms)


def test_astro_fixture_sample(tmp_path):
    """Smoke-test the sample.astro fixture end-to-end."""
    import pathlib
    fixture = pathlib.Path(__file__).parent / "fixtures" / "astro" / "sample.astro"
    content = fixture.read_text()
    syms = parse_file(content, str(fixture), "astro")
    by_kind = {s.kind for s in syms}
    assert "class" in by_kind        # component symbol
    assert "constant" in by_kind     # style block


def test_astro_fixture_with_props(tmp_path):
    """WithProps.astro: Props interface + formatTitle function extracted."""
    import pathlib
    fixture = pathlib.Path(__file__).parent / "fixtures" / "astro" / "WithProps.astro"
    content = fixture.read_text()
    syms = parse_file(content, str(fixture), "astro")
    names = {s.name for s in syms}
    assert "Props" in names
    assert "formatTitle" in names


def test_astro_fixture_content_page(tmp_path):
    """ContentPage.astro: prerender const + initPage JS function extracted."""
    import pathlib
    fixture = pathlib.Path(__file__).parent / "fixtures" / "astro" / "ContentPage.astro"
    content = fixture.read_text()
    syms = parse_file(content, str(fixture), "astro")
    names = {s.name for s in syms}
    assert "prerender" in names
    assert "initPage" in names


# ---------------------------------------------------------------------------
# AstroWind real-world fixture tests (patterns from github.com/withastro/astrowind)
# ---------------------------------------------------------------------------

def test_astro_fixture_dynamic_route():
    """DynamicRoute.astro: getStaticPaths + type Props + helper functions extracted.

    Note: export const prerender = true; is NOT independently extracted here.
    The `satisfies` keyword (TS 4.9+) causes tree-sitter-typescript to merge the
    two adjacent export-const declarations into a single AST node, so only
    `getStaticPaths` is seen.  The prerender pattern is tested in simpler fixtures
    (ContentPage.astro, ClientIsland.astro) where it parses correctly.
    """
    import pathlib
    fixture = pathlib.Path(__file__).parent / "fixtures" / "astro" / "DynamicRoute.astro"
    content = fixture.read_text()
    syms = parse_file(content, str(fixture), "astro")
    names = {s.name for s in syms}

    # Component-level class symbol
    assert "DynamicRoute" in names

    # getStaticPaths exported const (async IIFE satisfies GetStaticPaths)
    assert "getStaticPaths" in names

    # type Props = InferGetStaticPropsType<...>
    assert "Props" in names

    # Regular TypeScript functions in frontmatter
    assert "formatDate" in names
    assert "estimateReadingTime" in names


def test_astro_fixture_dynamic_route_kinds():
    """DynamicRoute.astro: symbols have the right kinds."""
    import pathlib
    fixture = pathlib.Path(__file__).parent / "fixtures" / "astro" / "DynamicRoute.astro"
    content = fixture.read_text()
    syms = parse_file(content, str(fixture), "astro")
    by_name = {s.name: s for s in syms}

    assert by_name["DynamicRoute"].kind == "class"
    assert by_name["getStaticPaths"].kind == "constant"
    assert by_name["Props"].kind == "type"
    assert by_name["formatDate"].kind == "function"
    assert by_name["estimateReadingTime"].kind == "function"


def test_astro_fixture_dynamic_route_imports():
    """DynamicRoute.astro: all frontmatter imports are extracted."""
    import pathlib
    from jcodemunch_mcp.parser.imports import extract_imports
    fixture = pathlib.Path(__file__).parent / "fixtures" / "astro" / "DynamicRoute.astro"
    content = fixture.read_text()
    imports = extract_imports(content, str(fixture), "astro")
    specifiers = {i["specifier"] for i in imports}

    assert "astro" in specifiers
    assert "~/layouts/PageLayout.astro" in specifiers
    assert "~/components/blog/SinglePost.astro" in specifiers
    assert "~/utils/blog" in specifiers


def test_astro_fixture_widget_component():
    """WidgetComponent.astro: import type aliased as Props, helper functions extracted."""
    import pathlib
    fixture = pathlib.Path(__file__).parent / "fixtures" / "astro" / "WidgetComponent.astro"
    content = fixture.read_text()
    syms = parse_file(content, str(fixture), "astro")
    names = {s.name for s in syms}

    assert "WidgetComponent" in names   # component class
    assert "normalizeItems" in names    # helper function
    assert "buildGridClass" in names    # helper function


def test_astro_fixture_widget_component_imports():
    """WidgetComponent.astro: import type { Features as Props } from '~/types' extracted."""
    import pathlib
    from jcodemunch_mcp.parser.imports import extract_imports
    fixture = pathlib.Path(__file__).parent / "fixtures" / "astro" / "WidgetComponent.astro"
    content = fixture.read_text()
    imports = extract_imports(content, str(fixture), "astro")
    specifiers = {i["specifier"] for i in imports}

    assert "astro-icon/components" in specifiers
    assert "~/components/ui/Button.astro" in specifiers
    assert "~/types" in specifiers


def test_astro_fixture_base_layout():
    """BaseLayout.astro: export interface Props + async resolveMetadata function."""
    import pathlib
    fixture = pathlib.Path(__file__).parent / "fixtures" / "astro" / "BaseLayout.astro"
    content = fixture.read_text()
    syms = parse_file(content, str(fixture), "astro")
    names = {s.name for s in syms}

    assert "BaseLayout" in names        # component class
    assert "Props" in names             # export interface Props
    assert "resolveMetadata" in names   # async function
    assert "buildLangAttr" in names     # helper function


def test_astro_fixture_base_layout_kinds():
    """BaseLayout.astro: Props is a type, resolveMetadata is a function."""
    import pathlib
    fixture = pathlib.Path(__file__).parent / "fixtures" / "astro" / "BaseLayout.astro"
    content = fixture.read_text()
    syms = parse_file(content, str(fixture), "astro")
    by_name = {s.name: s for s in syms}

    assert by_name["Props"].kind == "type"
    assert by_name["resolveMetadata"].kind == "function"
    assert by_name["buildLangAttr"].kind == "function"


def test_astro_fixture_base_layout_imports():
    """BaseLayout.astro: astrowind:config and component imports extracted."""
    import pathlib
    from jcodemunch_mcp.parser.imports import extract_imports
    fixture = pathlib.Path(__file__).parent / "fixtures" / "astro" / "BaseLayout.astro"
    content = fixture.read_text()
    imports = extract_imports(content, str(fixture), "astro")
    specifiers = {i["specifier"] for i in imports}

    assert "astrowind:config" in specifiers
    assert "~/components/common/CommonMeta.astro" in specifiers
    assert "~/types" in specifiers


def test_astro_fixture_client_island():
    """ClientIsland.astro: prerender false, interface Props, script functions extracted."""
    import pathlib
    fixture = pathlib.Path(__file__).parent / "fixtures" / "astro" / "ClientIsland.astro"
    content = fixture.read_text()
    syms = parse_file(content, str(fixture), "astro")
    names = {s.name for s in syms}

    assert "ClientIsland" in names   # component class
    assert "prerender" in names      # export const prerender = false
    assert "Props" in names          # interface Props


def test_astro_fixture_client_island_script_functions():
    """ClientIsland.astro: JS functions inside <script define:vars> are extracted."""
    import pathlib
    fixture = pathlib.Path(__file__).parent / "fixtures" / "astro" / "ClientIsland.astro"
    content = fixture.read_text()
    syms = parse_file(content, str(fixture), "astro")
    names = {s.name for s in syms}

    # Functions defined inside the <script define:vars={{ ... }}> block
    assert "updateDisplay" in names
    assert "increment" in names
    assert "decrement" in names


def test_astro_fixture_client_island_style():
    """ClientIsland.astro: <style> block is extracted as a constant symbol."""
    import pathlib
    fixture = pathlib.Path(__file__).parent / "fixtures" / "astro" / "ClientIsland.astro"
    content = fixture.read_text()
    syms = parse_file(content, str(fixture), "astro")
    assert any(s.kind == "constant" and "style" in s.name for s in syms)


def test_astro_fixture_client_island_prerender_value():
    """ClientIsland.astro: prerender constant is extracted (SSR opt-out pattern)."""
    import pathlib
    fixture = pathlib.Path(__file__).parent / "fixtures" / "astro" / "ClientIsland.astro"
    content = fixture.read_text()
    syms = parse_file(content, str(fixture), "astro")
    by_name = {s.name: s for s in syms}
    assert "prerender" in by_name
    assert by_name["prerender"].kind == "constant"

