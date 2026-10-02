// Shared shell for the operator console: API client, DOM helpers, and the router.
//
// No build step and no framework on purpose. The console is a thin, auditable view over the
// documented API, and anything it shows can also be fetched with curl.
//
// Everything derived from invoice text, supplier names or policy details is inserted as a text
// node, never as markup: invoice content is untrusted input and this page renders it.

const KEY_STORAGE = 'arc_payables.apiKey';
const APPROVAL_STORAGE = 'arc_payables.approvalToken';
const THEME_STORAGE = 'arc_payables.theme';
const GUIDE_STORAGE = 'arc_payables.guide';

export function theme() {
  return sessionStorage.getItem(THEME_STORAGE) || document.documentElement.dataset.theme || 'light';
}

export function setTheme(value) {
  const next = value === 'dark' ? 'dark' : 'light';
  document.documentElement.dataset.theme = next;
  try {
    sessionStorage.setItem(THEME_STORAGE, next);
  } catch {
    /* session storage may be unavailable; the page still holds the choice */
  }
  return next;
}

export function apiKey() {
  return sessionStorage.getItem(KEY_STORAGE) || '';
}

// Guided mode. A first-time operator may never have held a stablecoin, so a new session opens with
// the explanations switched on: a plain lede on each view, and every domain term underlined so it
// can be read where it stands. Turning it off is remembered for the tab, because a reader who has
// learned the words should not have to dismiss them again on every screen.
export function guided() {
  try {
    return sessionStorage.getItem(GUIDE_STORAGE) !== 'off';
  } catch {
    return true;
  }
}

export function setGuided(value) {
  const next = value === true;
  try {
    sessionStorage.setItem(GUIDE_STORAGE, next ? 'on' : 'off');
  } catch {
    /* session storage may be unavailable; the page still holds the choice for this render */
  }
  return next;
}

/** The definition of one domain term, in the backend's words, or empty until they have loaded. */
export function conceptText(code) {
  const entry = (explanations.concepts && explanations.concepts[code]) || null;
  return entry && entry.plain ? entry.plain : '';
}

/** What the backend suggests reading next for that term, when it wrote something. */
export function conceptAction(code) {
  const entry = (explanations.concepts && explanations.concepts[code]) || null;
  return entry && entry.action ? entry.action : '';
}

export function setApiKey(value) {
  sessionStorage.setItem(KEY_STORAGE, value || '');
}

export function approvalToken() {
  return sessionStorage.getItem(APPROVAL_STORAGE) || '';
}

export function setApprovalToken(value) {
  sessionStorage.setItem(APPROVAL_STORAGE, value || '');
}

function uuid4() {
  // Idempotency keys are UUID v4 on the server side; the create endpoints require one.
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (char) => {
    const random = (Math.random() * 16) | 0;
    const value = char === 'x' ? random : (random & 0x3) | 0x8;
    return value.toString(16);
  });
}

export class ApiError extends Error {
  constructor(status, detail) {
    const code = detail && detail.code ? detail.code : `HTTP ${status}`;
    const message = detail && detail.message ? detail.message : JSON.stringify(detail);
    super(`${code}: ${message}`);
    this.status = status;
    this.detail = detail;
  }
}

export async function api(path, { method = 'GET', body = null, approval = false } = {}) {
  const headers = { Accept: 'application/json' };
  if (apiKey()) headers['X-API-Key'] = apiKey();
  if (approval) {
    if (!approvalToken()) {
      throw new ApiError(0, {
        code: 'approval_token_required',
        message: 'This action needs a human approval token. Add it in the header field.',
      });
    }
    headers['X-Approval-Token'] = approvalToken();
  }
  const options = { method, headers };
  if (body !== null) {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  if (method === 'POST') headers['Idempotency-Key'] = uuid4();
  const response = await fetch(path, options);
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = { code: 'invalid_response', message: text.slice(0, 200) };
  }
  if (!response.ok) {
    const detail = payload && payload.detail ? payload.detail : payload;
    throw new ApiError(response.status, detail);
  }
  return payload;
}

export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// A domain term the reader may not know, underlined so it can be read on the spot. This is the
// whole beginner layer: the definition is fetched from the service rather than written here, so the
// console cannot describe the system differently from the way the system behaves.
//
// In normal view the word is plain text again. A reader who knows the vocabulary is not asked to
// walk past an explanation on every line.
export function concept(code, label = code) {
  if (!guided()) return label;
  return h(
    'span',
    {
      class: 'term',
      'data-concept': code,
      tabindex: '0',
      role: 'button',
      'aria-describedby': TIP_ID,
      'aria-expanded': 'false',
    },
    label,
  );
}

// The plain-language introduction to a view, shown only while the reader is still being walked
// through. Returns nothing in normal view, so callers can append it unconditionally.
export function lede(...children) {
  if (!guided()) return null;
  return h('p', { class: 'lede' }, ...children);
}

// One popover for the whole page, positioned in the viewport rather than inside a card. A tooltip
// nested in a scrolling table would be clipped by it, and a table is exactly where a reader meets
// an unfamiliar word.
const TIP_ID = 'concept-tip';
let tip = null;
let tipOwner = null;

function tipElement() {
  if (!tip) {
    tip = h(
      'div',
      { class: 'concept-tip', id: TIP_ID, role: 'tooltip', hidden: true },
      h('div', { class: 'concept-tip-plain' }),
      h('div', { class: 'concept-tip-action' }),
    );
    document.body.append(tip);
  }
  return tip;
}

function hideTip() {
  if (tipOwner) tipOwner.setAttribute('aria-expanded', 'false');
  tipOwner = null;
  if (tip) tip.hidden = true;
}

// Draw whatever the reader is currently asking about. Called again when the definitions arrive, so a
// term pointed at during the first moment of a page load is not silently ignored: the request is
// remembered, and it is answered as soon as there are words to answer it with.
function drawTip() {
  if (!tipOwner) return;
  if (!tipOwner.isConnected) {
    // The view was re-rendered out from under the reader.
    hideTip();
    return;
  }
  const plain = conceptText(tipOwner.getAttribute('data-concept'));
  if (!plain) {
    // Still loading, or a genuine gap in the glossary. Keep the request; show nothing meanwhile.
    if (tip) tip.hidden = true;
    tipOwner.setAttribute('aria-expanded', 'false');
    return;
  }
  const node = tipElement();
  node.querySelector('.concept-tip-plain').textContent = plain;
  const action = node.querySelector('.concept-tip-action');
  action.textContent = conceptAction(tipOwner.getAttribute('data-concept'));
  action.hidden = !action.textContent;
  node.hidden = false;
  tipOwner.setAttribute('aria-expanded', 'true');
  placeTip(node, tipOwner);
}

function placeTip(node, owner) {
  const anchor = owner.getBoundingClientRect();
  const box = node.getBoundingClientRect();
  const gap = 8;
  const left = Math.max(gap, Math.min(anchor.left + anchor.width / 2 - box.width / 2, window.innerWidth - box.width - gap));
  const below = anchor.bottom + gap;
  const above = anchor.top - box.height - gap;
  const top = below + box.height <= window.innerHeight - gap || above < gap
    ? Math.min(below, Math.max(gap, window.innerHeight - box.height - gap))
    : above;
  node.style.left = `${Math.round(left)}px`;
  node.style.top = `${Math.round(top)}px`;
}

function showTip(owner) {
  tipOwner = owner;
  drawTip();
}

// Delegated once, on the document, so a re-rendered view needs no listeners of its own.
function installConcepts() {
  const termIn = (event) => (event.target instanceof Element ? event.target.closest('[data-concept]') : null);
  document.addEventListener('pointerover', (event) => {
    if (event.pointerType === 'touch') return; // a tap is a click; let that path decide
    const owner = termIn(event);
    if (owner) showTip(owner);
  });
  document.addEventListener('pointerout', (event) => {
    if (event.pointerType === 'touch') return;
    if (termIn(event) === tipOwner) hideTip();
  });
  document.addEventListener('focusin', (event) => {
    const owner = termIn(event);
    if (owner) showTip(owner);
    else hideTip();
  });
  document.addEventListener('focusout', (event) => {
    if (termIn(event) === tipOwner) hideTip();
  });
  // A tap, or Enter on a focused term. Both land here, so touch and keyboard share one path.
  document.addEventListener('click', (event) => {
    const owner = termIn(event);
    if (owner) showTip(owner);
    else hideTip();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') hideTip();
  });
  window.addEventListener('scroll', hideTip, true);
  window.addEventListener('resize', hideTip);
}

// Installed at import rather than from startRouter, so a term is live wherever the helpers are used.
// The listeners are delegated and cheap, and the popover element itself is built lazily.
installConcepts();

// A heading may be a node, because the worst jargon on a screen is usually a column name. The
// phone reflow reads a cell's column back off this string, so a node's text is what gets used.
function headerLabel(header) {
  return header instanceof Node ? header.textContent : String(header);
}

// A table that survives a phone. Each cell carries its column name so the stylesheet can reflow the
// row into a labelled card below the breakpoint, which is how a wide operator table stays readable.
export function table(headers, rows) {
  const body = h(
    'tbody',
    {},
    rows.length ? rows : h('tr', {}, h('td', { colspan: headers.length, class: 'empty' }, 'Nothing to show')),
  );
  for (const row of body.querySelectorAll('tr')) {
    [...row.children].forEach((cell, index) => {
      if (headers[index] !== undefined) cell.setAttribute('data-label', headerLabel(headers[index]));
    });
  }
  return h(
    'div',
    { class: 'table-wrapper responsive-table' },
    h('table', {}, h('thead', {}, h('tr', {}, headers.map((label) => h('th', {}, label)))), body),
  );
}

const BADGE_TONES = {
  good: 'success',
  bad: 'critical',
  warn: 'warning',
  '': 'neutral',
  success: 'success',
  critical: 'critical',
  warning: 'warning',
  neutral: 'neutral',
};

export function badge(text, kind = '') {
  return h('span', { class: `badge ${BADGE_TONES[kind] || 'neutral'}` }, text);
}

// State to a colour tone, so a reviewer can read a workflow state at a glance. The badge always
// carries the state text as well, so the colour is reinforcement rather than the only signal.
// One labelled figure at a time, arranged by the caller in a `.stats` grid. Money is the hero:
// the value is the largest type on the page, the unit sits beside it, and the tone is reinforcement.
export function statGrid(items) {
  return h(
    'div',
    { class: 'stats' },
    items.map(({ label, value, unit, tone, concept: term, note, icon: glyph }) =>
      h(
        'div',
        { class: `stat${tone === 'bad' ? ' bad' : tone === 'warn' ? ' warn' : tone === 'good' ? ' ok' : ''}` },
        h(
          'div',
          { class: 'stat-head' },
          h('div', { class: 'label' }, term ? concept(term, label) : label),
          glyph ? h('span', { class: 'stat-icon' }, icon(glyph)) : null,
        ),
        h('div', { class: 'value' }, `${value ?? '—'}`, unit ? h('span', { class: 'unit' }, unit) : null),
        note ? h('div', { class: 'note' }, note) : null,
      ),
    ),
  );
}

/**
 * The page heading: a small uppercase eyebrow, the title, and how many rows are behind it.
 *
 * Every view opens with one, so a reader always knows which screen they are on and what it is
 * counting. The mock this console follows made the same move, and it is the cheapest way to stop a
 * long table looking like it starts in the middle of nowhere.
 */
export function sectionHeading({ eyebrow, title, count, trailing = null }) {
  return h(
    'div',
    { class: 'section-heading' },
    h(
      'div',
      { class: 'section-heading-copy' },
      eyebrow ? h('div', { class: 'section-eyebrow' }, eyebrow) : null,
      h('h2', { class: 'section-title' }, title),
    ),
    count === undefined && !trailing
      ? null
      : h(
          'div',
          { class: 'section-heading-side' },
          count === undefined ? null : h('span', { class: 'section-count' }, `${count} rows`),
          trailing,
        ),
  );
}

// The icon set, inline. No sprite sheet and no downloaded pack: the console may not load an
// external asset, and seven small paths cost less than the rule that would have to break.
const ICONS = {
  layout: ['rect|3|3|18|18|2', 'line|3|9|21|9', 'line|9|21|9|9'],
  alert: ['path|M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z', 'line|12|9|12|13', 'line|12|17|12.01|17'],
  activity: ['polyline|22 12 18 12 15 21 9 3 6 12 2 12'],
  settings: ['circle|12|12|3', 'path|M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z'],
  wallet: ['path|M21 12V7H5a2 2 0 0 1 0-4h14v4', 'path|M3 5v14a2 2 0 0 0 2 2h16v-5', 'path|M18 12a2 2 0 0 0 0 4h4v-4Z'],
  shield: ['path|M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z'],
  fileText: ['path|M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z', 'polyline|14 2 14 8 20 8', 'line|16|13|8|13', 'line|16|17|8|17'],
  list: ['line|8|6|21|6', 'line|8|12|21|12', 'line|8|18|21|18', 'line|3|6|3.01|6', 'line|3|12|3.01|12', 'line|3|18|3.01|18'],
  cpu: ['rect|4|4|16|16|2', 'rect|9|9|6|6', 'line|9|1|9|4', 'line|15|1|15|4', 'line|9|20|9|23', 'line|15|20|15|23', 'line|20|9|23|9', 'line|20|14|23|14', 'line|1|9|4|9', 'line|1|14|4|14'],
  check: ['polyline|20 6 9 17 4 12'],
};

/** One inline SVG. Unknown names fall back to the grid icon rather than rendering nothing. */
export function icon(name, size = 18) {
  const parts = ICONS[name] || ICONS.layout;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  for (const [key, value] of Object.entries({ width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' })) {
    svg.setAttribute(key, value);
  }
  for (const part of parts) {
    const [tag, ...rest] = part.split('|');
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    if (tag === 'polyline') {
      node.setAttribute('points', rest[0]);
    } else if (tag === 'path') {
      node.setAttribute('d', rest[0]);
    } else {
      const names = { rect: ['x', 'y', 'width', 'height', 'rx'], circle: ['cx', 'cy', 'r'], line: ['x1', 'y1', 'x2', 'y2'] }[tag] || [];
      rest.forEach((value, index) => names[index] && node.setAttribute(names[index], value));
    }
    svg.append(node);
  }
  return svg;
}

/**
 * A terminal-looking activity list: time, level, message.
 *
 * The level is a word as well as a colour, because a log that can only be read by hue is unreadable
 * to anyone who cannot separate the hues.
 */
export function logPanel(entries) {
  const lines = entries.length
    ? entries.map(({ time, level, message }) =>
        h(
          'div',
          { class: 'log-line' },
          h('span', { class: 'log-time' }, time || ''),
          h('span', { class: `log-level log-level-${level || 'info'}` }, `[${String(level || 'info').toUpperCase()}]`),
          h('span', { class: 'log-msg' }, message),
        ),
      )
    : h('div', { class: 'log-line' }, h('span', { class: 'log-time' }, ''), h('span', {}), h('span', { class: 'log-msg' }, 'Nothing recorded yet.'));
  return h(
    'div',
    { class: 'log-container' },
    ...lines,
    h(
      'div',
      { class: 'log-line' },
      h('span', { class: 'log-time log-prompt' }, '>'),
      h('span', {}),
      h('span', { class: 'log-cursor' }, '_'),
    ),
  );
}

export function stateTone(state) {
  if (!state) return '';
  if (['ELIGIBLE', 'CONFIRMED', 'ERP_RECORDED'].includes(state)) return 'good';
  if (['ESCALATED', 'HELD', 'NEEDS_RECONCILIATION', 'FAILED'].includes(state)) return 'bad';
  if (['WAITING', 'AUTHORIZED', 'SUBMITTED', 'ERP_PENDING'].includes(state)) return 'warn';
  return '';
}

export function panel(title, ...content) {
  return h('section', { class: 'card' }, h('h2', {}, title), ...content);
}

export function errorPanel(error) {
  return h('div', { class: 'error' }, h('strong', {}, 'Request failed'), h('div', {}, String(error.message || error)));
}

// One short line of plain language for anything the system says. Fetched once from the server's
// own words so the console cannot drift from the backend, and silent when it has not loaded yet
// rather than guessing. Exported under _test so the console suite can assert what is here.
let explanations = {};

fetch('/explanations.json')
  .then((response) => (response.ok ? response.json() : {}))
  .then((loaded) => {
    explanations = loaded || {};
    drawTip();
  })
  .catch(() => {});

export function explain(kind, code) {
  const entry = (explanations && explanations[kind] && explanations[kind][code]) || null;
  return entry && entry.plain ? entry.plain : '';
}

export function explainAction(kind, code) {
  const entry = (explanations && explanations[kind] && explanations[kind][code]) || null;
  return entry && entry.action ? entry.action : '';
}

// The service's plain words for one code, shown always rather than only while the explanations are
// on. Reading a screen should not require decoding NEEDS_RECONCILIATION first, whether or not the
// reader is new here, so this is part of the console rather than part of the guided layer.
export function plainWords(kind, code) {
  const text = explain(kind, code);
  return text ? h('div', { class: 'muted' }, text) : null;
}

// The backend's suggested next move for one code, shown only while the reader is being walked
// through. It is additive by design: a summary already says what happened, so this line only ever
// adds what to do about it, and it disappears entirely once the reader knows the vocabulary.
export function nextStep(kind, code) {
  if (!guided()) return null;
  const action = explainAction(kind, code);
  if (!action) return null;
  return h('p', { class: 'guide-note' }, h('span', { class: 'guide-note-label' }, 'What to do next: '), action);
}

const views = new Map();

// What the header calls each view. The nav labels the same destinations; these are the page titles,
// which say what the screen is rather than what it is called in the sidebar.
const TITLES = {
  overview: 'Overview',
  attention: 'Exceptions',
  queue: 'Queue',
  payments: 'Settlements',
  audit: 'Audit chain',
  worker: 'Worker',
  treasury: 'Treasury and risk',
  setup: 'Setup',
  invoice: 'Invoice',
};

export function registerView(name, render) {
  views.set(name, render);
}

// Money and liveness the sidebar always shows: the agent's health and the treasury balance, read
// from the same two endpoints the console already fetches. Best effort and silent, because the shell
// must never block a view on it.
export async function refreshShell() {
  const dot = document.getElementById('agent-dot');
  const text = document.getElementById('agent-text');
  const pill = document.getElementById('balance-pill');
  const count = document.getElementById('attention-count');
  if (!dot || !text || !pill || !count) return;
  try {
    const [attention, forecast] = await Promise.all([api('/attention'), api('/forecast?days=30')]);
    const problems = (attention.critical || 0) + (attention.warning || 0);
    dot.className = `agent-dot ${attention.critical > 0 ? 'bad' : attention.warning > 0 ? 'warn' : 'ok'}`;
    text.textContent = problems === 0 ? 'Agent idle, nothing waiting' : `Agent working, ${problems} waiting`;
    pill.textContent = forecast.balance_usdc === null || forecast.balance_usdc === undefined ? '—' : `${forecast.balance_usdc} USDC`;
    if (attention.critical > 0) {
      count.hidden = false;
      count.textContent = String(attention.critical);
    } else {
      count.hidden = true;
    }
  } catch {
    dot.className = 'agent-dot';
    text.textContent = 'Agent unreachable';
    pill.textContent = '—';
    count.hidden = true;
  }
}

async function route() {
  const hash = window.location.hash.replace(/^#\/?/, '') || 'overview';
  const [name, ...rest] = hash.split('/');
  const render = views.get(name) || views.get('overview');
  const root = document.getElementById('view');
  const status = document.getElementById('status');
  const title = document.getElementById('page-title');
  if (title) title.textContent = TITLES[name] || TITLES.overview;
  hideTip(); // the term a reader was pointing at is about to be replaced
  root.replaceChildren(h('p', { class: 'muted' }, 'Loading…'));
  status.replaceChildren(
    apiKey() ? h('span', { class: 'muted' }, 'API key set') : h('span', { class: 'warn-text' }, 'No API key set'),
  );
  for (const link of document.querySelectorAll('nav a')) {
    link.classList.toggle('active', link.getAttribute('href') === `#/${name}`);
  }
  try {
    await render(root, rest);
  } catch (error) {
    root.replaceChildren(errorPanel(error));
  }
}

/** Re-render the current view through the router, and the shell facts around it. */
export function refresh() {
  refreshShell();
  return route();
}

export function startRouter() {
  window.addEventListener('hashchange', route);
  refreshShell();
  route();
}
