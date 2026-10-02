// What needs a person. Read-only: the work waiting on a human and the alerts the last pass raised,
// taken from the same snapshot the metrics are rendered from, so the two cannot disagree.

import { api, badge, concept, h, lede, nextStep, panel, plainWords, registerView, sectionHeading, statGrid, table } from './app.js';

function tone(severity) {
  if (severity === 'critical') return 'bad';
  if (severity === 'warning') return 'warn';
  return '';
}

function money(value) {
  return value === null || value === undefined ? '—' : `${value} USDC`;
}

// The shape of an item's detail depends on the code, so only the shapes that exist are rendered.
function detailTable(item) {
  const detail = item.detail || {};
  if (detail.invoices) {
    return table(
      ['Invoice', 'Supplier', 'Amount', 'Due'],
      detail.invoices.map((row) =>
        h(
          'tr',
          {},
          h('td', {}, h('a', { href: `#/invoice/${row.invoice_id}` }, row.invoice_number)),
          h('td', {}, row.supplier_id),
          h('td', { class: 'num' }, money(row.amount_usdc)),
          h('td', {}, row.due_date),
        ),
      ),
    );
  }
  if (detail.payments) {
    return table(
      ['Invoice', 'Transaction', 'Ledger', 'Attempts', 'Error'],
      detail.payments.map((row) =>
        h(
          'tr',
          {},
          h('td', {}, h('a', { href: `#/invoice/${row.invoice_id}` }, row.invoice_number)),
          h('td', {}, row.transaction_hash || '—'),
          h('td', {}, row.erp_status || '—'),
          h('td', { class: 'num' }, String(row.erp_attempts || 0)),
          h('td', {}, row.erp_error_code || '—', plainWords('writeback', row.erp_error_code)),
        ),
      ),
    );
  }
  return null;
}

async function renderAttention(root) {
  const data = await api('/attention');
  root.append(sectionHeading({ eyebrow: 'Action queue', title: 'Exceptions', count: (data.items || []).length }));

  const kpis = statGrid([
    { label: 'Treasury', value: String(data.treasury_usdc ?? '—'), unit: 'USDC', concept: 'treasury' },
    data.critical === 0 && data.warning === 0
      ? { label: 'Needs you', value: '0', unit: 'items', tone: 'good' }
      : { label: 'Needs you', value: String(data.critical + data.warning), unit: 'items' },
    { label: 'Critical', value: String(data.critical), unit: 'items', tone: data.critical > 0 ? 'bad' : undefined },
    { label: 'Warning', value: String(data.warning), unit: 'items', tone: data.warning > 0 ? 'warn' : undefined },
  ]);
  root.append(kpis);

  root.append(
    panel(
      'Waiting on a person',
      lede(
        'Everything here is something the system would not decide on its own. It is waiting for a '
          + 'person, and none of it moves money while it waits. An item is either an ',
        concept('escalation', 'escalation'),
        ' or a payment whose result nobody knows yet.',
      ),
      h(
        'p',
        {},
        data.items.length === 0
          ? badge('nothing is waiting on a human', 'good')
          : h(
              'span',
              {},
              `${data.critical} critical · ${data.warning} warning`,
            ),
      ),
      h(
        'dl',
        { class: 'facts' },
        h('dt', {}, concept('treasury', 'Treasury')),
        h('dd', {}, money(data.treasury_usdc)),
        h('dt', {}, concept('audit_chain', 'Audit entries')),
        h('dd', {}, String(data.audit_entries)),
      ),
      h(
        'p',
        { class: 'muted' },
        'Nothing on this page resolves anything. Acting on an item goes through the same approval or reconciliation path as doing it by hand.',
      ),
    ),
  );

  if (data.items.length === 0) {
    root.append(panel('Issues', h('p', { class: 'muted' }, 'No escalations, no unconfirmed settlements, no unrecorded payments, and the audit chain verifies.')));
  }
  for (const item of data.items) {
    const detail = detailTable(item);
    const classes = `card finding ${tone(item.severity)}`;
    const head = h(
      'div',
      { class: 'finding-head' },
      h('span', { class: 'finding-code' }, item.code),
      badge(item.severity, tone(item.severity)),
    );
    const actions = item.code === 'invoice_escalated' || item.code === 'invoice_held'
      ? h('button', { onclick: () => location.hash = '#/queue' }, 'Review')
      : null;
    root.append(
      h(
        'section',
        { class: classes },
        head,
        h('p', {}, item.summary),
        nextStep('attention', item.code),
        detail,
        actions,
      ),
    );
  }

  const alertRows = (data.alerts || []).map((item) =>
    h(
      'tr',
      {},
      h('td', {}, badge(item.code, tone(item.severity))),
      h('td', {}, item.severity),
      h('td', {}, item.summary),
    ),
  );
  root.append(
    panel(
      'Alerts from the last pass',
      data.alerts && data.alerts.length
        ? table(['Alert', 'Severity', 'Summary'], alertRows)
        : h('p', { class: 'muted' }, 'The last pass raised no alerts.'),
    ),
  );
}

registerView('attention', renderAttention);
