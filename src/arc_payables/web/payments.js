// The settlement log, and the lookup for one payment.
//
// This is the screen for the question asked after the fact: did this payment succeed, and if not,
// why. A row links to the invoice for the deep evidence, and the lookup accepts whatever an operator
// is holding, which is usually a transaction hash from a block explorer.

import { actionButton, api, apiKey, badge, can, concept, explain, h, humanizeCode, lede, nextStep, panel, plainWords, registerView, sectionHeading, statGrid, table } from './app.js';

// Every recorded payment compared with the guard's own settlement log. Reads the chain and signs or
// sends nothing. Absence is only proof when the search began at block 0, so the coverage is shown.
function reconciliationPanel() {
  const feedback = h('div', { role: 'status', 'aria-live': 'polite' });
  const resultHost = h('div', {});
  const run = actionButton('Reconcile with the chain', async () => {
    const report = await api('/reconciliation/run', { method: 'POST' });
    resultHost.replaceChildren(...reconciliationResult(report));
  }, feedback, { class: 'btn-primary' });
  return panel(
    'Reconcile with the chain',
    lede(
      'This compares every recorded payment with what the guard settled on chain. It reads only. Run it after unattended payments and before closing a period.',
    ),
    can('operate')
      ? h('div', { class: 'credentials' }, run)
      : h('p', { class: 'muted' }, 'Operator role required to run reconciliation.'),
    feedback,
    resultHost,
  );
}

function reconciliationResult(report) {
  const coverage = report.coverage || {};
  const fromBlock = coverage.complete_from_block;
  const searched = fromBlock === 0
    ? 'block 0, so an absent settlement is proven absent'
    : 'a bounded or unstated range, so an absent settlement is not proof of absence';
  const summary = report.blocking
    ? badge(`${report.blocking} blocking finding(s)`, 'bad')
    : badge('no blocking disagreement', 'good');
  const facts = h(
    'dl',
    { class: 'facts' },
    h('dt', {}, 'Provider'), h('dd', {}, coverage.provider || '—'),
    h('dt', {}, 'Guard'), h('dd', { class: 'mono' }, coverage.guard_address || '—'),
    h('dt', {}, 'Records compared'), h('dd', {}, String(report.records)),
    h('dt', {}, 'Settlement events seen'), h('dd', {}, String(report.observations)),
    h('dt', {}, 'Searched'), h('dd', {}, searched),
  );
  const findingRows = (report.findings || []).map((item) =>
    h(
      'tr',
      {},
      h('td', {}, badge(item.severity, item.severity === 'blocking' ? 'bad' : 'warn')),
      h('td', {}, item.kind),
      h('td', {}, item.detail),
      h('td', {}, item.invoice_id ? h('a', { href: `#/payments/${encodeURIComponent(item.invoice_id)}` }, 'Open report') : '—'),
      h('td', { class: 'mono' }, item.transaction_hash ? `${item.transaction_hash.slice(0, 10)}…` : '—'),
    ),
  );
  return [
    h('p', {}, summary, h('span', { class: 'muted' }, ` ${report.review} for review`)),
    facts,
    findingRows.length
      ? table(['Severity', 'Kind', 'Detail', 'Payment', 'Transaction'], findingRows)
      : h('p', { class: 'muted' }, 'Nothing to report: every recorded payment is accounted for.'),
  ];
}

function money(value) {
  return value === null || value === undefined ? '—' : `${value} USDC`;
}

// An outcome that asks something of a person gets the next move beside it; a settled one does not.
const PROBLEM_OUTCOMES = ['settled_not_recorded', 'unconfirmed', 'failed', 'rejected'];

function outcomeWords(code) {
  return PROBLEM_OUTCOMES.includes(code) ? nextStep('outcomes', code) : null;
}

function outcomeTone(item) {
  if (!item) return '';
  if (item.success) return 'good';
  if (item.code === 'not_attempted' || item.code === 'in_flight') return 'warn';
  return 'bad';
}

function outcomeBadge(item) {
  return badge(item.code.replaceAll('_', ' '), outcomeTone(item));
}

function filters(state, onPick) {
  const options = [
    ['all', 'Everything'],
    ['settled', 'Settled and recorded'],
    ['unrecorded', 'Settled, not recorded'],
    ['unconfirmed', 'Unconfirmed'],
    ['failed', 'Failed'],
  ];
  return h(
    'div',
    { class: 'credentials' },
    options.map(([key, label]) =>
      h(
        'button',
        { onclick: () => onPick(key), disabled: state === key },
        label,
      ),
    ),
  );
}

function keep(row, filter) {
  if (filter === 'all') return true;
  if (filter === 'settled') return row.outcome.code === 'settled_and_recorded';
  if (filter === 'unrecorded') return row.outcome.code === 'settled_not_recorded';
  if (filter === 'unconfirmed') return row.outcome.code === 'unconfirmed';
  if (filter === 'failed') return ['failed', 'rejected'].includes(row.outcome.code);
  return true;
}

function reportPanels(report) {
  const panels = [];
  const settlement = report.settlement || {};
  const authorization = report.authorization || {};
  const ledger = report.ledger || {};

  panels.push(
    panel(
      'Outcome',
      h('p', {}, outcomeBadge(report.outcome), ' ', report.outcome.summary),
      plainWords('outcomes', report.outcome.code),
      report.outcome.reason ? h('p', { class: 'muted' }, `Reason: ${report.outcome.reason}`) : null,
      h(
        'dl',
        { class: 'facts' },
        h('dt', {}, 'Matched by'),
        h('dd', {}, `${report.reference.matched_by} · ${report.reference.value}`),
        h('dt', {}, 'Invoice'),
        h('dd', {}, h('a', { href: `#/invoice/${report.invoice.id}` }, report.invoice.invoice_number)),
        h('dt', {}, 'Amount'),
        h('dd', {}, money(settlement.amount_usdc)),
        h('dt', {}, 'Recipient'),
        h('dd', {}, settlement.recipient || '—'),
        h('dt', {}, 'Payer'),
        h('dd', {}, settlement.payer || '—'),
      ),
    ),
  );

  panels.push(
    panel(
      'Settlement',
      h(
        'dl',
        { class: 'facts' },
        h('dt', {}, 'Confirmation'),
        h('dd', {}, settlement.confirmation_status || '—', plainWords('confirmations', settlement.confirmation_status)),
        h('dt', {}, concept('network_fee', 'Fee we booked')),
        h('dd', {}, money(settlement.fee_usdc)),
        h('dt', {}, 'Settled at'),
        h('dd', {}, settlement.settled_at || '—'),
        h('dt', {}, 'Transaction'),
        h(
          'dd',
          {},
          // Only an https link is rendered. The server builds this from a fixed explorer base, but a
          // link whose href arrives from the API is worth a scheme check rather than trust: a
          // javascript: URL there would execute on click. The hash is shown regardless.
          settlement.explorer_url && String(settlement.explorer_url).startsWith('https://')
            ? h('a', { href: settlement.explorer_url, target: '_blank', rel: 'noreferrer' }, settlement.transaction_hash)
            : settlement.transaction_hash || '—',
        ),
      ),
    ),
  );

  panels.push(
    panel(
      'Authorization',
      h('p', { class: 'muted' }, 'The permit binds the amount and the destination to one evidence hash. The signature itself is not returned here, because a read-only screen should not hand back material that could authorize anything.'),
      plainWords('guard', 'permit'),
      h(
        'dl',
        { class: 'facts' },
        h('dt', {}, 'Permit'),
        h('dd', {}, authorization.permit_id || '—'),
        h('dt', {}, 'Evidence hash'),
        h('dd', {}, authorization.evidence_hash || '—'),
        h('dt', {}, 'Expires at'),
        h('dd', {}, authorization.expires_at === undefined || authorization.expires_at === null ? '—' : String(authorization.expires_at)),
        h('dt', {}, 'Signature verifies'),
        h(
          'dd',
          {},
          authorization.signature_verified === true
            ? badge('verified against the configured signer', 'good')
            : authorization.signature_verified === false
              ? badge('does not verify', 'bad')
              : '—',
        ),
        h('dt', {}, 'Signer'),
        h('dd', {}, authorization.signer_address || '—'),
      ),
      authorization.signature_note ? h('p', { class: 'muted' }, authorization.signature_note) : null,
    ),
  );

  panels.push(
    panel(
      'Ledger writeback',
      h('p', { class: 'muted' }, 'Money can move on the chain before the books accept it. Writing back is idempotent, so a retry cannot pay anyone twice.'),
      h(
        'dl',
        { class: 'facts' },
        h('dt', {}, 'Status'),
        h('dd', {}, ledger.status || '—'),
        h('dt', {}, 'Fee status'),
        h('dd', {}, ledger.fee_status || '—'),
        h('dt', {}, 'Payment entry'),
        h('dd', {}, ledger.payment_entry || '—'),
        h('dt', {}, 'Fee entry'),
        h('dd', {}, ledger.fee_entry || '—'),
        h('dt', {}, 'Attempts'),
        h('dd', {}, String(ledger.attempts === null || ledger.attempts === undefined ? '—' : ledger.attempts)),
        h('dt', {}, 'Next attempt'),
        h('dd', {}, ledger.next_attempt_at || '—'),
        h('dt', {}, 'Error'),
        h(
          'dd',
          {},
          ledger.error_code || '—',
          plainWords('writeback', ledger.error_code),
          nextStep('writeback', ledger.error_code),
        ),
      ),
    ),
  );

  const verifyOut = h('div', {});
  const verifyButton = h(
    'button',
    {
      onclick: async (event) => {
        event.target.disabled = true;
        event.target.textContent = 'Re-checking…';
        try {
          const outcome = await api(`/payments/${encodeURIComponent(report.reference.value)}/verify`, { method: 'POST' });
          if (!outcome.checked) {
            verifyOut.replaceChildren(h('p', { class: 'warn-text' }, `Not checked: ${outcome.detail}`));
          } else {
            verifyOut.replaceChildren(
              table(
                ['Question', 'Answer'],
                [
                  h('tr', {}, h('td', {}, 'What the provider says now'), h('td', {}, outcome.detail)),
                  h('tr', {}, h('td', {}, 'Agrees with our record'), h('td', {}, outcome.agrees_with_record ? badge('yes', 'good') : badge('no', 'bad'))),
                  // The two figures are different scopes: everything we absorbed across every
                  // on-chain operation, against the single operation the provider was asked about.
                  // Labelling them is what stops a total and a share from looking like a conflict.
                  h('tr', {}, h('td', {}, 'Fee we booked (all operations)'), h('td', {}, String(outcome.fee_units_recorded ?? '—'))),
                  h('tr', {}, h('td', {}, `Fee reported now (${outcome.fee_stage_compared || 'settlement'} operation)`), h('td', {}, String(outcome.fee_units_reported_now ?? '—'))),
                  h('tr', {}, h('td', {}, `Our record for that operation`), h('td', {}, String(outcome.fee_units_recorded_for_stage ?? '—'))),
                  h('tr', {}, h('td', {}, 'Fees agree for that operation'), h('td', {}, outcome.fee_agrees === null ? '—' : outcome.fee_agrees ? badge('yes', 'good') : badge('no', 'bad'))),
                ],
              ),
            );
          }
        } catch (error) {
          verifyOut.replaceChildren(h('p', { class: 'error' }, String(error.message)));
        } finally {
          event.target.disabled = false;
          event.target.textContent = 'Re-check the chain';
        }
      },
    },
    'Re-check the chain',
  );
  panels.push(
    panel(
      'Independent re-check',
      h('p', { class: 'muted' }, 'Everything above is our own record. This asks the provider and the chain again. It reads only, and it changes nothing.'),
      h('div', { class: 'credentials' }, verifyButton),
      verifyOut,
    ),
  );

  panels.push(
    panel(
      concept('audit_chain', 'Audit chain'),
      h(
        'p',
        {},
        report.audit.ok
          ? badge(`Intact · ${report.audit.entries} entries verified`, 'good')
          : badge(`Integrity failure: ${explain('audit', report.audit.reason) || humanizeCode(report.audit.reason)}`, 'bad'),
      ),
      table(
        ['When', 'Event', 'State', 'Why', 'Entry hash'],
        (report.timeline || []).map((event) =>
          h(
            'tr',
            {},
            h('td', {}, event.when),
            h('td', {}, event.event),
            h('td', {}, event.state),
            h('td', { class: 'muted' }, event.explanation || ''),
            h('td', {}, (event.entry_hash || '').slice(0, 18)),
          ),
        ),
      ),
    ),
  );
  return panels;
}

async function renderPayments(root, [reference] = []) {
  const log = await api('/payments');
  const exportFeedback = h('div', { role: 'status', 'aria-live': 'polite' });
  const exportCSV = actionButton('Export CSV', async () => {
    const headers = { Accept: 'text/csv' };
    if (apiKey()) headers['X-API-Key'] = apiKey();
    const response = await fetch('/payments/export', { headers });
    if (!response.ok) throw new Error(`Export failed (HTTP ${response.status}). Check API access and try again.`);
    const url = URL.createObjectURL(await response.blob());
    const link = h('a', { href: url, download: 'tameion-settlements.csv' });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    exportFeedback.replaceChildren(h('p', { class: 'muted' }, 'CSV exported from recorded payments.'));
  }, exportFeedback);
  root.append(sectionHeading({ eyebrow: 'Payment history', title: 'Settlements', count: (log.payments || []).length, trailing: exportCSV }), exportFeedback);
  const totals = log.totals || {};
  let filter = 'all';
  const tableHost = h('div', {});

  const paint = () => {
    const rows = (log.payments || []).filter((row) => keep(row, filter)).map((row) =>
      h(
        'tr',
        {},
        h('td', {}, h('a', { href: `#/invoice/${row.invoice_id}` }, row.invoice_number)),
        h('td', {}, row.supplier_id),
        h('td', { class: 'num' }, money(row.amount_usdc)),
        h('td', {}, row.recipient || '—'),
        h('td', {}, row.confirmation_status, plainWords('confirmations', row.confirmation_status)),
        h('td', {}, row.erp_status || '—'),
        h('td', { class: 'num' }, row.fee_usdc ? money(row.fee_usdc) : '—'),
        h(
          'td',
          {},
          row.explorer_url ? h('a', { href: row.explorer_url, target: '_blank', rel: 'noreferrer' }, `${row.transaction_hash.slice(0, 12)}…`) : '—',
        ),
        h('td', {}, outcomeBadge(row.outcome), plainWords('outcomes', row.outcome.code), outcomeWords(row.outcome.code)),
      ),
    );
    tableHost.replaceChildren(
      table(
        [
          'Invoice',
          'Supplier',
          'Amount',
          'Recipient',
          'Confirmation',
          concept('ledger', 'Ledger'),
          concept('network_fee', 'Fee'),
          'Transaction',
          'Outcome',
        ],
        rows,
      ),
    );
  };

  root.append(
    statGrid([
      { label: 'Value moved', value: String(totals.value_usdc ?? '—'), unit: 'USDC' },
      { label: 'Fees absorbed', value: String(totals.fees_usdc ?? '—'), unit: 'USDC', concept: 'network_fee' },
      { label: 'Settled and recorded', value: String(totals.settled ?? 0) },
      { label: 'Needs attention', value: String((totals.settled_not_recorded ?? 0) + (totals.unconfirmed ?? 0) + (totals.failed ?? 0)), tone: ((totals.settled_not_recorded ?? 0) + (totals.unconfirmed ?? 0) + (totals.failed ?? 0)) > 0 ? 'bad' : 'good' },
    ]),
  );

  root.append(
    lede(
      'What has left the treasury, and whether the books have caught up with it. A payment can '
      + 'settle on the chain before the ',
      concept('ledger', 'ledger'),
      ' accepts it, so the two are shown side by side.',
    ),
  );

  root.append(
    panel(
      'Settlement log',
      h('p', { class: 'muted' }, 'A payment that failed is in the log too, with the reason. A log that only holds successes cannot answer the question an operator actually has.'),
    ),
  );

  root.append(reconciliationPanel());

  const lookupInput = h('input', { 'aria-label': 'Payment reference', placeholder: 'Invoice id, invoice number, payment id or transaction hash' });
  const reportHost = h('div', {});
  const lookup = h(
    'button',
    {
      onclick: async (event) => {
        const reference = lookupInput.value.trim();
        if (!reference) return;
        event.target.disabled = true;
        try {
          const report = await api(`/payments/${encodeURIComponent(reference)}`);
          reportHost.replaceChildren(...reportPanels(report));
        } catch (error) {
          reportHost.replaceChildren(h('p', { class: 'error' }, String(error.message)));
        } finally {
          event.target.disabled = false;
        }
      },
    },
    'Look up',
  );
  lookupInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') lookup.click();
  });

  root.append(
    panel(
      'Look up one payment',
      lede(
        'The whole story of one payment: the outcome, the authorization, the settlement, the ledger ',
        'entry and the audit chain. Paste a transaction hash straight out of a block explorer.',
      ),
      h('div', { class: 'credentials' }, lookupInput, lookup),
      reportHost,
    ),
  );

  const filterHost = h('div', {});

  const paintFilters = () => {
    filterHost.replaceChildren(
      filters(filter, (next) => {
        filter = next;
        paintFilters();
        paint();
      }),
    );
  };

  root.append(
    panel(
      'All payments',
      filterHost,
      tableHost,
    ),
  );
  paintFilters();
  paint();
  if (reference) {
    lookupInput.value = decodeURIComponent(reference);
    try {
      const report = await api(`/payments/${encodeURIComponent(lookupInput.value)}`);
      reportHost.replaceChildren(...reportPanels(report));
    } catch (error) {
      reportHost.replaceChildren(h('p', { class: 'error', role: 'alert' }, String(error.message)));
    }
  }
}

registerView('payments', renderPayments);
