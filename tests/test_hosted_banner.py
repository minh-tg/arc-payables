"""The hosted demo must say so, on the header label and on the Overview banner."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is required to parse the console modules")
def test_the_hosted_flag_toggles_the_label_and_the_banner(tmp_path):
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
globalThis.document = {
  addEventListener() {},
  createElement: (tag) => new El(tag),
  createTextNode: (text) => { const node = new El('#text'); node.text = text; return node; },
};
globalThis.window = { addEventListener() {} };
globalThis.sessionStorage = { getItem() { return ''; } };
globalThis.fetch = async () => ({ok: true, json: async () => ({})});

const app = await import('./app.js');
assert.equal(app.deploymentLabel({hosted: true, mode: 'demo'}), 'Hosted demo · simulated payments & ledger');
assert.equal(app.deploymentLabel({mode: 'demo'}), 'Demo · simulated payments & ledger');
assert.equal(app.deploymentLabel({hosted: true, mode: 'testnet'}), 'Arc Testnet · real testnet payments');
assert.equal(app.deploymentLabel({hosted: false, mode: 'demo'}), 'Demo · simulated payments & ledger');

const { hostedBanner } = await import('./overview.js');
assert.equal(hostedBanner(null), null);
assert.equal(hostedBanner({hosted: false, mode: 'demo'}), null);
const banner = hostedBanner({hosted: true, mode: 'demo'});
assert.equal(banner.tag, 'section');
assert.equal(banner.className, 'card finding warning');
assert.equal(banner.attributes['aria-labelledby'], 'hosted-demo-title');
assert.equal(banner.children.length, 3);
assert.equal(banner.children[0].tag, 'h2');
assert.equal(banner.children[0].attributes.id, 'hosted-demo-title');
assert.equal(banner.children[0].children[0].text, 'Hosted demo with simulated payments');
assert.match(banner.children[1].children[0].text, /no funds move/);
assert.equal(banner.children[2].children[0].text, 'The data is disposable and may reset when the host restarts. ');
assert.equal(banner.children[2].children[1].tag, 'a');
assert.equal(banner.children[2].children[1].attributes.href, '#/setup');
''')
    result = subprocess.run([NODE, str(runner)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
