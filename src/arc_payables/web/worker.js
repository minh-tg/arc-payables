// The worker's face: recent passes, whether they finished cleanly, and what they asked
// a human to look at. Read-only. Alerts are recorded on the pass, not recomputed here.

import { actionButton, api, badge, can, concept, formatTimestamp, h, humanizeCode, lede, nextStep, panel, plainWords, refresh, registerView, sectionHeading, table } from './app.js';

// Backups are local copies of the live database, verified before they are published. A restore drill
// proves a copy is usable by restoring it into a scratch file. Nothing here ships data off the host.
// Taking a backup re-renders the view, which would erase its own confirmation, so the message is carried
// across the re-render once and shown by the panel that draws next.
let backupFlash = null;

async function backupsPanel() {
  const listing = await api('/backups');
  const feedback = h('div', { role: 'status', 'aria-live': 'polite' });
  if (backupFlash) {
    feedback.append(h('p', { role: 'status' }, backupFlash));
    backupFlash = null;
  }
  const rows = listing.backups.map((item) =>
    h(
      'tr',
      {},
      h('td', { class: 'mono' }, item.name),
      h('td', {}, item.complete ? badge('complete', 'good') : badge('no manifest', 'bad')),
      h('td', {}, item.created_at || '—'),
      h('td', {}, item.data_reaches || '—'),
      h('td', { class: 'num' }, item.size_bytes != null ? `${item.size_bytes} bytes` : '—'),
      h(
        'td',
        {},
        !item.complete
          ? h('span', { class: 'muted' }, 'Not restorable')
          : can('operate')
            ? actionButton('Restore drill', async () => {
                const drill = await api('/backups/restore-drill', { method: 'POST', body: { backup: item.name } });
                feedback.replaceChildren(h(
                  'p',
                  { role: 'status' },
                  `Restore drill passed for ${item.name} in ${drill.restore_seconds} s. The restored copy verifies and its row counts match the manifest.`,
                ));
              }, feedback)
            : h('span', { class: 'muted' }, 'Operator role required'),
      ),
    ),
  );
  const backUp = can('operate')
    ? actionButton('Back up now', async () => {
        const made = await api('/backups', { method: 'POST' });
        backupFlash = `Backup ${made.name} written and verified. Data reaches ${made.data_reaches || 'the start of the database'}.`;
        await refresh();
      }, feedback, { class: 'btn-primary' })
    : h('p', { class: 'muted' }, 'Operator role required to back up.');
  return panel(
    'Backups on this host',
    lede(
      'A backup is a consistent copy of the database, checked before it is published. A restore drill restores one into a scratch file and verifies it. The copy stays on this host. An off-host copy is a separate command-line step.',
    ),
    h('div', { class: 'credentials' }, backUp),
    feedback,
    table(['File', 'Manifest', 'Recorded', 'Data reaches', 'Size', 'Restore'], rows),
  );
}

// One test alert through the same sink the worker uses, so delivery is shown rather than assumed.
function alertTestPanel() {
  const feedback = h('div', { role: 'status', 'aria-live': 'polite' });
  const send = can('operate')
    ? actionButton('Send test alert', async () => {
        const outcome = await api('/alerts/test', { method: 'POST' });
        feedback.replaceChildren(outcome.delivered
          ? h('p', { role: 'status' }, `Delivered to ${outcome.destination}. A worker pass would deliver real alerts the same way.`)
          : h('p', { class: 'error', role: 'alert' }, `Not delivered to ${outcome.destination}: ${outcome.error}`));
      }, feedback, { class: 'btn-primary' })
    : h('p', { class: 'muted' }, 'Operator role required to send a test alert.');
  return panel(
    'Alert delivery test',
    lede(
      'Alerts are recorded on every pass. This sends a labelled test alert to verify notification delivery.',
    ),
    h('div', { class: 'credentials' }, send),
    feedback,
  );
}


function levelTone(level) {
  if (level === 'critical') return 'bad';
  if (level === 'warning') return 'warn';
  return '';
}

function outcomeTone(outcome) {
  if (outcome === 'ok') return 'good';
  if (outcome === 'failed' || outcome === 'degraded') return 'bad';
  return 'warn';
}

function stepSummary(step) {
  const acted = `${step.acted}/${step.examined}`;
  if (step.failed) return `${acted} (${step.failed} failed)`;
  if (step.error) return `${acted} (error)`;
  return acted;
}

// Why a step did not act on something. A pass that declines a payable has to say why, or the
// operator cannot tell a missing FX rate from an outage.
const MAX_NOTES = 3;

function stepNotes(step) {
  const detail = step.detail || {};
  const notes = [];
  if (step.error) notes.push(step.error);

  const declined = detail.declined || [];
  for (const item of declined.slice(0, MAX_NOTES)) {
    notes.push(`${item.external_id}: ${item.reason || item.code}`);
  }
  const declinedTotal = Object.values(detail.declined_codes || {}).reduce((sum, n) => sum + n, 0);
  if (declinedTotal > declined.length) {
    notes.push(`and ${declinedTotal - declined.length} more declined`);
  }

  for (const [code, count] of Object.entries(detail.refusals || {})) {
    notes.push(`refused ${count}× ${code}`);
  }
  for (const item of (detail.errors || []).slice(0, MAX_NOTES)) {
    notes.push(`${item.invoice_id || item.external_id}: ${item.error}`);
  }
  if (detail.deferred) notes.push(`${detail.deferred} deferred to the next pass`);
  if (detail.waiting) notes.push(`${detail.waiting} waiting out a writeback backoff`);
  if (detail.stopped_reason) notes.push(`Stopped: ${detail.stopped_reason}`);
  if (detail.reevaluated?.length) notes.push(`${detail.reevaluated.length} changed decisions re-evaluated`);
  for (const item of (detail.plans || []).slice(0, MAX_NOTES)) {
    notes.push(`Plan ${item.id}: ${item.status || 'outcome not recorded'} · ${item.ordered_by}${item.reason ? ` · ${item.reason}` : ''}`);
  }
  if ((detail.plans || []).length > MAX_NOTES) notes.push('More plan choices and outcomes in Payment queue.');
  return notes;
}

async function renderWorker(root) {
  const status = await api('/worker/status');
  root.append(
    sectionHeading({
      eyebrow: 'Background pass',
      title: 'Worker',
      count: (status.last && status.last.detail && status.last.detail.steps ? status.last.detail.steps.length : 0),
    }),
  );

  const last = status.last;
  const facts = h(
    'dl',
    { class: 'facts' },
    h('dt', {}, 'Last pass'),
    h('dd', {}, last ? `${last.finished_at} · ${last.outcome}` : 'no pass recorded yet'),
    h('dt', {}, 'Consecutive failures'),
    h('dd', {}, String(status.consecutive_failures)),
    h('dt', {}, 'Passes observed'),
    h('dd', {}, String(status.observed)),
  );

  root.append(
    panel(
      'Worker health',
      lede(
        'A pass is one trip through the work: reading what the ', concept('ledger', 'ledger'),
        ' still owes, paying what policy already authorized, finishing the ledger entries, and asking ',
        'the chain about anything still unconfirmed. The worker approves nothing.',
      ),
      status.last ? facts : h('p', { class: 'muted' }, 'No pass has been recorded yet. The loop writes here once it runs.'),
      h('a', { href: '#/queue' }, 'Inspect recorded payment plans'),
      last && last.detail && last.detail.stopped_reason
        ? h('p', { class: 'error' }, `Stopped: ${last.detail.stopped_reason}`)
        : null,
    ),
  );

  const alerts = status.alerts || [];
  const alertRows = alerts.map((item) =>
    h(
      'tr',
      {},
      h('td', {}, badge(item.code, levelTone(item.severity)), plainWords('alerts', item.code)),
      h('td', {}, item.severity),
      h('td', {}, item.summary, nextStep('alerts', item.code)),
    ),
  );
  const delivery = status.alert_delivery || [];
  root.append(
    panel(
      'Latest alerts',
      alerts.length
        ? table(['Alert', 'Severity', 'Summary'], alertRows)
        : h('p', { class: 'muted' }, 'No alerts on the latest pass.'),
      delivery.length
        ? h('p', { class: 'muted' }, `Delivery problems: ${delivery.map((item) => `${item.code}: ${item.error}`).join('; ')}`)
        : null,
    ),
  );

  const steps = (last && last.detail && last.detail.steps) || [];
  const stepRows = steps.map((step) => {
    const notes = stepNotes(step);
    return h(
      'tr',
      {},
      h('td', {}, humanizeCode(step.name), plainWords('steps', step.name)),
      h('td', {}, stepSummary(step)),
      h('td', { class: 'num' }, String(step.skipped)),
      h('td', {}, step.failed || step.error ? badge('needs attention', 'bad') : badge('Healthy', 'good')),
      h(
        'td',
        { class: 'muted' },
        notes.length
          ? h('ul', { class: 'tight' }, notes.map((note) => h('li', {}, note)))
          : '',
      ),
    );
  });
  if (last) {
    root.append(
      panel(
        `Latest pass · ${formatTimestamp(last.started_at)}`,
        h('p', { class: 'muted' }, `Outcome: ${humanizeCode(last.outcome)}`),
        plainWords('passes', last.outcome),
        table(['Step', 'Acted / examined', 'Skipped', 'State', 'Why'], stepRows),
      ),
    );
  }

  const outcomes = status.outcomes || {};
  const entries = Object.entries(outcomes);
  const nextSteps = entries.map(([outcome]) => nextStep('passes', outcome));
  // A column that is always empty reads as unfinished work, so it only appears when a pass in the
  // list actually asks something of a person.
  const withNext = nextSteps.some(Boolean);
  const outcomeRows = entries.map(([outcome, count], index) =>
    h(
      'tr',
      {},
      h('td', {}, badge(outcome, outcomeTone(outcome)), plainWords('passes', outcome)),
      h('td', { class: 'num' }, String(count)),
      withNext ? h('td', {}, nextSteps[index]) : null,
    ),
  );
  root.append(
    panel(
      'Passes by outcome',
      table(withNext ? ['Outcome', 'Count', 'What to do next'] : ['Outcome', 'Count'], outcomeRows),
    ),
  );

  root.append(alertTestPanel());
  root.append(await backupsPanel());
}

registerView('worker', renderWorker);
