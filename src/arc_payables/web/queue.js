// The payable queue: what the agent would pay first, and why, plus every invoice's state.

import { actionButton, api, badge, can, concept, h, lede, nextStep, panel, refresh, registerView, sectionHeading, stateTone, table } from './app.js';

function money(value) {
  return value === null || value === undefined ? '—' : `${value} USDC`;
}

// Two ways in. Importing adopts the accounting record, which is the normal route. Recording a document
// holds it until a person links it to a payable, because a captured document is not a payable.
function addPayablePanel() {
  const feedback = h('div', { role: 'status', 'aria-live': 'polite' });

  const importId = h('input', { type: 'text', maxlength: '140', autocomplete: 'off', placeholder: 'ACC-PINV-2026-00007' });
  const importButton = actionButton('Import payable', async () => {
    const external = importId.value.trim();
    if (!external) throw new Error('Enter the ERPNext purchase invoice number to import.');
    const created = await api('/invoices/import', { method: 'POST', body: { external_invoice_id: external } });
    window.location.hash = `#/invoice/${encodeURIComponent(created.invoice.id)}`;
  }, feedback, { class: 'btn-primary' });

  const fields = {
    supplier_id: h('input', { type: 'text', maxlength: '140', autocomplete: 'off' }),
    invoice_number: h('input', { type: 'text', maxlength: '140', autocomplete: 'off' }),
    invoice_date: h('input', { type: 'date' }),
    due_date: h('input', { type: 'date' }),
    amount: h('input', { type: 'text', inputmode: 'decimal', placeholder: '0.01', autocomplete: 'off' }),
  };
  const labels = {
    supplier_id: 'Supplier ID', invoice_number: 'Invoice number', invoice_date: 'Invoice date',
    due_date: 'Due date', amount: 'Amount (USDC)',
  };
  const recordButton = actionButton('Record invoice', async () => {
    const body = { currency: 'USDC' };
    for (const key of Object.keys(fields)) body[key] = fields[key].value.trim();
    const missing = Object.keys(fields).filter((key) => !body[key]).map((key) => labels[key]);
    if (missing.length) throw new Error(`Fill in: ${missing.join(', ')}.`);
    const created = await api('/invoices', { method: 'POST', body });
    window.location.hash = `#/invoice/${encodeURIComponent(created.invoice.id)}`;
  }, feedback);

  return panel(
    'Add a payable',
    h('h3', {}, 'Import from ERPNext'),
    h('p', { class: 'muted' }, 'Adopts the accounting record\'s amount, currency and lines. This is the normal route into the queue.'),
    h('div', { class: 'credentials' }, h('label', { class: 'field' }, h('span', {}, 'ERPNext purchase invoice'), importId), importButton),
    h('h3', {}, 'Record an invoice document'),
    h('p', { class: 'muted' }, 'A document recorded here is held until someone links it to an ERPNext payable. Its own text never sets the amount or the destination.'),
    h('div', { class: 'stats' }, Object.keys(fields).map((key) => h('label', { class: 'field' }, h('span', {}, labels[key]), fields[key]))),
    h('div', { class: 'credentials' }, recordButton),
    feedback,
  );
}

async function renderQueue(root) {
  const [plan, invoices, history] = await Promise.all([api('/plan'), api('/invoices'), api('/plans')]);
  root.append(sectionHeading({ eyebrow: 'Plan', title: 'Payment queue', count: invoices.length }));

  root.append(
    panel(
      'Treasury',
      lede(
        'This is a read-only preview, not an execution record. The worker plans only policy-eligible invoices and rechecks before each payment. Every invoice is priced in ',
        concept('usdc', 'USDC'),
        ', a digital dollar, so nothing here depends on an exchange rate.',
      ),
      h(
        'dl',
        { class: 'facts' },
        h('dt', {}, 'Balance'),
        h('dd', {}, money(plan.balance_usdc)),
        h('dt', {}, concept('reserve_floor', 'Reserve floor')),
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
      // "Not payable now" is where a first-time operator meets ESCALATE and has no idea what to do
      // about it, so the guided view answers that beside the reason.
      h('td', {}, entry.reason, nextStep('decisions', entry.decision_action)),
    ),
  );
  root.append(panel('Not payable now', table(['Invoice', 'Amount', 'Policy', 'Reason'], excludedRows)));

  const labels = {
    recorded: ['Outcome not recorded', 'warn'], executed: ['Payment attempted', ''],
    invalidated: ['Replanned', 'warn'], deferred: ['Deferred', 'warn'],
    failed: ['Stopped with error', 'bad'], empty: ['Nothing payable', ''],
  };
  const historyRows = history.plans.map((record) => {
    const saved = record.plan;
    const outcome = record.outcome || {};
    const [label, tone] = labels[record.status] || [record.status, ''];
    const chosen = saved.ordered.find((entry) => entry.invoice_id === outcome.invoice_id);
    return h('tr', {},
      h('td', {}, record.created_at),
      h('td', {}, badge(label, tone), h('p', { class: 'muted' }, outcome.reason || 'No completion proof; this advice will not be replayed.')),
      h('td', {}, saved.ordered_by),
      h('td', {}, outcome.invoice_id
        ? h('a', { href: `#/invoice/${encodeURIComponent(outcome.invoice_id)}` }, chosen?.invoice_number || outcome.invoice_id)
        : '—'),
      h('td', {}, outcome.confirmation_status || '—', outcome.erp_status ? h('p', { class: 'muted' }, `Ledger: ${outcome.erp_status}`) : null),
      h('td', {}, h('details', {},
        h('summary', {}, 'Plan details'),
        h('p', {}, saved.rationale),
        h('p', {}, `Balance: ${money(saved.balance_usdc)} · Reserve: ${money(saved.reserve_floor_usdc)} · Projected spend: ${money(saved.planned_spend_usdc)}`),
        h('ol', { class: 'tight' }, saved.ordered.map((entry) => h('li', {},
          h('a', { href: `#/invoice/${encodeURIComponent(entry.invoice_id)}` }, entry.invoice_number),
          ` · ${money(entry.amount_usdc)} · ${entry.reason}`))),
        saved.excluded.length ? h('p', {}, 'Excluded: ', saved.excluded.map((entry) => `${entry.invoice_number}: ${entry.reason}`).join('; ')) : null,
        h('p', { class: 'muted' }, `Record ${record.id} · digest ${record.digest}`),
        saved.deliberations.length ? h('p', { class: 'muted' }, saved.deliberations.map((item) =>
          `${item.outcome}${item.model ? ` · ${item.model}` : ''}${item.latency_ms !== undefined ? ` · ${item.latency_ms} ms` : ''}`).join('; ')) : null,
      )),
    );
  });
  root.append(panel('Recorded worker plans',
    h('p', { class: 'muted' }, 'Advice is recorded before authorization. Only the first selected invoice is attempted, then the worker replans. A payment attempt is not proof of settlement; inspect its confirmation and ledger result. Reading this history never moves funds.'),
    historyRows.length ? table(['Recorded at', 'Outcome', 'Ordered by', 'Selected invoice', 'Settlement', 'Evidence'], historyRows)
      : h('p', { class: 'muted' }, 'No worker plans recorded yet. Manual payments do not create worker plans.'),
  ));

  const feedback = h('div', { role: 'status', 'aria-live': 'polite' });
  const evaluate = async (invoiceId) => {
    await api(`/invoices/${encodeURIComponent(invoiceId)}/evaluate`, { method: 'POST' });
    await refresh();
  };

  // The list endpoint answers with an object per invoice carrying its own state, not a pair.
  const invoiceRows = invoices.map(({ invoice, state, payment }) =>
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
        payment ? h('a', { class: 'btn', href: `#/invoice/${encodeURIComponent(invoice.id)}` }, 'View payment') : can('operate') ? actionButton('Evaluate', () => evaluate(invoice.id), feedback) : h('span', { class: 'muted' }, 'Operator role required'),
      ),
    ),
  );

  root.append(
    can('operate')
      ? addPayablePanel()
      : panel('Add a payable', h('p', { class: 'muted' }, 'Operator role required to import or record an invoice.')),
  );

  root.append(
    panel(
      'All invoices',
      lede(
        'Every invoice, whatever state it is in, with the row that says why. The state is the ',
        concept('policy_check', 'policy'),
        ' result, not a human judgement.',
      ),
      table(['ID', 'Number', 'Amount', 'Due', 'State', 'Action'], invoiceRows),
      feedback,
    ),
  );
}

registerView('queue', renderQueue);
