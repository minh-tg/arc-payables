// Treasury visibility: forward coverage against what is coming, and the counterparty risk that
// constrains it. Both are read-only except for re-screening.

import { api, badge, h, panel, refresh, registerView, table } from './app.js';

function money(value) {
  return value === null || value === undefined ? '—' : `${value} USDC`;
}

async function renderTreasury(root) {
  const forecast = await api('/forecast?days=30');
  const suppliers = await api('/suppliers');

  const coverage = h(
    'dl',
    { class: 'facts' },
    h('dt', {}, 'Balance'),
    h('dd', {}, money(forecast.balance_usdc)),
    h('dt', {}, 'Reserve floor'),
    h('dd', {}, money(forecast.reserve_floor_usdc)),
    h('dt', {}, 'Due within horizon'),
    h('dd', {}, money(forecast.due_within_horizon_usdc)),
    h('dt', {}, 'Expected inflows'),
    h('dd', {}, money(forecast.inflow_within_horizon_usdc)),
    h('dt', {}, 'Coverable'),
    h('dd', {}, money(forecast.coverable_usdc)),
    h('dt', {}, 'Beyond horizon'),
    h('dd', {}, money(forecast.beyond_horizon_usdc)),
  );

  root.append(
    panel(
      `Forward coverage · next ${forecast.horizon_days} days`,
      forecast.shortfall
        ? h('p', { class: 'error' }, `Shortfall of ${money(forecast.shortfall_usdc)} from ${forecast.shortfall_date}: ${forecast.uncovered_invoice_ids.length} obligation(s) the balance cannot cover while keeping the reserve.`)
        : h('p', {}, badge('every obligation in the horizon is covered while keeping the reserve', 'good')),
      coverage,
      h('p', { class: 'muted' }, forecast.rationale),
      forecast.notes.length ? h('ul', { class: 'tight' }, forecast.notes.map((note) => h('li', {}, note))) : null,
    ),
  );

  const obligationRows = forecast.obligations.map((item) =>
    h(
      'tr',
      {},
      h('td', {}, h('a', { href: `#/invoice/${item.invoice_id}` }, item.invoice_number)),
      h('td', {}, item.supplier_id),
      h('td', { class: 'num' }, money(item.amount_usdc)),
      h('td', {}, item.due_date),
      h('td', {}, String(item.days_until_due)),
      h('td', {}, item.coverable ? badge('covered', 'good') : badge('uncovered', 'bad')),
      h('td', {}, item.payable_by_agent ? badge('payable', 'good') : badge('not payable', 'warn')),
      h('td', {}, item.discount_value_usdc ? `${item.discount_value_usdc} USDC by ${item.discount_deadline}` : '—'),
      h('td', {}, item.not_payable_reason || (item.reasons || []).join(', ')),
    ),
  );
  root.append(
    panel(
      'Obligations, in due-date order',
      h('p', { class: 'muted' }, 'Money owed is counted whether or not the agent may pay it, so a blocked invoice cannot quietly disappear from the forecast.'),
      table(['Invoice', 'Supplier', 'Amount', 'Due', 'Days', 'Coverage', 'Agent', 'Discount', 'Why not payable'], obligationRows),
    ),
  );

  const inflowRows = (forecast.inflows || []).map((item) =>
    h(
      'tr',
      {},
      h('td', {}, item.external_id),
      h('td', {}, item.customer || '—'),
      h('td', {}, item.reference || '—'),
      h('td', { class: 'num' }, money(item.amount_usdc)),
      h('td', {}, item.expected_date),
      h('td', { class: 'num' }, String(item.days_until_expected)),
    ),
  );
  root.append(
    panel(
      'Expected inflows, in expected-date order',
      h('p', { class: 'muted' }, 'Money customers owe us, added back to the running balance on the expected date. Inflows never authorize anything.'),
      table(['Sales invoice', 'Customer', 'Reference', 'Amount', 'Expected', 'Days'], inflowRows),
    ),
  );

  const rescreen = h(
    'button',
    {
      onclick: async (event) => {
        event.target.disabled = true;
        try {
          await api('/monitoring/rescreen?force=true', { method: 'POST' });
          await refresh();
        } catch (error) {
          alert(String(error.message));
          event.target.disabled = false;
        }
      },
    },
    'Re-screen all counterparties',
  );
  const riskRows = suppliers.map((item) =>
    h(
      'tr',
      {},
      h('td', {}, item.supplier_id),
      h('td', {}, item.name || '—'),
      h('td', {}, item.wallet ? `${item.wallet}${item.wallet_verified ? '' : ' (unverified)'}` : '—'),
      h('td', {}, item.risk_tier ? badge(item.risk_tier, item.risk_tier === 'low' ? 'good' : item.risk_tier === 'high' ? 'bad' : 'warn') : '—'),
      h('td', {}, item.latest_screening ? `${item.latest_screening.status} · ${item.latest_screening.checked_at}` : 'never screened'),
      h('td', { class: 'num' }, item.automatic_limit_usdc ? money(item.automatic_limit_usdc) : '—'),
      h('td', { class: 'num' }, String(item.open_invoices)),
      h('td', { class: 'num' }, money(item.open_amount_usdc)),
    ),
  );
  root.append(
    panel(
      'Counterparty risk',
      h('p', { class: 'muted' }, 'The tier scales the automatic limit rather than gating the counterparty: an unclear result buys a smaller unattended payment, and a flagged one still needs a human.'),
      h('div', { class: 'credentials' }, rescreen),
      table(['Supplier', 'Name', 'Wallet', 'Tier', 'Latest screening', 'Automatic limit', 'Open', 'Exposure'], riskRows),
    ),
  );
}

registerView('treasury', renderTreasury);
