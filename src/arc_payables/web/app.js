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
      if (headers[index] !== undefined) cell.setAttribute('data-label', headers[index]);
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
    items.map(({ label, value, unit, tone, concept: term }) =>
      h(
        'div',
        { class: `stat${tone === 'bad' ? ' bad' : tone === 'warn' ? ' warn' : tone === 'good' ? ' ok' : ''}` },
        h('div', { class: 'label' }, term ? concept(term, label) : label),
        h('div', { class: 'value' }, `${value ?? '—'}`, unit ? h('span', { class: 'unit' }, unit) : null),
      ),
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
  const hash = window.location.hash.replace(/^#\/?/, '') || 'attention';
  const [name, ...rest] = hash.split('/');
  const render = views.get(name) || views.get('attention');
  const root = document.getElementById('view');
  const status = document.getElementById('status');
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
