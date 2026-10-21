'use strict';
/* Vouching Agent - single-page UI. No external libraries; all data is rendered with textContent (no innerHTML). */

// Element.append(null) would render the text "null"; drop empty children everywhere in this app.
const _append = Element.prototype.append;
Element.prototype.append = function (...kids) {
  return _append.apply(this, kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));
};

const root = document.getElementById('app');
const state = { user: null, system: null, timers: [], drawer: null };
const TYPES = ['purchase', 'sales', 'expense', 'payroll', 'fixed_asset', 'bank_payment', 'other'];
const DOC_TYPES = ['invoice', 'receipt', 'credit_note', 'payslip', 'expense_report', 'remittance', 'bank_advice',
  'statement', 'purchase_order', 'contract', 'quotation', 'unknown'];
const STATUS_LABEL = { pending: 'Pending', resolved: 'Resolved', confirmed: 'Confirmed exception', followup: 'Follow-up' };
const DISP_LABEL = { escalate: 'Escalated', review: 'Review', auto_cleared: 'Cleared' };
const CODE_LABEL = {
  NO_VOUCHER: 'No document', AMOUNT_MISMATCH: 'Amount differs', COUNTERPARTY_MISMATCH: 'Wrong counterparty',
  REFERENCE_MISMATCH: 'Reference differs', CURRENCY_MISMATCH: 'Currency differs', CUTOFF_ISSUE: 'Cut-off',
  DATE_MISMATCH: 'Date issue', VOUCHER_REUSED: 'Document reused', MISSING_APPROVAL: 'No approval',
  LOW_CONFIDENCE_EXTRACTION: 'Hard to read', FIELD_MISSING: 'Field missing', WRONG_DOC_TYPE: 'Wrong document type',
};

/* ---------- tiny DOM helper ---------- */
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  let value;
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'value') value = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'checked' || k === 'disabled' || k === 'selected' || k === 'multiple') el[k] = !!v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  add(el, kids);
  if (value !== undefined) el.value = value;
  return el;
}
function add(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}
const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };
const chip = (text, cls) => h('span', { class: 'chip ' + (cls || 'plain') }, text);
const money = (v, cur) => (v === null || v === undefined || v === '') ? '–'
  : (cur ? cur + ' ' : '') + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const when = (s) => s ? new Date(s).toLocaleString() : '';
const short = (txn) => String(txn || '').replace(/^Transaction:/, '');
const enc = encodeURIComponent;

class ApiError extends Error { constructor(msg, status) { super(msg); this.status = status; } }

async function api(path, opts = {}) {
  const init = { method: opts.method || 'GET', headers: { 'X-VRA': '1' }, credentials: 'same-origin' };
  if (opts.json !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.json); }
  if (opts.form) init.body = opts.form;
  let r;
  try { r = await fetch(path, init); } catch (e) { throw new ApiError('Cannot reach the server. Is it still running?', 0); }
  const isJson = (r.headers.get('content-type') || '').includes('json');
  const data = opts.text && r.ok ? await r.text() : (isJson ? await r.json() : null);
  if (!r.ok) {
    if (r.status === 401 && state.user) { state.user = null; go('#/login'); }
    throw new ApiError((data && data.error) || `Request failed (${r.status})`, r.status);
  }
  return data;
}

function toast(msg, kind) {
  const t = h('div', { class: 'toast ' + (kind || '') }, msg);
  document.getElementById('toasts').append(t);
  setTimeout(() => t.remove(), kind === 'err' ? 9000 : 4500);
}
const fail = (e) => toast(e.message || String(e), 'err');
async function guard(btn, fn) {
  if (btn) btn.disabled = true;
  try { return await fn(); } catch (e) { fail(e); } finally { if (btn) btn.disabled = false; }
}

/* ---------- router ---------- */
function go(hash) { if (location.hash === hash) route(); else location.hash = hash; }
window.addEventListener('hashchange', route);
function every(fn, ms) { const id = setInterval(fn, ms); state.timers.push(id); }
function stopTimers() { state.timers.forEach(clearInterval); state.timers = []; }

async function route() {
  stopTimers(); closeDrawer(); closeModal();
  try { state.system = await api('/api/system'); state.user = state.system.user; } catch (e) { return fatal(e); }
  if (state.system.setup_required) return viewAuth('setup');
  if (!state.user) return viewAuth('login');
  const [path, query = ''] = (location.hash || '#/').slice(1).split('?');
  const q = new URLSearchParams(query);
  const p = path.split('/').filter(Boolean);
  try {
    if (!p.length) return await viewEngagements();
    if (p[0] === 'e' && p[1]) return await viewEngagement(+p[1], p[2] || 'setup', p[3] ? +p[3] : null, q);
    if (p[0] === 'users') return await viewUsers();
    if (p[0] === 'settings') return await viewSettings();
    if (p[0] === 'login') return go('#/');
    shell('', h('div', { class: 'card empty' }, 'Page not found. ', h('a', { href: '#/' }, 'Go home')));
  } catch (e) { if (e.status !== 401) shell('', h('div', { class: 'banner err' }, e.message)); }
}
function fatal(e) {
  clear(root).append(h('div', { class: 'page' }, h('div', { class: 'banner err' }, e.message)));
}

function shell(active, ...content) {
  const u = state.user;
  const nav = h('nav', { class: 'nav' },
    h('a', { href: '#/', class: active === 'home' ? 'active' : '' }, 'Engagements'),
    u.role === 'admin' ? h('a', { href: '#/users', class: active === 'users' ? 'active' : '' }, 'Users') : null,
    h('a', { href: '#/settings', class: active === 'settings' ? 'active' : '' }, 'Settings'));
  clear(root).append(
    h('div', { class: 'topbar' },
      h('a', { class: 'brand', href: '#/' }, h('span', { class: 'logo' }, '✓'), 'Vouching Agent'), nav,
      h('div', { class: 'spacer' }),
      state.system.solo ? chip('Solo mode', 'plain') : null,
      h('div', { class: 'userbox' }, h('span', null, u.display_name, ' ', chip(u.role, 'plain')),
        h('button', { class: 'btn sm', onclick: async () => { await api('/api/logout', { method: 'POST' }); state.user = null; go('#/'); } }, 'Sign out'))),
    h('div', { class: 'page' }, ...content));
  window.scrollTo(0, 0);
}

/* ---------- login / first-run setup ---------- */
function viewAuth(mode) {
  const setup = mode === 'setup';
  const user = h('input', { id: 'u', autocomplete: 'username', autofocus: true });
  const name = h('input', { id: 'n', autocomplete: 'name' });
  const pw = h('input', { id: 'p', type: 'password', autocomplete: setup ? 'new-password' : 'current-password' });
  const err = h('div', { class: 'banner err', style: 'display:none' });
  const submit = async (ev) => {
    ev.preventDefault();
    err.style.display = 'none';
    try {
      await api(setup ? '/api/setup' : '/api/login', { method: 'POST', json: { username: user.value, display_name: name.value, password: pw.value } });
      go('#/');
    } catch (e) { err.textContent = e.message; err.style.display = 'block'; }
  };
  clear(root).append(h('div', { class: 'auth card' },
    h('div', { class: 'logo' }, '✓'),
    h('h1', null, setup ? 'Welcome - create the administrator account' : 'Sign in'),
    h('p', { class: 'muted' }, setup ? 'This is the first start. The account you create can add colleagues later. Use at least 10 characters for the password.'
      : 'Vouching & Reconciliation Agent'),
    err,
    h('form', { onsubmit: submit },
      h('div', { style: 'margin-bottom:12px' }, h('label', { for: 'u' }, 'Username'), user),
      setup ? h('div', { style: 'margin-bottom:12px' }, h('label', { for: 'n' }, 'Your name'), name) : null,
      h('div', { style: 'margin-bottom:16px' }, h('label', { for: 'p' }, 'Password'), pw),
      h('button', { class: 'btn primary', style: 'width:100%', type: 'submit' }, setup ? 'Create account and continue' : 'Sign in'))));
}

/* ---------- engagements ---------- */
async function viewEngagements() {
  const list = await api('/api/engagements');
  const body = list.length ? h('div', { class: 'card flush scroll' }, h('table', null,
    h('thead', null, h('tr', null, ['Engagement', 'Client', 'Period', 'Documents', 'Latest run', 'Created by'].map((x) => h('th', null, x)))),
    h('tbody', null, list.map((e) => {
      const r = e.last_run;
      return h('tr', { class: 'click', onclick: () => go(`#/e/${e.id}/${r ? 'results' : 'setup'}`) },
        h('td', null, h('strong', null, e.name)), h('td', null, e.client || '–'),
        h('td', { class: 'nowrap' }, e.period_start ? `${e.period_start} → ${e.period_end || '…'}` : '–'),
        h('td', null, `${e.n_docs} file${e.n_docs === 1 ? '' : 's'}`, e.n_ledger ? '' : h('span', { class: 'muted' }, ' · no ledger')),
        h('td', null, r ? [chip(r.status, r.status), ' ', r.summary ? `${r.summary.exceptions} exceptions` : '', r.locked ? ' · signed off' : ''] : h('span', { class: 'muted' }, 'not run yet')),
        h('td', null, e.created_by_name || ''));
    })))) : h('div', { class: 'card empty' }, h('h2', null, 'No engagements yet'),
      h('p', null, 'An engagement is one piece of vouching work: a ledger plus its supporting documents.'),
      h('button', { class: 'btn primary', onclick: newEngagementModal }, 'Create your first engagement'));
  shell('home', h('div', { class: 'row between', style: 'margin-bottom:16px' },
    h('div', null, h('h1', null, 'Engagements'), h('div', { class: 'muted' }, 'Choose an engagement or start a new one.')),
    h('button', { class: 'btn primary', onclick: newEngagementModal }, '+ New engagement')), body);
}

function newEngagementModal() {
  const f = {
    name: h('input', { placeholder: 'e.g. FY2026 Q1 purchases vouching' }), client: h('input', { placeholder: 'Client name' }),
    start: h('input', { type: 'date' }), end: h('input', { type: 'date' }),
    type: h('select', null, TYPES.map((t) => h('option', { value: t }, t.replace('_', ' ')))),
  };
  modal('New engagement', h('div', { class: 'grid2' },
    h('div', { style: 'grid-column:1/-1' }, h('label', null, 'Engagement name'), f.name), h('div', { style: 'grid-column:1/-1' }, h('label', null, 'Client'), f.client),
    h('div', null, h('label', null, 'Audit period start'), f.start), h('div', null, h('label', null, 'Audit period end'), f.end),
    h('div', { style: 'grid-column:1/-1' }, h('label', null, 'Default transaction type (used when the ledger has no "Type" column)'), f.type),
    h('p', { class: 'muted small', style: 'grid-column:1/-1;margin:0' }, 'The period is used for cut-off testing: entries inside the period supported by documents dated outside it are flagged.')),
    async (btn) => {
      const r = await api('/api/engagements', { method: 'POST', json: { name: f.name.value, client: f.client.value, period_start: f.start.value || null, period_end: f.end.value || null, default_type: f.type.value } });
      closeModal(); go(`#/e/${r.id}/setup`);
    }, 'Create');
}

/* ---------- modal ---------- */
function modal(title, content, onSave, saveLabel, extra) {
  closeModal();
  const box = h('div', { class: 'modal', role: 'dialog', 'aria-label': title },
    h('h2', null, title), content,
    h('div', { class: 'row', style: 'margin-top:16px;justify-content:flex-end' }, extra || null,
      h('button', { class: 'btn', onclick: closeModal }, 'Cancel'),
      onSave ? h('button', { class: 'btn primary', onclick: (e) => guard(e.currentTarget, () => onSave(e.currentTarget)) }, saveLabel || 'Save') : null));
  document.body.append(h('div', { class: 'backdrop', id: 'modal-bd', style: 'z-index:59', onclick: closeModal }), box);
  box.id = 'modal';
  const first = box.querySelector('input,select,textarea'); if (first) first.focus();
}
function closeModal() { ['modal', 'modal-bd'].forEach((id) => { const e = document.getElementById(id); if (e) e.remove(); }); }
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { if (document.getElementById('modal')) closeModal(); else closeDrawer(); }
  if (state.drawer && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) && !document.getElementById('modal')) {
    if (e.key === 'ArrowRight' || e.key === 'j') state.drawer.step(1);
    if (e.key === 'ArrowLeft' || e.key === 'k') state.drawer.step(-1);
  }
});

/* ---------- engagement page ---------- */
const TABS = [['setup', 'Files & run'], ['results', 'Results'], ['documents', 'Documents'], ['workpaper', 'Workpaper & sign-off'], ['activity', 'Activity']];

async function viewEngagement(eid, tab, rid, q) {
  const data = await api('/api/engagements/' + eid);
  const e = data.engagement, runs = data.runs;
  const run = rid ? runs.find((r) => r.id === rid) : (runs.find((r) => r.status === 'completed') || runs[0]);
  const ctx = { eid, data, tab, run, rid: run ? run.id : null, q, box: h('div') };
  ctx.reload = () => viewEngagement(eid, tab, ctx.rid, q);
  const runSel = runs.length ? h('select', { style: 'width:auto', title: 'Which run to look at', onchange: (ev) => go(`#/e/${eid}/${tab}/${ev.target.value}`) },
    runs.map((r) => h('option', { value: r.id, selected: run && r.id === run.id },
      `Run ${r.id} · ${when(r.started_at)} · ${r.status}${r.locked ? ' · signed off' : ''}`))) : null;
  shell('home',
    h('div', { class: 'row between top' },
      h('div', null, h('a', { href: '#/', class: 'muted small' }, '← All engagements'), h('h1', null, e.name),
        h('div', { class: 'muted' }, [e.client, e.period_start ? `Period ${e.period_start} → ${e.period_end || '…'}` : 'No audit period set (cut-off testing is off)', `Default type: ${e.default_type.replace('_', ' ')}`].filter(Boolean).join('  ·  '))),
      h('div', { class: 'row' }, runSel, h('button', { class: 'btn sm', onclick: () => editEngagementModal(ctx) }, 'Edit details'))),
    h('div', { class: 'tabs' }, TABS.map(([k, label]) => h('a', { href: `#/e/${eid}/${k}${ctx.rid ? '/' + ctx.rid : ''}`, class: k === tab ? 'active' : '' }, label))),
    ctx.box);
  const renderers = { setup: renderSetup, results: renderResults, documents: renderDocuments, workpaper: renderWorkpaper, activity: renderActivity };
  await (renderers[tab] || renderSetup)(ctx);
}

function editEngagementModal(ctx) {
  const e = ctx.data.engagement;
  const f = { name: h('input', { value: e.name }), client: h('input', { value: e.client }), start: h('input', { type: 'date', value: e.period_start || '' }),
    end: h('input', { type: 'date', value: e.period_end || '' }),
    type: h('input', { value: e.default_type, list: 'types' }) };
  modal('Engagement details', h('div', { class: 'grid2' }, h('datalist', { id: 'types' }, TYPES.map((t) => h('option', { value: t }))),
    h('div', { style: 'grid-column:1/-1' }, h('label', null, 'Name'), f.name), h('div', { style: 'grid-column:1/-1' }, h('label', null, 'Client'), f.client),
    h('div', null, h('label', null, 'Period start'), f.start), h('div', null, h('label', null, 'Period end'), f.end),
    h('div', { style: 'grid-column:1/-1' }, h('label', null, 'Default transaction type'), f.type)),
    async () => {
      await api('/api/engagements/' + ctx.eid, { method: 'PATCH', json: { name: f.name.value, client: f.client.value, period_start: f.start.value || null, period_end: f.end.value || null, default_type: f.type.value } });
      closeModal(); toast('Saved. Re-run to apply the new period.', 'ok'); ctx.reload();
    }, 'Save',
    h('button', { class: 'btn danger', style: 'margin-right:auto', onclick: async () => {
      if (!confirm('Archive this engagement? It will disappear from the list (data is kept).')) return;
      await api('/api/engagements/' + ctx.eid, { method: 'PATCH', json: { status: 'archived' } }); closeModal(); go('#/');
    } }, 'Archive'));
}

/* ---------- tab: files & run ---------- */
async function filesFromDrop(dt) {
  const entries = [...(dt.items || [])].map((i) => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null)).filter(Boolean);
  if (!entries.length) return [...dt.files];
  const out = [];
  async function walk(entry, path) {
    if (entry.isFile) {
      const f = await new Promise((res, rej) => entry.file(res, rej));
      Object.defineProperty(f, 'relpath', { value: path + f.name });
      out.push(f);
    } else {
      const reader = entry.createReader();
      let batch;
      do {
        batch = await new Promise((res, rej) => reader.readEntries(res, rej));
        for (const c of batch) await walk(c, path + entry.name + '/');
      } while (batch.length);
    }
  }
  for (const en of entries) await walk(en, '');
  return out;
}

async function uploadFiles(eid, kind, files, onProgress) {
  const batches = []; let cur = [], size = 0;
  for (const f of files) {
    if (cur.length >= 25 || (cur.length && size + f.size > 40e6)) { batches.push(cur); cur = []; size = 0; }
    cur.push(f); size += f.size;
  }
  if (cur.length) batches.push(cur);
  let done = 0; const skipped = [];
  for (const b of batches) {
    const fd = new FormData(); fd.append('kind', kind);
    for (const f of b) { fd.append('files', f, f.name); fd.append('paths', f.relpath || f.webkitRelativePath || f.name); }
    try {
      const res = await api(`/api/engagements/${eid}/files`, { method: 'POST', form: fd });
      skipped.push(...res.skipped);
    } catch (e) { skipped.push({ filename: b.map((f) => f.name).join(', '), reason: e.message }); }
    done += b.length; onProgress(done, files.length);
  }
  return skipped;
}

function renderSetup(ctx) {
  const { eid, data } = ctx, box = ctx.box;
  const ledger = data.files.filter((f) => f.kind === 'ledger'), docs = data.files.filter((f) => f.kind === 'document');
  const status = h('div', { class: 'muted small', style: 'margin-top:8px' });
  const afterUpload = (skipped) => {
    if (skipped.length) toast(`${skipped.length} file(s) not stored: ${skipped.slice(0, 3).map((s) => `${s.filename} (${s.reason})`).join('; ')}`, 'err');
    else toast('Upload complete', 'ok');
    ctx.reload();
  };
  const doUpload = async (kind, files) => {
    if (!files.length) return;
    status.textContent = `Uploading ${files.length} file(s)…`;
    const skipped = await uploadFiles(eid, kind, files, (d, t) => { status.textContent = `Uploaded ${d} of ${t}…`; });
    afterUpload(skipped);
  };

  const ledgerInput = h('input', { type: 'file', accept: '.xlsx,.xlsm,.csv,.tsv', onchange: (e) => doUpload('ledger', [...e.target.files]) });
  const fileInput = h('input', { type: 'file', multiple: true, style: 'display:none', onchange: (e) => doUpload('document', [...e.target.files]) });
  const folderInput = h('input', { type: 'file', webkitdirectory: true, directory: true, multiple: true, style: 'display:none', onchange: (e) => doUpload('document', [...e.target.files]) });
  const drop = h('div', { class: 'drop' }, h('div', null, h('strong', null, 'Drop documents or a whole folder here')),
    h('div', { class: 'small', style: 'margin:4px 0 10px' }, 'PDF, scans (PNG/JPG/TIFF), Excel, Word, CSV, text, email (.eml), or a .zip of them'),
    h('div', { class: 'row', style: 'justify-content:center' },
      h('button', { class: 'btn', onclick: () => fileInput.click() }, 'Choose files'),
      h('button', { class: 'btn', onclick: () => folderInput.click() }, 'Choose folder')), fileInput, folderInput);
  ['dragenter', 'dragover'].forEach((n) => drop.addEventListener(n, (e) => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((n) => drop.addEventListener(n, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', async (e) => { doUpload('document', await filesFromDrop(e.dataTransfer)); });

  const rm = (f) => h('button', { class: 'btn link', title: 'Remove', onclick: async () => { await api('/api/files/' + f.id, { method: 'DELETE' }); ctx.reload(); } }, 'remove');
  const filter = h('input', { placeholder: 'Filter documents…', style: 'max-width:260px', oninput: () => draw() });
  const tbody = h('tbody');
  const draw = () => {
    clear(tbody);
    const q = filter.value.toLowerCase();
    docs.filter((d) => d.filename.toLowerCase().includes(q)).slice(0, 300).forEach((d) =>
      tbody.append(h('tr', null, h('td', null, d.filename), h('td', { class: 'right nowrap' }, d.size < 1024 ? '<1 KB' : d.size < 1048576 ? Math.round(d.size / 1024) + ' KB' : (d.size / 1048576).toFixed(1) + ' MB'), h('td', { class: 'right' }, rm(d)))));
  };
  draw();

  box.append(
    h('div', { class: 'banner' }, h('strong', null, 'How it works: '), '1) upload the ledger entries you want vouched, 2) upload the supporting documents, 3) run. The agent reads every document, links it to a ledger entry and tests it. You then review only the exceptions.'),
    h('div', { class: 'card' }, h('h2', null, '1. Ledger (Excel or CSV)'),
      ledger.length ? h('div', { class: 'row' }, ledger.map((f) => h('span', null, h('strong', null, f.filename), h('span', { class: 'muted' }, ` · uploaded ${when(f.uploaded_at)} by ${f.uploaded_by || '?'} `), rm(f)))) : null,
      h('p', { class: 'muted small', style: 'margin:8px 0' }, 'Needs a header row with at least Date and Amount (or Debit/Credit). Optional columns: Type, Counterparty/Vendor/Customer, Reference/Invoice No, Currency, Approver, Description. Uploading a new ledger replaces the current one.'),
      ledgerInput),
    h('div', { class: 'card' }, h('h2', null, `2. Supporting documents ${docs.length ? '(' + docs.length + ')' : ''}`), drop, status,
      docs.length ? h('div', { style: 'margin-top:12px' }, h('div', { class: 'row between', style: 'margin-bottom:6px' }, filter, h('span', { class: 'muted small' }, docs.length > 300 ? 'showing first 300' : '')),
        h('div', { class: 'scroll', style: 'max-height:280px;border:1px solid var(--border);border-radius:8px' }, h('table', null, tbody))) : null),
    runCard(ctx, ledger.length > 0 && docs.length > 0),
    historyCard(ctx));
}

function runCard(ctx, ready) {
  const last = ctx.data.runs[0] ? ctx.data.runs[0].options : {};
  const foundry = state.system.foundry_configured;
  const pol = (last && last.policy) || {};
  const sel = (opts, cur) => opts.map(([v, label, needs]) => h('option', { value: v, selected: cur === v, disabled: needs && !foundry }, label + (needs && !foundry ? ' (not configured)' : '')));
  const f = {
    extractor: h('select', null, sel([['heuristic', 'Built-in reader (offline, fast)'], ['foundry', 'AI model reads documents (Foundry)', 1]], last.extractor || 'heuristic')),
    analyst: h('select', null, sel([['rule', 'Built-in explanations'], ['foundry', 'AI model explains exceptions (Foundry)', 1]], last.analyst || 'rule')),
    ocr: h('select', null, sel([['none', 'No OCR (scans are flagged as unread)'], ['foundry', 'AI model reads scans/images (Foundry)', 1]], last.ocr || 'none')),
    dayfirst: h('select', null, h('option', { value: '1', selected: last.dayfirst !== false }, 'Day first (31/12/2026)'), h('option', { value: '0', selected: last.dayfirst === false }, 'Month first (12/31/2026)')),
    materiality: h('input', { type: 'number', min: 0, step: 'any', value: pol.materiality || '', placeholder: 'default 1,000' }),
    approval_limit: h('input', { type: 'number', min: 0, step: 'any', value: pol.approval_limit || '', placeholder: 'default 10,000' }),
    abs_tolerance: h('input', { type: 'number', min: 0, step: 'any', value: pol.abs_tolerance || '', placeholder: 'default 0.50' }),
    auto_clear_limit: h('input', { type: 'number', min: 0, step: 'any', value: pol.auto_clear_limit || '', placeholder: 'default 5,000' }),
    programmes: h('textarea', { placeholder: '{"lease": [{"test": "amount_agrees"}, {"test": "date_ok", "max_days_after": 10}]}', style: 'font-family:var(--mono);font-size:12px', value: last.programmes_json || '' }),
  };
  const showDefaults = async () => {
    const p = await api('/api/programmes');
    modal('Built-in vouching programmes', h('div', null, h('p', { class: 'muted small' }, 'Tests available: ' + p.tests.join(', ') + '. Paste a JSON object like the one shown in the box to add or replace a programme for a transaction type.'),
      h('pre', { class: 'mono', style: 'max-height:380px;overflow:auto;background:var(--hover);padding:10px;border-radius:8px' }, JSON.stringify(p.programmes, null, 1))));
  };
  const start = async (btn) => {
    const policy = {};
    ['materiality', 'approval_limit', 'abs_tolerance', 'auto_clear_limit'].forEach((k) => { if (f[k].value !== '') policy[k] = f[k].value; });
    const r = await api(`/api/engagements/${ctx.eid}/runs`, { method: 'POST', json: {
      extractor: f.extractor.value, analyst: f.analyst.value, ocr: f.ocr.value, dayfirst: f.dayfirst.value === '1', policy, programmes_json: f.programmes.value } });
    go(`#/e/${ctx.eid}/results/${r.id}`);
  };
  const running = ctx.data.runs.some((r) => ['queued', 'running'].includes(r.status));
  return h('div', { class: 'card' }, h('h2', null, '3. Run the vouching'),
    ctx.data.overrides && (Object.keys(ctx.data.overrides.vouchers).length || Object.keys(ctx.data.overrides.links).length)
      ? h('div', { class: 'banner ok' }, `Reviewer corrections will be applied: ${Object.keys(ctx.data.overrides.vouchers).length} extraction fix(es), ${Object.keys(ctx.data.overrides.links).length} manual link(s).`) : null,
    h('div', { class: 'grid2' },
      h('div', null, h('label', null, 'How documents are read'), f.extractor), h('div', null, h('label', null, 'How exceptions are explained'), f.analyst),
      h('div', null, h('label', null, 'Scans and images'), f.ocr), h('div', null, h('label', null, 'Numeric dates in documents'), f.dayfirst)),
    !foundry ? h('p', { class: 'muted small' }, 'AI options are greyed out because Microsoft Foundry is not configured on this server. See Settings.') : null,
    h('details', { style: 'margin-top:12px' }, h('summary', null, 'Thresholds and programmes (optional)'),
      h('div', { class: 'grid2' },
        h('div', null, h('label', null, 'Materiality: amounts at/above this are high severity'), f.materiality),
        h('div', null, h('label', null, 'Approval limit: approver required at/above'), f.approval_limit),
        h('div', null, h('label', null, 'Amount tolerance (absolute)'), f.abs_tolerance),
        h('div', null, h('label', null, 'Clean entries above this are still sampled for review'), f.auto_clear_limit)),
      h('div', { style: 'margin-top:12px' }, h('label', null, 'Custom vouching programmes (JSON, optional) ', h('button', { class: 'btn link', onclick: showDefaults }, 'show built-in programmes')), f.programmes)),
    h('div', { class: 'row', style: 'margin-top:16px' },
      h('button', { class: 'btn primary', disabled: !ready || running, onclick: (e) => guard(e.currentTarget, () => start(e.currentTarget)) }, running ? 'A run is in progress…' : 'Run vouching'),
      !ready ? h('span', { class: 'muted' }, 'Upload the ledger and at least one document first.') : null));
}

function historyCard(ctx) {
  const runs = ctx.data.runs;
  if (!runs.length) return null;
  return h('div', { class: 'card flush' }, h('div', { style: 'padding:14px 20px 0' }, h('h2', null, 'Run history')),
    h('table', null, h('thead', null, h('tr', null, ['Run', 'Started', 'By', 'Status', 'Exceptions', 'Sign-off', ''].map((x) => h('th', null, x)))),
      h('tbody', null, runs.map((r) => h('tr', null, h('td', null, r.id), h('td', null, when(r.started_at)), h('td', null, r.started_by_name || ''),
        h('td', null, chip(r.status, r.status), r.error ? h('div', { class: 'small', style: 'color:var(--danger)' }, r.error) : null),
        h('td', null, r.summary ? r.summary.exceptions : ''), h('td', null, r.locked ? `${r.signoff_by_name} · ${when(r.signoff_at)}` : '–'),
        h('td', { class: 'right' }, h('a', { href: `#/e/${ctx.eid}/results/${r.id}` }, 'Open →')))))));
}

/* ---------- tab: results ---------- */
const VIEW_DEFAULT = { status: 'needs_decision', disposition: '', severity: '', code: '', q: '', offset: 0 };
const view = { ...VIEW_DEFAULT, runKey: null };
const PAGE = 50;

async function renderResults(ctx) {
  const box = ctx.box, run0 = ctx.run;
  if (!run0) return box.append(h('div', { class: 'card empty' }, h('h2', null, 'No runs yet'), h('p', null, 'Upload files and start a run on the first tab.'), h('a', { class: 'btn primary', href: `#/e/${ctx.eid}/setup` }, 'Go to Files & run')));
  const run = await api('/api/runs/' + run0.id);
  ctx.run = run;
  if (run.status === 'queued' || run.status === 'running') {
    box.append(h('div', { class: 'card' }, h('h2', null, h('span', { class: 'spin' }), ' Run ' + run.id + ' is ' + run.status + '…'),
      h('div', { class: 'bar' }, h('i')), h('p', { class: 'muted' }, run.progress || 'Waiting to start'),
      h('p', { class: 'muted small' }, 'You can leave this page; the run continues on the server. Large sets of scanned documents or AI reading can take several minutes.')));
    every(async () => { const r = await api('/api/runs/' + run.id).catch(() => null); if (r && !['queued', 'running'].includes(r.status)) ctx.reload(); else if (r) { const p = box.querySelector('.muted'); if (p) p.textContent = r.progress || ''; } }, 1500);
    return;
  }
  if (run.status === 'failed') {
    return box.append(h('div', { class: 'banner err' }, h('strong', null, 'This run failed. '), run.error || ''), h('a', { class: 'btn', href: `#/e/${ctx.eid}/setup` }, 'Back to Files & run'));
  }
  if (view.runKey !== run.id) Object.assign(view, VIEW_DEFAULT, { runKey: run.id });  // fresh filters for a different run
  const s = run.summary, d = s.dispositions;
  const tiles = h('div', { class: 'tiles' },
    tile(s.population, 'Ledger entries', ''), tile(money(s.population_value), 'Total value', ''),
    tile(d.escalate, 'Escalated', d.escalate ? 'alert' : 'good'), tile(d.review, 'For review', d.review ? 'warn' : 'good'),
    tile(d.auto_cleared, 'Cleared by tests', 'good'), tile(money(s.exposure), 'Amount at risk', s.exposure ? 'alert' : 'good'),
    tile(run.outstanding, 'Awaiting your decision', run.outstanding ? 'warn' : 'good'));
  const notes = [];
  if (run.locked) notes.push(h('div', { class: 'banner ok' }, `🔒 Signed off by ${run.signoff_by_name} on ${when(run.signoff_at)}. This run is locked; start a new run to make changes.`));
  if (s.intake_issues) notes.push(h('div', { class: 'banner warn' }, h('strong', null, `${s.intake_issues} document problem(s). `), 'Some files could not be fully read (scans without OCR, corrupt or unsupported files). ', h('a', { href: `#/e/${ctx.eid}/documents/${run.id}` }, 'See details')));
  if (s.unlinked_documents) notes.push(h('div', { class: 'banner warn' }, h('strong', null, `${s.unlinked_documents} document(s) match no ledger entry `), '(possible unrecorded items). ', h('a', { href: `#/e/${ctx.eid}/documents/${run.id}` }, 'See which')));
  box.append(h('div', { class: 'muted small', style: 'margin-bottom:10px' }, `Run ${run.id} · ${s.seconds}s · reader: ${s.extractor} · explanations: ${s.analyst} · OCR: ${s.ocr}`), tiles, ...notes);

  const listBox = h('div', { class: 'card flush' });
  const mkSel = (key, opts) => h('select', { style: 'width:auto', onchange: (e) => { view[key] = e.target.value; view.offset = 0; load(); } },
    opts.map(([v, l]) => h('option', { value: v, selected: view[key] === v }, l)));
  const search = h('input', { placeholder: 'Search counterparty, reference, document…', value: view.q, style: 'max-width:300px', oninput: debounce((e) => { view.q = e.target.value; view.offset = 0; load(); }, 300) });
  box.append(h('div', { class: 'row', style: 'margin-bottom:10px' },
    mkSel('status', [['needs_decision', 'Needs a decision'], ['', 'All items'], ['pending', 'Not yet reviewed'], ['resolved', 'Resolved'], ['confirmed', 'Confirmed exceptions'], ['followup', 'Follow-up']]),
    mkSel('disposition', [['', 'Any outcome'], ['escalate', 'Escalated'], ['review', 'Review'], ['auto_cleared', 'Cleared']]),
    mkSel('severity', [['', 'Any severity'], ['high', 'High'], ['medium', 'Medium'], ['low', 'Low']]),
    mkSel('code', [['', 'Any finding'], ...Object.entries(CODE_LABEL).map(([k, l]) => [k, l])]), search), listBox);

  let last = { items: [], total: 0 };
  async function load() {
    const params = new URLSearchParams({ limit: PAGE, offset: view.offset });
    ['status', 'disposition', 'severity', 'code', 'q'].forEach((k) => view[k] && params.set(k, view[k]));
    last = await api(`/api/runs/${run.id}/items?${params}`);
    clear(listBox);
    if (!last.items.length) return listBox.append(h('div', { class: 'empty' }, view.status === 'needs_decision' ? '🎉 Nothing is waiting for a decision.' : 'No items match these filters.'));
    listBox.append(h('div', { class: 'scroll', style: 'max-height:none' }, h('table', null,
      h('thead', null, h('tr', null, ['Status', 'Item', 'Date', 'Counterparty / reference', 'Amount', 'Findings', 'Document'].map((x, i) => h('th', { class: i === 4 ? 'right' : '' }, x)))),
      h('tbody', null, last.items.map((it, idx) => h('tr', { class: 'click', onclick: () => openDrawer(ctx, last.items.map((x) => x.txn_id), idx, refreshAll) },
        h('td', null, it.disposition === 'auto_cleared' ? chip('Cleared', 'auto_cleared') : [chip(STATUS_LABEL[it.status], it.status)]),
        h('td', null, h('strong', null, short(it.txn_id)), h('div', { class: 'small muted' }, it.type.replace('_', ' '))),
        h('td', { class: 'nowrap' }, it.date),
        h('td', null, it.counterparty || '–', h('div', { class: 'small muted' }, it.reference)),
        h('td', { class: 'right nowrap' }, money(it.amount, it.currency)),
        h('td', null, it.codes.length ? it.codes.map((c) => [chip(CODE_LABEL[c] || c, it.severity), ' ']) : h('span', { class: 'muted' }, it.disposition === 'review' ? 'Sampled (no exception)' : '✓ no exceptions')),
        h('td', { class: 'small' }, it.document || h('span', { style: 'color:var(--danger)' }, 'none'))))))));
    listBox.append(h('div', { class: 'pager' }, h('span', { class: 'muted' }, `${view.offset + 1}–${Math.min(view.offset + PAGE, last.total)} of ${last.total}`),
      h('button', { class: 'btn sm', disabled: view.offset === 0, onclick: () => { view.offset = Math.max(0, view.offset - PAGE); load(); } }, '‹ Prev'),
      h('button', { class: 'btn sm', disabled: view.offset + PAGE >= last.total, onclick: () => { view.offset += PAGE; load(); } }, 'Next ›')));
  }
  const refreshAll = () => { ctx.reload(); };
  await load();
  if (ctx.q && ctx.q.get('item')) {
    const item = ctx.q.get('item'); ctx.q = null;
    history.replaceState(null, '', location.hash.split('?')[0]);
    openDrawer(ctx, [item], 0, refreshAll);
  }
}
const tile = (n, label, cls) => h('div', { class: 'tile ' + cls }, h('div', { class: 'n' }, n), h('div', { class: 'l' }, label));
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

/* ---------- item drawer ---------- */
function closeDrawer() { ['drawer', 'drawer-bd'].forEach((id) => { const e = document.getElementById(id); if (e) e.remove(); }); state.drawer = null; }

const FIELD_CODES = {
  amount: ['AMOUNT_MISMATCH'], counterparty: ['COUNTERPARTY_MISMATCH'], reference: ['REFERENCE_MISMATCH'],
  currency: ['CURRENCY_MISMATCH'], date: ['CUTOFF_ISSUE', 'DATE_MISMATCH'], type: ['WRONG_DOC_TYPE'], approver: ['MISSING_APPROVAL'],
};

async function openDrawer(ctx, ids, idx, onChange) {
  closeDrawer();
  const bd = h('div', { class: 'backdrop', id: 'drawer-bd', onclick: closeDrawer });
  const dr = h('aside', { class: 'drawer', id: 'drawer', role: 'dialog', 'aria-label': 'Item detail' });
  document.body.append(bd, dr);
  const st = state.drawer = { idx, step(n) { const j = st.idx + n; if (j >= 0 && j < ids.length) { st.idx = j; paint(); } } };
  const run = ctx.run;
  const canDecide = state.user.role !== 'preparer' && !run.locked;

  async function paint() {
    clear(dr).append(h('div', { class: 'empty' }, h('span', { class: 'spin' }), ' Loading…'));
    try { render(await api(`/api/runs/${run.id}/item?id=${enc(ids[st.idx])}`)); } catch (e) { fail(e); closeDrawer(); }
  }

  function render(data) {
    const t = data.transaction, v = data.voucher, codes = new Set(data.findings.map((f) => f.code));
    const bad = (field) => (FIELD_CODES[field] || []).some((c) => codes.has(c));
    const cell = (field, text) => h('td', { class: v ? (bad(field) ? 'bad' : (text && text !== '–' ? 'good' : '')) : '' }, text || '–');
    const cmp = h('table', { class: 'cmp' }, h('thead', null, h('tr', null, h('th', null, ''), h('th', null, 'Ledger says'), h('th', null, v ? 'Document says' : 'Document'))),
      h('tbody', null,
        h('tr', null, h('td', null, 'Type'), h('td', null, t.type.replace('_', ' ')), cell('type', v ? v.doc_type.replace('_', ' ') : '–')),
        h('tr', null, h('td', null, 'Date'), h('td', null, t.date), cell('date', v && v.date)),
        h('tr', null, h('td', null, 'Amount'), h('td', null, money(t.amount, t.currency)), cell('amount', v && v.amount != null ? money(v.amount, v.currency) + (v.tax != null ? `  (tax ${money(v.tax)})` : '') : '–')),
        h('tr', null, h('td', null, 'Counterparty'), h('td', null, t.counterparty || '–'), cell('counterparty', v && v.parties.join('; '))),
        h('tr', null, h('td', null, 'Reference'), h('td', null, t.reference || '–'), cell('reference', v && v.references.join('; '))),
        h('tr', null, h('td', null, 'Currency'), h('td', null, t.currency || '–'), cell('currency', v && v.currency)),
        h('tr', null, h('td', null, 'Approval'), h('td', null, t.approver || '–'), cell('approver', v && v.approvals.join('; '))),
        t.description ? h('tr', null, h('td', null, 'Description'), h('td', { colspan: 2 }, t.description)) : null));

    const link = data.link;
    const linkLine = link.marked_no_document ? h('span', null, 'A reviewer marked this entry as having ', h('strong', null, 'no supporting document'), '.')
      : v ? h('span', null, link.manual ? 'Linked manually by a reviewer.' : `Matched to the document by: ${link.reasons.join(', ')} (score ${link.score}).`,
        v.method === 'reviewer-corrected' ? ' Extracted values were corrected by a reviewer.' : '', v.method === 'ocr' ? ' Text was read by OCR, so check the figures.' : '')
      : h('span', null, 'No document cleared the match threshold.', data.closest_file ? [' Closest by content: ', h('strong', null, data.closest_file), ` (score ${link.score}).`] : '');

    const a = data.analysis;
    const left = h('div', null,
      h('div', { class: 'card' }, h('h3', null, 'Findings'),
        data.findings.length ? data.findings.map((f) => h('div', { class: 'finding ' + f.severity },
          h('div', null, chip(f.severity, f.severity), ' ', h('strong', null, CODE_LABEL[f.code] || f.code), Number(f.amount_at_risk) ? h('span', { class: 'muted' }, `  ·  at risk ${money(f.amount_at_risk)}`) : null),
          h('div', null, f.message)))
          : h('div', null, data.disposition === 'review' ? 'No exceptions. This large item was selected for a sample check: compare the document with the ledger below.' : 'All tests passed for this entry.')),
      h('div', { class: 'card' }, h('h3', null, 'Ledger vs document'), cmp, h('p', { class: 'small muted', style: 'margin:10px 0 0' }, linkLine)),
      a ? h('div', { class: 'card' }, h('h3', null, 'Assessment'),
        h('div', { class: 'row' }, chip(a.classification.replace(/_/g, ' '), 'plain'), h('span', { class: 'muted small' }, `confidence ${Number(a.confidence).toFixed(2)} · ${a.analyst}`)),
        a.analyst.startsWith('rule') ? null : h('p', null, a.explanation),
        h('p', null, h('strong', null, 'Suggested action: '), a.suggested_action),
        data.problems.length ? h('div', { class: 'banner err' }, h('strong', null, 'Automatic check flagged this assessment: '), h('ul', { class: 'plain' }, data.problems.map((p) => h('li', null, p)))) : null) : null,
      h('div', { class: 'card' }, h('h3', null, 'Fix the input'),
        h('p', { class: 'small muted', style: 'margin-top:0' }, 'If the agent misread a document or linked the wrong one, correct it here and re-run. Your other decisions are kept for items that did not change.'),
        h('div', { class: 'row' },
          v ? h('button', { class: 'btn sm', onclick: () => voucherEditor(ctx, v, data.source_file && data.source_file.filename) }, 'Correct what was read from the document') : null,
          h('button', { class: 'btn sm', onclick: () => linkPicker(ctx, data) }, v ? 'Link a different document' : 'Link a document manually'))));

    const sf = data.source_file;
    const right = h('div', null,
      h('div', { class: 'card' }, h('div', { class: 'row between' }, h('h3', { style: 'margin:0' }, sf ? sf.filename : 'Supporting document'),
        sf ? h('div', { class: 'row' }, h('a', { class: 'btn sm', target: '_blank', rel: 'noopener', href: docUrl(run.id, sf.filename) }, 'Open'),
          h('a', { class: 'btn sm', href: docUrl(run.id, sf.filename) + '&download=1' }, 'Download')) : null),
        h('div', { class: 'preview', style: 'margin-top:10px' }, sf ? previewNode(run.id, sf) : h('div', { class: 'empty' }, 'No document to show.'))),
      decisionCard(data));

    clear(dr).append(
      h('header', null,
        h('div', null, h('div', { style: 'font-weight:700;font-size:16px' }, `${short(data.txn_id)} · ${t.counterparty || 'no counterparty'} · ${money(t.amount, t.currency)}`),
          h('div', { class: 'small muted' }, `${t.date} · ${t.type.replace('_', ' ')} · ${t.reference || 'no reference'}`)),
        chip(DISP_LABEL[data.disposition], data.disposition), chip(STATUS_LABEL[data.status], data.status), h('div', { class: 'spacer' }),
        ids.length > 1 ? [h('button', { class: 'btn sm', disabled: st.idx === 0, onclick: () => st.step(-1), title: 'Previous (←)' }, '‹'),
          h('span', { class: 'muted small' }, `${st.idx + 1}/${ids.length}`), h('button', { class: 'btn sm', disabled: st.idx === ids.length - 1, onclick: () => st.step(1), title: 'Next (→)' }, '›')] : null,
        h('button', { class: 'btn sm', onclick: closeDrawer }, 'Close ✕')),
      h('div', { class: 'body' }, left, right));
  }

  function decisionCard(data) {
    const note = h('textarea', { placeholder: canDecide ? 'Why? e.g. "Agreed to signed contract, price increase approved by CFO on 12 Feb."' : 'Add a comment…' });
    const send = (action) => async (ev) => {
      await guard(ev.currentTarget, async () => {
        const updated = await api(`/api/runs/${run.id}/decision`, { method: 'POST', json: { txn_id: data.txn_id, action, note: note.value } });
        toast('Saved', 'ok'); render(updated); onChange && onChange();
      });
    };
    return h('div', { class: 'card' }, h('h3', null, 'Your decision'),
      run.locked ? h('div', { class: 'banner ok' }, 'This run is signed off and locked.') : null,
      !canDecide && !run.locked ? h('div', { class: 'banner' }, 'Only a reviewer can resolve or confirm items. You can add comments.') : null,
      data.decisions.length ? h('div', { class: 'hist' }, data.decisions.map((d) => h('div', null,
        h('div', null, chip(d.action === 'comment' ? 'Comment' : (STATUS_LABEL[d.action] || d.action), d.action === 'comment' || d.action === 'reopen' ? 'plain' : d.action), ' ', h('strong', null, d.user || ''), h('span', { class: 'muted small' }, ' · ' + when(d.created_at))),
        d.note ? h('div', null, d.note) : null))) : h('p', { class: 'muted' }, 'No decisions yet.'),
      run.locked ? null : [h('div', { style: 'margin-top:10px' }, note),
        h('div', { class: 'row', style: 'margin-top:10px' },
          h('button', { class: 'btn ok', disabled: !canDecide, onclick: send('resolved'), title: 'The exception is explained or acceptable' }, '✓ Resolved'),
          h('button', { class: 'btn danger', disabled: !canDecide, onclick: send('confirmed'), title: 'This is a real exception to report' }, '⚑ Confirm exception'),
          h('button', { class: 'btn', disabled: !canDecide, onclick: send('followup'), title: 'Need more information' }, '? Needs follow-up'),
          h('button', { class: 'btn', onclick: send('comment') }, 'Comment'),
          data.status !== 'pending' ? h('button', { class: 'btn link', disabled: !canDecide, onclick: send('reopen') }, 'Reopen') : null),
        h('p', { class: 'small muted' }, h('span', { class: 'kbd' }, '←'), ' ', h('span', { class: 'kbd' }, '→'), ' move between items')]);
  }
  paint();
}

const docUrl = (rid, name) => `/api/runs/${rid}/document?name=${enc(name)}`;
function previewNode(rid, sf) {
  const ext = sf.filename.split('.').pop().toLowerCase();
  if (ext === 'pdf') return h('iframe', { src: docUrl(rid, sf.filename), title: sf.filename });
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'].includes(ext)) return h('img', { src: docUrl(rid, sf.filename), alt: sf.filename, onerror: (e) => e.target.replaceWith(h('div', { class: 'empty' }, 'This image format cannot be shown in the browser. Use Download.')) });
  const pre = h('pre', null, 'Loading…');
  api(`/api/runs/${rid}/document-text?name=${enc(sf.filename)}`).then((d) => { pre.textContent = d.text || d.note || '(no text)'; }).catch((e) => { pre.textContent = e.message; });
  return pre;
}

function voucherEditor(ctx, v, filename) {
  const o = v.original || v;
  const f = {
    type: h('select', null, DOC_TYPES.map((t) => h('option', { value: t, selected: t === v.doc_type }, t.replace('_', ' ')))),
    date: h('input', { type: 'date', value: v.date || '' }), amount: h('input', { type: 'number', step: '0.01', value: v.amount ?? '' }),
    tax: h('input', { type: 'number', step: '0.01', value: v.tax ?? '' }), currency: h('input', { value: v.currency || '', maxlength: 3, placeholder: 'USD' }),
    parties: h('textarea', { value: v.parties.join('\n') }), refs: h('textarea', { value: v.references.join('\n') }), appr: h('textarea', { value: v.approvals.join('\n') }),
  };
  const lines = (el) => el.value.split('\n').map((x) => x.trim()).filter(Boolean);
  const origText = `Originally read: ${o.doc_type}, ${o.date || 'no date'}, total ${o.amount ?? 'none'}, ${o.parties.join('; ') || 'no parties'}`;
  modal('Correct what was read from ' + (filename || v.source_doc), h('div', null,
    h('p', { class: 'muted small', style: 'margin-top:0' }, origText + '. Corrections are applied the next time you run. They are recorded with your name.'),
    h('div', { class: 'grid2' },
      h('div', null, h('label', null, 'Document type'), f.type), h('div', null, h('label', null, 'Document date'), f.date),
      h('div', null, h('label', null, 'Total amount'), f.amount), h('div', null, h('label', null, 'Tax'), f.tax),
      h('div', null, h('label', null, 'Currency (3 letters)'), f.currency), h('div'),
      h('div', null, h('label', null, 'Parties named (one per line)'), f.parties),
      h('div', null, h('label', null, 'Document numbers (one per line)'), f.refs),
      h('div', null, h('label', null, 'Approved by (one per line)'), f.appr))),
    async () => {
      await api(`/api/engagements/${ctx.eid}/voucher-override`, { method: 'PUT', json: { voucher_id: v.id, note: 'corrected in review', fields: {
        doc_type: f.type.value, date: f.date.value || null, amount: f.amount.value, tax: f.tax.value, currency: f.currency.value,
        parties: lines(f.parties), references: lines(f.refs), approvals: lines(f.appr) } } });
      closeModal(); toast('Correction saved', 'ok'); rerunBar(ctx);
    }, 'Save correction',
    v.corrected ? h('button', { class: 'btn danger', style: 'margin-right:auto', onclick: async () => {
      await api(`/api/engagements/${ctx.eid}/voucher-override?voucher_id=${enc(v.id)}`, { method: 'DELETE' }); closeModal(); toast('Correction removed', 'ok'); rerunBar(ctx);
    } }, 'Remove my correction') : null);
}

async function linkPicker(ctx, data) {
  const docs = await api(`/api/runs/${ctx.run.id}/documents`);
  const opts = [];
  docs.documents.forEach((d) => d.vouchers.forEach((v) => opts.push([v.id, `${d.filename}  ·  ${v.doc_type.replace('_', ' ')}  ·  ${v.amount != null ? money(v.amount) : 'no total'}  ·  ${v.parties[0] || ''}`])));
  const sel = h('select', null, h('option', { value: '' }, '— choose a document —'), h('option', { value: '__none__' }, 'There is no supporting document for this entry'),
    opts.map(([id, label]) => h('option', { value: id, selected: data.link.voucher_id === id }, label)));
  const note = h('input', { placeholder: 'Reason (optional)' });
  modal('Link ' + short(data.txn_id) + ' to a document', h('div', null,
    h('p', { class: 'muted small', style: 'margin-top:0' }, 'Use this when the agent linked the wrong document or missed the right one. It is applied the next time you run, and recorded with your name.'),
    h('label', null, 'Document'), sel, h('div', { style: 'margin-top:10px' }, h('label', null, 'Reason'), note),
    data.link.manual || data.link.marked_no_document ? h('button', { class: 'btn link', onclick: async () => {
      await api(`/api/engagements/${ctx.eid}/link-override?txn_id=${enc(data.txn_id)}`, { method: 'DELETE' }); closeModal(); toast('Manual link removed', 'ok'); rerunBar(ctx);
    } }, 'Remove manual link, let the agent choose') : null),
    async () => {
      if (!sel.value) throw new Error('Choose a document first.');
      await api(`/api/engagements/${ctx.eid}/link-override`, { method: 'PUT', json: { txn_id: data.txn_id, voucher_id: sel.value === '__none__' ? null : sel.value, note: note.value } });
      closeModal(); toast('Link saved', 'ok'); rerunBar(ctx);
    }, 'Save link');
}

function rerunBar(ctx) {
  const old = document.getElementById('rerun'); if (old) old.remove();
  const last = ctx.data.runs[0];
  if (!last) return;
  document.body.append(h('div', { id: 'rerun', class: 'card', style: 'position:fixed;left:50%;bottom:20px;transform:translateX(-50%);z-index:70;display:flex;gap:12px;align-items:center;margin:0' },
    h('span', null, 'Correction saved. ', h('strong', null, 'Re-run to apply it.'), ' Your other decisions are kept.'),
    h('button', { class: 'btn primary sm', onclick: (e) => guard(e.currentTarget, async () => {
      const r = await api(`/api/engagements/${ctx.eid}/runs`, { method: 'POST', json: last.options });
      document.getElementById('rerun')?.remove(); closeDrawer(); go(`#/e/${ctx.eid}/results/${r.id}`);
    }) }, 'Re-run now'),
    h('button', { class: 'btn sm', onclick: () => document.getElementById('rerun')?.remove() }, 'Later')));
}

/* ---------- tab: documents ---------- */
async function renderDocuments(ctx) {
  const box = ctx.box, run = ctx.run;
  if (!run || run.status !== 'completed') return box.append(h('div', { class: 'card empty' }, 'Run the vouching first to see what was read from each document.'));
  const d = await api(`/api/runs/${run.id}/documents`);
  const problemsOnly = h('input', { type: 'checkbox', onchange: () => draw() });
  const search = h('input', { placeholder: 'Filter by file, party or number…', style: 'max-width:280px', oninput: debounce(() => draw(), 200) });
  const tbody = h('tbody');
  const rows = [];
  d.documents.forEach((doc) => {
    if (!doc.vouchers.length) rows.push({ doc, v: null });
    doc.vouchers.forEach((v) => rows.push({ doc, v }));
  });
  function draw() {
    clear(tbody);
    const q = search.value.toLowerCase();
    rows.filter(({ doc, v }) => (!problemsOnly.checked || doc.issues.length || !v || v.confidence < 0.7 || !v.linked_txns.length)
      && (!q || (doc.filename + ' ' + (v ? v.parties.join(' ') + v.references.join(' ') : '')).toLowerCase().includes(q)))
      .forEach(({ doc, v }) => tbody.append(h('tr', null,
        h('td', null, h('a', { target: '_blank', rel: 'noopener', href: docUrl(run.id, doc.filename) }, doc.filename)),
        h('td', { class: 'small' }, v ? (v.method === 'text' ? doc.kind : v.method) : doc.method),
        h('td', null, v ? v.doc_type.replace('_', ' ') : '–'), h('td', { class: 'nowrap' }, v && v.date || '–'),
        h('td', { class: 'right nowrap' }, v && v.amount != null ? money(v.amount, v.currency) : '–'),
        h('td', { class: 'small' }, v ? v.parties.join('; ') : ''), h('td', { class: 'small' }, v ? v.references.join('; ') : ''),
        h('td', null, v ? [chip(v.confidence.toFixed(2), v.confidence >= 0.7 ? 'auto_cleared' : 'medium'), v.corrected ? [' ', chip('corrected', 'plain')] : null] : '–'),
        h('td', null, v ? (v.linked_txns.length ? v.linked_txns.map((t) => [h('a', { href: `#/e/${ctx.eid}/results/${run.id}?item=${enc(t)}` }, short(t)), ' ']) : h('span', { style: 'color:var(--warn)' }, 'not linked')) : ''),
        h('td', { class: 'small', style: doc.issues.length ? 'color:var(--danger)' : '' }, doc.issues.join(' · ') || (v && v.notes.length ? v.notes.join(' · ') : '')),
        h('td', null, v ? h('button', { class: 'btn sm', onclick: () => voucherEditor(ctx, v, doc.filename) }, 'Correct') : ''))));
  }
  box.append(
    d.issues.length ? h('div', { class: 'banner warn' }, h('strong', null, 'Other problems: '), h('ul', { class: 'plain' }, d.issues.map((i) => h('li', null, i)))) : null,
    d.unlinked_documents.length ? h('div', { class: 'banner warn' }, h('strong', null, 'Documents that match no ledger entry (possible unrecorded items): '), d.unlinked_documents.join(', ')) : null,
    h('div', { class: 'row', style: 'margin-bottom:10px' }, search, h('label', { style: 'display:flex;gap:6px;align-items:center;margin:0' }, problemsOnly, 'Only show problems'),
      h('span', { class: 'muted small' }, `${d.documents.length} files · ${rows.filter((r) => r.v).length} documents extracted`)),
    h('div', { class: 'card flush' }, h('div', { class: 'scroll', style: 'max-height:none' }, h('table', null,
      h('thead', null, h('tr', null, ['File', 'Read by', 'Type', 'Date', 'Total', 'Parties', 'Numbers', 'Confidence', 'Linked to', 'Problems', ''].map((x) => h('th', null, x)))), tbody))));
  draw();
}

/* ---------- tab: workpaper & sign-off ---------- */
async function renderWorkpaper(ctx) {
  const box = ctx.box;
  let run = ctx.run;
  if (!run || run.status !== 'completed') return box.append(h('div', { class: 'card empty' }, 'Run the vouching first.'));
  run = await api('/api/runs/' + run.id); ctx.run = run;
  const s = run.summary, rid = run.id;
  const dl = (fmt, label, ext) => h('a', { class: 'btn', href: `/api/runs/${rid}/export/${fmt}`, download: `vouching_run_${rid}.${ext}` }, label);
  const needAck = s.intake_issues || s.unlinked_documents;
  const ack = h('input', { type: 'checkbox' }), note = h('textarea', { placeholder: 'Sign-off note (optional)' });
  const mine = run.started_by === state.user.id && !state.system.solo;
  const canSign = ['admin', 'reviewer'].includes(state.user.role) && !mine && !run.locked && run.outstanding === 0;
  box.append(
    h('div', { class: 'card' }, h('h2', null, 'Download the workpaper'),
      h('div', { class: 'row' }, dl('docx', 'Word (.docx)', 'docx'), dl('xlsx', 'Excel (.xlsx)', 'xlsx'), dl('md', 'Markdown', 'md'), dl('audit', 'Audit log (JSON lines)', 'jsonl'), dl('report', 'Full report (JSON)', 'json')),
      h('p', { class: 'muted small', style: 'margin-bottom:0' }, 'Exports include reviewer decisions, corrections and sign-off. The audit log is hash-chained: any later edit to it can be detected.')),
    h('div', { class: 'card' }, h('h2', null, 'Sign-off'),
      run.locked ? h('div', { class: 'banner ok' }, `Signed off by ${run.signoff_by_name} on ${when(run.signoff_at)}.`, run.signoff_note ? h('div', null, '“' + run.signoff_note + '”') : null)
        : [h('p', null, run.outstanding ? h('span', null, h('strong', null, run.outstanding), ' item(s) still need a decision (resolved or confirmed) before this can be signed off. ', h('a', { href: `#/e/${ctx.eid}/results/${rid}` }, 'Go to results'))
          : 'All items have a decision.'),
        mine ? h('div', { class: 'banner' }, 'You prepared this run, so a different reviewer must sign it off (segregation of duties).') : null,
        !['admin', 'reviewer'].includes(state.user.role) ? h('div', { class: 'banner' }, 'Only a reviewer can sign off.') : null,
        needAck ? h('label', { style: 'display:flex;gap:8px;align-items:center;font-size:14px;color:var(--text)' }, ack, `I have considered the ${s.intake_issues} unreadable-document problem(s) and ${s.unlinked_documents} unlinked document(s).`) : null,
        h('div', { style: 'margin:10px 0' }, note),
        h('button', { class: 'btn primary', disabled: !canSign, onclick: (e) => guard(e.currentTarget, async () => {
          if (!confirm('Sign off this run? It will be locked.')) return;
          await api(`/api/runs/${rid}/signoff`, { method: 'POST', json: { note: note.value, acknowledge_issues: ack.checked } });
          toast('Signed off', 'ok'); ctx.reload();
        }) }, 'Sign off and lock')]),
    h('div', { class: 'card' }, h('h2', null, 'Preview'), (() => {
      const pre = h('pre', { class: 'mono', style: 'white-space:pre-wrap;max-height:520px;overflow:auto;margin:0' }, 'Loading…');
      api(`/api/runs/${rid}/export/md`, { text: true }).then((t) => { pre.textContent = t; });
      return pre;
    })()));
}

/* ---------- tab: activity ---------- */
async function renderActivity(ctx) {
  const rows = await api(`/api/engagements/${ctx.eid}/activity`);
  ctx.box.append(h('div', { class: 'card flush' }, rows.length ? h('table', null, h('thead', null, h('tr', null, ['When', 'Who', 'What', 'Detail'].map((x) => h('th', null, x)))),
    h('tbody', null, rows.map((r) => h('tr', null, h('td', { class: 'nowrap' }, when(r.ts)), h('td', null, r.user || 'system'), h('td', null, r.action.replace(/_/g, ' ')), h('td', { class: 'small' }, r.detail || ''))))) : h('div', { class: 'empty' }, 'Nothing yet.')));
}

/* ---------- users (admin) ---------- */
async function viewUsers() {
  if (state.user.role !== 'admin') return go('#/');
  const users = await api('/api/users');
  const f = { u: h('input', { autocomplete: 'off' }), n: h('input'), p: h('input', { type: 'password', autocomplete: 'new-password' }),
    r: h('select', null, ['preparer', 'reviewer', 'admin'].map((x) => h('option', { value: x }, x))) };
  shell('users', h('h1', null, 'Users'),
    h('p', { class: 'muted' }, h('strong', null, 'Preparer'), ' uploads, runs and corrects. ', h('strong', null, 'Reviewer'), ' also decides on exceptions and signs off (never their own run). ', h('strong', null, 'Admin'), ' can do everything and manage users.'),
    h('div', { class: 'card flush' }, h('table', null, h('thead', null, h('tr', null, ['Username', 'Name', 'Role', 'Active', ''].map((x) => h('th', null, x)))),
      h('tbody', null, users.map((u) => h('tr', null, h('td', null, u.username), h('td', null, u.display_name),
        h('td', null, h('select', { style: 'width:auto', onchange: (e) => api('/api/users/' + u.id, { method: 'PATCH', json: { role: e.target.value } }).then(() => toast('Role updated', 'ok')).catch((er) => { fail(er); viewUsers(); }) },
          ['preparer', 'reviewer', 'admin'].map((x) => h('option', { value: x, selected: u.role === x }, x)))),
        h('td', null, h('input', { type: 'checkbox', checked: u.active, onchange: (e) => api('/api/users/' + u.id, { method: 'PATCH', json: { active: e.target.checked } }).then(() => toast('Updated', 'ok')).catch((er) => { fail(er); viewUsers(); }) })),
        h('td', { class: 'right' }, h('button', { class: 'btn sm', onclick: () => {
          const pw = prompt(`New password for ${u.username} (min 10 characters):`);
          if (pw) api('/api/users/' + u.id, { method: 'PATCH', json: { password: pw } }).then(() => toast('Password reset; they were signed out', 'ok')).catch(fail);
        } }, 'Reset password'))))))),
    h('div', { class: 'card' }, h('h2', null, 'Add a user'), h('div', { class: 'grid2' },
      h('div', null, h('label', null, 'Username'), f.u), h('div', null, h('label', null, 'Display name'), f.n),
      h('div', null, h('label', null, 'Initial password (min 10 characters)'), f.p), h('div', null, h('label', null, 'Role'), f.r)),
      h('div', { style: 'margin-top:12px' }, h('button', { class: 'btn primary', onclick: (e) => guard(e.currentTarget, async () => {
        await api('/api/users', { method: 'POST', json: { username: f.u.value, display_name: f.n.value, password: f.p.value, role: f.r.value } });
        toast('User created', 'ok'); viewUsers();
      }) }, 'Create user'))));
}

/* ---------- settings ---------- */
async function viewSettings() {
  const sys = state.system;
  const result = h('div', { class: 'small', style: 'margin-top:8px' });
  const cur = h('input', { type: 'password', autocomplete: 'current-password' }), nw = h('input', { type: 'password', autocomplete: 'new-password' });
  shell('settings', h('h1', null, 'Settings'),
    h('div', { class: 'card' }, h('h2', null, 'Microsoft Foundry (AI model)'),
      sys.foundry_configured ? h('div', { class: 'banner ok' }, 'Connection settings are present on the server.') : h('div', { class: 'banner warn' }, 'Not configured. AI options are disabled; the built-in reader and explanations work offline.'),
      h('p', { class: 'muted' }, 'Set these environment variables (or put them in a ', h('span', { class: 'mono' }, '.env'), ' file in the folder where you start vra-web), then restart the server:'),
      h('pre', { class: 'mono', style: 'background:var(--hover);padding:10px;border-radius:8px;overflow:auto' }, 'AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com\nAZURE_OPENAI_DEPLOYMENT=<your deployment name>\nAZURE_OPENAI_API_KEY=<key>        # or leave empty and run "az login" to use your work account'),
      state.user.role === 'admin' ? h('button', { class: 'btn', disabled: !sys.foundry_configured, onclick: (e) => guard(e.currentTarget, async () => {
        result.textContent = 'Testing…';
        const r = await api('/api/system/test-foundry', { method: 'POST' });
        result.textContent = r.ok ? `✓ Connected to ${r.deployment}. Model replied: ${r.reply}` : `✗ ${r.error}`;
        result.style.color = r.ok ? 'var(--ok)' : 'var(--danger)';
      }) }, 'Test connection') : null, result,
      h('p', { class: 'muted small' }, 'Documents sent to an AI model leave this server for your Foundry tenant. Confirm this is approved for client data before using the AI options.')),
    h('div', { class: 'card' }, h('h2', null, 'About'), h('table', null, h('tbody', null,
      h('tr', null, h('td', null, 'Version'), h('td', null, sys.version)), h('tr', null, h('td', null, 'Signed in as'), h('td', null, `${state.user.display_name} (${state.user.role})`)),
      h('tr', null, h('td', null, 'Sign-off rule'), h('td', null, sys.solo ? 'Solo mode: you may sign off your own runs (for testing).' : 'A different reviewer must sign off.')),
      h('tr', null, h('td', null, 'Max upload size'), h('td', null, sys.max_upload_mb + ' MB per file'))))),
    h('div', { class: 'card' }, h('h2', null, 'Change my password'), h('div', { class: 'grid2' }, h('div', null, h('label', null, 'Current password'), cur), h('div', null, h('label', null, 'New password (min 10 characters)'), nw)),
      h('div', { style: 'margin-top:12px' }, h('button', { class: 'btn', onclick: (e) => guard(e.currentTarget, async () => {
        await api('/api/me/password', { method: 'POST', json: { current: cur.value, new: nw.value } }); toast('Password changed. Please sign in again.', 'ok'); state.user = null; go('#/');
      }) }, 'Change password'))));
}

route();
