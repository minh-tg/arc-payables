// One decision workspace. UI affordances never replace the server's policy or authorization.
import { actionButton, api, approvalToken, can, usesOIDC, badge, concept, confirmPayment, deploymentContext, deploymentLabel, errorPanel, explain, formatTimestamp, h, humanizeCode, lede, panel, plainWords, refresh, registerView, sectionHeading, stateTone, table } from './app.js';

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

// What to correct for one failing check. A record that a person can edit is named, a decision only a
// reviewer can take says so, and a missing record says there is nothing to open yet.
function caseRecord(item) {
  if (item.fix === 'human_review') return 'A reviewer decides on this page';
  if (!item.source_id) {
    return item.source_type === 'Treasury balance' ? item.source_type : 'No record found. Create or link one.';
  }
  return h('span', {}, item.source_type, ' ', h('span', { class: 'mono' }, item.source_id));
}

// The case file: what failed, which record to correct and who usually handles it. Nothing here is a
// control. A cleared case is deliberately neutral, because passing checks are not an approval.
function caseCard(caseFile) {
  const open = caseFile.status === 'open';
  const status = open ? 'Open' : caseFile.status === 'cleared' ? 'Checks now pass' : humanizeCode(caseFile.status);
  const lead = open
    ? caseFile.last_checked_at
      ? 'Still held after the last check. Correct the records below. The worker rechecks on its next pass.'
      : 'This invoice is held. Correct the records below in the accounting system. The worker rechecks on its next pass.'
    : caseFile.status === 'cleared'
      ? 'The last check found nothing failing. This is not an approval. The invoice continues through the normal payment path and its own checks.'
      : null;
  const events = caseFile.events || [];
  const card = panel(
    'Exception case',
    h(
      'div',
      { class: 'finding-head' },
      badge(status, open ? 'warn' : 'neutral'),
      h('span', { class: 'finding-code' }, caseFile.last_checked_at ? `Checked ${formatTimestamp(caseFile.last_checked_at)}` : 'Not checked yet'),
    ),
    lead ? h('p', {}, lead) : null,
    table(
      ['Failed check', 'Record to correct', 'Usually handled by'],
      (caseFile.items || []).map((item) =>
        h(
          'tr',
          {},
          h('td', {}, h('div', { style: 'font-weight: 500;' }, humanizeCode(item.check_code)), h('p', { class: 'muted' }, item.detail)),
          h('td', {}, caseRecord(item)),
          h('td', {}, item.owner_role),
        ),
      ),
    ),
    h('p', { class: 'muted' }, 'A change in the accounting system never approves or waives a check. Each check is evaluated again from scratch.'),
    h(
      'details',
      {},
      h('summary', {}, `Case history (${events.length})`),
      table(
        ['When', 'Event', 'Detail'],
        events.map((event) =>
          h('tr', {}, h('td', {}, formatTimestamp(event.at)), h('td', {}, humanizeCode(event.type)), h('td', {}, event.detail || '—')),
        ),
      ),
    ),
  );
  card.classList.add('finding');
  if (open) card.classList.add('warning');
  return card;
}

// A missing case is nothing to show. Any other failure is reported inside the card's own panel, so
// the rest of the invoice page still renders whatever the case route did.
function casePanel(result) {
  if (result && result.status === 404) return null;
  if (result instanceof Error) return panel('Exception case', errorPanel(result));
  return caseCard(result);
}

async function renderInvoice(root, [invoiceId]) {
  if (!invoiceId) { root.append(h('p', { class: 'muted' }, 'Pick an invoice from the payment queue.')); return; }
  const [detail, events, suppliers, setup, caseResult] = await Promise.all([
    api(`/invoices/${encodeURIComponent(invoiceId)}`), api(`/invoices/${encodeURIComponent(invoiceId)}/events`), api('/suppliers'), api('/setup').catch(() => null),
    api(`/invoices/${encodeURIComponent(invoiceId)}/case`).catch((error) => error),
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
  const caseNode = casePanel(caseResult);
  if (caseNode) root.append(caseNode);
  const addressesMatch = supplier?.wallet && invoice.invoice_payee_address && supplier.wallet.toLowerCase() === invoice.invoice_payee_address.toLowerCase();
  const addressBadge = !invoice.invoice_payee_address
    ? badge('Not provided on document', 'neutral')
    : addressesMatch
      ? badge('Matches ERP record · Verification passed', 'good')
      : badge('Mismatch ignored · Document address discarded', 'warn');

  const addressNote = !invoice.invoice_payee_address
    ? 'No address was printed on the invoice. Payout safely uses the verified ERP record.'
    : addressesMatch
      ? 'The invoice prints the correct address. Payout routes exclusively to the verified ERP record on the left.'
      : 'The document prints a different address (potential invoice tampering). The agent safely pays the verified ERP record on the left.';

  root.append(panel('Where this payment will go',
    lede('Payouts route exclusively to the verified supplier record in your accounting system. The agent never sends funds to an address printed on an invoice document, preventing invoice tampering fraud.'),
    h('div', { class: 'destination-grid' },
      h('div', { class: 'destination trusted' },
        h('h3', {}, 'Verified payout destination (ERP master record)'),
        h('p', { class: 'mono' }, supplier?.wallet || 'No supplier wallet configured'),
        badge(supplier?.wallet_verified ? 'Active destination · Human verified' : 'Not verified · payment blocked', supplier?.wallet_verified ? 'good' : 'bad'),
        h('p', { class: 'muted', style: 'font-size: 11.5px; margin-top: 8px; margin-bottom: 0;' }, 'This is the destination that receives funds on chain.'),
      ),
      h('div', { class: 'destination' },
        h('h3', {}, 'Address printed on invoice document'),
        h('p', { class: 'mono' }, invoice.invoice_payee_address || 'Not supplied on document'),
        addressBadge,
        h('p', { class: 'muted', style: 'font-size: 11.5px; margin-top: 8px; margin-bottom: 0;' }, addressNote),
      ),
    ),
    h('p', { class: 'guide-note' }, `Linked payable: ${invoice.purchase_invoice_id || 'Not linked'}. Supplier wallet changes must be verified in the accounting system, not on this screen.`),
  ));

  if (!invoice.purchase_invoice_id && !detail.payment && can('operate')) {
    const payable = field('ERPNext Purchase Invoice', 'linked-payable', 'Purchase Invoice name');
    const reviewer = usesOIDC() ? null : field('Reviewer', 'link-reviewer', 'Your name');
    root.append(panel('Link independent accounting evidence',
      h('p', { class: 'muted' }, usesOIDC() ? 'Linking requires your verified operator identity; the invoice cannot choose its own payable.' : 'A captured invoice is not payable until it matches an accounting payable. Linking requires a human approval token.'),
      h('div', { class: 'review-fields' }, payable.node, reviewer?.node),
      actionButton('Link to ERP payable', async () => {
        if (!payable.input.reportValidity() || (reviewer && !reviewer.input.reportValidity())) return;
        await mutate(`${base}/link`, { approval: true, body: { purchase_invoice_id: payable.input.value.trim(), ...(reviewer ? { reviewer: reviewer.input.value.trim() } : {}) } });
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
  if (actions.evaluate && can('operate')) controls.append(actionButton(decision ? 'Re-evaluate evidence' : 'Evaluate invoice', () => mutate(`${base}/evaluate`), feedback, { class: !decision ? 'btn-primary' : '', 'data-action': 'evaluate' }));
  if (actions.pay && can('pay')) controls.append(actionButton(setup?.deployment?.payment_provider === 'mock' ? 'Review simulated payment' : 'Review payment', async () => {
    // Re-read the mode at the moment of confirmation. Unknown/missing metadata must fail closed.
    const deployment = await deploymentContext();
    if (!supplier?.wallet || !supplier.wallet_verified) throw new Error('A human-verified supplier destination is required.');
    if (await confirmPayment(invoice, supplier, deployment)) await mutate(`${base}/payment`);
  }, feedback, { class: 'btn-primary', disabled: !supplier?.wallet_verified, 'data-action': 'review-payment' }));
  if (actions.writeback && can('operate')) controls.append(actionButton('Retry ledger writeback', () => mutate(`${base}/payment/erp-writeback`), feedback, { class: 'btn-primary' }));
  if (actions.reconcile && can('pay')) controls.append(actionButton('Reconcile existing payment', () => mutate(`${base}/payment/reconcile`), feedback));
  if (detail.payment) controls.append(h('a', { class: 'btn', href: `#/payments/${encodeURIComponent(invoice.id)}` }, 'Open settlement report'));
  next.append(controls, feedback);
  root.append(next);

  if (decision) {
    const ADVISORY_CHECKS = new Set(['duplicate_number_other_supplier', 'similar_amount_recent', 'history_insufficient']);
    const checks = decision.policy_checks || [];
    const advisory = checks.filter((check) => ADVISORY_CHECKS.has(check.code));
    const material = checks.filter((check) => !ADVISORY_CHECKS.has(check.code));
    const advisoryNote = advisory.length
      ? ` ${advisory.length} advisory note${advisory.length === 1 ? '' : 's'} ${advisory.length === 1 ? 'does' : 'do'} not change the decision.`
      : '';
    root.append(panel('Evidence checks',
      h('p', { class: 'muted' }, `${material.filter((check) => check.passed).length}/${material.length} checks passed. Blocking evidence and reviewable exceptions are different decisions.${advisoryNote}`),
      table(['Check', 'Result', 'What you can do', 'Evidence'], checks.map((check) => {
        const advisoryCheck = ADVISORY_CHECKS.has(check.code);
        return h('tr', {},
          h('td', {},
            h('div', { style: 'font-weight: 500;' }, humanizeCode(check.code)),
          ),
          h('td', {}, advisoryCheck ? badge('Noted', 'neutral') : badge(check.passed ? 'Passed' : 'Failed', check.passed ? 'good' : 'bad')),
          h('td', {}, advisoryCheck ? 'Nothing required. Does not change the decision.' : check.passed ? 'Nothing needed' : check.overridable && check.requires_human ? 'Human review permitted' : 'Correct evidence · no override'),
          h('td', {}, check.detail, !check.passed || advisoryCheck ? h('p', { class: 'muted' }, explain('checks', check.code)) : null),
        );
      })),
    ));
    if (actions.approve && can('approve')) {
      const reviewer = usesOIDC() ? null : field('Reviewer', 'approval-reviewer', 'Your name');
      const note = field('Decision note', 'approval-note', 'Why these exceptions are acceptable');
      const boxes = actions.reviewable.map((check) => h('label', { class: 'check' },
        h('input', { type: 'checkbox', value: check.code }),
        h('span', {},
          h('span', { style: 'font-weight: 500;' }, humanizeCode(check.code)),
          h('span', { class: 'muted' }, ` · ${check.detail}`),
        ),
      ));
      root.append(panel('Acknowledge reviewable exceptions',
        h('p', { class: 'muted' }, usesOIDC() ? 'Your verified approver identity will be recorded. Acknowledge every listed exception; amount and destination cannot change.' : 'A separate approval token is required. Acknowledge every listed exception and explain your judgement. Amount and destination cannot change.'),
        !usesOIDC() && !approvalToken() ? h('p', { class: 'warn-text' }, 'Add your approval token in the navigation credentials section. Do not paste it into a decision note.') : null,
        h('div', { class: 'checks' }, boxes), h('div', { class: 'review-fields' }, reviewer?.node, note.node),
        actionButton('Record approval & re-evaluate', async () => {
          if ((reviewer && !reviewer.input.reportValidity()) || !note.input.reportValidity()) return;
          const acknowledged = boxes.filter((box) => box.querySelector('input').checked).map((box) => box.querySelector('input').value);
          if (acknowledged.length !== boxes.length) throw new Error('Acknowledge every listed exception before recording approval.');
          await mutate(`${base}/approval`, { approval: true, body: { ...(reviewer ? { reviewer: reviewer.input.value.trim() } : {}), approved: true, note: note.input.value.trim(), acknowledged_checks: acknowledged } });
        }, feedback, { 'data-action': 'record-approval' }),
      ));
    }
    root.append(h('details', { class: 'card' }, h('summary', {}, 'Automated decision evaluation'),
      h('dl', { class: 'facts' }, h('dt', {}, 'Decision'), h('dd', {}, humanizeCode(decision.action), plainWords('decisions', decision.action)), h('dt', {}, 'Reason'), h('dd', {}, decision.reason),
        h('dt', {}, 'Layer'), h('dd', {}, `${decision.advisory?.decided_by || 'policy'} · ${decision.advisory?.rationale || 'Deterministic checks are authoritative.'}`),
        h('dt', {}, 'Evidence hash'), h('dd', { title: decision.evidence_hash || '' }, short(decision.evidence_hash)), h('dt', {}, 'Policy version'), h('dd', {}, decision.policy_version), h('dt', {}, 'Evaluated at'), h('dd', {}, formatTimestamp(decision.evaluated_at))),
      (decision.advisory?.deliberations || []).map((item) => h('p', { class: 'mono' }, `${item.layer} · ${item.model || 'no model'} · ${item.outcome} · prompt ${short(item.prompt_sha256)} · response ${short(item.response_sha256)}`)),
    ));
  }

  if (detail.payment) root.append(panel('Settlement & accounting entries',
    h('dl', { class: 'facts' }, h('dt', {}, 'Settlement'), h('dd', {},
      detail.payment.confirmation_status ? badge(detail.payment.confirmation_status, stateTone(detail.payment.confirmation_status)) : '—',
      setup?.deployment?.payment_provider === 'mock' ? badge('Simulated', 'warn') : null,
    ),
      h('dt', {}, 'Transaction'), h('dd', {}, detail.payment.transaction_hash || '—'), h('dt', {}, 'Payment Entry'), h('dd', {}, detail.payment.erp_entry_id || 'Not recorded yet'),
      h('dt', {}, 'Fee Entry'), h('dd', {}, detail.payment.erp_fee_entry_id || 'Not recorded yet')),
  ));
  const verdict = h('div', { role: 'status', 'aria-live': 'polite' });
  root.append(panel(concept('audit_chain', 'Verifiable history'),
    actionButton('Verify audit chain', async () => {
      const result = await api('/audit/verify');
      verdict.replaceChildren(
        result.ok
          ? badge(`Intact · ${result.checked} entries · ${result.signed} signed`, 'good')
          : h('div', { class: 'card finding critical', style: 'margin-top: 10px; padding: 12px;' },
              h('div', { class: 'finding-head' },
                badge(`Integrity failure · Entry #${result.first_broken_id}`, 'bad'),
                result.reason ? h('span', { class: 'finding-code' }, humanizeCode(result.reason)) : null,
              ),
              h('p', { class: 'text-danger', style: 'margin-top: 6px; font-weight: 600; font-size: 13px;' },
                explain('audit', result.reason) || humanizeCode(result.reason) || 'The audit chain did not verify.',
              ),
              plainWords('audit', result.reason),
            ),
      );
    }, verdict, { 'data-action': 'verify-audit' }), verdict,
    table(['When', 'Event', 'State', 'Entry hash', 'Signature'], events.map((event) => h('tr', {}, h('td', {}, formatTimestamp(event.created_at)), h('td', {}, humanizeCode(event.type)), h('td', {}, event.state ? badge(event.state, stateTone(event.state)) : '—'), h('td', { title: event.event_hash || '' }, short(event.event_hash)), h('td', {}, event.signature ? badge('Signed', 'good') : 'Unsigned')))),
  ));
}

registerView('invoice', renderInvoice);
