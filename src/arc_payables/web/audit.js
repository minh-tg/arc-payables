// The audit chain: whether the record still verifies, and where to check one payment's chain.
//
// The mock this view follows drew this screen as an empty state with a verification button. There is
// no global event endpoint behind the console, so the button is real and the per-payment history is
// reached through the invoice it belongs to, rather than being invented here.

import { api, badge, concept, h, lede, panel, plainWords, registerView, sectionHeading, statGrid, table } from './app.js';

function clock(value) {
  const match = typeof value === 'string' ? value.match(/T(\d{2}:\d{2}:\d{2})/) : null;
  return match ? match[1] : value || '—';
}

async function renderAudit(root) {
  const log = await api('/payments');
  const payments = (log.payments || []).slice(0, 25);

  root.append(
    sectionHeading({
      eyebrow: 'Evidence',
      title: 'Audit chain',
      count: payments.length,
    }),
  );

  root.append(
    lede(
      'Every event is hashed together with the one before it and signed. Verifying recomputes the ',
      'chain from the start, so an edited or removed entry is found rather than trusted. It reads ',
      'only, and it changes nothing.',
    ),
  );

  const verdict = h('div', { style: 'margin-top:12px' });
  const verify = h(
    'button',
    {
      class: 'btn btn-primary',
      onclick: async (event) => {
        event.target.disabled = true;
        event.target.textContent = 'Verifying…';
        try {
          const result = await api('/audit/verify');
          verdict.replaceChildren(
            result.ok
              ? h(
                  'div',
                  {},
                  badge(`intact · ${result.checked} entries · ${result.signed} signed`, 'good'),
                  h('p', { class: 'muted', style: 'margin-top:8px' }, 'The chain recomputed to the same hash it was written with.'),
                )
              : h(
                  'div',
                  {},
                  badge(`broken at entry ${result.first_broken_id}`, 'bad'),
                  h('p', { class: 'text-danger', style: 'margin-top:8px' }, result.reason || 'The chain did not verify.'),
                  plainWords('attention', 'audit_chain_broken'),
                ),
          );
        } catch (error) {
          verdict.replaceChildren(h('p', { class: 'error' }, String(error.message)));
        } finally {
          event.target.disabled = false;
          event.target.textContent = 'Run verification';
        }
      },
    },
    'Run verification',
  );

  root.append(
    panel(
      'Chain verification',
      h('p', { class: 'muted' }, 'This is the whole database, not one payment. Anything that no longer reconciles is named by entry.'),
      h('div', { class: 'credentials' }, verify),
      verdict,
    ),
  );

  const totals = log.totals || {};
  const broken = payments.filter((row) => row.outcome && row.outcome.code === 'unconfirmed').length;
  root.append(
    statGrid([
      { label: 'Settlements recorded', value: String(totals.settled ?? 0), icon: 'fileText', note: 'Payments the chain accepted and the books took' },
      { label: 'Signed events', value: String(payments.length), icon: 'shield', note: 'Each entry is signed by the configured signer' },
      {
        label: 'Results nobody knows',
        value: String(broken),
        icon: 'alert',
        tone: broken ? 'bad' : 'good',
        note: broken ? 'Reconcile these against the chain' : 'Every recorded result is known',
      },
    ]),
  );

  const rows = payments.map((row) =>
    h(
      'tr',
      {},
      h('td', {}, h('a', { href: `#/invoice/${row.invoice_id}` }, row.invoice_number)),
      h('td', {}, row.supplier_id),
      h('td', {}, clock(row.settled_at || row.confirmed_at)),
      h('td', {}, row.confirmation_status),
      h(
        'td',
        {},
        row.transaction_hash
          ? h('span', { class: 'mono' }, `${String(row.transaction_hash).slice(0, 18)}…`)
          : '—',
      ),
      h(
        'td',
        { style: 'text-align:right' },
        h('a', { href: `#/invoice/${row.invoice_id}` }, 'Open chain'),
      ),
    ),
  );

  root.append(
    panel(
      concept('audit_chain', 'Per-payment chains'),
      h(
        'p',
        { class: 'muted' },
        'One payment at a time, on the invoice it belongs to. Pick one to read its events in order, with the entry hash and whether it was signed.',
      ),
      payments.length
        ? table(['Invoice', 'Supplier', 'Settled', 'Confirmation', 'Transaction', ''], rows)
        : h('p', { class: 'muted' }, 'No payment has been authorized yet, so there is no chain to read.'),
      h('p', { class: 'muted' }, 'Every attempt, refusal and reconciliation is in the same chain as the payment itself.'),
    ),
  );
}

registerView('audit', renderAudit);
