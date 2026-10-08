// The guided demo uses real workflow records. Merely opening this view never changes them.
import { actionButton, api, badge, can, h, panel, refresh } from './app.js';

export function journeyProgress(invoices) {
  const eligible = invoices.find((row) => row.invoice.id === 'demo-invoice-legitimate');
  const blocked = invoices.find((row) => row.invoice.id === 'demo-invoice-suspicious');
  return {
    eligible, blocked,
    captured: Boolean(eligible && blocked),
    evaluated: Boolean(eligible?.decision && blocked?.decision && blocked.decision.policy_checks.some((check) => !check.passed && !check.overridable)),
    recorded: eligible?.state === 'ERP_RECORDED',
  };
}

export function demoJourney(invoices, deployment) {
  if (!deployment?.demo_available || !can('operate')) return null;
  const progress = journeyProgress(invoices);
  const feedback = h('div', { role: 'status', 'aria-live': 'polite' });
  const invoiceLink = (row, text) => row ? h('a', { class: 'btn', href: `#/invoice/${encodeURIComponent(row.invoice.id)}` }, text) : null;
  const initialize = actionButton('Load demo invoices', async () => {
    await api('/demo/start', { method: 'POST' });
    await refresh();
  }, feedback, { class: 'btn-primary', 'data-action': 'start-demo' });
  // No reset control: existing evidence, payments and fixtures are never overwritten by onboarding.
  const start = progress.captured
    ? badge('Examples loaded', 'good')
    : invoices.length
      ? h('a', { class: 'btn', href: '#/queue' }, 'Explore existing invoices')
      : initialize;
  const steps = [
    ['Explore the evidence', progress.captured, 'Two example invoices: one matches the ledger; one claims a different amount and destination.', start],
    ['Understand the decision', progress.evaluated, 'Evaluate both. See why the clean invoice can proceed and why approval cannot fix conflicting evidence.', h('div', { class: 'action-row' }, invoiceLink(progress.eligible, 'Check clean invoice'), invoiceLink(progress.blocked, 'Inspect blocked invoice'))],
    ['Simulate and reconcile', progress.recorded, 'Confirm the exact amount and trusted recipient. The mock provider moves no on-chain funds and records a simulated ledger entry.', invoiceLink(progress.eligible, progress.recorded ? 'View recorded payment' : 'Open payment review')],
    ['Verify the history', false, 'Recompute the signed chain. A result is shown only after verification actually succeeds.', actionButton('Verify demo history', async () => {
      const result = await api('/audit/verify');
      feedback.replaceChildren(result.ok
        ? badge(`Verified now · ${result.checked} entries · ${result.signed} signed`, 'good')
        : badge(`Broken at ${result.first_broken_id}: ${result.reason}`, 'bad'));
    }, feedback)],
  ];
  return panel('Your first payment, without moving funds',
    h('p', { class: 'muted' }, 'A guided walkthrough of the same policy, evidence and audit APIs used by the operator console. Every action is explicit; nothing pays itself here.'),
    h('ol', { class: 'journey' }, steps.map(([title, done, description, action], index) =>
      h('li', { class: done ? 'journey-step done' : 'journey-step' },
        h('span', { class: 'step-number', 'aria-label': done ? `Step ${index + 1} complete` : `Step ${index + 1}` }, done ? '✓' : index + 1),
        h('div', {}, h('h3', {}, title), h('p', { class: 'muted' }, description), action),
      ),
    )), feedback,
  );
}
