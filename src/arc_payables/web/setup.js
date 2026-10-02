// What this deployment still needs, and what breaks without it. Read-only: the console never writes
// a setting, because credentials belong in the environment and secret files rather than in a page
// reachable with one shared API key.

import { api, badge, concept, h, lede, panel, plainWords, registerView, table } from './app.js';

function stateBadge(state) {
  if (state === 'set') return badge('set', 'good');
  if (state === 'demo_default') return badge('demo example', 'warn');
  if (state === 'not_needed') return badge('not needed here', '');
  return badge('missing', 'bad');
}

async function renderSetup(root) {
  const data = await api('/setup');

  root.append(
    panel(
      'Configuration',
      lede(
        'A setting that is missing is not a warning about something that might happen. It is the ',
        'reason a named piece of the system is not working right now.',
      ),
      h(
        'p',
        {},
        data.ready
          ? badge('every setting this deployment needs is present', 'good')
          : badge(`${data.missing.length} setting(s) missing`, 'bad'),
      ),
      h(
        'dl',
        { class: 'facts' },
        h('dt', {}, 'Missing'),
        h('dd', {}, data.missing.join(', ') || 'none'),
        h('dt', {}, 'Still a demo example'),
        h('dd', {}, data.demo_defaults.join(', ') || 'none'),
      ),
      h(
        'p',
        { class: 'muted' },
        'Secrets are reported as set or missing and never in full. A URL is reported as its host, because the RPC endpoints for this project carry a token in the path.',
      ),
    ),
  );

  for (const group of data.groups) {
    const rows = group.requirements.map((item) =>
      h(
        'tr',
        {},
        h('td', {}, item.env),
        h('td', {}, stateBadge(item.state), plainWords('setup', item.state)),
        h('td', {}, item.value === null || item.value === undefined ? '—' : String(item.value)),
        h('td', { class: 'muted' }, item.breaks),
      ),
    );
    root.append(
      panel(
        h('span', {}, `${group.name} `, group.complete ? badge('complete', 'good') : badge('incomplete', 'warn')),
        h('p', { class: 'muted' }, group.summary),
        table(['Setting', 'State', 'Value', 'What breaks without it'], rows),
      ),
    );
  }

  const results = h('div', {});
  const run = h(
    'button',
    {
      onclick: async (event) => {
        event.target.disabled = true;
        event.target.textContent = 'Checking…';
        try {
          const outcome = await api('/setup/checks', { method: 'POST' });
          const rows = outcome.checks.map((check) =>
            h(
              'tr',
              {},
              h('td', {}, check.name),
              h('td', {}, check.ok ? badge('ok', 'good') : badge('failed', 'bad')),
              h('td', {}, check.detail),
            ),
          );
          results.replaceChildren(table(['Check', 'Result', 'Detail'], rows));
          for (const check of outcome.checks) {
            if (check.name === 'arc testnet and guard' && check.findings) {
              results.append(
                h(
                  'details',
                  {},
                  h('summary', {}, `Full chain and guard report (${check.findings.length} lines)`),
                  h('ul', { class: 'tight' }, check.findings.map((line) => h('li', {}, line))),
                ),
              );
            }
          }
        } catch (error) {
          results.replaceChildren(h('p', { class: 'error' }, String(error.message)));
        } finally {
          event.target.disabled = false;
          event.target.textContent = 'Run live checks';
        }
      },
    },
    'Run live checks',
  );

  root.append(
    panel(
      'Live checks',
      lede(
        'These ask the accounting system and the chain what they actually hold, rather than what the ',
        'settings say they should. The ', concept('guard', 'guard'), ' is the contract that caps payments.',
      ),
      h(
        'p',
        { class: 'muted' },
        'Reads only. Nothing is signed, sent or written. The chain check reads back the guard address, its policy signer, its payment token and every cap, and the wallet balance.',
      ),
      h('div', { class: 'credentials' }, run),
      results,
    ),
  );
}

registerView('setup', renderSetup);
