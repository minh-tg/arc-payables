// Shared shell for the operator console: API client, small DOM helpers, and the router.
//
// No build step and no framework on purpose. The console is a thin, auditable view over the
// documented API, and anything it shows can also be fetched with curl.
//
// Everything derived from invoice text, supplier names or policy details is inserted as a text
// node, never as markup: invoice content is untrusted input and this page renders it.

const KEY_STORAGE = 'arc_payables.apiKey';
const APPROVAL_STORAGE = 'arc_payables.approvalToken';

export function apiKey() {
  return sessionStorage.getItem(KEY_STORAGE) || '';
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

export function table(headers, rows) {
  return h(
    'table',
    { class: 'grid' },
    h('thead', {}, h('tr', {}, headers.map((label) => h('th', {}, label)))),
    h(
      'tbody',
      {},
      rows.length ? rows : h('tr', {}, h('td', { colspan: headers.length, class: 'empty' }, 'Nothing to show')),
    ),
  );
}

export function badge(text, kind = '') {
  return h('span', { class: `badge ${kind}` }, text);
}

// State to a colour tone, so a reviewer can read a workflow state at a glance.
export function stateTone(state) {
  if (!state) return '';
  if (['ELIGIBLE', 'CONFIRMED', 'ERP_RECORDED'].includes(state)) return 'good';
  if (['ESCALATED', 'HELD', 'NEEDS_RECONCILIATION', 'FAILED'].includes(state)) return 'bad';
  if (['WAITING', 'AUTHORIZED', 'SUBMITTED', 'ERP_PENDING'].includes(state)) return 'warn';
  return '';
}

export function panel(title, ...content) {
  return h('section', { class: 'panel' }, h('h2', {}, title), ...content);
}

export function errorPanel(error) {
  return h('div', { class: 'error' }, h('strong', {}, 'Request failed'), h('div', {}, String(error.message || error)));
}

const views = new Map();

export function registerView(name, render) {
  views.set(name, render);
}

async function route() {
  const hash = window.location.hash.replace(/^#\/?/, '') || 'queue';
  const [name, ...rest] = hash.split('/');
  const render = views.get(name) || views.get('queue');
  const root = document.getElementById('view');
  const status = document.getElementById('status');
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

/** Re-render the current view through the router. */
export function refresh() {
  return route();
}

export function startRouter() {
  window.addEventListener('hashchange', route);
  route();
}
