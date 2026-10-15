"""The exception case surface: a card on the invoice and a count on the attention queue.

The card carries API text, so it has to render that text as text, use only classes the stylesheet
defines, and stay read-only. These run the card and the queue cell through Node against a recorded
DOM, which is where a wrong helper or an extra class would otherwise reach a browser unnoticed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
WEB = Path(__file__).resolve().parents[1] / "src" / "arc_payables" / "web"

PRELUDE = """
import assert from 'node:assert/strict';
function h(tag, attrs = {}, ...children) {
  const node = { tag, attrs, children: children.flat().filter((child) => child !== null && child !== undefined && child !== false) };
  node.classList = { add(...names) { node.attrs.class = [node.attrs.class, ...names].filter(Boolean).join(' '); } };
  return node;
}
function panel(title, ...content) { return h('section', { class: 'card' }, h('h2', {}, title), ...content); }
function table(headers, rows) { return h('div', { class: 'table-wrapper responsive-table' }, h('table', {}, rows)); }
function badge(text, kind) { return h('span', { class: 'badge' }, text); }
function humanizeCode(code) { return String(code).replace(/_/g, ' '); }
function formatTimestamp(value) { return String(value); }
function collect(node, tags = [], texts = []) {
  if (typeof node === 'string') { texts.push(node); return { tags, texts }; }
  if (!node || !node.tag) return { tags, texts };
  tags.push(node.tag);
  for (const value of Object.values(node.attrs || {})) {
    assert.ok(!String(value).includes('<img'), `API text became an attribute: ${value}`);
  }
  (node.children || []).forEach((child) => collect(child, tags, texts));
  return { tags, texts };
}
"""


def _function(source: str, name: str) -> str:
    match = re.search(rf"function {name}\(.*?\n\}}\n", source, re.S)
    assert match, f"could not find {name} in the console source"
    return match.group()


def _run(tmp_path: Path, source: str) -> None:
    path = tmp_path / "console_case.mjs"
    path.write_text(source)
    result = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


CASE_FUNCTIONS = ("caseRecord", "caseCard", "draftCard")

HOSTILE = '<img src=x onerror="alert(1)">'


def _case_card_harness(extra: str) -> str:
    source = (WEB / "invoice.js").read_text()
    helpers = "".join(_function(source, name) for name in CASE_FUNCTIONS)
    return PRELUDE + helpers + extra


@pytest.mark.skipif(NODE is None, reason="node is required for console behavior checks")
def test_the_case_card_renders_api_text_as_text_and_stays_read_only(tmp_path):
    _run(tmp_path, _case_card_harness(f"""
const card = caseCard({{
  invoice_id: 'inv-1', status: 'open', last_checked_at: null,
  items: [{{check_code: 'receipt_match', detail: '{HOSTILE}', fix: 'source_record',
           source_type: 'Purchase Receipt', source_id: '{HOSTILE}', owner_role: 'Receiving'}}],
  events: [{{type: '{HOSTILE}', at: '2026-10-15T09:00:00+00:00', detail: '{HOSTILE}'}}],
}});
const {{ tags, texts }} = collect(card);
assert.equal(card.tag, 'section');
assert.equal(card.attrs.class, 'card finding warning');
assert.ok(texts.includes('{HOSTILE}'), 'case detail stays a text child');
assert.ok(tags.includes('details') && tags.includes('summary'), 'the history is a native disclosure');
assert.ok(!tags.includes('button') && !tags.includes('a') && !tags.includes('input'), 'nothing on the card is an action');
assert.ok(texts.some((text) => String(text).includes('never approves or waives')), 'the card states its limit');
"""))


@pytest.mark.skipif(NODE is None, reason="node is required for console behavior tests")
def test_a_cleared_case_is_neutral_and_says_it_is_not_an_approval(tmp_path):
    _run(tmp_path, _case_card_harness("""
const card = caseCard({invoice_id: 'inv-1', status: 'cleared', last_checked_at: '2026-10-15T09:30:00+00:00', items: [], events: []});
const { texts } = collect(card);
assert.equal(card.attrs.class, 'card finding');
assert.ok(texts.includes('Checks now pass'));
assert.ok(texts.some((text) => String(text).includes('This is not an approval')));
assert.ok(texts.some((text) => String(text).startsWith('Checked ')));
"""))


@pytest.mark.skipif(NODE is None, reason="node is required for console behavior checks")
def test_an_unknown_status_is_humanized_without_a_claim(tmp_path):
    _run(tmp_path, _case_card_harness("""
const card = caseCard({invoice_id: 'inv-1', status: 'reopened', last_checked_at: null, items: [], events: []});
const { texts } = collect(card);
assert.ok(texts.includes('reopened'));
assert.equal(card.attrs.class, 'card finding');
assert.ok(!texts.some((text) => String(text).includes('not an approval')), 'an unknown status adds no claim');
"""))


@pytest.mark.skipif(NODE is None, reason="node is required for console behavior checks")
def test_the_to_fix_cell_counts_records_and_reviews(tmp_path):
    source = (WEB / "attention.js").read_text()
    _run(tmp_path, PRELUDE + _function(source, "caseCell") + """
assert.equal(caseCell({case: {status: 'open', source_fixes: 2, reviews: 1}}).children[0], '2 records, 1 review');
assert.equal(caseCell({case: {status: 'open', source_fixes: 1, reviews: 0}}).children[0], '1 record');
assert.equal(caseCell({case: {status: 'open', source_fixes: 0, reviews: 1}}).children[0], '1 review');
assert.equal(caseCell({}), '—');
const cleared = caseCell({case: {status: 'cleared', source_fixes: 0, reviews: 0}});
assert.equal(cleared.children[0], '—');
assert.equal(cleared.children[1], ' ');
assert.equal(cleared.children[2].children[0], 'Checks now pass');
""")


@pytest.mark.skipif(NODE is None, reason="node is required for console behavior checks")
def test_the_draft_card_labels_an_unsent_model_note_and_renders_text_as_text(tmp_path):
    _run(tmp_path, _case_card_harness(f"""
const card = draftCard({{text: '{HOSTILE}', source: 'model', check_code: 'receipt_match', owner_role: 'Receiving', created_at: '2026-10-15T09:00:00+00:00'}});
const {{ tags, texts }} = collect(card);
assert.equal(card.tag, 'section');
assert.ok(texts.includes('{HOSTILE}'), 'draft text stays a text child');
assert.ok(texts.includes('Draft, not sent'), 'the badge states the draft is unsent');
assert.ok(texts.some((text) => String(text).includes('planner model')), 'a model draft says so');
assert.ok(texts.some((text) => String(text).includes('It changes no check, approval or payment')), 'the notice states its limit');
assert.ok(!tags.includes('button') && !tags.includes('a') && !tags.includes('input'), 'nothing on the card is an action');
"""))


@pytest.mark.skipif(NODE is None, reason="node is required for console behavior checks")
def test_the_draft_card_never_names_a_model_for_the_template(tmp_path):
    _run(tmp_path, _case_card_harness("""
const card = draftCard({text: 'To Receiving: a check on Purchase Receipt PR-9 is failing.', source: 'template', check_code: 'receipt_match', owner_role: 'Receiving', created_at: '2026-10-15T09:00:00+00:00'});
const { texts } = collect(card);
assert.ok(texts.some((text) => String(text).includes('no model used')), 'the template says no model was used');
assert.ok(!texts.some((text) => String(text).includes('planner model')), 'the template never names a model');
"""))


@pytest.mark.skipif(NODE is None, reason="node is required for console behavior checks")
def test_the_case_panel_adds_the_draft_only_for_an_open_case_with_one(tmp_path):
    source = (WEB / "invoice.js").read_text()
    assert "function casePanel(" in source
    assert "result.status === 'open' && result.draft" in source
    assert "nodes.push(draftCard(result.draft))" in source


@pytest.mark.skipif(NODE is None, reason="node is required for console behavior checks")
def test_the_case_surface_moves_no_focus():
    """Nothing on the card is a control, so no helper may touch focus or tab order."""
    source = (WEB / "invoice.js").read_text()
    helpers = "".join(_function(source, name) for name in CASE_FUNCTIONS) + _function(
        (WEB / "attention.js").read_text(), "caseCell"
    )
    for forbidden in ("focus(", "actionButton(", "tabindex", "autofocus"):
        assert forbidden not in helpers, forbidden


def test_every_class_the_case_surface_writes_has_a_style():
    html = (WEB / "index.html").read_text()
    css = re.search(r"<style>([\s\S]*?)</style>", html).group(1)
    tokens = set()
    for module in ("invoice.js", "attention.js"):
        for value in re.findall(r"class: '([^']*)'", (WEB / module).read_text()):
            tokens.update(value.split())
    assert {"finding", "finding-head", "finding-code", "muted", "mono"} <= tokens
    missing = sorted(name for name in tokens if f".{name}" not in css)
    assert not missing, f"these classes have no stylesheet rule: {missing}"


def test_the_invoice_view_reads_a_missing_case_as_nothing_to_show():
    source = (WEB / "invoice.js").read_text()
    assert re.search(r"api\(`/invoices/\$\{encodeURIComponent\(invoiceId\)\}/case`\)\.catch\(", source)
    assert "casePanel(result)" in source
    assert "result.status === 404" in source
