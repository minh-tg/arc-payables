"""The operator console shell.

The console is deliberately inert without credentials, so these tests check that the shell is
served without a key, that its assets are reachable, and that serving it does not open the API.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from arc_payables.api import create_app
from arc_payables.seed import seed_demo
from arc_payables.settings import Settings
from arc_payables.store import SQLiteEvidenceStore


def _client(tmp_path: Path) -> TestClient:
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "console.sqlite3",
        api_key="console-key",
        min_reserve_usdc=Decimal("0"),
    )
    store = SQLiteEvidenceStore(settings.database_path)
    store.initialize()
    seed_demo(store)
    return TestClient(create_app(settings=settings, store=store))


def test_the_console_shell_is_served_without_an_api_key(tmp_path):
    response = _client(tmp_path).get("/console/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    # The shell names the views it offers and states the testnet boundary it operates within.
    assert "operator console" in body
    assert "#/queue" in body and "#/treasury" in body
    assert "Arc Testnet only" in body


def test_the_console_assets_are_reachable(tmp_path):
    client = _client(tmp_path)
    for asset in ("app.js", "queue.js", "invoice.js", "treasury.js"):
        response = client.get(f"/console/{asset}")
        assert response.status_code == 200, asset
        assert "javascript" in response.headers["content-type"], asset


def test_serving_the_console_does_not_open_the_api(tmp_path):
    """The shell holds no data; the API behind it still requires a key."""
    client = _client(tmp_path)
    assert client.get("/console/").status_code == 200
    assert client.get("/invoices").status_code == 401
    assert client.get("/invoices", headers={"X-API-Key": "console-key"}).status_code == 200


def test_unknown_console_paths_are_not_served(tmp_path):
    client = _client(tmp_path)
    assert client.get("/console/does-not-exist.js").status_code == 404


WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "arc_payables" / "web"
NODE = shutil.which("node")


def test_link_buttons_do_not_hide_their_bottom_outline():
    html = (WEB_DIR / "index.html").read_text()
    css = re.search(r"<style>([\s\S]*?)</style>", html).group(1)
    button_rules = [(selector, body) for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
                    if ".btn" in selector]
    assert button_rules
    # Button links use the shared four-sided border, not the underline override of text links.
    for selector, body in button_rules:
        assert not re.search(r"border-bottom(?:-(?:color|style|width))?\s*:", body), selector


def _parses_as_module(tmp_path: Path, source: str) -> None:
    """Ask Node to parse the source as an ES module; nothing is executed."""
    target = tmp_path / "module.mjs"
    target.write_text(source)
    result = subprocess.run([NODE, "--check", str(target)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="node is required to parse the console modules")
def test_every_console_module_parses(tmp_path):
    """A syntax error here would leave a blank page that no server-side test would notice."""
    modules = sorted(WEB_DIR.glob("*.js"))
    assert modules, "expected the console modules to be present"
    for module in modules:
        _parses_as_module(tmp_path, module.read_text())


@pytest.mark.skipif(NODE is None, reason="node is required to parse the page script")
def test_the_inline_page_script_parses(tmp_path):
    html = (WEB_DIR / "index.html").read_text()
    scripts = re.findall(r'<script type="module">(.*?)</script>', html, re.DOTALL)
    assert scripts, "the console shell should bootstrap its modules"
    for script in scripts:
        _parses_as_module(tmp_path, script)


def test_the_console_never_writes_markup():
    """Everything shown comes from the API, so it is inserted as a text node and never as markup.

    The guided view reads its definitions from the backend, which makes this rule load-bearing
    rather than stylistic: a term's definition is untrusted content like everything else.
    """
    sources = sorted(WEB_DIR.glob("*.js")) + [WEB_DIR / "index.html"]
    offenders = [
        path.name
        for path in sources
        if re.search(r"\b(innerHTML|outerHTML|insertAdjacentHTML|document\.write)\b", path.read_text())
    ]
    assert not offenders, f"these write markup instead of text: {offenders}"


def test_the_shell_offers_the_guided_view_switch():
    """The beginner view has to be switchable off, and the switch has to be wired to the preference."""
    html = (WEB_DIR / "index.html").read_text()
    assert 'id="guide-toggle"' in html
    assert "Explaining:" in html
    assert "setGuided(" in html and "guided()" in html
    app = (WEB_DIR / "app.js").read_text()
    for name in ("concept", "conceptText", "conceptAction", "guided", "lede", "nextStep", "setGuided"):
        assert f"function {name}(" in app, f"app.js must export {name}"


def test_every_class_the_guided_view_writes_has_a_style():
    """A renamed class leaves a term that looks like body text and a popover with no box.

    Nothing else would notice: the markup still renders, the words are still there, and the page
    simply stops looking like it has explanations on it.
    """
    app = (WEB_DIR / "app.js").read_text()
    html = (WEB_DIR / "index.html").read_text()
    written = set(re.findall(r"class: '([a-z-]+)", app))
    # These carry the look: a term has to read as a term, and the popover has to have a box.
    for name in ("term", "lede", "concept-tip", "concept-tip-action", "guide-note"):
        assert name in written, f"app.js should still write .{name}"
        assert f".{name}" in html, f"index.html has no rule for .{name}"
    # Whatever the popover looks up has to be something it wrote, or it fills nothing at all.
    for name in set(re.findall(r"querySelector(?:All)?\('\.([a-z-]+)'\)", app)):
        assert name in written, f"app.js looks up .{name} but never writes it"


def test_a_view_does_not_import_what_it_does_not_use():
    """An unused import is the residue of a helper that was meant to be wired up and was not."""
    unused = []
    for module in sorted(WEB_DIR.glob("*.js")):
        if module.name == "app.js":
            continue
        text = module.read_text()
        for names in re.findall(r"import \{([^}]+)\} from '\./app\.js'", text):
            for name in (item.strip() for item in names.split(",")):
                if name and len(re.findall(rf"\b{re.escape(name)}\b", text)) < 2:
                    unused.append(f"{module.name} imports {name} and never uses it")
    assert not unused, unused


def test_a_column_heading_may_carry_a_term_without_breaking_the_phone_label():
    """A heading is where the jargon usually is, so a heading may be a term node.

    The phone reflow labels every cell from its column, which needs a string. Reading the label back
    off the node's text is what keeps that working; script/console-smoke.sh catches the failure in a
    browser, where '[object HTMLSpanElement]' would actually appear.
    """
    app = (WEB_DIR / "app.js").read_text()
    assert re.search(r"header instanceof Node\s*\?\s*header\.textContent", app)


def test_no_panel_is_built_from_guided_text_alone():
    """Guided text vanishes in normal view, so a panel made only of it becomes an empty card.

    The card keeps its heading and loses everything under it, which reads as a screen that failed to
    load. Every panel needs at least one child that is rendered whatever the reader's setting is.
    This catches the exact shape `panel('X', lede(...))`; the invariant is broader than the regex.
    """
    pattern = re.compile(
        r"panel\(\s*(?:'[^']*'|`[^`]*`)\s*,\s*lede\((?:[^()]|\([^()]*\))*\)\s*,?\s*\)",
        re.DOTALL,
    )
    offenders = [
        module.name
        for module in sorted(WEB_DIR.glob("*.js"))
        if module.name != "app.js" and pattern.search(module.read_text())
    ]
    assert not offenders, f"these build a card from guided text alone: {offenders}"


def test_every_nav_destination_has_a_view_and_every_view_is_loaded():
    """A nav item leading nowhere, or a view module the shell never imports, fails silently.

    A registered view whose module is not imported simply never registers, and its route falls back
    to the landing page without an error. A nav link with no view beside it is worse: it looks like
    a screen and behaves like a reload.
    """
    html = (WEB_DIR / "index.html").read_text()
    registered = set()
    for module in sorted(WEB_DIR.glob("*.js")):
        registered |= set(re.findall(r"registerView\('([a-z]+)'", module.read_text()))
    loaded = set(re.findall(r"import '\./([a-z]+)\.js'", html))
    linked = set(re.findall(r'href="#/([a-z]+)"', html))

    assert linked <= registered, f"nav links with no view behind them: {sorted(linked - registered)}"
    assert registered <= loaded, f"views the shell never imports: {sorted(registered - loaded)}"
    # The four destinations the console is organised around.
    assert {"overview", "attention", "payments", "audit"} <= linked


def test_every_token_the_stylesheet_uses_is_defined_for_both_themes():
    """A token defined in one theme only is a rule that silently loses its value in the other.

    The light block is `:root, :root[data-theme='light']`, so a token missing from the dark block
    still resolves while the light block happens to carry it. That makes the omission invisible until
    somebody narrows the light selector, which is exactly the kind of change a theme invites.
    """
    html = (WEB_DIR / "index.html").read_text()
    shared = re.search(r"\n      :root \{(.*?)\n      \}", html, re.DOTALL)
    light = re.search(r":root, :root\[data-theme='light'\] \{(.*?)\n      \}", html, re.DOTALL)
    dark = re.search(r":root\[data-theme='dark'\] \{(.*?)\n      \}", html, re.DOTALL)
    assert shared and light and dark, "the three token blocks should all be present"

    def names(block):
        return set(re.findall(r"(--[a-z0-9-]+):", block))

    declared = names(shared.group(1)) | names(light.group(1)) | names(dark.group(1))
    used = set(re.findall(r"var\((--[a-z0-9-]+)\)", html))
    assert not used - declared, f"used but never defined: {sorted(used - declared)}"
    # Anything that varies by theme has to be stated in both, or the theme change half-applies.
    for name in sorted(used & names(light.group(1))):
        assert name in names(dark.group(1)), f"{name} is set for light and not for dark"


def test_every_helper_a_view_imports_exists():
    """The modules import from app.js by name; a rename would otherwise fail only in a browser."""
    exported = set(re.findall(r"export (?:async )?function (\w+)", (WEB_DIR / "app.js").read_text()))
    missing = []
    for module in sorted(WEB_DIR.glob("*.js")):
        if module.name == "app.js":
            continue
        for names in re.findall(r"import \{([^}]+)\} from '\./app\.js'", module.read_text()):
            for name in (item.strip() for item in names.split(",")):
                if name and name not in exported:
                    missing.append(f"{module.name} imports {name}")
    assert not missing, missing


def test_badges_in_fact_values_use_the_page_sans_stack():
    html = (WEB_DIR / "index.html").read_text()
    assert "dl.facts dd .badge { font-family: Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; }" in html


def test_settlement_timeline_humanizes_codes_and_formats_times():
    payments = (WEB_DIR / "payments.js").read_text()
    assert "humanizeCode(event.event)" in payments
    assert "humanizeCode(event.state)" in payments
    assert "formatTimestamp(timestamp)" in payments
    assert "new Date(Number(rawWhen) * 1000).toISOString()" in payments


def test_reconciliation_findings_render_full_transaction_hashes():
    payments = (WEB_DIR / "payments.js").read_text()
    assert "item.simulated ? badge('Simulated', 'warn') : null" in payments
    assert "h('span', { class: 'mono' }, item.transaction_hash)" in payments
    assert "item.transaction_hash.slice(0, 10)" not in payments


def test_phone_layout_gives_inline_controls_tap_height():
    html = (WEB_DIR / "index.html").read_text()
    mobile = re.search(r"@media \(max-width: 899px\) \{([\s\S]*?)\n      \}", html)
    assert mobile
    assert "td a, .term, .card > a, .health-summary > a { display: inline-flex; align-items: center; min-height: 44px; }" in mobile.group(1)
    assert "input:not([type='checkbox']) { min-height: 44px; }" in mobile.group(1)


def test_design_smoke_uses_portable_browser_and_screenshot_paths():
    smoke = (WEB_DIR.parents[2] / "script" / "console-design-smoke.py").read_text()
    assert "CONSOLE_DESIGN_SHOTS_DIR" in smoke
    assert "tempfile.gettempdir()" in smoke
    assert "CHROMIUM_PATH = os.environ.get(\"CHROMIUM_PATH\")" in smoke
    assert "playwright.chromium.launch(**launch_options)" in smoke
    assert "Playwright is not installed" in smoke
    assert "Chromium is unavailable" in smoke
    assert "/home/minh/projects/hackathons/tameion" not in smoke


def test_desktop_table_badges_can_wrap_inside_narrow_cells():
    html = (WEB_DIR / "index.html").read_text()
    desktop = re.search(r"@media \(min-width: 901px\) \{([\s\S]*?)\n      \}", html)
    assert desktop
    assert ".table-wrapper td .badge { max-width: 100%; white-space: normal; }" in desktop.group(1)


def _oklch_luminance(lightness: float, chroma: float, hue: float) -> float:
    import math

    a = chroma * math.cos(math.radians(hue))
    b = chroma * math.sin(math.radians(hue))
    l = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    red = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    green = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    blue = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def test_light_control_border_reaches_three_to_one_on_white():
    html = (WEB_DIR / "index.html").read_text()
    match = re.search(r"--control-border: oklch\((\d+)% ([\d.]+) (\d+)\);", html)
    assert match
    border = _oklch_luminance(int(match[1]) / 100, float(match[2]), float(match[3]))
    assert (1.0 + 0.05) / (border + 0.05) >= 3.0


def test_fields_use_the_control_border_in_both_themes():
    html = (WEB_DIR / "index.html").read_text()
    assert html.count("--control-border:") == 2
    assert re.search(r"\n      input \{[^}]*border: 1px solid var\(--control-border\)", html)
    assert re.search(r"\.nav-end input \{[^}]*border: 1px solid var\(--control-border\)", html)


def test_font_sizes_come_from_the_type_scale():
    html = (WEB_DIR / "index.html").read_text()
    rules = html.split("* { box-sizing", 1)[1]
    assert not re.findall(r"font-size: \d", rules)
    tokens = re.findall(r"--text-[\w]+: (\d+)px;", html)
    assert len(tokens) == 8
    for script in WEB_DIR.glob("*.js"):
        assert not re.search(r"font-size: \d", script.read_text()), script.name


def test_theme_colours_and_radii_are_tokens():
    html = (WEB_DIR / "index.html").read_text()
    rules = html.split("* { box-sizing", 1)[1]
    assert "oklch(100% 0 0)" not in rules
    assert "#0a0f1e" not in rules
    assert not re.search(r"\.log-level-\w+ \{ color: oklch", rules)
    assert "border-radius: 12px" not in rules
    assert "rgba(10, 15, 30" not in rules


def test_phone_header_keeps_refresh_on_the_title_row():
    html = (WEB_DIR / "index.html").read_text()
    mobile = re.search(r"@media \(max-width: 899px\) \{([\s\S]*?)\n      \}", html)
    assert mobile
    rules = mobile.group(1)
    assert ".header-meta { display: contents; }" in rules
    assert ".header-refresh { grid-column: 2; grid-row: 1; }" in rules
    assert "#environment-label { grid-column: 1; grid-row: 2;" in rules
