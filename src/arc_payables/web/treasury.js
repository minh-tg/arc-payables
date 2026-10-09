// Treasury visibility: forward coverage against what is coming, and the counterparty risk that
// constrains it. Both are read-only except for re-screening.

import { actionButton, api, badge, can, concept, h, lede, panel, plainWords, refresh, registerView, sectionHeading, statGrid, table } from './app.js';

function money(value) {
  return value === null || value === undefined ? '—' : `${value} USDC`;
}

// Expected inflows as the forecast counts them. Syncing reads open sales invoices from the accounting
// system; marking one collected stops counting it. Neither authorizes or moves anything.
// A sync re-renders the view, which would erase its own confirmation, so the message is carried across
// the re-render once and shown by the panel that draws next.
let syncFlash = null;

async function receivablesPanel() {
  const listing = await api('/receivables');
  const feedback = h('div', { role: 'status', 'aria-live': 'polite' });
  if (syncFlash) {
    feedback.append(h('p', { role: 'status' }, syncFlash));
    syncFlash = null;
  }
  const sync = can('operate')
    ? actionButton('Sync from accounting', async () => {
        const outcome = await api('/receivables/sync', { method: 'POST' });
        syncFlash = `${outcome.recorded} open sales invoice(s) recorded as expected inflows.`;
        await refresh();
      }, feedback, { class: 'btn-primary' })
    : h('p', { class: 'muted' }, 'Operator role required to sync.');
  const rows = listing.receivables.map((item) =>
    h(
      'tr',
      {},
      h('td', {}, item.external_id),
      h('td', {}, item.customer || '—'),
      h('td', {}, item.reference || '—'),
      h('td', { class: 'num' }, money(item.amount_usdc)),
      h('td', {}, item.expected_date),
      h('td', {}, can('operate')
        ? actionButton('Mark collected', async () => {
            await api(`/receivables/${encodeURIComponent(item.external_id)}/collect`, { method: 'POST' });
            await refresh();
          }, feedback)
        : '—'),
    ),
  );
  return panel(
    'Receivables from accounting',
    lede(
      'Money customers owe, counted into the running balance on its expected date. Once it has arrived, mark it collected so it stops counting.',
    ),
    h('div', { class: 'credentials' }, sync),
    feedback,
    table(['Sales invoice', 'Customer', 'Reference', 'Amount', 'Expected', 'Action'], rows),
  );
}

async function renderTreasury(root) {
  const forecast = await api('/forecast?days=30');
  const suppliers = await api('/suppliers');
  root.append(sectionHeading({ eyebrow: 'Coverage and risk', title: 'Treasury and risk', count: suppliers.length }));

  // A view-level lede, rendered whatever the deployment publishes. Placed inside a panel that only
  // appears when a provider reports caps, it would disappear on any deployment without them.
  root.append(
    lede(
      'What the money covers, what is coming, and who may be paid without a person looking. ',
      'Coverage counts what is owed whether or not the agent may pay it, so a blocked invoice cannot ',
      'quietly leave the forecast.',
    ),
  );

  root.append(
    statGrid([
      { label: 'Balance', value: String(forecast.balance_usdc ?? '—'), unit: 'USDC', concept: 'treasury' },
      { label: 'Reserve floor', value: String(forecast.reserve_floor_usdc ?? '—'), unit: 'USDC', concept: 'reserve_floor' },
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
        lede(
          'The ', concept('guard', 'guard'), ' holds these caps on the chain itself, so they apply even '
          + 'if everything above this contract is wrong. A budget cannot be edited in place, and a '
          + 'refund does not give back what it spent.',
        ),
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

  root.append(await receivablesPanel());

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
      h(
        'td',
        {},
        item.risk_tier ? badge(item.risk_tier, item.risk_tier === 'low' ? 'good' : item.risk_tier === 'high' ? 'bad' : 'warn') : '—',
        plainWords('tiers', item.risk_tier),
      ),
      h(
        'td',
        {},
        item.latest_screening ? `${item.latest_screening.status} · ${item.latest_screening.checked_at}` : 'never screened',
        plainWords('screening', item.latest_screening && item.latest_screening.status),
      ),
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
      table(
        [
          'Supplier',
          'Name',
          concept('wallet', 'Wallet'),
          concept('tier', 'Tier'),
          concept('screening', 'Latest screening'),
          'Automatic limit',
          'Open',
          'Exposure',
        ],
        riskRows,
      ),
    ),
  );
}

registerView('treasury', renderTreasury);
