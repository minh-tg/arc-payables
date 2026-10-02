// The landing screen: what is owed, what the guard will allow, what is waiting on a person, and
// what the agent has been doing.
//
// The activity panel is the point of this view. The agent's work is otherwise invisible on a screen
// that shows outcomes, so the pass, its steps and its refusals are printed where a reader lands.

import { api, badge, concept, h, lede, logPanel, panel, plainWords, registerView, sectionHeading, statGrid, table } from './app.js';

// A landing screen should not go blank because one optional fact is unavailable.
const optional = (path, fallback) => api(path).catch(() => fallback);

function clock(value) {
  // Postgres-style ISO timestamps are the only shape the service emits; anything else is shown as is.
  const match = typeof value === 'string' ? value.match(/T(\d{2}:\d{2}:\d{2})/) : null;
  return match ? match[1] : '';
}

function money(value) {
  return value === null || value === undefined ? '—' : `${value}`;
}

function decisionLayer(setup) {
  for (const group of (setup && setup.groups) || []) {
    for (const item of group.requirements || []) {
      if (item.env === 'DECISION_LAYER') return item;
    }
  }
  return null;
}

function activityLines(status, layer) {
  const lines = [];
  const last = status.last;
  if (layer) {
    // Say which layer is answering, and whether a model is in it. An operator who believes there is
    // one where there is not has been misled by the screen, not by the log.
    const dual = String(layer.value || '') === 'dual_process';
    lines.push({
      level: dual ? 'info' : 'warn',
      message: dual
        ? `Decision layer: ${layer.value}. A model advises on trade-offs; the policy still decides.`
        : `Decision layer: ${layer.value || 'unknown'}. Rule-based, so no model is called on this path.`,
    });
  }
  if (!last) {
    lines.push({ level: 'warn', message: 'No pass has been recorded, so nothing is running on its own yet.' });
    return lines;
  }
  lines.push({ time: clock(last.started_at), level: 'info', message: 'Pass started.' });
  for (const step of last.detail && last.detail.steps ? last.detail.steps : []) {
    const acted = `acted ${step.acted}/${step.examined}`;
    const skipped = step.skipped ? `, skipped ${step.skipped}` : '';
    const failed = step.failed ? `, failed ${step.failed}` : '';
    lines.push({
      level: step.failed || step.error ? 'error' : step.acted ? 'info' : 'warn',
      message: `${step.name}: ${acted}${skipped}${failed}${step.error ? ` (${step.error})` : ''}`,
    });
  }
  for (const alert of status.alerts || []) {
    lines.push({
      time: clock(last.finished_at),
      level: alert.severity === 'critical' ? 'error' : 'warn',
      message: `${alert.code}: ${alert.summary}`,
    });
  }
  lines.push({ time: clock(last.finished_at), level: last.outcome === 'ok' ? 'info' : 'error', message: `Pass finished: ${last.outcome}.` });
  return lines;
}

async function renderOverview(root) {
  const [forecast, attention, worker, setup] = await Promise.all([
    api('/forecast?days=30'),
    api('/attention'),
    optional('/worker/status', {}),
    optional('/setup', {}),
  ]);

  const caps = forecast.guard_caps_usdc || null;
  const waiting = (attention.critical || 0) + (attention.warning || 0);
  const layer = decisionLayer(setup);

  root.append(
    statGrid([
      {
        label: 'Due within horizon',
        value: String(forecast.due_within_horizon_usdc ?? '—'),
        unit: 'USDC',
        icon: 'fileText',
        note: `Over the next ${forecast.horizon_days} days, from the forecast`,
      },
      caps
        ? {
            label: 'Guard limit',
            value: String(caps.per_payment ?? '—'),
            unit: 'USDC / payment',
            icon: 'shield',
            concept: 'guard',
            note: `${caps.epoch} per epoch · ${caps.recipient_epoch} per recipient. Fixed at deployment.`,
          }
        : {
            label: 'Treasury',
            value: money(forecast.balance_usdc),
            unit: 'USDC',
            icon: 'wallet',
            concept: 'treasury',
            note: 'No guard caps published by the configured provider',
          },
      waiting === 0
        ? { label: 'Agent escalations', value: '0', icon: 'check', tone: 'good', note: 'Nothing is waiting on a person' }
        : {
            label: 'Agent escalations',
            value: String(waiting),
            icon: 'alert',
            tone: 'bad',
            note: 'Require human judgement to proceed',
          },
    ]),
  );

  const items = (attention.items || []).slice(0, 5);
  const itemRows = items.map((item) =>
    h(
      'tr',
      {},
      h('td', { class: 'mono' }, item.code),
      h('td', {}, badge(item.severity, item.severity === 'critical' ? 'bad' : 'warn')),
      h('td', { class: 'text-danger' }, item.summary),
      h('td', { style: 'text-align:right' }, h('button', { class: 'btn btn-secondary', onclick: () => { location.hash = '#/attention'; } }, 'Review')),
    ),
  );

  const activity = h(
    'section',
    { class: 'card' },
    sectionHeading({ eyebrow: 'Live stream', title: 'Agent activity', count: activityLines(worker, layer).length }),
    logPanel(activityLines(worker, layer)),
    lede(
      'This is the background pass as it ran, not a recording. The ',
      concept('policy_check', 'policy'),
      ' decides, the worker only does what a decision already authorized, and it approves nothing.',
    ),
  );

  const exceptions = h(
    'section',
    { class: 'card' },
    sectionHeading({
      eyebrow: 'Action queue',
      title: 'Exceptions',
      count: (attention.items || []).length,
      trailing: h('button', { class: 'btn btn-secondary', onclick: () => { location.hash = '#/attention'; } }, 'View all'),
    }),
    items.length
      ? table(['Code', 'Severity', 'Reason', ''], itemRows)
      : h('p', { class: 'muted' }, 'Nothing is waiting on a person.'),
  );

  root.append(
    h('div', { class: 'split' }, h('div', {}, exceptions), h('div', {}, activity)),
  );

  root.append(
    panel(
      'Coverage',
      h(
        'dl',
        { class: 'facts' },
        h('dt', {}, concept('treasury', 'Balance')),
        h('dd', {}, money(forecast.balance_usdc)),
        h('dt', {}, concept('reserve_floor', 'Reserve floor')),
        h('dd', {}, money(forecast.reserve_floor_usdc)),
        h('dt', {}, 'Coverable'),
        h('dd', {}, money(forecast.coverable_usdc)),
      ),
      forecast.shortfall
        ? h('p', { class: 'error' }, `Shortfall of ${forecast.shortfall_usdc} USDC from ${forecast.shortfall_date}.`, plainWords('attention', 'reserve_breached'))
        : h('p', {}, badge('every obligation in the horizon is covered while keeping the reserve', 'good')),
    ),
  );
}

registerView('overview', renderOverview);
