"""A view heading must not repeat the page title that the header already shows."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is required to parse the console modules")
def test_a_repeated_title_becomes_a_toolbar_row(tmp_path):
    web = ROOT / "src" / "arc_payables" / "web"
    for module in web.glob("*.js"):
        shutil.copy(module, tmp_path / module.name)
    (tmp_path / "package.json").write_text(json.dumps({"type": "module"}))
    runner = tmp_path / "check.mjs"
    runner.write_text(r'''
import assert from 'node:assert/strict';
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.className = ''; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener() {}
  append(...children) { this.children.push(...children); }
}
globalThis.Node = El;
const pageTitle = { textContent: 'Settlements' };
globalThis.document = {
  addEventListener() {},
  getElementById: (id) => (id === 'page-title' ? pageTitle : null),
  createElement: (tag) => new El(tag),
  createTextNode: (text) => { const node = new El('#text'); node.text = text; return node; },
  createDocumentFragment: () => new El('#fragment'),
};
globalThis.window = { addEventListener() {} };
globalThis.sessionStorage = { getItem() { return ''; } };
globalThis.fetch = async () => ({ok: true, json: async () => ({})});

const { sectionHeading } = await import('./app.js');
const tags = (node) => [node.tag, ...node.children.filter(Boolean).flatMap((child) => (child.tag ? tags(child) : []))];

const repeated = sectionHeading({ eyebrow: 'Payment history', title: 'Settlements', count: 3 });
assert.equal(repeated.className, 'section-heading section-toolbar');
assert.ok(!tags(repeated).includes('h2'));

const bare = sectionHeading({ eyebrow: 'Payment history', title: 'Settlements' });
assert.equal(bare.tag, '#fragment');

const distinct = sectionHeading({ eyebrow: 'Next actions', title: 'Needs your judgement', count: 1 });
assert.equal(distinct.className, 'section-heading');
assert.ok(tags(distinct).includes('h2'));
''')
    result = subprocess.run([NODE, str(runner)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
