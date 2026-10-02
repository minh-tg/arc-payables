// Treasury visibility: forward coverage against what is coming, and the counterparty risk that
// constrains it. Both are read-only except for re-screening.

import { api, badge, h, panel, refresh, registerView, statGrid, table } from './app.js';

function money(value) {
  return value === null || value === undefined ? '—' : `${value} USDC`;
}

async function renderTreasury(root) {
  const forecast = await api('/forecast?days=30');
  const suppliers = await api('/suppliers');

  root.append(
    statGrid([
      { label: 'Balance', value: String(forecast.balance_usdc ?? '—'), unit: 'USDC' },
      { label: 'Reserve floor', value: String(forecast.reserve_floor_usdc ?? '—'), unit: 'USDC' },
      { label: 'Due within horizon', value: String(forecast.due_within_horizon_usdc ?? '—'), unit: 'USDC' },
      { label: 'Coverable', value: String(forecast.coverable_usdc ?? '—'), unit: 'USDC' },
    ]),
  );

  // The caps the contract enforces, beside the coverage they constrain. Read-only numbers the
  // provider already publishes, shown where the money they bind is actually discussed.
  if (forecast.guard_caps_usdc) {
    const caps = forecast.guard_caps_usdc;
    const bar = (used, cap) => {
      const share = cap > 0 ? Math.min(100, Math.max(0, (used / cap) * 100)) : 0;
      return h(
        'div',
        { class: `meter${share >= 100 ? ' full' : share >= 75 ? ' hot' : ''}`, title: `${used.toLocaleString()} of ${cap.toLocaleString()} USDC` },
        h('i', { style: `width:${share}%` }),
      );
    };
    const epochSpent = Number(caps.epoch_spent ?? 0);
    root.append(
      panel(
        'Guard budgets · per payment, per epoch, per recipient',
        table(
          ['Budget', 'Cap', 'Used', ''],
          [
            h('tr', {}, h('td', {}, 'Per payment'), h('td', { class: 'num' }, `${caps.per_payment} USDC`), h('td', { class: 'muted' }, 'hard cap per call'), h('td', {}, bar(0, caps.per_payment))),
            h('tr', {}, h('td', {}, 'Epoch total'), h('td', { class: 'num' }, `${caps.epoch} USDC`), h('td', { class: 'num' }, `${epochSpent} USDC`), h('td', {}, bar(epochSpent, caps.epoch))),
            h('tr', {}, h('td', {}, 'Per recipient'), h('td', { class: 'num' }, `${caps.recipient_epoch} USDC`), h('td', { class: 'muted' }, 'tracked per recipient on chain'), h('td', {}, null)),
          ],
        ),
        h('p', { class: 'muted' }, caps.paused ? 'The guard is paused: no payment can settle.' : `Window rolls every ${caps.epoch_days} day(s). Refunds do not restore a spent allowance.`),
      ),
    );
  }

  root.append(
    panel(
      `Forward coverage · next ${forecast.horizon_days} days`,
      forecast.shortfall
        ? h('p', { class: 'error' }, `Shortfall of ${money(forecast.shortfall_usdc)} from ${forecast.shortfall_date}: ${forecast.uncovered_invoice_ids.length} obligation(s) the balance cannot cover while keeping the reserve.`)
        : h('p', {}, badge('every obligation in the horizon is covered while keeping the reserve', 'good')),
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
