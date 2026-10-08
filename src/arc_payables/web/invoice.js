// One decision workspace. UI affordances never replace the server's policy or authorization.
import { actionButton, api, approvalToken, badge, concept, confirmPayment, deploymentContext, deploymentLabel, explain, h, lede, panel, plainWords, refresh, registerView, sectionHeading, stateTone, table } from './app.js';

export function invoiceActions(detail) {
  const checks = detail.decision?.policy_checks || [];
  const failed = checks.filter((check) => !check.passed);
  const blocking = failed.filter((check) => !check.overridable);
  const reviewable = failed.filter((check) => check.overridable && check.requires_human);
  return {
    blocking, reviewable,
    evaluate: !detail.payment,
    approve: !detail.payment && detail.decision?.action === 'ESCALATE' && reviewable.length > 0 && blocking.length === 0,
    pay: !detail.payment && detail.decision?.action === 'PAY_NOW' && failed.length === 0,
    writeback: detail.payment?.confirmation_status === 'CONFIRMED' && detail.state !== 'ERP_RECORDED',
    reconcile: Boolean(detail.payment) && detail.state === 'NEEDS_RECONCILIATION',
  };
}

function short(hash) { return typeof hash === 'string' && hash.length > 20 ? `${hash.slice(0, 10)}…${hash.slice(-6)}` : hash || '—'; }
function field(label, id, placeholder) {
  const input = h('input', { id, placeholder, required: true });
  return { input, node: h('label', { class: 'field', for: id }, h('span', {}, label), input) };
}

async function renderInvoice(root, [invoiceId]) {
  if (!invoiceId) { root.append(h('p', { class: 'muted' }, 'Pick an invoice from the payment queue.')); return; }
  const [detail, events, suppliers, setup] = await Promise.all([
    api(`/invoices/${encodeURIComponent(invoiceId)}`), api(`/invoices/${encodeURIComponent(invoiceId)}/events`), api('/suppliers'), api('/setup').catch(() => null),
  ]);
  const invoice = detail.invoice, decision = detail.decision;
  const supplier = suppliers.find((item) => item.supplier_id === invoice.supplier_id);
  const actions = invoiceActions(detail);
  const base = `/invoices/${encodeURIComponent(invoiceId)}`;
  const feedback = h('div', { role: 'status', 'aria-live': 'polite' });
  const mutate = async (path, options = {}) => { await api(path, { method: 'POST', ...options }); await refresh(); };

  root.append(sectionHeading({ eyebrow: 'Invoice review', title: invoice.invoice_number, trailing: h('a', { class: 'btn', href: '#/queue' }, 'Back to queue') }));
  root.append(h('div', { class: 'invoice-summary card' },
    h('div', {}, badge(detail.state, stateTone(detail.state)), plainWords('states', detail.state), h('p', { class: 'muted' }, `${invoice.supplier_id}${supplier?.name ? ` · ${supplier.name}` : ''} · Due ${invoice.due_date}`)),
    h('div', { class: 'invoice-amount mono' }, invoice.amount, h('span', { class: 'muted' }, ' USDC')),
  ));
  root.append(h('ol', { class: 'payment-stages', 'aria-label': 'Payment lifecycle' },
    [['Evidence', Boolean(decision)], ['Authorized', Boolean(detail.payment)], ['Settled', detail.payment?.confirmation_status === 'CONFIRMED'], ['Ledger recorded', detail.state === 'ERP_RECORDED']].map(([label, complete]) => h('li', { class: complete ? 'done' : '' }, `${complete ? '✓ ' : ''}${label}`)),
  ));
  root.append(panel('Where this payment would go',
    lede('The ', concept('wallet', 'trusted destination'), ' comes only from a human-verified supplier record. The invoice cannot choose where its own money goes.'),
    h('div', { class: 'destination-grid' },
      h('div', { class: 'destination trusted' }, h('h3', {}, 'Trusted supplier destination'), h('p', { class: 'mono' }, supplier?.wallet || 'No supplier wallet configured'), badge(supplier?.wallet_verified ? 'Human verified' : 'Not verified · payment blocked', supplier?.wallet_verified ? 'good' : 'bad')),
      h('div', { class: 'destination' }, h('h3', {}, 'Address printed on invoice'), h('p', { class: 'mono' }, invoice.invoice_payee_address || 'Not supplied'), badge('Untrusted · never used as destination', 'warn')),
    ),
    h('p', { class: 'guide-note' }, `Linked payable: ${invoice.purchase_invoice_id || 'Not linked'}. Supplier wallet changes must be verified in the accounting system, not on this screen.`),
  ));

  if (!invoice.purchase_invoice_id && !detail.payment) {
    const payable = field('ERPNext Purchase Invoice', 'linked-payable', 'Purchase Invoice name');
    const reviewer = field('Reviewer', 'link-reviewer', 'Your name');
    root.append(panel('Link independent accounting evidence',
      h('p', { class: 'muted' }, 'A captured invoice is not payable until it matches an accounting payable. Linking requires a human approval token.'),
      h('div', { class: 'review-fields' }, payable.node, reviewer.node),
      actionButton('Link to ERP payable', async () => {
        if (!payable.input.reportValidity() || !reviewer.input.reportValidity()) return;
        await mutate(`${base}/link`, { approval: true, body: { purchase_invoice_id: payable.input.value.trim(), reviewer: reviewer.input.value.trim() } });
      }, feedback),
    ));
  }

  const next = panel('Your next action',
    h('p', { class: 'muted' }, deploymentLabel(setup?.deployment)),
    h('p', {}, detail.state === 'ERP_RECORDED' ? 'Settlement and ledger writeback are complete. You can inspect the entries and verify the history below.'
      : actions.blocking.length ? 'Correct the conflicting or missing evidence before payment. Human approval cannot override these blocks.'
        : actions.writeback ? 'The payment settled. Retry only the ledger writeback; this cannot send funds again.'
          : actions.reconcile ? 'The payment outcome is uncertain. Reconcile the existing attempt before doing anything else.'
            : actions.pay ? 'Checks passed. Review the exact amount and trusted destination before confirming.'
              : actions.approve ? 'A human must acknowledge the reviewable exceptions. Approval will trigger fresh evaluation, not an immediate payment.'
                : decision ? decision.reason : 'Evaluate the independent evidence first. This action never moves funds.'),
  );
  const controls = h('div', { class: 'action-row' });
  if (actions.evaluate) controls.append(actionButton(decision ? 'Re-evaluate evidence' : 'Evaluate invoice', () => mutate(`${base}/evaluate`), feedback, { class: !decision ? 'btn-primary' : '', 'data-action': 'evaluate' }));
  if (actions.pay) controls.append(actionButton(setup?.deployment?.payment_provider === 'mock' ? 'Review simulated payment' : 'Review payment', async () => {
    // Re-read the mode at the moment of confirmation. Unknown/missing metadata must fail closed.
    const deployment = await deploymentContext();
    if (!supplier?.wallet || !supplier.wallet_verified) throw new Error('A human-verified supplier destination is required.');
    if (await confirmPayment(invoice, supplier, deployment)) await mutate(`${base}/payment`);
  }, feedback, { class: 'btn-primary', disabled: !supplier?.wallet_verified, 'data-action': 'review-payment' }));
  if (actions.writeback) controls.append(actionButton('Retry ledger writeback', () => mutate(`${base}/payment/erp-writeback`), feedback, { class: 'btn-primary' }));
  if (actions.reconcile) controls.append(actionButton('Reconcile existing payment', () => mutate(`${base}/payment/reconcile`), feedback));
  if (detail.payment) controls.append(h('a', { class: 'btn', href: `#/payments/${encodeURIComponent(invoice.id)}` }, 'Open settlement report'));
  next.append(controls, feedback);
  root.append(next);

  if (decision) {
    const checks = decision.policy_checks || [];
    root.append(panel('Evidence checks',
      h('p', { class: 'muted' }, `${checks.filter((check) => check.passed).length}/${checks.length} checks passed. Blocking evidence and reviewable exceptions are different decisions.`),
      table(['Check', 'Result', 'What you can do', 'Evidence'], checks.map((check) => h('tr', {},
        h('td', {}, check.code),
        h('td', {}, badge(check.passed ? 'Passed' : 'Failed', check.passed ? 'good' : 'bad')),
        h('td', {}, check.passed ? 'Nothing needed' : check.overridable && check.requires_human ? 'Human review permitted' : 'Correct evidence · no override'),
        h('td', {}, check.detail, !check.passed ? h('p', { class: 'muted' }, explain('checks', check.code)) : null),
      ))),
    ));
    if (actions.approve) {
      const reviewer = field('Reviewer', 'approval-reviewer', 'Your name');
      const note = field('Decision note', 'approval-note', 'Why these exceptions are acceptable');
      const boxes = actions.reviewable.map((check) => h('label', { class: 'check' }, h('input', { type: 'checkbox', value: check.code }), h('span', {}, check.code, h('span', { class: 'muted' }, ` · ${check.detail}`))));
      root.append(panel('Acknowledge reviewable exceptions',
        h('p', { class: 'muted' }, 'A separate approval token is required. Acknowledge every listed exception and explain your judgement. Amount and destination cannot change.'),
        !approvalToken() ? h('p', { class: 'warn-text' }, 'Add your approval token in the navigation credentials section. Do not paste it into a decision note.') : null,
        h('div', { class: 'checks' }, boxes), h('div', { class: 'review-fields' }, reviewer.node, note.node),
        actionButton('Record approval & re-evaluate', async () => {
          if (!reviewer.input.reportValidity() || !note.input.reportValidity()) return;
          const acknowledged = boxes.filter((box) => box.querySelector('input').checked).map((box) => box.querySelector('input').value);
          if (acknowledged.length !== boxes.length) throw new Error('Acknowledge every listed exception before recording approval.');
          await mutate(`${base}/approval`, { approval: true, body: { reviewer: reviewer.input.value.trim(), approved: true, note: note.input.value.trim(), acknowledged_checks: acknowledged } });
        }, feedback),
      ));
    }
    root.append(h('details', { class: 'card' }, h('summary', {}, 'Decision provenance'),
      h('dl', { class: 'facts' }, h('dt', {}, 'Decision'), h('dd', {}, decision.action, plainWords('decisions', decision.action)), h('dt', {}, 'Reason'), h('dd', {}, decision.reason),
        h('dt', {}, 'Layer'), h('dd', {}, `${decision.advisory?.decided_by || 'policy'} · ${decision.advisory?.rationale || 'Deterministic checks are authoritative.'}`),
        h('dt', {}, 'Evidence hash'), h('dd', { title: decision.evidence_hash || '' }, short(decision.evidence_hash)), h('dt', {}, 'Policy version'), h('dd', {}, decision.policy_version), h('dt', {}, 'Evaluated at'), h('dd', {}, decision.evaluated_at)),
      (decision.advisory?.deliberations || []).map((item) => h('p', { class: 'mono' }, `${item.layer} · ${item.model || 'no model'} · ${item.outcome} · prompt ${short(item.prompt_sha256)} · response ${short(item.response_sha256)}`)),
    ));
  }

  if (detail.payment) root.append(panel('Settlement & accounting entries',
    h('dl', { class: 'facts' }, h('dt', {}, 'Settlement'), h('dd', {}, detail.payment.confirmation_status || '—'),
      h('dt', {}, 'Transaction'), h('dd', {}, detail.payment.transaction_hash || '—'), h('dt', {}, 'Payment Entry'), h('dd', {}, detail.payment.erp_entry_id || 'Not recorded yet'),
      h('dt', {}, 'Fee Entry'), h('dd', {}, detail.payment.erp_fee_entry_id || 'Not recorded yet')),
  ));
  const verdict = h('div', { role: 'status', 'aria-live': 'polite' });
  root.append(panel(concept('audit_chain', 'Verifiable history'),
    actionButton('Verify audit chain', async () => {
      const result = await api('/audit/verify');
      verdict.replaceChildren(result.ok ? badge(`Intact · ${result.checked} entries · ${result.signed} signed (whole database)`, 'good') : badge(`Broken at entry ${result.first_broken_id}: ${result.reason}`, 'bad'));
    }, verdict, { 'data-action': 'verify-audit' }), verdict,
    table(['When', 'Event', 'State', 'Entry hash', 'Signature'], events.map((event) => h('tr', {}, h('td', {}, event.created_at), h('td', {}, event.type), h('td', {}, event.state), h('td', { title: event.event_hash || '' }, short(event.event_hash)), h('td', {}, event.signature ? badge('Signed', 'good') : 'Unsigned')))),
  ));
}

registerView('invoice', renderInvoice);
