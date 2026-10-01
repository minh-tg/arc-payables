// The payable queue: what the agent would pay first, and why, plus every invoice's state.

import { api, badge, h, panel, refresh, registerView, stateTone, table } from './app.js';

function money(value) {
  return value === null || value === undefined ? '—' : `${value} USDC`;
}

async function renderQueue(root) {
  const [plan, invoices] = await Promise.all([api('/plan'), api('/invoices')]);

  root.append(
    panel(
      'Treasury',
      h(
        'dl',
        { class: 'facts' },
        h('dt', {}, 'Balance'),
        h('dd', {}, money(plan.balance_usdc)),
        h('dt', {}, 'Reserve floor'),
        h('dd', {}, money(plan.reserve_floor_usdc)),
        h('dt', {}, 'Spendable'),
        h('dd', {}, money(plan.spendable_usdc)),
        h('dt', {}, 'Planned spend'),
        h('dd', {}, money(plan.planned_spend_usdc)),
        h('dt', {}, 'Ordered by'),
        h('dd', {}, plan.ordered_by),
      ),
      h('p', { class: 'muted' }, plan.rationale),
      plan.deliberations.length
        ? h(
            'details',
            {},
            h('summary', {}, `Deliberation (${plan.deliberations.length})`),
            h(
              'ul',
              { class: 'tight' },
              plan.deliberations.map((item) =>
                h(
                  'li',
                  {},
                  `${item.task || item.layer} · ${item.outcome}${item.model ? ` · ${item.model}` : ''}${
                    item.prompt_sha256 ? ` · prompt ${item.prompt_sha256.slice(0, 12)}` : ''
                  }`,
                ),
              ),
            ),
          )
        : null,
    ),
  );

  const orderedRows = plan.ordered.map((entry, index) =>
    h(
      'tr',
      {},
      h('td', {}, String(index + 1)),
      h('td', {}, h('a', { href: `#/invoice/${entry.invoice_id}` }, entry.invoice_number)),
      h('td', {}, entry.supplier_id),
      h('td', { class: 'num' }, money(entry.amount_usdc)),
      h('td', {}, (entry.reasons || []).join(', ') || '—'),
      h('td', { class: 'num' }, money(entry.projected_balance_usdc)),
      h('td', {}, entry.reason),
    ),
  );
  root.append(panel('Pay in this order', table(['#', 'Invoice', 'Supplier', 'Amount', 'Why', 'Balance after', 'Detail'], orderedRows)));

  const excludedRows = plan.excluded.map((entry) =>
    h(
      'tr',
      {},
      h('td', {}, h('a', { href: `#/invoice/${entry.invoice_id}` }, entry.invoice_number)),
      h('td', { class: 'num' }, money(entry.amount_usdc)),
      h('td', {}, entry.decision_action ? badge(entry.decision_action, stateTone(entry.decision_action)) : '—'),
      h('td', {}, entry.reason),
    ),
  );
  root.append(panel('Not payable now', table(['Invoice', 'Amount', 'Policy', 'Reason'], excludedRows)));

  const evaluate = async (invoiceId, button) => {
    button.disabled = true;
    button.textContent = 'Evaluating…';
    try {
      await api(`/invoices/${invoiceId}/evaluate`, { method: 'POST' });
    } finally {
      await refresh();
    }
  };

  // The list endpoint answers with an object per invoice carrying its own state, not a pair.
  const invoiceRows = invoices.map(({ invoice, state }) =>
    h(
      'tr',
      {},
      h('td', {}, h('a', { href: `#/invoice/${invoice.id}` }, invoice.id)),
      h('td', {}, invoice.invoice_number),
      h('td', { class: 'num' }, money(invoice.amount)),
      h('td', {}, invoice.due_date),
      h('td', {}, badge(state, stateTone(state))),
      h(
        'td',
        {},
        h('button', { onclick: (event) => evaluate(invoice.id, event.target) }, 'Evaluate'),
      ),
    ),
  );
  root.append(panel('All invoices', table(['ID', 'Number', 'Amount', 'Due', 'State', ''], invoiceRows)));
}

registerView('queue', renderQueue);
