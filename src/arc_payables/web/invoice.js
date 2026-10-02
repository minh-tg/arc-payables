// One invoice in full: the evidence checks, which layer decided and why, the audit chain, and the
// actions a human can take. The destination is read from the trusted supplier record, never from
// the invoice.

import { api, badge, concept, explain, h, lede, panel, plainWords, refresh, registerView, sectionHeading, stateTone, table } from './app.js';

function money(value) {
  return value === null || value === undefined ? '—' : `${value} USDC`;
}

function short(hash) {
  return typeof hash === 'string' && hash.length > 20 ? `${hash.slice(0, 10)}…${hash.slice(-6)}` : hash || '—';
}

async function renderInvoice(root, [invoiceId]) {
  if (!invoiceId) {
    root.append(h('p', { class: 'muted' }, 'Pick an invoice from the queue.'));
    return;
  }
  const detail = await api(`/invoices/${encodeURIComponent(invoiceId)}`);
  const events = await api(`/invoices/${encodeURIComponent(invoiceId)}/events`);
  const suppliers = await api('/suppliers');
  const invoice = detail.invoice;
  const decision = detail.decision;
  const supplier = suppliers.find((item) => item.supplier_id === invoice.supplier_id);
  root.append(sectionHeading({ eyebrow: 'Evidence', title: `Invoice ${invoice.invoice_number}`, count: (detail.decision && detail.decision.policy_checks ? detail.decision.policy_checks.length : 0) }));

  root.append(
    panel(
      `Invoice ${invoice.invoice_number}`,
      lede(
        'One invoice end to end. The destination comes from the supplier record a human verified, ',
        'never from the invoice itself, and the invoice cannot name where its own money goes.',
      ),
      h('p', { class: 'muted' }, explain('states', detail.state)),
      h(
        'dl',
        { class: 'facts' },
        h('dt', {}, 'State'),
        h('dd', {}, badge(detail.state, stateTone(detail.state))),
        h('dt', {}, 'Amount'),
        h('dd', {}, money(invoice.amount)),
        h('dt', {}, 'Due'),
        h('dd', {}, invoice.due_date),
        h('dt', {}, 'Supplier'),
        h('dd', {}, `${invoice.supplier_id}${supplier && supplier.name ? ` · ${supplier.name}` : ''}`),
        h('dt', {}, concept('wallet', 'Trusted destination')),
        h(
          'dd',
          {},
          supplier && supplier.wallet
            ? `${supplier.wallet}${supplier.wallet_verified ? '' : ' (unverified — a human must verify it)'}`
            : 'no approved wallet on the supplier record',
        ),
        h('dt', {}, 'Payee on the captured invoice'),
        h('dd', { class: 'muted' }, invoice.invoice_payee_address || 'none (untrusted field, never a destination)'),
        h('dt', {}, concept('ledger', 'Linked ERP payable')),
        h('dd', {}, invoice.purchase_invoice_id || 'not linked'),
      ),
    ),
  );

  if (!invoice.purchase_invoice_id) {
    const field = h('input', { placeholder: 'ERPNext Purchase Invoice name' });
    const reviewer = h('input', { placeholder: 'Reviewer' });
    const link = h(
      'button',
      {
        onclick: async (event) => {
          event.target.disabled = true;
          try {
            await api(`/invoices/${encodeURIComponent(invoiceId)}/link`, {
              method: 'POST',
              approval: true,
              body: { purchase_invoice_id: field.value.trim(), reviewer: reviewer.value.trim() },
            });
            await refresh();
          } catch (error) {
            alert(String(error.message));
            event.target.disabled = false;
          }
        },
      },
      'Link to ERP payable',
    );
    root.append(
      panel(
        'Not payable yet',
        h('p', { class: 'muted' }, 'A captured invoice becomes payable only once a human links it to an accounting payable.'),
        h('div', { class: 'credentials' }, field, reviewer, link),
      ),
    );
  }

  if (decision) {
    const advisory = decision.advisory || {};
    root.append(
      panel(
        `Decision · ${decision.action}`,
        h('p', {}, plainWords('decisions', decision.action)),
        h(
          'dl',
          { class: 'facts' },
          h('dt', {}, 'Reason'),
          h('dd', {}, decision.reason),
          h('dt', {}, 'Decided by'),
          h('dd', {}, `${advisory.decided_by || 'unknown'}${advisory.confidence ? ` · confidence ${advisory.confidence}` : ''}`),
          h('dt', {}, 'Rationale'),
          h('dd', {}, advisory.rationale || '—'),
          h('dt', {}, 'Evidence hash'),
          h('dd', { title: decision.evidence_hash || '' }, short(decision.evidence_hash)),
          h('dt', {}, 'Policy version'),
          h('dd', {}, decision.policy_version),
          h('dt', {}, 'Evaluated at'),
          h('dd', {}, decision.evaluated_at),
        ),
        decision.missing_evidence && decision.missing_evidence.length
          ? h('div', {}, h('strong', {}, 'Missing evidence'), h('ul', { class: 'tight' }, decision.missing_evidence.map((item) => h('li', {}, item))))
          : null,
        decision.conflicts && decision.conflicts.length
          ? h('div', {}, h('strong', {}, 'Conflicts'), h('ul', { class: 'tight' }, decision.conflicts.map((item) => h('li', {}, item))))
          : null,
        (advisory.deliberations || []).length
          ? h(
              'details',
              {},
              h('summary', {}, `Deliberation (${advisory.deliberations.length})`),
              h(
                'ul',
                { class: 'tight' },
                advisory.deliberations.map((item) =>
                  h('li', {}, `${item.layer || ''}${item.model ? ` · ${item.model}` : ''} · ${item.outcome}${item.latency_ms !== undefined ? ` · ${item.latency_ms}ms` : ''} · prompt ${short(item.prompt_sha256)}`),
                ),
              ),
            )
          : null,
      ),
    );

    const checks = decision.policy_checks || [];
    const checkRows = checks.map((check) => {
      const words = explain('checks', check.code);
      return h(
        'tr',
        {},
        h('td', {}, check.code),
        h('td', {}, check.passed ? badge('pass', 'good') : badge('fail', 'bad')),
        h('td', {}, check.requires_human ? (check.overridable ? 'human, overridable' : 'human, blocking') : '—'),
        h('td', {}, h('div', {}, check.detail), h('div', { class: 'muted' }, words)),
      );
    });
    root.append(panel('Evidence checks', lede('Each rule the policy applied, and whether it passed. A failing rule that a human may override is the only thing standing between this invoice and payment.'), table(['Check', 'Result', 'Review', 'Detail'], checkRows)));

    const acknowledgeable = checks.filter((check) => !check.passed && check.requires_human && check.overridable);
    const boxes = acknowledgeable.map((check) =>
      h('label', { class: 'check' }, h('input', { type: 'checkbox', value: check.code }), ` ${check.code}`),
    );
    const reviewer = h('input', { placeholder: 'Reviewer' });
    const note = h('input', { placeholder: 'Note (required)' });
    const approve = h(
      'button',
      {
        onclick: async (event) => {
          const acknowledged = boxes.filter((box) => box.querySelector('input').checked).map((box) => box.querySelector('input').value);
          event.target.disabled = true;
          try {
            await api(`/invoices/${encodeURIComponent(invoiceId)}/approval`, {
              method: 'POST',
              approval: true,
              body: {
                reviewer: reviewer.value.trim(),
                approved: true,
                note: note.value.trim(),
                acknowledged_checks: acknowledged,
              },
            });
            await refresh();
          } catch (error) {
            alert(String(error.message));
            event.target.disabled = false;
          }
        },
      },
      'Record approval',
    );
    const evaluate = h(
      'button',
      {
        onclick: async (event) => {
          event.target.disabled = true;
          try {
            await api(`/invoices/${encodeURIComponent(invoiceId)}/evaluate`, { method: 'POST' });
          } finally {
            await refresh();
          }
        },
      },
      'Re-evaluate',
    );
    const pay = h(
      'button',
      {
        onclick: async (event) => {
          const destination = supplier && supplier.wallet ? supplier.wallet : 'the trusted supplier wallet';
          if (!window.confirm(`Send ${invoice.amount} USDC to ${destination}?\n\nThis submits a real testnet payment.`)) return;
          event.target.disabled = true;
          try {
            const result = await api(`/invoices/${encodeURIComponent(invoiceId)}/payment`, { method: 'POST' });
            alert(`Result: ${result.state}`);
            await refresh();
          } catch (error) {
            alert(String(error.message));
            event.target.disabled = false;
          }
        },
      },
      'Pay now',
    );
    root.append(
      panel(
        'Human review',
        lede(
          'Approval is the ', concept('approval_token', 'approval token'), ' at work: a second '
          + 'credential a person supplies, which is why the API key on its own can never approve ',
          + 'anything.',
        ),
        h('p', { class: 'muted' }, 'Approval acknowledges specific checks and is bound to the evidence hash; it can never change the destination or the amount.'),
        boxes.length ? h('div', { class: 'checks' }, boxes) : h('p', { class: 'muted' }, 'No overridable checks are currently failing.'),
        h('div', { class: 'credentials' }, reviewer, note, approve, evaluate, pay),
      ),
    );
  } else {
    root.append(
      panel(
        'No decision recorded yet',
        h(
          'button',
          {
            onclick: async (event) => {
              event.target.disabled = true;
              try {
                await api(`/invoices/${encodeURIComponent(invoiceId)}/evaluate`, { method: 'POST' });
              } finally {
                await refresh();
              }
            },
          },
          'Evaluate now',
        ),
      ),
    );
  }

  const verify = h('button', {}, 'Verify audit chain');
  const verdict = h('span', { class: 'muted' });
  verify.addEventListener('click', async () => {
    verify.disabled = true;
    try {
      const result = await api('/audit/verify');
      verdict.replaceChildren(
        result.ok
          ? badge(`intact · ${result.checked} entries · ${result.signed} signed`, 'good')
          : badge(`broken at entry ${result.first_broken_id}: ${result.reason}`, 'bad'),
      );
    } catch (error) {
      verdict.replaceChildren(badge(String(error.message), 'bad'));
    } finally {
      verify.disabled = false;
    }
  });
  const eventRows = events.map((event) =>
    h(
      'tr',
      {},
      h('td', {}, event.created_at),
      h('td', {}, event.type),
      h('td', {}, event.state),
      h('td', { title: event.event_hash || '' }, short(event.event_hash)),
      h('td', {}, event.signature ? badge('signed', 'good') : '—'),
    ),
  );
  root.append(
    panel(
      concept('audit_chain', 'Audit chain'),
      h('div', { class: 'credentials' }, verify, verdict),
      table(['When', 'Event', 'State', 'Entry hash', 'Signature'], eventRows),
    ),
  );
}

registerView('invoice', renderInvoice);
