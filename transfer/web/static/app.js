'use strict';

/* ---------- Утилиты ---------- */

const TOKEN = document.querySelector('meta[name="transfer-token"]').content;
const $ = (sel, root) => (root || document).querySelector(sel);
const PROPS = new Set(['checked', 'value', 'disabled', 'selected', 'indeterminate']);

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style') el.style.cssText = v; // CSSOM: не блокируется CSP style-src 'self'
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (PROPS.has(k)) el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function flat(children) {
  return children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false && c !== '')
    .map((c) => (c instanceof Node ? c : document.createTextNode(String(c))));
}

// replaceChildren без "null" и с раскрытием массивов, как в h().
function fill(el, ...children) {
  el.replaceChildren(...flat(children));
  return el;
}

async function api(method, path, body) {
  const opts = { method, headers: { 'X-Transfer-Token': TOKEN } };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch(path, opts);
  } catch (e) {
    throw new Error('сервер не отвечает — запущен ли ./ui.sh?');
  }
  const ct = r.headers.get('Content-Type') || '';
  const data = ct.includes('application/json') ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.error) || r.statusText);
  return data;
}

const store = {
  get(k, d) { try { const v = localStorage.getItem('transfer.' + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem('transfer.' + k, JSON.stringify(v)); } catch (e) { /* без хранилища */ } },
};

let toastTimer = null;
function toast(msg, isErr) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast' + (isErr ? ' err' : '');
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, isErr ? 7000 : 3000);
}

function modal(title, body, okText, danger) {
  return new Promise((resolve) => {
    const m = $('#modal');
    $('#modal-title').textContent = title;
    const b = $('#modal-body');
    fill(b, body instanceof Node ? body : h('p', null, body));
    const ok = $('#modal-ok');
    ok.textContent = okText || 'OK';
    ok.className = 'btn ' + (danger ? 'danger' : 'primary');
    m.hidden = false;
    ok.focus();
    const done = (v) => { m.hidden = true; ok.onclick = null; $('#modal-cancel').onclick = null; document.onkeydown = null; resolve(v); };
    ok.onclick = () => done(true);
    $('#modal-cancel').onclick = () => done(false);
    document.onkeydown = (e) => { if (e.key === 'Escape') done(false); };
  });
}

function fmtSize(n) {
  if (n < 1024) return n + ' Б';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' КБ';
  return (n / 1024 / 1024).toFixed(1) + ' МБ';
}

function fmtDate(ts) {
  const d = new Date(ts * 1000);
  const p = (x) => String(x).padStart(2, '0');
  return `${p(d.getDate())}.${p(d.getMonth() + 1)}.${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fmtAgo(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return 'только что';
  if (s < 3600) return Math.floor(s / 60) + ' мин назад';
  if (s < 86400) return Math.floor(s / 3600) + ' ч назад';
  if (s < 86400 * 7) return Math.floor(s / 86400) + ' дн назад';
  return fmtDate(ts).slice(0, 10);
}

function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

// Копирование в буфер. Текст должен быть уже загружен: Safari разрешает запись в буфер только
// прямо в обработчике клика, без ожидания сети. Если браузер не дал доступ — окно с выделенным
// текстом для ручного Cmd/Ctrl+C.
async function copyText(text, label) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (e) { /* пробуем запасной способ */ }
  }
  const ta = h('textarea', { style: 'position:fixed;top:-1000px;left:0' });
  ta.value = text;
  document.body.append(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
  ta.remove();
  if (ok) return true;
  const area = h('textarea', { class: 'manual-copy', readonly: true, spellcheck: 'false' });
  area.value = text;
  const body = h('div', null, h('p', null, 'Браузер не дал доступ к буферу обмена. Текст выделен — нажмите Cmd+C / Ctrl+C.'), area);
  setTimeout(() => { area.focus(); area.select(); }, 50);
  await modal(label || 'Скопируйте текст', body, 'Готово');
  return false;
}

function spinner() { return h('span', { class: 'spinner' }); }

function fileList(items, statusKey) {
  return h('ul', { class: 'file-list' }, items.map((f) => typeof f === 'string'
    ? h('li', null, h('span', { class: 'st ' + statusKey }, statusKey[0]), f)
    : h('li', null, h('span', { class: 'st ' + f.status }, f.status), f.path)));
}

function listDetails(title, items, statusKey, open) {
  if (!items || !items.length) return null;
  return h('details', { open: open || items.length <= 15 },
    h('summary', null, `${title} (${items.length})`), fileList(items, statusKey));
}

/* ---------- Вкладки ---------- */

const app = { tab: 'copy', state: null, projects: [] };

function switchTab(tab) {
  app.tab = tab;
  store.set('tab', tab);
  document.querySelectorAll('.tab').forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
  for (const v of ['copy', 'apply', 'history', 'clean']) $('#view-' + v).hidden = v !== tab;
  if (tab === 'apply') loadInbox();
  if (tab === 'history') loadHistory();
  if (tab === 'clean') loadStorage();
}

/* ---------- Копирование ---------- */

const cp = {
  filter: '', project: null, info: null, mode: 'commits', branches: [], branch: null,
  commits: [], more: false, q: '', loading: false, selected: new Map(), anchor: null,
  expanded: new Map(), preview: null, previewSig: null, result: null, withBinaries: false, busy: false, exports: [],
  history: null, job: null,
};

function historyLimit() {
  if (cp.history === null) cp.history = store.get('history', app.state ? app.state.history_commits : 10);
  return cp.history;
}

async function loadProjects() {
  const data = await api('GET', '/api/projects');
  app.projects = data.projects;
  renderProjectList();
  const last = store.get('project', null);
  if (!cp.project && last && app.projects.some((p) => p.name === last)) selectProject(last);
}

function renderProjectList() {
  const f = cp.filter.toLowerCase();
  const ul = $('#project-list');
  fill(ul, ...app.projects.filter((p) => !f || p.name.toLowerCase().includes(f)).map((p) =>
    h('li', { class: p.name === cp.project ? 'active' : '', title: p.git ? p.name : p.name + ' — без git', onclick: () => selectProject(p.name) },
      h('span', { class: 'dot' + (p.git ? ' git' : '') }), p.name)));
}

async function selectProject(name) {
  Object.assign(cp, {
    project: name, info: null, commits: [], selected: new Map(), anchor: null, expanded: new Map(),
    preview: null, previewSig: null, result: null, q: '', branch: null, branches: [], more: false, exports: [],
  });
  store.set('project', name);
  renderProjectList();
  renderCopy();
  try {
    cp.info = await api('GET', `/api/projects/${encodeURIComponent(name)}`);
    if (cp.project !== name) return;
    if (cp.info.git) {
      const br = await api('GET', `/api/projects/${encodeURIComponent(name)}/branches`);
      cp.branches = br.branches;
      cp.branch = br.current && !br.current.startsWith('(') ? br.current : 'HEAD';
      const m = store.get('mode', 'commits');
      cp.mode = ['last', 'commits', 'project'].includes(m) ? m : 'commits';
      await loadCommits(true);
    } else {
      cp.mode = 'project';
    }
    await loadExports();
  } catch (e) {
    toast(e.message, true);
  }
  renderCopy();
}

async function loadCommits(reset) {
  if (!cp.info || !cp.info.git) return;
  cp.loading = true;
  const skip = reset ? 0 : cp.commits.length;
  const qs = new URLSearchParams({ ref: cp.branch || 'HEAD', skip, limit: 100 });
  if (cp.q) qs.set('q', cp.q);
  try {
    const data = await api('GET', `/api/projects/${encodeURIComponent(cp.project)}/commits?${qs}`);
    cp.commits = reset ? data.commits : cp.commits.concat(data.commits);
    cp.more = data.commits.length === 100;
  } finally {
    cp.loading = false;
  }
}

async function loadExports() {
  const data = await api('GET', `/api/projects/${encodeURIComponent(cp.project)}/exports`);
  cp.exports = data.files;
}

async function setMode(mode) {
  cp.mode = mode;
  store.set('mode', mode);
  cp.preview = null;
  if (mode === 'last' && cp.q) {
    cp.q = '';
    await loadCommits(true);
  }
  renderCopy();
}

function copyRequest() {
  const body = { project: cp.project, mode: cp.mode, ref: cp.branch, with_binaries: cp.withBinaries };
  if (cp.mode === 'commits') body.commits = [...cp.selected.keys()];
  if (cp.mode === 'project') body.history = cp.info && cp.info.git ? historyLimit() : 0;
  return body;
}

function sig() { return JSON.stringify(copyRequest()); }

function doPreview() { return startCopyJob(true); }
function doCopy() { return startCopyJob(false); }

function reveal(sel) {
  const el = $(sel);
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/* ---------- Фоновые задачи: лог, ход, остановка ---------- */

// cp.job — последняя задача копирования/предпросмотра; лог дописывается в постоянный элемент,
// чтобы перерисовка карточки не сбрасывала прокрутку и не строила тысячи строк заново.
const jobView = { log: null, details: null, timer: null, showGit: store.get('showGit', true) };

async function startCopyJob(preview) {
  cp.busy = true; renderActions();
  try {
    const job = await api('POST', '/api/jobs/copy', Object.assign(copyRequest(), { preview }));
    attachJob(job);
  } catch (e) {
    cp.busy = false; renderActions();
    toast(e.message, true);
    checkActiveJob();
  }
}

function attachJob(job) {
  cp.job = Object.assign({}, job, { lines: [] });
  cp.busy = true;
  jobView.log = h('div', { class: 'joblog' + (jobView.showGit ? '' : ' hide-git') });
  jobView.follow = true;
  jobView.log.addEventListener('scroll', () => {
    const log = jobView.log;
    jobView.follow = log.scrollHeight - log.scrollTop - log.clientHeight < 30;
  });
  jobView.details = null;
  appendJobLines(job.lines);
  renderCopy();
  reveal('#job-card');
  clearTimeout(jobView.timer);
  if (job.status === 'running') jobView.timer = setTimeout(pollJob, 400);
  else finishJob();
}

async function checkActiveJob() {
  try {
    const data = await api('GET', '/api/jobs/active');
    if (!data.job || (cp.job && cp.job.id === data.job.id)) return;
    const project = data.job.meta && data.job.meta.request && data.job.meta.request.project;
    if (project && project !== cp.project && app.projects.some((p) => p.name === project)) await selectProject(project);
    attachJob(data.job);
  } catch (e) { /* сервер старой версии или недоступен */ }
}

function appendJobLines(lines) {
  if (!lines || !lines.length) return;
  const log = jobView.log;
  const frag = document.createDocumentFragment();
  for (const [t, level, text] of lines) {
    frag.append(h('div', { class: 'ln ' + level }, h('span', { class: 't' }, t.toFixed(2).padStart(7)), text));
    cp.job.lines.push([t, level, text]);
  }
  log.append(frag);
  // Прокрутка вслед за логом, пока пользователь сам не отмотал вверх.
  if (jobView.follow) requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
}

async function pollJob() {
  const job = cp.job;
  if (!job) return;
  let info;
  try {
    info = await api('GET', `/api/jobs/${job.id}?since=${job.next}`);
  } catch (e) {
    if (cp.job !== job) return;
    job.status = 'error'; job.error = e.message;
    finishJob();
    return;
  }
  if (cp.job !== job) return;
  appendJobLines(info.lines);
  delete info.lines;
  Object.assign(job, info);
  renderJob();
  if (job.status === 'running') jobView.timer = setTimeout(pollJob, 500);
  else finishJob();
}

async function finishJob() {
  const job = cp.job;
  cp.busy = false;
  const req = Object.assign({}, (job.meta && job.meta.request) || {});
  delete req.preview;
  const same = req.project === cp.project;
  if (job.status === 'done') {
    if (job.meta.op === 'preview') {
      if (same) { cp.preview = job.result; cp.previewSig = JSON.stringify(req); }
    } else {
      if (same) { cp.result = job.result; try { await loadExports(); } catch (e) { /* покажется при следующей загрузке */ } }
      toast(`Готово: ${job.result.parts.length} ${plural(job.result.parts.length, 'часть', 'части', 'частей')}`);
    }
  } else if (job.status === 'cancelled') {
    toast('Операция остановлена');
  } else {
    toast(job.error || 'ошибка', true);
  }
  if (jobView.details) jobView.details.open = job.status !== 'done';
  renderCopy();
  if (job.status === 'done') reveal(job.meta.op === 'preview' ? '#preview-card' : '#exports-card');
}

async function cancelJob() {
  const job = cp.job;
  if (!job) return;
  try {
    Object.assign(job, await api('POST', `/api/jobs/${job.id}/cancel`), { lines: job.lines });
  } catch (e) {
    toast(e.message, true);
  }
  renderJob();
}

const JOB_STATUS = {
  running: ['выполняется', 'accent'], done: ['готово', 'ok'], error: ['ошибка', 'err'], cancelled: ['остановлено', 'warn'],
};

function renderJob() {
  const card = $('#job-card');
  if (!card) return;
  const job = cp.job;
  if (!job) { card.hidden = true; return; }
  card.hidden = false;
  const running = job.status === 'running';
  const [label, cls] = JOB_STATUS[job.status] || [job.status, ''];
  const pct = job.total ? Math.min(100, Math.round((job.done / job.total) * 100)) : null;
  const gitToggle = h('label', { class: 'check small muted', title: 'Показывать каждый вызов git с длительностью и объёмом вывода' },
    h('input', {
      type: 'checkbox', checked: jobView.showGit, onchange: (e) => {
        jobView.showGit = e.target.checked; store.set('showGit', jobView.showGit);
        jobView.log.classList.toggle('hide-git', !jobView.showGit);
      },
    }), 'вызовы git');
  if (!jobView.details) {
    jobView.details = h('details', { open: true }, h('summary', null, 'Лог'), jobView.log);
  }
  // Лог переносится в карточку только один раз: при повторной вставке браузер сбрасывает его прокрутку.
  if (jobView.details.parentNode !== card) {
    fill(card, h('div', { class: 'job-head' }), h('div', { class: 'job-body' }), jobView.details);
  }
  fill(jobView.details.querySelector('summary'), `Лог (${job.lines.length} ${plural(job.lines.length, 'строка', 'строки', 'строк')})`);
  fill(card.querySelector('.job-head'),
    h('div', { class: 'card-head' },
      h('h3', null, job.title),
      h('span', { class: 'badge ' + cls }, running ? spinner() : null, ' ', label),
      h('span', { class: 'muted small' }, `${job.elapsed.toFixed(1)} с`),
      h('span', { class: 'spacer' }),
      gitToggle,
      h('button', {
        class: 'btn small', title: 'Скопировать лог целиком — например, чтобы разобраться, где тормозит', onclick: () =>
          copyText(job.lines.map(([t, level, text]) => `[${t.toFixed(2).padStart(7)}] ${level === 'warn' || level === 'error' ? level.toUpperCase() + ': ' : ''}${text}`).join('\n'), 'Лог')
            .then((ok) => { if (ok) toast('Лог в буфере'); }),
      }, 'Копировать лог'),
      running ? h('button', {
        class: 'btn small danger', disabled: !job.cancellable || job.cancelling, onclick: cancelJob,
        title: job.cancellable ? 'Прервать операцию: текущий вызов git будет остановлен, файлы выгрузки не изменятся' : 'Идёт запись результата — остановить уже нельзя',
      }, job.cancelling ? 'Останавливается…' : 'Остановить')
        : h('button', { class: 'btn small ghost', onclick: () => { cp.job = null; renderJob(); } }, 'Скрыть')));
  fill(card.querySelector('.job-body'),
    running && job.stage ? h('div', { class: 'job-stage' },
      h('span', null, job.stage), pct !== null ? h('span', { class: 'muted small' }, ` ${job.done} / ${job.total}`) : null) : null,
    running ? h('div', { class: 'progress' + (pct === null ? ' indeterminate' : '') },
      h('div', { style: pct === null ? '' : `width:${pct}%` })) : null,
    job.status === 'error' && job.error ? h('div', { class: 'alert err' }, job.error) : null);
}

function renderCopy() {
  const root = $('#copy-content');
  if (!cp.project) { fill(root, h('div', { class: 'empty' }, 'Выберите проект слева')); return; }
  const info = cp.info;
  const head = h('div', { class: 'project-head' },
    h('h2', null, cp.project),
    !info ? spinner() : [
      info.git ? h('span', { class: 'badge accent' }, info.branch || 'HEAD') : h('span', { class: 'badge warn' }, 'без git'),
      info.git && info.dirty ? h('span', { class: 'badge warn', title: 'незакоммиченные изменения не попадают в выгрузку коммитов' }, `не закоммичено: ${info.dirty}`) : null,
      info.self ? h('span', { class: 'badge' }, 'сам инструмент') : null,
      h('span', { class: 'muted small path' }, info.path),
    ]);
  if (!info) { fill(root, head); return; }

  const modes = h('div', { class: 'segmented', role: 'tablist' },
    [['last', 'Последний коммит'], ['commits', 'Коммиты'], ['project', 'Весь проект']].map(([m, label]) =>
      h('button', { class: cp.mode === m ? 'active' : '', disabled: !info.git && m !== 'project', onclick: () => setMode(m) }, label)));

  let body;
  if (cp.mode === 'last') body = renderLast();
  else if (cp.mode === 'commits') body = renderCommits();
  else body = renderProjectMode();

  fill(root, head, h('div', { class: 'row', style: 'margin-bottom:14px' }, modes), body,
    h('div', { class: 'card', id: 'job-card', hidden: true }),
    renderPreview(), renderExports(), cp.mode === 'commits' ? h('div', { class: 'selbar', id: 'selbar' }) : null);
  if (cp.mode === 'commits') renderCommitRows();
  renderJob();
  renderActions();
}

function branchSelect() {
  const opts = cp.branches.includes(cp.branch) ? cp.branches : [cp.branch].concat(cp.branches);
  return h('select', {
    title: 'Ветка', onchange: async (e) => {
      cp.branch = e.target.value; cp.preview = null;
      await loadCommits(true); renderCopy();
    },
  }, opts.map((b) => h('option', { value: b, selected: b === cp.branch }, b)));
}

function renderLast() {
  const c = cp.commits[0];
  return h('div', { class: 'card' },
    h('div', { class: 'card-head' }, h('h3', null, 'Последний коммит ветки'), branchSelect()),
    !c ? h('div', { class: 'muted' }, 'В ветке нет коммитов') : h('div', null,
      h('div', { class: 'row' }, h('code', { class: 'mono' }, c.short), h('b', null, c.subject)),
      h('div', { class: 'muted small' }, `${c.author} · ${fmtDate(c.time)}`),
      h('div', { class: 'row', style: 'margin-top:12px' }, actionButtons())));
}

function actionButtons() {
  const disabled = cp.busy || (cp.job && cp.job.status === 'running') || (cp.mode === 'commits' && !cp.selected.size);
  return [
    h('button', { class: 'btn', disabled, onclick: doPreview }, cp.busy ? spinner() : null, 'Предпросмотр'),
    h('button', { class: 'btn primary', disabled, onclick: doCopy }, 'Скопировать'),
  ];
}

function renderProjectMode() {
  return h('div', { class: 'card' },
    h('div', { class: 'card-head' }, h('h3', null, 'Снимок проекта целиком')),
    h('p', { class: 'muted small' }, cp.info.git
      ? 'Все файлы, которые видит git: отслеживаемые и новые неигнорируемые (как при коммите). Сборка, .idea и секреты из .gitignore не попадают.'
      : 'Проект без git: все файлы, кроме служебных каталогов сборки и IDE. Проверьте, что в снимок не попадут локальные секреты.'),
    h('label', { class: 'check' }, h('input', {
      type: 'checkbox', checked: cp.withBinaries, onchange: (e) => { cp.withBinaries = e.target.checked; cp.preview = null; renderCopy(); },
    }), 'Переносить бинарные файлы (jar, картинки, keystore)'),
    cp.info.git ? h('label', { class: 'check', title: 'Изменения последних коммитов текущей ветки: при применении они воссоздаются как отдельные коммиты с теми же авторами, датами и сообщениями' },
      'История: последние',
      h('input', {
        type: 'number', min: 0, max: 1000, class: 'num', value: historyLimit(),
        onchange: (e) => {
          cp.history = Math.max(0, Math.min(1000, parseInt(e.target.value, 10) || 0));
          store.set('history', cp.history); cp.preview = null; renderCopy();
        },
      }), plural(historyLimit(), 'коммит', 'коммита', 'коммитов'), historyLimit() ? '' : ' (без истории)') : null,
    h('div', { class: 'row', style: 'margin-top:12px' }, actionButtons()));
}

function renderCommits() {
  const search = h('input', { type: 'search', placeholder: 'Поиск по сообщению или хешу…', value: cp.q });
  let timer = null;
  search.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(async () => { cp.q = search.value.trim(); await loadCommits(true); renderCommitRows(); }, 300);
  });
  const table = h('div', { class: 'table-wrap' }, h('table', { class: 'commits' }, h('tbody', { id: 'commit-rows' })));
  const wrap = h('div', { class: 'card' },
    h('div', { class: 'toolbar' }, branchSelect(), search),
    table,
    h('div', { class: 'row', style: 'margin-top:10px', id: 'more-row' }));
  return wrap;
}

function renderCommitRows() {
  const tbody = $('#commit-rows');
  if (!tbody) return;
  const rows = [];
  if (!cp.commits.length) rows.push(h('tr', null, h('td', { class: 'muted', colspan: 4 }, cp.loading ? 'Загрузка…' : 'Коммитов не найдено')));
  cp.commits.forEach((c, idx) => {
    const selected = cp.selected.has(c.sha);
    const cb = h('input', { type: 'checkbox', checked: selected, 'aria-label': 'выбрать ' + c.short });
    cb.addEventListener('click', (e) => { e.stopPropagation(); toggleCommit(idx, cb.checked, e.shiftKey); });
    const tr = h('tr', { class: 'commit' + (selected ? ' selected' : ''), dataset: null, onclick: () => toggleFiles(c) },
      h('td', { class: 'cb' }, cb),
      h('td', { class: 'hash' }, c.short),
      h('td', { class: 'subject' }, c.refs.map((r) => h('span', { class: 'ref' }, r.replace(/^HEAD -> /, ''))), c.subject,
        c.merge ? h('span', { class: 'badge', style: 'margin-left:6px' }, 'merge') : null),
      h('td', { class: 'meta', title: fmtDate(c.time) }, `${c.author} · ${fmtAgo(c.time)}`,
        c.files !== null && c.files !== undefined ? ` · ${c.files} ф.` : ''));
    tr.dataset.sha = c.sha;
    rows.push(tr);
    const exp = cp.expanded.get(c.sha);
    if (exp) {
      rows.push(h('tr', { class: 'files' }, h('td', { colspan: 4 },
        exp === 'loading' ? spinner() : exp.error ? h('span', { class: 'muted' }, exp.error)
          : [exp.merge ? h('div', { class: 'muted small' }, 'merge: изменения относительно первого родителя') : null,
            exp.files.length ? fileList(exp.files) : h('span', { class: 'muted' }, 'нет изменённых файлов')])));
    }
  });
  fill(tbody, ...rows);
  const more = $('#more-row');
  if (more) {
    fill(more, cp.more ? h('button', {
      class: 'btn', onclick: async () => { await loadCommits(false); renderCommitRows(); },
    }, 'Загрузить ещё 100') : null);
  }
  renderActions();
}

function toggleCommit(idx, checked, shift) {
  const list = cp.commits;
  let from = idx, to = idx;
  if (shift && cp.anchor !== null && cp.anchor < list.length) {
    from = Math.min(cp.anchor, idx); to = Math.max(cp.anchor, idx);
  }
  for (let i = from; i <= to; i++) {
    if (checked) cp.selected.set(list[i].sha, list[i]); else cp.selected.delete(list[i].sha);
  }
  cp.anchor = idx;
  cp.preview = null;
  renderCommitRows();
  const pv = $('#preview-card');
  if (pv) pv.remove();
}

async function toggleFiles(c) {
  if (cp.expanded.has(c.sha)) { cp.expanded.delete(c.sha); renderCommitRows(); return; }
  cp.expanded.set(c.sha, 'loading');
  renderCommitRows();
  try {
    const data = await api('GET', `/api/projects/${encodeURIComponent(cp.project)}/commits/${c.sha}/files`);
    cp.expanded.set(c.sha, data);
  } catch (e) {
    cp.expanded.set(c.sha, { error: e.message });
  }
  renderCommitRows();
}

function renderActions() {
  const bar = $('#selbar');
  if (!bar) return;
  const n = cp.selected.size;
  fill(bar,
    h('b', null, n ? `Выбрано: ${n} ${plural(n, 'коммит', 'коммита', 'коммитов')}` : 'Отметьте коммиты (Shift — диапазон)'),
    n ? h('button', { class: 'btn ghost small', onclick: () => { cp.selected.clear(); cp.preview = null; renderCopy(); } }, 'Снять выделение') : null,
    h('span', { class: 'spacer' }),
    actionButtons());
}

function renderPreview() {
  const p = cp.preview;
  if (!p || cp.previewSig !== sig()) return null;
  if (cp.mode === 'project') {
    return h('div', { class: 'card', id: 'preview-card' },
      h('div', { class: 'card-head' }, h('h3', null, 'Предпросмотр снимка'), h('span', { class: 'badge' }, p.mode === 'git' ? 'отбор через git' : 'отбор обходом каталогов')),
      p.warnings.map((w) => h('div', { class: 'alert warn' }, w)),
      h('div', { class: 'summary' },
        h('div', { class: 'stat' }, h('b', null, p.files), h('span', null, 'файлов')),
        h('div', { class: 'stat' }, h('b', null, fmtSize(p.bytes)), h('span', null, 'объём')),
        h('div', { class: 'stat' }, h('b', null, p.keep.length), h('span', null, 'бинарных не переносится')),
        h('div', { class: 'stat' }, h('b', null, p.history.length), h('span', null, 'коммитов истории'))),
      historyList(p.history),
      listDetails('Бинарные — остаются как есть (KEEP)', p.keep, 'KEEP'),
      h('details', null, h('summary', null, `Файлы снимка (${p.files})`), fileList(p.file_list, 'A')));
  }
  const between = p.between || {};
  return h('div', { class: 'card', id: 'preview-card' },
    h('div', { class: 'card-head' }, h('h3', null, 'Предпросмотр выгрузки'),
      h('span', { class: 'badge' }, `${p.commits.length} ${plural(p.commits.length, 'коммит', 'коммита', 'коммитов')}`)),
    between.truncated ? h('div', { class: 'alert warn' }, `Между выбранными коммитами ${between.count} других — проверка пересечений пропущена.`) : null,
    between.commits && between.commits.length ? h('div', { class: 'alert warn' },
      'Невыбранные коммиты между выбранными меняли те же файлы — их правки тоже попадут в выгрузку (берётся полное содержимое файла):',
      h('ul', null, between.commits.map((c) => h('li', null, h('code', null, c.short), ' ', c.subject, ' — ', c.paths.slice(0, 4).join(', '), c.count > 4 ? ' …' : ''))),
      h('button', {
        class: 'btn small', style: 'margin-top:6px', onclick: () => {
          between.commits.forEach((c) => cp.selected.set(c.sha, { sha: c.sha, short: c.short, subject: c.subject }));
          doPreview();
        },
      }, 'Добавить их в выбор')) : null,
    p.skipped.length ? h('div', { class: 'alert warn' }, 'Не переносятся:',
      h('ul', null, p.skipped.map((s) => h('li', null, `${s.path} — ${s.reason}`)))) : null,
    h('div', { class: 'summary' },
      h('div', { class: 'stat NEW' }, h('b', null, p.files.filter((f) => f.status === 'A').length), h('span', null, 'добавлено')),
      h('div', { class: 'stat UPDATED' }, h('b', null, p.files.filter((f) => f.status !== 'A').length), h('span', null, 'изменено')),
      h('div', { class: 'stat DELETE' }, h('b', null, p.deleted.length), h('span', null, 'удалено'))),
    listDetails('Файлы', p.files, 'M', true),
    listDetails('Удаляемые', p.deleted, 'D', true),
    h('details', null, h('summary', null, 'Коммиты'), h('ul', { class: 'file-list' },
      p.commits.map((c) => h('li', null, h('span', { class: 'mono hash-inline' }, c.short), c.subject)))));
}

function historyList(commits, title) {
  if (!commits || !commits.length) return null;
  return h('details', { open: commits.length <= 15 }, h('summary', null, `${title || 'История'} (${commits.length})`),
    h('ul', { class: 'file-list' }, commits.map((c) => h('li', null,
      h('span', { class: 'mono hash-inline' }, (c.short || c.sha || '').slice(0, 10)), c.subject,
      c.author ? h('span', { class: 'muted small' }, ` — ${c.author}${c.time ? ', ' + fmtDate(c.time) : ''}`) : null))));
}

// Тексты частей загружаются заранее, чтобы копирование шло прямо в обработчике клика.
const partCache = new Map();

function renderExports() {
  if (!cp.exports.length) return null;
  const groups = new Map();
  for (const f of cp.exports) {
    const key = f.id || f.name;
    if (!groups.has(key)) groups.set(key, { kind: f.kind, id: f.id, files: [] });
    groups.get(key).files.push(f);
  }
  const res = cp.result;
  return h('div', { class: 'card', id: 'exports-card' },
    h('div', { class: 'card-head' }, h('h3', null, 'Выгрузки проекта'),
      h('span', { class: 'muted small path' }, cp.exports[0].path.replace(/[^/\\]+$/, ''))),
    res ? h('div', { class: 'alert ok' },
      res.kind === 'snapshot'
        ? `Снимок: ${res.files} файлов (${fmtSize(res.bytes)}), история: ${res.history.length} ${plural(res.history.length, 'коммит', 'коммита', 'коммитов')}, архив ${fmtSize(res.archive_bytes)}.`
        : `Коммитов: ${res.commits.length}, файлов: ${res.files}, удалений: ${res.deleted}.`,
      ' Скопируйте каждую часть и вставьте на другой машине во вкладке «Применить».') : null,
    [...groups.values()].map((g) => h('div', { style: 'margin-top:8px' },
      h('div', { class: 'row' }, h('span', { class: 'badge ' + (g.kind === 'snapshot' ? 'accent' : 'ok') }, g.kind === 'snapshot' ? 'снимок' : 'изменения'),
        h('span', { class: 'muted small' }, g.id || ''),
        h('span', { class: 'spacer' }),
        g.files[0].storage_key ? h('button', {
          class: 'btn ghost small del', title: 'Удалить файлы этой выгрузки из export/', onclick: () => deleteExport(g),
        }, 'Удалить') : null),
      h('ul', { class: 'parts' }, g.files.map((f) => {
        const key = f.project_dir + '/' + f.name + ':' + f.mtime;
        const load = () => {
          if (!partCache.has(key)) {
            partCache.set(key, api('GET', `/api/export/${encodeURIComponent(f.project_dir)}/${encodeURIComponent(f.name)}`)
              .then((text) => { partCache.set(key, text); return text; }, (e) => { partCache.delete(key); throw e; }));
          }
          return Promise.resolve(partCache.get(key)); // в кэше уже может лежать готовая строка
        };
        load().catch(() => {});
        const btn = h('button', { class: 'btn small' }, 'Копировать');
        btn.addEventListener('click', async () => {
          try {
            const cached = partCache.get(key);
            const text = typeof cached === 'string' ? cached : await load();
            if (await copyText(text, `Часть ${f.part || ''}`)) {
              btn.classList.add('done');
              btn.textContent = '✓ Скопировано';
              toast(`В буфере: часть ${f.part || ''} (${fmtSize(f.bytes)})`);
            }
          } catch (e) {
            toast(e.message, true);
          }
        });
        return h('li', null, h('span', { class: 'name' }, f.name),
          h('span', { class: 'badge' }, `часть ${f.part || '?'}`),
          h('span', { class: 'muted small' }, fmtSize(f.bytes)), btn);
      })))));
}

async function deleteExport(g) {
  const bytes = g.files.reduce((a, f) => a + f.bytes, 0);
  const what = g.kind === 'snapshot' ? 'снимка' : 'выгрузки изменений';
  if (!await modal('Удалить выгрузку?', `Файлы ${what} ${g.id || ''} (${g.files.length} ${plural(g.files.length, 'часть', 'части', 'частей')}, ${fmtSize(bytes)}) будут удалены из export/.`, 'Удалить', true)) return;
  try {
    const r = await api('POST', '/api/storage/delete', { items: [{ area: 'export', key: g.files[0].storage_key }] });
    if (r.errors.length) throw new Error(r.errors.join('; '));
    if (cp.result && cp.result.id === g.id) cp.result = null;
    toast(`Удалено, освобождено ${fmtSize(r.bytes)}`);
    await loadExports();
  } catch (e) {
    toast(e.message, true);
  }
  renderCopy();
}

/* ---------- Применение ---------- */

const ap = {
  packages: [], key: null, target: '', report: null, reportSig: null, commit: false, initGit: true, force: false, busy: false, last: null,
  useBranch: true, branchName: '',
};

function todaySuffix() {
  const d = new Date();
  const p = (x) => String(x).padStart(2, '0');
  return `-${p(d.getDate())}-${p(d.getMonth() + 1)}-${String(d.getFullYear()).slice(2)}`;
}

async function loadInbox(selectFile) {
  try {
    // Список проектов тоже перечитывается: нужно знать, существует ли целевой проект.
    const [data, pr] = await Promise.all([api('GET', '/api/inbox'), api('GET', '/api/projects')]);
    app.projects = pr.projects;
    renderProjectList();
    setPackages(data.packages, selectFile);
    $('#inbox-dir').textContent = data.dir;
  } catch (e) {
    toast(e.message, true);
  }
}

function setPackages(packages, selectFile) {
  ap.packages = packages;
  if (selectFile) {
    const p = packages.find((x) => x.files.includes(selectFile));
    if (p) selectPackage(p.key, true);
  }
  if (ap.key && !packages.some((p) => p.key === ap.key)) ap.key = null;
  const ready = packages.filter((p) => p.status === 'ready').length;
  const cnt = $('#inbox-count');
  cnt.hidden = !ready;
  cnt.textContent = ready;
  renderPackages();
  renderApply();
}

const STATUS = {
  ready: ['готов', 'ok'], incomplete: ['не хватает частей', 'warn'], error: ['ошибка', 'err'], unknown: ['не распознан', 'err'],
};

function renderPackages() {
  const ul = $('#packages');
  if (!ap.packages.length) {
    fill(ul, h('li', { class: 'muted small', style: 'cursor:default' }, 'Inbox пуст. Вставьте текст выше или положите файлы в папку inbox.'));
    return;
  }
  fill(ul, ...ap.packages.map((p) => {
    const [label, cls] = STATUS[p.status];
    return h('li', { class: p.key === ap.key ? 'active' : '', onclick: () => selectPackage(p.key) },
      h('div', { class: 'title' },
        h('span', { class: 'badge ' + (p.kind === 'snapshot' ? 'accent' : p.kind ? 'ok' : '') }, p.kind === 'snapshot' ? 'снимок' : p.kind ? 'изменения' : '?'),
        h('span', null, p.project || '(проект не указан)'),
        h('span', { class: 'spacer' }),
        h('button', {
          class: 'btn ghost small del', title: 'Удалить файлы пакета из inbox',
          onclick: (e) => { e.stopPropagation(); deletePackage(p); },
        }, 'Удалить')),
      h('div', { class: 'sub' },
        h('span', { class: 'badge ' + cls }, label), ' ',
        `частей ${p.parts_present.length}/${p.parts_total}`, p.legacy ? ' · старый формат' : '', ' · ', fmtAgo(p.mtime)));
  }));
}

function selectPackage(key, keepReport) {
  const p = ap.packages.find((x) => x.key === key);
  if (!p) return;
  if (ap.key !== key) {
    ap.key = key;
    ap.target = p.local_project || p.project || '';
    if (!keepReport) ap.last = null;
    ap.report = null; ap.force = false; ap.commit = false; ap.initGit = true;
    ap.useBranch = store.get('useBranch', true);
    ap.branchName = store.get('branchName', '') || p.snapshot_branch || 'import';
  }
  renderPackages();
  renderApply();
}

function currentPackage() { return ap.packages.find((p) => p.key === ap.key) || null; }
function targetExists(name) { return app.projects.some((p) => p.name === name); }
function targetHasGit(name) { return app.projects.some((p) => p.name === name && p.git); }
function branchApplies() {
  const p = currentPackage();
  return !!p && p.kind === 'snapshot' && targetHasGit(ap.target) && ap.useBranch;
}
function applyRequest(dry) {
  const branch = branchApplies() ? ap.branchName.trim() : '';
  return { key: ap.key, target: ap.target, dry_run: dry, force: ap.force, commit: ap.commit && !branch, init_git: ap.initGit, branch };
}
function applySig() { return JSON.stringify(applyRequest(true)); }

function renderApply() {
  const root = $('#apply-content');
  const p = currentPackage();
  if (!p) {
    fill(root, ap.last ? renderReport(ap.last, true) : h('div', { class: 'empty' }, 'Вставьте выгрузку или выберите пакет из inbox'));
    return;
  }
  const [label, cls] = STATUS[p.status];
  const kindName = p.kind === 'snapshot' ? 'Снимок проекта' : p.kind === 'changes' ? 'Изменения коммитов' : 'Неизвестный файл';
  const idTime = /^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}/.test(p.id || '') ? p.id.slice(0, 19).replace('_', ' ').replace(/-(\d\d)-(\d\d)$/, ':$1:$2') : null;

  const kv = h('dl', { class: 'kv' },
    h('dt', null, 'Проект в выгрузке'), h('dd', null, p.project || '— (старый формат, укажите проект)'),
    h('dt', null, 'Создана'), h('dd', null, idTime || p.id || '—'),
    h('dt', null, 'Части'), h('dd', null, `${p.parts_present.length} из ${p.parts_total}`,
      p.missing.length ? h('span', { class: 'badge warn', style: 'margin-left:8px' }, 'нет: ' + p.missing.join(', ')) : null),
    h('dt', null, 'Файлы в inbox'), h('dd', { class: 'mono small' }, p.files.join(', ')),
    p.kind === 'snapshot' && p.snapshot_files !== undefined ? [h('dt', null, 'Содержимое'), h('dd', null, `${p.snapshot_files} файлов`)] : null,
    p.kind === 'changes' && p.changes_files !== undefined ? [h('dt', null, 'Содержимое'), h('dd', null, `${p.changes_files} файлов, удалений: ${p.changes_deleted}`)] : null,
    p.commits && p.commits.length ? [h('dt', null, p.kind === 'snapshot' ? 'История' : 'Коммиты'), h('dd', null, h('ul', { class: 'file-list' },
      p.commits.map((c) => h('li', null, h('span', { class: 'mono hash-inline' }, c.sha), c.subject))))] : null);

  const datalist = h('datalist', { id: 'project-names' }, app.projects.map((x) => h('option', { value: x.name })));
  const targetInput = h('input', { type: 'text', value: ap.target, list: 'project-names', placeholder: 'имя проекта', 'aria-label': 'Проект для применения' });
  targetInput.addEventListener('input', () => { ap.target = targetInput.value.trim(); ap.report = null; renderTargetBadge(); renderApplyTail(); });
  const targetBadge = h('span', { id: 'target-badge' });

  const ready = p.status === 'ready';
  fill(root,
    h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', null, kindName), h('span', { class: 'badge ' + cls }, label), p.legacy ? h('span', { class: 'badge' }, 'старый формат') : null),
      kv,
      p.errors.length ? h('div', { class: 'alert err' }, h('ul', null, p.errors.map((e) => h('li', null, e)))) : null,
      p.warnings.length ? h('div', { class: 'alert warn' }, h('ul', null, p.warnings.map((e) => h('li', null, e)))) : null,
      !ready ? null : [
        h('div', { class: 'target-row' }, h('b', null, 'Применить к проекту:'), targetInput, datalist, targetBadge),
        h('div', { class: 'options', id: 'apply-options' }),
        h('div', { class: 'row', id: 'apply-actions' })],
      h('div', { class: 'row', style: 'margin-top:8px' }, h('span', { class: 'spacer' }),
        h('button', { class: 'btn ghost small', onclick: discardPackage, title: 'Перенести файлы в inbox/discarded' }, 'Убрать из inbox'),
        h('button', { class: 'btn ghost small del', onclick: () => deletePackage(p) }, 'Удалить'))),
    h('div', { id: 'apply-report' }));
  renderTargetBadge();
  renderApplyTail();
}

function renderTargetBadge() {
  const el = $('#target-badge');
  const p = currentPackage();
  if (!el || !p) return;
  if (!ap.target) { fill(el, h('span', { class: 'badge err' }, 'укажите проект')); return; }
  const exists = targetExists(ap.target);
  fill(el, exists ? h('span', { class: 'badge ok' }, 'проект есть')
    : p.kind === 'snapshot' ? h('span', { class: 'badge accent' }, 'будет создан') : h('span', { class: 'badge err' }, 'проект не найден'));
}

function renderApplyTail() {
  const p = currentPackage();
  const opts = $('#apply-options');
  if (!p || !opts) return;
  const exists = targetExists(ap.target);
  const cb = (label, key, title) => h('label', { class: 'check', title: title || '' }, h('input', {
    type: 'checkbox', checked: ap[key], onchange: (e) => { ap[key] = e.target.checked; ap.report = null; renderApplyTail(); },
  }), label);
  const withBranch = branchApplies();
  let branchRow = null;
  if (p.kind === 'snapshot' && targetHasGit(ap.target)) {
    const nameInput = h('input', { type: 'text', value: ap.branchName, placeholder: 'имя ветки', 'aria-label': 'Имя ветки', disabled: !ap.useBranch });
    nameInput.addEventListener('input', () => {
      ap.branchName = nameInput.value; store.set('branchName', ap.branchName.trim()); ap.report = null;
      $('#branch-full').textContent = (ap.branchName.trim() || '…') + todaySuffix();
      const rep = $('#apply-report'); if (rep && !ap.last) fill(rep);
      document.querySelectorAll('#apply-actions button').forEach((b) => { b.disabled = ap.busy || !ap.branchName.trim(); });
    });
    branchRow = h('div', { class: 'branch-row' },
      h('label', { class: 'check', title: 'Ветка отводится от текущего HEAD; в неё записываются коммиты истории из снимка и незакоммиченные правки источника' },
        h('input', {
          type: 'checkbox', checked: ap.useBranch,
          onchange: (e) => { ap.useBranch = e.target.checked; store.set('useBranch', ap.useBranch); ap.report = null; renderApplyTail(); },
        }), 'Отвести ветку'),
      nameInput, h('span', { class: 'muted small' }, '→ ', h('code', { id: 'branch-full' }, (ap.branchName.trim() || '…') + todaySuffix())));
  }
  fill(opts,
    branchRow,
    p.kind === 'snapshot' && !exists ? cb('git init + стартовый коммит', 'initGit', 'история из снимка попадёт в ветку источника') : null,
    exists && !withBranch ? cb('Закоммитить после применения', 'commit', (p.kind === 'changes'
      ? 'каждый перенесённый коммит — отдельным коммитом с исходным сообщением и автором; '
      : 'коммит только применённых файлов; ') + 'если в индексе уже что-то есть — коммит не создаётся') : null,
    p.kind === 'snapshot' && exists ? cb('Удалять, даже если файлов много (force)', 'force', 'защита от применения снимка не к тому проекту') : null);

  const canRun = ap.target && (exists || p.kind === 'snapshot') && !ap.busy && !(withBranch && !ap.branchName.trim());
  fill($('#apply-actions'),
    h('button', { class: 'btn', disabled: !canRun, onclick: () => runApply(true) }, ap.busy ? spinner() : null, 'Предпросмотр'),
    h('button', { class: 'btn primary', disabled: !canRun, onclick: () => runApply(false) }, 'Применить'));
  const rep = $('#apply-report');
  fill(rep, ap.report && ap.reportSig === applySig() ? renderReport(ap.report) : ap.last ? renderReport(ap.last, true) : '');
}

async function runApply(dry) {
  ap.busy = true; renderApplyTail();
  try {
    if (dry) {
      ap.report = await api('POST', '/api/apply', applyRequest(true));
      ap.reportSig = applySig();
      ap.last = null;
      return;
    }
    let preview = ap.report && ap.reportSig === applySig() ? ap.report : null;
    if (!preview) {
      preview = await api('POST', '/api/apply', applyRequest(true));
      ap.report = preview; ap.reportSig = applySig();
    }
    if (preview.blocked) { toast(preview.error, true); return; }
    const body = h('div', null,
      h('p', null, preview.create ? `Будет создан проект ${preview.target}:` : `Проект ${preview.target}:`),
      h('div', { class: 'summary' },
        h('div', { class: 'stat NEW' }, h('b', null, preview.new.length), h('span', null, 'создать')),
        h('div', { class: 'stat UPDATED' }, h('b', null, preview.updated.length), h('span', null, 'изменить')),
        h('div', { class: 'stat DELETE' }, h('b', null, preview.deleted.length), h('span', null, 'удалить'))),
      preview.warnings.length ? h('div', { class: 'alert warn' }, h('ul', null, preview.warnings.map((w) => h('li', null, w)))) : null,
      preview.branch ? h('p', null, 'Ветка ', h('code', null, preview.branch), ` от текущего HEAD, история: ${preview.history.length} ${plural(preview.history.length, 'коммит', 'коммита', 'коммитов')}.`) : null,
      !preview.create ? h('p', { class: 'muted small' }, 'Перед изменением делается бэкап — откатить можно во вкладке «История».') : null);
    ap.busy = false; renderApplyTail();
    if (!await modal('Применить выгрузку?', body, 'Применить', preview.deleted.length > 0)) return;
    ap.busy = true; renderApplyTail();
    const report = await api('POST', '/api/apply', applyRequest(false));
    ap.last = report;
    ap.report = null;
    if (report.error) { toast(report.error, true); } else {
      toast(report.create ? `Проект ${report.target} создан` : `Применено к ${report.target}`);
      ap.key = null;
    }
    await Promise.all([loadInbox(), loadProjects()]);
  } catch (e) {
    toast(e.message, true);
  } finally {
    ap.busy = false;
    renderApplyTail();
    renderApply();
  }
}

function renderReport(r, applied) {
  const dry = r.dry_run;
  return h('div', { class: 'card' },
    h('div', { class: 'card-head' },
      h('h3', null, dry ? 'Предпросмотр применения' : 'Результат применения'),
      h('span', { class: 'badge' }, r.target),
      dry ? h('span', { class: 'badge warn' }, 'ничего не изменено') : h('span', { class: 'badge ok' }, 'применено'),
      r.create ? h('span', { class: 'badge accent' }, dry ? 'проект будет создан' : 'проект создан') : null),
    r.error ? h('div', { class: 'alert err' }, r.error) : null,
    r.warnings.length ? h('div', { class: 'alert warn' }, h('ul', null, r.warnings.map((w) => h('li', null, w)))) : null,
    h('div', { class: 'summary' },
      h('div', { class: 'stat NEW' }, h('b', null, r.new.length), h('span', null, dry ? 'будет создано' : 'создано')),
      h('div', { class: 'stat UPDATED' }, h('b', null, r.updated.length), h('span', null, dry ? 'будет изменено' : 'изменено')),
      h('div', { class: 'stat DELETE' }, h('b', null, r.deleted.length), h('span', null, dry ? 'будет удалено' : 'удалено')),
      h('div', { class: 'stat' }, h('b', null, r.unchanged), h('span', null, 'без изменений')),
      r.absent && r.absent.length ? h('div', { class: 'stat' }, h('b', null, r.absent.length), h('span', null, 'уже отсутствовали')) : null),
    listDetails('Новые', r.new, 'NEW'),
    listDetails('Изменённые', r.updated, 'UPDATED'),
    listDetails(dry ? 'Будут удалены' : 'Удалённые', r.deleted, 'DELETE', true),
    listDetails('Уже отсутствовали', r.absent, 'D'),
    !dry && r.backup ? h('p', { class: 'muted small' }, `Бэкап: ${r.backup} — откат во вкладке «История».`) : null,
    dry && r.branch ? h('div', { class: 'alert ok' }, 'Будет создана ветка ', h('code', null, r.branch), ' — HEAD переключится на неё.') : null,
    dry ? historyList(r.history, 'История в снимке') : null,
    !dry && r.git && r.git.branch ? h('div', { class: 'alert ok' }, 'Создана ветка ', h('code', null, r.git.branch), ', HEAD переключён на неё.') : null,
    !dry && r.git && r.git.commit ? h('p', { class: 'muted small' }, `git: ${r.git.init ? 'init, ' : ''}коммит ${r.git.commit.slice(0, 10)}`
      + (r.git.reused ? `, уже было в проекте: ${r.git.reused} ${plural(r.git.reused, 'коммит', 'коммита', 'коммитов')}` : '')) : null,
    !dry && r.git ? historyList([...(r.git.commits || [])].reverse(), 'Созданные коммиты') : null,
    applied && r.archived ? h('p', { class: 'muted small' }, 'Файлы выгрузки перенесены в inbox/applied.') : null);
}

async function discardPackage() {
  const p = currentPackage();
  if (!p) return;
  if (!await modal('Убрать из inbox?', `Файлы (${p.files.join(', ')}) будут перенесены в inbox/discarded.`, 'Убрать')) return;
  try {
    const data = await api('POST', '/api/inbox/discard', { key: p.key });
    ap.key = null;
    setPackages(data.packages);
  } catch (e) {
    toast(e.message, true);
  }
}

async function deletePackage(p) {
  const body = h('div', null, h('p', null, `Файлы пакета будут удалены безвозвратно (в inbox/discarded не переносятся):`),
    h('ul', { class: 'file-list' }, p.files.map((f) => h('li', null, f))));
  if (!await modal('Удалить из inbox?', body, 'Удалить', true)) return;
  try {
    const data = await api('POST', '/api/inbox/delete', { key: p.key });
    if (ap.key === p.key) ap.key = null;
    toast(`Удалено файлов: ${data.removed.length}`);
    setPackages(data.packages);
  } catch (e) {
    toast(e.message, true);
  }
}

async function saveText(text, name) {
  const data = await api('POST', '/api/inbox', { text, name });
  $('#paste-status').textContent = 'сохранено: ' + data.saved;
  setPackages(data.packages, data.saved);
  return data;
}

async function onPasteSave() {
  const ta = $('#paste-text');
  if (!ta.value.trim()) { toast('Вставьте текст выгрузки'); return; }
  try {
    await saveText(ta.value);
    ta.value = '';
  } catch (e) {
    toast(e.message, true);
  }
}

async function onPasteClipboard() {
  try {
    const text = await navigator.clipboard.readText();
    if (!text.trim()) { toast('Буфер обмена пуст'); return; }
    await saveText(text);
  } catch (e) {
    toast('Нет доступа к буферу: ' + e.message + ' — вставьте текст в поле вручную', true);
  }
}

async function onFiles(e) {
  const files = [...e.target.files];
  e.target.value = '';
  let last = null;
  for (const f of files) {
    try {
      last = await saveText(await f.text(), f.name);
    } catch (err) {
      toast(`${f.name}: ${err.message}`, true);
    }
  }
  if (last) toast(`Добавлено файлов: ${files.length}`);
}

/* ---------- История ---------- */

async function loadHistory() {
  const root = $('#history-content');
  fill(root, spinner());
  try {
    const data = await api('GET', '/api/backups');
    fill(root,
      h('div', { class: 'card-head' }, h('h2', null, 'История применений'),
        h('span', { class: 'muted small' }, 'перед каждым применением делается бэкап затронутых файлов')),
      !data.backups.length ? h('div', { class: 'empty' }, 'Применений ещё не было') :
        h('div', { class: 'card table-scroll' }, h('table', { class: 'history' },
          h('thead', null, h('tr', null, ['Когда', 'Проект', 'Вид', 'Создано', 'Изменено', 'Удалено', ''].map((t) => h('th', null, t)))),
          h('tbody', null, data.backups.map((b) => h('tr', null,
            h('td', { class: 'when' }, b.created_at),
            h('td', null, h('b', null, b.project), b.package ? h('div', { class: 'muted small mono' }, b.package) : null),
            h('td', null, b.kind === 'snapshot' ? 'снимок' : 'изменения'),
            h('td', null, b.counts.created), h('td', null, b.counts.modified), h('td', null, b.counts.deleted),
            h('td', null, b.restored_at ? h('span', { class: 'badge', title: b.restored_at }, 'откатан')
              : b.project_created ? h('span', { class: 'badge accent' }, 'проект создан')
                : h('button', { class: 'btn small', onclick: () => restoreBackup(b) }, 'Откатить'))))))));
  } catch (e) {
    fill(root, h('div', { class: 'alert err' }, e.message));
  }
}

async function restoreBackup(b) {
  const body = h('div', null,
    h('p', null, `Проект ${b.project} вернётся к состоянию до применения ${b.created_at}:`),
    h('ul', null,
      h('li', null, `удалить созданные файлы: ${b.counts.created}`),
      h('li', null, `вернуть прежние версии: ${b.counts.modified + b.counts.deleted}`)),
    h('div', { class: 'alert warn' }, 'Правки этих файлов, сделанные после применения, будут потеряны. '
      + (b.git ? `HEAD вернётся на прежнюю ветку, ветка ${b.git.branch.replace('refs/heads/', '')} останется в git.`
        : 'Откатываются только файлы: коммит, созданный при применении, остаётся в git.')));
  if (!await modal('Откатить применение?', body, 'Откатить', true)) return;
  try {
    const r = await api('POST', '/api/backups/restore', { id: b.id });
    toast(`Откат: удалено ${r.removed.length}, восстановлено ${r.restored.length}` + (r.git ? '. ' + r.git : ''));
    loadHistory();
  } catch (e) {
    toast(e.message, true);
  }
}

/* ---------- Очистка ---------- */

const cl = { items: [], dirs: {}, selected: new Set(), days: store.get('cleanDays', 7), busy: false };

const AREAS = [
  ['export', 'Выгрузки', 'Части, подготовленные на этой машине. Для каждого проекта хранится последний комплект каждого вида — удаляйте, когда части уже перенесены.'],
  ['applied', 'Применённые пакеты', 'Файлы выгрузок, которые уже применены (inbox/applied).'],
  ['discarded', 'Убранные из inbox', 'Пакеты, убранные кнопкой «Убрать из inbox» (inbox/discarded).'],
  ['backups', 'Бэкапы применений', 'Прежние версии файлов для отката. После удаления бэкапа откатить это применение во вкладке «История» нельзя.'],
];

function itemKey(it) { return it.area + ':' + it.key; }

async function loadStorage() {
  const root = $('#clean-content');
  if (!cl.items.length) fill(root, spinner());
  try {
    const data = await api('GET', '/api/storage');
    cl.items = data.items; cl.dirs = data.dirs;
    const keys = new Set(cl.items.map(itemKey));
    for (const k of [...cl.selected]) if (!keys.has(k)) cl.selected.delete(k);
    renderStorage();
  } catch (e) {
    fill(root, h('div', { class: 'alert err' }, e.message));
  }
}

function selectedItems() { return cl.items.filter((it) => cl.selected.has(itemKey(it))); }

function renderStorage() {
  const root = $('#clean-content');
  const sel = selectedItems();
  const selBytes = sel.reduce((a, it) => a + it.bytes, 0);
  const total = cl.items.reduce((a, it) => a + it.bytes, 0);
  const days = h('input', { type: 'number', min: 0, max: 3650, class: 'num', value: cl.days, 'aria-label': 'дней' });
  days.addEventListener('change', () => { cl.days = Math.max(0, parseInt(days.value, 10) || 0); store.set('cleanDays', cl.days); });
  const selectOld = () => {
    const limit = Date.now() / 1000 - cl.days * 86400;
    cl.items.forEach((it) => { if (it.mtime < limit) cl.selected.add(itemKey(it)); });
    renderStorage();
  };
  fill(root,
    h('div', { class: 'card-head' }, h('h2', null, 'Очистка'),
      h('span', { class: 'muted small' }, `всего ${cl.items.length} · ${fmtSize(total)}`),
      h('span', { class: 'spacer' }),
      h('button', { class: 'btn ghost small', onclick: loadStorage }, 'Обновить')),
    h('div', { class: 'card clean-bar' },
      h('span', null, 'Отметить старше'), days, h('span', null, 'дн.'),
      h('button', { class: 'btn small', onclick: selectOld }, 'Отметить'),
      sel.length ? h('button', { class: 'btn ghost small', onclick: () => { cl.selected.clear(); renderStorage(); } }, 'Снять выбор') : null,
      h('span', { class: 'spacer' }),
      h('b', null, sel.length ? `Выбрано: ${sel.length} · ${fmtSize(selBytes)}` : 'Ничего не выбрано'),
      h('button', { class: 'btn danger', disabled: !sel.length || cl.busy, onclick: deleteSelected }, cl.busy ? spinner() : null, 'Удалить выбранное')),
    !cl.items.length ? h('div', { class: 'empty' }, 'Удалять нечего') : null,
    AREAS.map(([area, title, hint]) => renderArea(area, title, hint)));
}

function renderArea(area, title, hint) {
  const items = cl.items.filter((it) => it.area === area);
  if (!items.length) return null;
  const n = items.filter((it) => cl.selected.has(itemKey(it))).length;
  const all = h('input', {
    type: 'checkbox', checked: n === items.length, indeterminate: n > 0 && n < items.length, 'aria-label': 'выбрать все: ' + title,
    onchange: (e) => { items.forEach((it) => (e.target.checked ? cl.selected.add(itemKey(it)) : cl.selected.delete(itemKey(it)))); renderStorage(); },
  });
  return h('div', { class: 'card' },
    h('div', { class: 'card-head' }, all, h('h3', null, title),
      h('span', { class: 'badge' }, `${items.length} · ${fmtSize(items.reduce((a, it) => a + it.bytes, 0))}`),
      h('span', { class: 'muted small path' }, cl.dirs[area] || '')),
    h('p', { class: 'muted small', style: 'margin:0 0 6px' }, hint),
    h('div', { class: 'table-scroll' }, h('table', { class: 'history clean' },
      h('tbody', null, items.map((it) => {
        const k = itemKey(it);
        const cb = h('input', { type: 'checkbox', checked: cl.selected.has(k), 'aria-label': 'выбрать ' + it.key });
        const toggle = () => { if (cl.selected.has(k)) cl.selected.delete(k); else cl.selected.add(k); renderStorage(); };
        cb.addEventListener('click', (e) => { e.stopPropagation(); toggle(); });
        return h('tr', { class: cl.selected.has(k) ? 'selected' : '', onclick: toggle },
          h('td', { class: 'cb' }, cb),
          h('td', null, h('b', null, it.project || '—'),
            h('div', { class: 'muted small mono' }, it.id || it.key)),
          h('td', null, it.kind ? h('span', { class: 'badge ' + (it.kind === 'snapshot' ? 'accent' : 'ok') }, it.kind === 'snapshot' ? 'снимок' : 'изменения') : null,
            it.restored_at ? h('span', { class: 'badge', style: 'margin-left:4px', title: it.restored_at }, 'откатан') : null),
          h('td', { class: 'muted small' }, `${it.files} ${plural(it.files, 'файл', 'файла', 'файлов')}`),
          h('td', null, fmtSize(it.bytes)),
          h('td', { class: 'when muted small', title: fmtDate(it.mtime) }, fmtAgo(it.mtime)));
      })))));
}

async function deleteSelected() {
  const sel = selectedItems();
  if (!sel.length) return;
  const byArea = AREAS.map(([area, title]) => [title, sel.filter((it) => it.area === area)]).filter(([, l]) => l.length);
  const backups = sel.filter((it) => it.area === 'backups' && !it.restored_at).length;
  const body = h('div', null,
    h('p', null, `Будет удалено безвозвратно (${fmtSize(sel.reduce((a, it) => a + it.bytes, 0))}):`),
    h('ul', null, byArea.map(([title, l]) => h('li', null, `${title}: ${l.length}`))),
    backups ? h('div', { class: 'alert warn' }, `Для ${backups} ${plural(backups, 'применения', 'применений', 'применений')} пропадёт возможность отката.`) : null);
  if (!await modal('Удалить выбранное?', body, 'Удалить', true)) return;
  cl.busy = true; renderStorage();
  try {
    const r = await api('POST', '/api/storage/delete', { items: sel.map((it) => ({ area: it.area, key: it.key })) });
    r.removed.forEach((it) => cl.selected.delete(it.area + ':' + it.key));
    if (r.errors.length) toast('Не удалось: ' + r.errors.join('; '), true);
    else toast(`Удалено: ${r.removed.length}, освобождено ${fmtSize(r.bytes)}`);
    if (cp.project && r.removed.some((it) => it.area === 'export')) {
      cp.result = null;
      loadExports().then(renderCopy, () => {});
    }
  } catch (e) {
    toast(e.message, true);
  } finally {
    cl.busy = false;
  }
  loadStorage();
}

/* ---------- Старт ---------- */

async function init() {
  document.querySelectorAll('.tab').forEach((b) => b.addEventListener('click', () => switchTab(b.dataset.tab)));
  $('#project-filter').addEventListener('input', (e) => { cp.filter = e.target.value; renderProjectList(); });
  $('#paste-save').addEventListener('click', onPasteSave);
  $('#paste-clipboard').addEventListener('click', onPasteClipboard);
  $('#paste-files').addEventListener('change', onFiles);
  $('#paste-text').addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) onPasteSave(); });
  $('#inbox-refresh').addEventListener('click', () => loadInbox());
  window.addEventListener('focus', () => { if (app.tab === 'apply') loadInbox(); });

  try {
    app.state = await api('GET', '/api/state');
    $('#where').textContent = `${app.state.host} · ${app.state.projects_root}`;
    $('#where').title = `inbox: ${app.state.inbox_dir}\nexport: ${app.state.export_dir}\nмакс. размер части: ${fmtSize(app.state.max_part_bytes)}`;
    await loadProjects();
  } catch (e) {
    toast(e.message, true);
  }
  switchTab(store.get('tab', 'copy'));
  loadInbox();
  checkActiveJob();
}

init();
