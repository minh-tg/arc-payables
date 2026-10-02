// The worker's face: recent passes, whether they finished cleanly, and what they asked
// a human to look at. Read-only. Alerts are recorded on the pass, not recomputed here.

import { api, badge, concept, h, lede, nextStep, panel, plainWords, registerView, table } from './app.js';

function levelTone(level) {
  if (level === 'critical') return 'bad';
  if (level === 'warning') return 'warn';
  return '';
}

function outcomeTone(outcome) {
  if (outcome === 'ok') return 'good';
  if (outcome === 'failed' || outcome === 'degraded') return 'bad';
  return 'warn';
}

function stepSummary(step) {
  const acted = `${step.acted}/${step.examined}`;
  if (step.failed) return `${acted} (${step.failed} failed)`;
  if (step.error) return `${acted} (error)`;
  return acted;
}

// Why a step did not act on something. A pass that declines a payable has to say why, or the
// operator cannot tell a missing FX rate from an outage.
const MAX_NOTES = 3;

function stepNotes(step) {
  const detail = step.detail || {};
  const notes = [];
  if (step.error) notes.push(step.error);

  const declined = detail.declined || [];
  for (const item of declined.slice(0, MAX_NOTES)) {
    notes.push(`${item.external_id}: ${item.reason || item.code}`);
  }
  const declinedTotal = Object.values(detail.declined_codes || {}).reduce((sum, n) => sum + n, 0);
  if (declinedTotal > declined.length) {
    notes.push(`and ${declinedTotal - declined.length} more declined`);
  }

  for (const [code, count] of Object.entries(detail.refusals || {})) {
    notes.push(`refused ${count}× ${code}`);
  }
  for (const item of (detail.errors || []).slice(0, MAX_NOTES)) {
    notes.push(`${item.invoice_id || item.external_id}: ${item.error}`);
  }
  if (detail.deferred) notes.push(`${detail.deferred} deferred to the next pass`);
  if (detail.waiting) notes.push(`${detail.waiting} waiting out a writeback backoff`);
  return notes;
}

async function renderWorker(root) {
  const status = await api('/worker/status');

  const last = status.last;
  const facts = h(
    'dl',
    { class: 'facts' },
    h('dt', {}, 'Last pass'),
    h('dd', {}, last ? `${last.finished_at} · ${last.outcome}` : 'no pass recorded yet'),
    h('dt', {}, 'Consecutive failures'),
    h('dd', {}, String(status.consecutive_failures)),
    h('dt', {}, 'Passes observed'),
    h('dd', {}, String(status.observed)),
  );

  root.append(
    panel(
      'Worker health',
      lede(
        'A pass is one trip through the work: reading what the ', concept('ledger', 'ledger'),
        ' still owes, paying what policy already authorized, finishing the ledger entries, and asking ',
        'the chain about anything still unconfirmed. The worker approves nothing.',
      ),
      status.last ? facts : h('p', { class: 'muted' }, 'No pass has been recorded yet. The loop writes here once it runs.'),
      last && last.detail && last.detail.stopped_reason
        ? h('p', { class: 'error' }, `Stopped: ${last.detail.stopped_reason}`)
        : null,
    ),
  );

  const alerts = status.alerts || [];
  const alertRows = alerts.map((item) =>
    h(
      'tr',
      {},
      h('td', {}, badge(item.code, levelTone(item.severity)), plainWords('alerts', item.code)),
      h('td', {}, item.severity),
      h('td', {}, item.summary, nextStep('alerts', item.code)),
    ),
  );
  const delivery = status.alert_delivery || [];
  root.append(
    panel(
      'Latest alerts',
      alerts.length
        ? table(['Alert', 'Severity', 'Summary'], alertRows)
        : h('p', { class: 'muted' }, 'No alerts on the latest pass.'),
      delivery.length
        ? h('p', { class: 'muted' }, `Delivery problems: ${delivery.map((item) => `${item.code}: ${item.error}`).join('; ')}`)
        : null,
    ),
  );

  const steps = (last && last.detail && last.detail.steps) || [];
  const stepRows = steps.map((step) => {
    const notes = stepNotes(step);
    return h(
      'tr',
      {},
      h('td', {}, step.name, plainWords('steps', step.name)),
      h('td', {}, stepSummary(step)),
      h('td', { class: 'num' }, String(step.skipped)),
      h('td', {}, step.failed || step.error ? badge('needs attention', 'bad') : badge('fine', 'good')),
      h(
        'td',
        { class: 'muted' },
        notes.length
          ? h('ul', { class: 'tight' }, notes.map((note) => h('li', {}, note)))
          : '',
      ),
    );
  });
  if (last) {
    root.append(
      panel(
        `Latest pass · ${last.started_at}`,
        h('p', { class: 'muted' }, `Outcome: ${last.outcome}`),
        plainWords('passes', last.outcome),
        table(['Step', 'Acted / examined', 'Skipped', 'State', 'Why'], stepRows),
      ),
    );
  }

  const outcomes = status.outcomes || {};
  const entries = Object.entries(outcomes);
  const nextSteps = entries.map(([outcome]) => nextStep('passes', outcome));
  // A column that is always empty reads as unfinished work, so it only appears when a pass in the
  // list actually asks something of a person.
  const withNext = nextSteps.some(Boolean);
  const outcomeRows = entries.map(([outcome, count], index) =>
    h(
      'tr',
      {},
      h('td', {}, badge(outcome, outcomeTone(outcome)), plainWords('passes', outcome)),
      h('td', { class: 'num' }, String(count)),
      withNext ? h('td', {}, nextSteps[index]) : null,
    ),
  );
  root.append(
    panel(
      'Passes by outcome',
      table(withNext ? ['Outcome', 'Count', 'What to do next'] : ['Outcome', 'Count'], outcomeRows),
    ),
  );
}

registerView('worker', renderWorker);
