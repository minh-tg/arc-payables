"""Action controls keep keyboard focus without stealing it after async work."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
WEB = Path(__file__).resolve().parents[1] / "src/arc_payables/web"


@pytest.mark.skipif(NODE is None, reason="node is required for console behavior checks")
@pytest.mark.parametrize("scenario", [
    "cancel", "error", "other-focus", "removed-control", "unfocused-control", "existing-focus",
])
def test_async_action_restores_only_owned_available_focus(tmp_path, scenario):
    source = (WEB / "app.js").read_text()
    match = re.search(r"export function actionButton\(.*?\n\}\n", source, re.S)
    assert match, "Could not locate the shared action helper"
    action = match.group().replace("export function", "function", 1)
    harness = """
import assert from 'node:assert/strict';
const scenario = SCENARIO;
const document = {body: {}, activeElement: null};
const other = {};
let feedbackUpdates = 0;
const feedback = {replaceChildren() {feedbackUpdates++;}};
function errorPanel(error) {return {error};}
function h(tag, attrs, label) {
  return {
    onclick: attrs.onclick, textContent: label, isConnected: true, _disabled: false, focusCalls: 0,
    get disabled() {return this._disabled;},
    set disabled(value) {
      this._disabled = value;
      if (value && document.activeElement === this) document.activeElement = document.body;
    },
    focus() {
      assert.ok(this.isConnected && !this.disabled, 'Focused unavailable control');
      document.activeElement = this;
      this.focusCalls++;
    },
  };
}
ACTION
const button = actionButton('Review simulated payment', async () => {
  assert.equal(button.disabled, true);
  if (scenario === 'other-focus') document.activeElement = other;
  if (scenario === 'removed-control') button.isConnected = false;
  if (scenario === 'unfocused-control') document.activeElement = document.body;
  if (scenario === 'existing-focus') document.activeElement = button;
  if (scenario === 'error') throw Error('Injected read-only metadata error');
}, feedback);
document.activeElement = scenario === 'unfocused-control' ? other : button;
await button.onclick({currentTarget: button});
assert.equal(button.disabled, false);
assert.equal(button.textContent, 'Review simulated payment');
const restore = ['cancel', 'error'].includes(scenario);
assert.equal(button.focusCalls, restore ? 1 : 0);
const expected = restore || scenario === 'existing-focus'
  ? button : scenario === 'other-focus' ? other : document.body;
assert.equal(document.activeElement, expected, 'Incorrect final focus');
assert.equal(feedbackUpdates, scenario === 'error' ? 2 : 1);
"""
    harness = harness.replace("SCENARIO", json.dumps(scenario)).replace("ACTION", action)
    path = tmp_path / "focus.mjs"
    path.write_text(harness)
    result = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
