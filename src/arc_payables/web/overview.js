// Business questions first; operational detail remains available without dominating the landing page.
import { api, badge, concept, h, lede, logPanel, panel, registerView, sectionHeading, statGrid, table, workerHealth } from './app.js';
import { demoJourney } from './journey.js';

const optional = (path) => api(path).catch(() => null);

function activityLines(status) {
  if (!status) return [{ level: 'warn', message: 'Worker status could not be read. Open Worker to retry.' }];
  if (!status.last) return [{ level: 'warn', message: 'No background pass recorded. Manual review still works.' }];
  const last = status.last;
  return [
    { level: 'info', message: `Last recorded pass: ${last.started_at}. This is history, not a live stream.` },
    ...(last.detail?.steps || []).map((step) => ({
      level: step.failed || step.error ? 'error' : 'info',
      message: `${step.name}: ${step.acted}/${step.examined} acted, ${step.skipped || 0} skipped, ${step.failed || 0} failed${step.error ? ` · ${step.error}` : ''}`,
    })),
    { level: last.outcome === 'ok' ? 'info' : 'error', message: `Pass finished: ${last.outcome}.` },
  ];
}

async function renderOverview(root) {
  const [forecast, attention, worker, setup, plan, invoices] = await Promise.all([
    api('/forecast?days=30'), api('/attention'), optional('/worker/status'), optional('/setup'), optional('/plan'), optional('/invoices'),
  ]);
  const waiting = (attention.critical || 0) + (attention.warning || 0);
  root.append(sectionHeading({ eyebrow: 'Payment operations', title: 'Know what can move. See what needs you.', trailing: h('a', { class: 'btn btn-primary', href: '#/queue' }, 'Open payment queue') }));
  root.append(lede('Verified supplier invoices, bounded payments, and a ', concept('ledger', 'ledger'), ' that stays in sync. The policy decides what is safe; you decide how to resolve exceptions.'));
  root.append(statGrid([
    { label: 'Planned payment value', value: String(plan?.planned_spend_usdc ?? '—'), unit: 'USDC', note: 'Current read-only plan, not payment authorization', icon: 'check' },
    { label: 'Due in 30 days', value: String(forecast.due_within_horizon_usdc ?? '—'), unit: 'USDC', note: 'Includes obligations the agent cannot yet pay', icon: 'fileText' },
    { label: 'Available after reserve', value: String(plan?.spendable_usdc ?? '—'), unit: 'USDC', note: `Reserve floor: ${forecast.reserve_floor_usdc ?? '—'} USDC`, concept: 'reserve_floor', icon: 'wallet' },
    { label: 'Issues needing you', value: String(waiting), tone: waiting ? 'warn' : 'good', note: `${attention.critical || 0} critical · ${attention.warning || 0} warning`, icon: 'alert' },
  ]));
  if (!setup) root.append(h('p', { class: 'warn-text', role: 'status' }, 'Environment details unavailable. Payment mode must be confirmed again before submitting.'));
  const journey = demoJourney(invoices || [], setup?.deployment);
  if (journey) root.append(h('details', { class: 'demo-guide', open: !(invoices || []).some((row) => row.invoice.id === 'demo-invoice-legitimate' && row.state === 'ERP_RECORDED') }, h('summary', {}, 'Guided demo · no on-chain funds move'), journey));
  else root.append(panel('Ready for your first payment?',
    h('p', { class: 'muted' }, 'Check the ledger connection, verify supplier destinations, and preview policy decisions before enabling unattended payments.'),
    h('a', { class: 'btn', href: '#/setup' }, 'Review setup checklist'),
  ));

  const items = (attention.items || []).slice(0, 5);
  const rows = items.map((item) => {
    const invoice = item.detail?.invoices?.[0] || item.detail?.payments?.[0];
    const href = invoice ? `#/invoice/${encodeURIComponent(invoice.invoice_id)}` : '#/attention';
    return h('tr', {},
      h('td', {}, badge(item.severity, item.severity === 'critical' ? 'bad' : 'warn')),
      h('td', {}, item.summary),
      h('td', {}, invoice?.invoice_number || 'Deployment'),
      h('td', {}, h('a', { class: 'btn', href }, invoice ? 'Review invoice' : 'Resolve issue')),
    );
  });
  const exceptions = h('section', { class: 'card' },
    sectionHeading({ eyebrow: 'Next actions', title: 'Needs your judgement', count: (attention.items || []).length, trailing: h('a', { class: 'btn', href: '#/attention' }, 'View all') }),
    rows.length ? table(['Priority', 'What needs attention', 'Invoice', 'Action'], rows) : h('p', { class: 'muted' }, 'No issues waiting on you. Open the queue to review upcoming payments.'),
  );
  const health = workerHealth(worker);
  const coverage = panel('Treasury coverage',
    h('p', {}, forecast.shortfall ? badge('Shortfall forecast', 'warn') : badge('Covered within horizon', 'good')),
    forecast.shortfall
      ? h('p', { class: 'guide-note' }, `${forecast.shortfall_usdc} USDC shortfall from ${forecast.shortfall_date}. Review inflows and payment timing; do not bypass the reserve.`)
      : h('p', { class: 'muted' }, 'Due obligations are covered while preserving the configured reserve.'),
    h('a', { class: 'btn', href: '#/treasury' }, 'Review cash coverage'),
    h('div', { class: 'health-summary' }, badge(health.label, health.tone === 'ok' ? 'good' : health.tone), h('a', { href: '#/worker' }, 'Worker history')),
  );
  root.append(h('div', { class: 'split' }, exceptions, coverage));

  const caps = forecast.guard_caps_usdc;
  root.append(h('details', { class: 'card' },
    h('summary', {}, 'Payment safeguards & recorded activity'),
    h('p', { class: 'muted' }, caps
      ? `${caps.per_payment} USDC per payment · ${caps.epoch} per epoch · ${caps.recipient_epoch} per recipient. Fixed at deployment; refunds do not restore spent allowance.`
      : 'No on-chain guard budgets were published by this provider. Demo values are not deployed contract limits.'),
    caps?.paused ? badge('Guard paused', 'bad') : null,
    h('p', { class: 'guide-note' }, 'An agent can advise, but it cannot approve an exception or choose a different recipient.'),
    logPanel(activityLines(worker)),
    setup ? h('a', { href: '#/setup' }, 'Configuration & decision layer') : null,
  ));
}

registerView('overview', renderOverview);
