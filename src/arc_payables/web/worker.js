// The worker's face: recent passes, whether they finished cleanly, and what they asked
// a human to look at. Read-only. Alerts are recorded on the pass, not recomputed here.

import { api, badge, h, panel, registerView, table } from './app.js';

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
      h('td', {}, badge(item.code, levelTone(item.severity))),
      h('td', {}, item.severity),
      h('td', {}, item.summary),
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
  const stepRows = steps.map((step) =>
    h(
      'tr',
      {},
      h('td', {}, step.name),
      h('td', {}, stepSummary(step)),
      h('td', { class: 'num' }, String(step.skipped)),
      h('td', {}, step.failed || step.error ? badge('needs attention', 'bad') : badge('fine', 'good')),
    ),
  );
  if (last) {
    root.append(
      panel(
        `Latest pass · ${last.started_at}`,
        h('p', { class: 'muted' }, `Outcome: ${last.outcome}`),
        table(['Step', 'Acted / examined', 'Skipped', 'State'], stepRows),
      ),
    );
  }

  const outcomes = status.outcomes || {};
  const outcomeRows = Object.entries(outcomes).map(([outcome, count]) =>
    h('tr', {}, h('td', {}, badge(outcome, outcomeTone(outcome))), h('td', { class: 'num' }, String(count))),
  );
  root.append(panel('Passes by outcome', table(['Outcome', 'Count'], outcomeRows)));
}

registerView('worker', renderWorker);
