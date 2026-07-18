const STAGE_NAMES = ['Intake', 'PRD', 'Recon', 'Design', 'Plan', 'Build 1', 'Build 2+', 'Test', 'Review', 'Ship'];
const KIND_LABEL = { sentry: 'sentry', task: 'request', feature: 'feature', memory: 'memory', watch: 'watch' };
const STATUS_LABEL = { received: 'Received', queued: 'Queued', running: 'Running',
  awaiting_input: 'Awaiting you', pr_opened: 'PR opened', no_fix: 'No fix',
  skipped: 'Skipped', error: 'Error', timeout: 'Timeout',
  watching: 'Watching', done: 'Done' };
const LIVE = ['received', 'queued', 'running', 'awaiting_input', 'watching'];
const NL = String.fromCharCode(10);

// session-wide state, declared BEFORE anything that can run during initial
// script evaluation (renderInbox fires from routeHash at load — a later `let`
// would be a temporal-dead-zone crash that kills the whole script)
let ME = null;
let WORKSPACES = [];

function esc(s) { const d = document.createElement('span'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }

// URL matcher built WITHOUT regex-literal escapes: this file ships inside a
// Python string, so backslashes are banned. fromCharCode(9,10,13,34,39) puts
// tab, LF, CR, double-quote and single-quote in the negated class.
const URL_RE = new RegExp('https://[^ <>)' + String.fromCharCode(9, 10, 13, 34, 39) + ']+', 'g');
function linkify(s) {
  // esc() first: matched URLs then contain no <>"' and are attribute-safe
  return esc(s).replace(URL_RE, (u) => `<a href="${u}" target="_blank">${u}</a>`);
}

function fmtMMSS(ms) {
  if (!ms) return '0:00';
  const t = Math.round(ms / 1000);
  return Math.floor(t / 60) + ':' + String(t % 60).padStart(2, '0');
}
function fmtWait(sec) {
  if (!sec) return '&mdash;';
  sec = Math.round(sec);
  if (sec < 60) return sec + 's';
  const m = Math.floor(sec / 60);
  if (m < 60) return m + 'm ' + (sec % 60) + 's';
  const h = Math.floor(m / 60);
  if (h < 24) return h + 'h ' + (m % 60) + 'm';
  return Math.floor(h / 24) + 'd ' + (h % 24) + 'h';
}
function fmtAgo(ts) {
  if (!ts) return '';
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 45) return 'now';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm';
  const h = Math.floor(m / 60);
  if (h < 24) return h + 'h';
  return Math.floor(h / 24) + 'd';
}

// ---------- inbox (left pane) ----------

let jobsCache = [];
let filter = 'all';
let sel = null;            // selected job id, or null (welcome view)

function setFilter(f) {
  filter = f;
  for (const b of document.querySelectorAll('.fchip')) b.classList.toggle('on', b.dataset.f === f);
  renderInbox();
}

function rowMini(j) {
  const cur = Number(j.stage) || 0;
  const still = LIVE.includes(j.status) ? '' : ' still';
  let segs = '';
  for (let i = 0; i < 10; i++)
    segs += `<i class="${i < cur ? 'd' : (i === cur ? 'c' + still : '')}"></i>`;
  return `<div class="mini">${segs}</div>`;
}

function row(j) {
  const stageTxt = j.kind === 'feature' ? ` &middot; P${Number(j.stage) || 0} ${esc(j.stage_name || STAGE_NAMES[Number(j.stage) || 0] || '')}` : ` &middot; ${esc(KIND_LABEL[j.kind] || j.kind)}`;
  // inbox items (Epic A4) carry ownership + SLA state
  const overdue = j.overdue ? '<span class="badge overdue">overdue</span>' : '';
  const owner = j.gate_owner ? ` &middot; ${esc(j.gate_owner.display)}${j.gate_owner.is_you ? ' (you)' : ''}` : '';
  return `<div class="row${j.issue_id === sel ? ' sel' : ''}" data-status="${esc(j.status)}" onclick="openItem('${esc(j.issue_id)}')">
    <span class="rail"></span>
    <div class="body">
      <div class="t">${esc(j.title || j.issue_id)}</div>
      <div class="m"><span class="badge ${esc(j.status)}">${esc(STATUS_LABEL[j.status] || j.status)}</span>${overdue}${esc(j.project)}${stageTxt}${owner}</div>
      ${j.kind === 'feature' ? rowMini(j) : ''}
    </div>
    <span class="when">${fmtAgo(j.updated_at)}</span>
  </div>`;
}

function renderInbox() {
  let items;
  if (filter === 'await' && inboxCache && inboxCache.items) {
    // the server-ordered personal queue: overdue first, then oldest gate first
    items = inboxCache.items.filter(jobInActiveWs);
  } else {
    items = jobsCache.slice().sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
    items = items.filter(jobInActiveWs);
    if (filter === 'active') items = items.filter(j => LIVE.includes(j.status));
    if (filter === 'await') items = items.filter(j => j.status === 'awaiting_input');
  }
  // an unassigned member sees an empty instance by design (fail-closed
  // membership) — say why, instead of a bare "nothing here"
  const empty = (ME && ME.role === 'member' && WORKSPACES && !WORKSPACES.length)
    ? 'no workspace access yet &mdash; ask your CtrlLoop admin to assign you to a workspace'
    : 'nothing here';
  document.getElementById('inbox-list').innerHTML =
    items.length ? items.map(row).join('') : `<div class="empty">${empty}</div>`;
}

let lastJobs = '';
async function refresh(force) {
  try {
    const r = await fetch('api/jobs');
    if (r.status === 401) { location.href = 'login'; return; }  // session expired
    const jobs = await r.json();
    const sig = JSON.stringify(jobs);
    if (force || sig !== lastJobs) { lastJobs = sig; jobsCache = jobs; renderInbox(); }
  } catch (e) { document.getElementById('msg').textContent = 'refresh failed: ' + e; }
  refreshInbox();
}

// ---------- per-person queue (Epic A4: api/inbox) ----------

let inboxCache = null;
let lastInbox = '';
async function refreshInbox() {
  try {
    const r = await fetch('api/inbox');
    if (!r.ok) return;
    const data = await r.json();
    const sig = JSON.stringify(data);
    if (sig === lastInbox) return;
    lastInbox = sig; inboxCache = data;
    const n = (data.counts && data.counts.mine) || 0;
    const b = document.getElementById('await-badge');
    if (b) { b.textContent = String(n); b.style.display = n ? '' : 'none'; }
    const chip = document.getElementById('me-chip');
    if (chip) chip.classList.toggle('has-due', n > 0);
    if (filter === 'await') renderInbox();
  } catch (e) { /* advisory — the jobs list still renders */ }
}

// ---------- selection / routing ----------

const JOB_HASH_RE = new RegExp('^#/job/(.+)$');
function routeHash() {
  const m = (location.hash || '').match(JOB_HASH_RE);
  if (m) openItem(decodeURIComponent(m[1]), true); else closeItem(true);
}

function openItem(id, fromHash) {
  if (!fromHash && (location.hash || '') !== '#/job/' + encodeURIComponent(id))
    history.replaceState(null, '', '#/job/' + encodeURIComponent(id));
  const changed = sel !== id;
  sel = id;
  document.getElementById('shell').className = 'shell split';
  document.getElementById('welcome').style.display = 'none';
  const dp = document.getElementById('dpane');
  dp.style.display = 'flex';
  renderInbox();
  if (changed) resetDetail(id);
  loadDetail(id);
  if (dPoll) clearInterval(dPoll);
  dPoll = setInterval(() => { if (sel) loadDetail(sel); }, 5000);
}

function closeItem(fromHash) {
  if (!fromHash && (location.hash || '').indexOf('#/job/') === 0)
    history.replaceState(null, '', location.pathname + location.search);
  sel = null;
  document.getElementById('shell').className = 'shell solo';
  document.getElementById('welcome').style.display = '';
  document.getElementById('dpane').style.display = 'none';
  stopStageStream(); stopChatStream();
  if (dPoll) { clearInterval(dPoll); dPoll = null; }
  renderInbox();
}

function openNew() {
  // the intake view: split open with the welcome pane (no item selected)
  closeItem();
  document.getElementById('shell').className = 'shell split';
}

// ---------- detail (right pane) ----------

let dPoll = null;
let lastSnap = '';
let composerMode = 'ask';
let liveLog = null;        // persistent live-stage event log (survives thread rebuilds)
let chatLive = null;       // streaming reply bubble (survives conversation rebuilds)

function resetDetail(id) {
  lastSnap = '';
  document.getElementById('d-thread').innerHTML = '<div class="lead">loading&hellip;</div>';
  liveLog = null; chatLive = null;
  stopStageStream(); stopChatStream();
  const box = document.getElementById('c-in');
  box.dataset.draft = 'gb-chat-' + id;
  box.value = localStorage.getItem('gb-chat-' + id) || '';
  setMode('ask');
}

async function loadDetail(id) {
  try {
    const r = await fetch('api/jobs/' + encodeURIComponent(id) + '/session');
    if (!r.ok) { if (r.status === 404) closeItem(); return; }
    const data = await r.json();
    const sig = JSON.stringify(data);
    if (sig === lastSnap) return;
    lastSnap = sig;
    renderDetail(data);
    if (data.live) startStageStream(id);  // feature stages AND v1 runs stream
  } catch (e) { /* keep the last frame */ }
}

function dHeader(title, status, links, sub, stage, isFeature, live) {
  document.getElementById('d-title').textContent = title;
  const st = document.getElementById('d-status');
  st.className = 'badge ' + esc(status);
  st.textContent = STATUS_LABEL[status] || status;
  const strip = document.getElementById('d-strip');
  if (isFeature) {
    let segs = '';
    const still = LIVE.includes(status) ? '' : ' still';
    for (let i = 0; i < 10; i++)
      segs += `<i class="${i < stage ? 'd' : (i === stage ? 'c' + still : '')}" title="P${i} ${esc(STAGE_NAMES[i])}"></i>`;
    strip.innerHTML = segs; strip.style.display = '';
  } else strip.style.display = 'none';
  document.getElementById('d-links').innerHTML = links;
  document.getElementById('d-sub').innerHTML = sub;
}

// capture/restore open state + focused draft across thread rebuilds
function threadState() {
  const open = new Set();
  for (const d of document.querySelectorAll('#d-thread details[data-key]')) if (d.open) open.add(d.dataset.key);
  const t = document.querySelector('#d-thread textarea[data-draft]');
  let draft = null;
  if (t) draft = { key: t.dataset.draft, val: t.value, focus: t === document.activeElement,
                   start: t.selectionStart, end: t.selectionEnd };
  return { open, draft };
}
function restoreThread(st) {
  for (const d of document.querySelectorAll('#d-thread details[data-key]'))
    if (st.open.has(d.dataset.key)) d.open = true;
  const t = document.querySelector('#d-thread textarea[data-draft]');
  if (t) {
    const v = st.draft && st.draft.key === t.dataset.draft ? st.draft.val
            : localStorage.getItem(t.dataset.draft);
    if (v) t.value = v;
    if (st.draft && st.draft.focus && st.draft.key === t.dataset.draft) {
      t.focus();
      try { t.setSelectionRange(st.draft.start, st.draft.end); } catch (e) { /* ok */ }
    }
  }
}

function stageCards(data) {
  const j = data.job;
  const by = {};
  for (const r of data.runs || []) {
    const s = by[r.stage] || (by[r.stage] = { n: 0, dur: 0, cost: 0, status: '' });
    s.n += 1; s.dur += r.duration_ms || 0; s.cost += r.cost_usd || 0;
    if (r.result_status) s.status = r.result_status;
  }
  const arts = {};
  for (const a of data.artifacts || []) {
    // artifact names encode their stage as a P<digit>- prefix (e.g. P3-design.md);
    // [0-9] keeps this file free of regex-escape backslashes
    const m = a.name.match(new RegExp('^P([0-9])-'));
    if (m) arts[Number(m[1])] = a;
  }
  const stages = Object.keys(by).map(Number).sort((a, b) => a - b);
  const cur = Number(j.stage) || 0;
  let h = '';
  for (const stg of stages) {
    const s = by[stg];
    const isLive = stg === cur && (j.status === 'running' || data.live);
    const a = arts[stg];
    const meta = isLive
      ? `running &middot; ${fmtMMSS(s.dur)} &middot; $${s.cost.toFixed(2)}`
      : `${s.n > 1 ? s.n + '&times; &middot; ' : ''}${fmtMMSS(s.dur)} &middot; $${s.cost.toFixed(2)}${s.status ? ' &middot; ' + esc(s.status) : ''}`;
    const inner = isLive
      ? `<div class="inner" id="live-slot"></div>`
      : `<div class="inner">${a ? `<div class="art">${esc(a.content || '(empty)')}${a.truncated ? '… (truncated)' : ''}</div>` : '<div class="lead" style="margin-top:8px">no artifact for this stage</div>'}${transcriptAccordions(data, stg)}</div>`;
    h += `<details class="stage${isLive ? ' live' : ''}" data-key="st-${stg}"${isLive ? ' open' : ''}>
      <summary><span class="sx">P${stg}</span><span class="snm">${esc(STAGE_NAMES[stg] || '')}</span>
      ${!isLive && s.status === 'done' ? '<span class="tick">&#10003;</span>' : ''}<span class="sm">${meta}</span></summary>
      ${inner}</details>`;
  }
  return h || '<div class="lead">no stage runs yet</div>';
}

const PR_STATE_LABEL = { draft: 'Draft', ready: 'Ready', in_review: 'In review',
  changes_requested: 'Changes requested', approved: 'Approved', merged: 'Merged', closed: 'Closed' };
const PR_STATE_CLASS = { draft: 'queued', ready: 'received', in_review: 'running',
  changes_requested: 'awaiting_input', approved: 'pr_opened', merged: 'pr_opened', closed: 'no_fix' };

function prSection(data) {
  const prs = data.prs || [];
  if (!prs.length) return '';
  const rows = prs.map(p => {
    const name = p.repo && p.number ? esc(p.repo) + '#' + esc(String(p.number)) : esc(p.url);
    const rounds = p.review_rounds > 1 ? ` &middot; round ${esc(String(p.review_rounds))}` : '';
    const note = p.detail ? ` &middot; ${esc(String(p.detail).slice(0, 120))}` : '';
    return `<li><span class="badge ${esc(PR_STATE_CLASS[p.state] || 'queued')}">${esc(PR_STATE_LABEL[p.state] || p.state)}</span>
      <a href="${esc(p.url)}" target="_blank">${name}</a><span class="meta">${rounds}${note}</span></li>`;
  }).join('');
  return `<div class="daydiv">&mdash; pull requests &mdash;</div><ul class="prlist">${rows}</ul>`;
}

function convoThread(data) {
  // one chronological timeline: chat turns AND steers (from the guidance log)
  // merged and sorted by their `at` timestamps — never chat-then-steers.
  const items = [];
  for (const t of data.chat || []) items.push({ at: t.at || 0, kind: 'chat', t });
  for (const g of data.guidance || [])
    if (g.action === 'steer') items.push({ at: g.at || 0, kind: 'steer', g });
  items.sort((a, b) => a.at - b.at);
  let h = '';
  for (const it of items) {
    if (it.kind === 'steer') {
      const g = it.g;
      h += `<div class="turn human steer"><div class="b">&#8618; Steer: ${esc(g.text)}</div><div class="c">steer &middot; P${esc(String(g.stage))}</div></div>`;
      continue;
    }
    const t = it.t;
    const human = t.role === 'human';
    let meta = human ? 'you' : 'engine';
    meta += ' · P' + t.stage;
    if (t.lane === 'fast') meta += ' ⚡';
    if (t.degraded) meta += ' (from documents only)';
    if (t.cost_usd != null) meta += ' $' + Number(t.cost_usd).toFixed(2);
    h += `<div class="turn ${human ? 'human' : 'engine'}"><div class="b">${esc(t.text)}</div><div class="c">${esc(meta)}</div></div>`;
  }
  if (data.chat_pending) h += '<div class="turn wait" id="convo-wait"><span class="spin"></span>answering&hellip;</div>';
  return h;
}

function gatePacket(data) {
  const j = data.job;
  const feature = j.kind === 'feature';
  if (j.status !== 'awaiting_input' && j.status !== 'error' && j.status !== 'timeout') return '';
  const id = esc(j.issue_id);
  // role-exclusive gates (Epic A3): the server enforces; the UI mirrors it —
  // non-owners see disabled buttons, admins get an explicit audited override
  const owner = data.gate_owner;
  const locked = !!(feature && owner && owner.enforce && !owner.is_you);
  const isAdmin = ME && ME.role === 'admin';
  const dis = locked ? ` disabled title="Owned by ${esc(owner.display)} — only they (or an admin override) can answer"` : '';
  const ownerLine = feature && owner && owner.enforce
    ? `<div class="g-owner">Owned by <b>${esc(owner.display)}</b> &middot; ${esc(owner.role)} gate${owner.is_you ? ' &mdash; you' : ''}</div>` : '';
  const ovrBox = locked && isAdmin
    ? `<label class="ovr"><input type="checkbox" id="ovr-${id}" onchange="armOverride(this.checked)"> answer anyway (admin override &mdash; audited)</label>` : '';
  if (j.status !== 'awaiting_input') {
    // re-kick (redo) exists only for feature pipelines
    if (!feature) return `<div class="gate"><div class="g-h">${esc(j.status)}</div>
      <div class="q">${esc((j.detail || '(no detail)').slice(0, 600))}</div></div>`;
    return `<div class="gate"><div class="g-h">Stage ${j.status} — re-kick</div>
      ${ownerLine}<div class="answer"><div class="btns">
      <button class="redo" onclick="answer('${id}','redo',this)"${dis}>Re-kick (redo)</button></div>${ovrBox}</div></div>`;
  }
  const watch = j.kind === 'watch';
  const isAsk = feature && j.gate_kind === 'ask';
  const goLabel = isAsk ? 'Answer' : 'Proceed';
  const head = isAsk ? 'The engine asks — resumes in place'
    : (feature ? 'Gate — P' + j.stage + ' ' + esc(j.stage_name || '')
      : (watch ? 'Iterate gate — outcome verdict' : 'Needs your input'));
  const btns = (feature || watch)
    ? `<button class="go" onclick="answer('${id}','proceed',this)"${dis}>${goLabel}</button>
       <button class="redo" onclick="answer('${id}','redo',this)"${dis}>Redo</button>
       <button class="no" onclick="answer('${id}','skip',this)"${dis}>Skip</button>`
    : `<button class="go" onclick="answer('${id}','proceed',this)">Proceed</button>
       <button class="no" onclick="answer('${id}','skip',this)">Skip</button>`;
  const hint = feature
    ? `<div class="hint">Redo re-runs this stage with your notes as corrections &mdash; optionally prefix 'P3 ' to redo an earlier stage.</div>`
    : (watch
      ? `<div class="hint">Proceed = log the learning &amp; close &middot; Redo &lt;days&gt; = watch again &middot; Skip = close without a learning.</div>`
      : '');
  return `<div class="gate"><div class="g-h">${head}</div>
    ${ownerLine}
    <div class="q">${esc(j.question || (j.detail || '(see analysis)').slice(0, 500))}</div>
    ${j.evidence ? `<details class="sub" data-key="g-ev"><summary>evidence</summary><pre>${linkify(j.evidence)}</pre></details>` : ''}
    ${j.analysis ? `<details class="sub" data-key="g-an"><summary>full analysis</summary><pre>${esc(j.analysis)}</pre></details>` : ''}
    <div class="answer"><textarea id="ans-${id}" data-draft="gb-draft-${id}" placeholder="Your answer / guidance&hellip;"></textarea>
    ${hint}<div class="btns">${btns}</div>${ovrBox}</div></div>`;
}

// arm/disarm the gate buttons when the admin toggles the override checkbox
function armOverride(on) {
  for (const b of document.querySelectorAll('#d-thread .gate .btns button')) b.disabled = !on;
}

function verdictCard(data) {
  // outcome loop (Epic B5): the measured verdict on watch + feature panes
  const o = data.outcome;
  const j = data.job;
  if (!o && !(j.kind === 'watch' || j.success_metric || j.metric_event)) return '';
  let inner = '';
  if (o) {
    inner = `<span class="vchip v-${esc(o.verdict || 'unmeasured')}">${esc(o.verdict || 'unmeasured')}</span>
      <span class="vc-m">${esc(o.metric || '(no metric)')}${o.target ? ' &middot; target ' + esc(o.target) : ''}
      &middot; observed ${o.observed == null ? '&mdash;' : esc(String(o.observed))}
      ${o.baseline == null ? '' : '&middot; baseline ' + esc(String(o.baseline))}
      &middot; ${esc(String(o.window_days || '?'))}d window</span>`
      + (o.learning ? `<div class="vc-l">Learning: ${esc(o.learning)}</div>` : '');
  } else {
    const dl = j.watch_deadline ? new Date(j.watch_deadline * 1000).toLocaleDateString() : '';
    inner = `<span class="vc-m">Metric: ${esc(j.success_metric || j.metric_event || '(none)')}`
      + (j.metric_target ? ' &middot; target ' + esc(j.metric_target) : '')
      + (j.metric_window_days ? ' &middot; ' + esc(String(j.metric_window_days)) + 'd window' : '')
      + (j.kind === 'watch' && dl ? ' &middot; verdict due ' + esc(dl) : '') + '</span>';
  }
  const reads = (data.readings || []).filter((r) => r.window_day != null);
  const readRow = reads.length
    ? `<div class="vc-r">${reads.slice(-14).map((r) =>
        `<span title="day ${esc(String(r.window_day))}">d${esc(String(r.window_day))}: ${r.observed == null ? '?' : esc(String(r.observed))}</span>`).join(' ')}</div>`
    : '';
  return `<div class="daydiv">&mdash; outcome &mdash;</div><div class="stage"><div class="inner vcard" style="border-top:0">${inner}${readRow}</div></div>`;
}

function renderDetail(data) {
  const j = data.job;
  const feature = j.kind === 'feature';
  let links = '';
  if (j.issue_url) links += `<a href="${esc(j.issue_url)}" target="_blank">Sentry</a>`;
  if (j.clickup_task_url) links += `<a href="${esc(j.clickup_task_url)}" target="_blank">ClickUp</a>`;
  if (j.pr_url) links += `<a href="${esc(j.pr_url)}" target="_blank">PR</a>`;
  let sub;
  if (feature) {
    sub = `P${j.stage} ${esc(j.stage_name || '')} &middot; ${esc(j.project)} &middot; ${esc(j.gate_mode)} gates`
      + `<span style="margin-left:auto"><details class="sub" style="margin:0" data-key="d-stats"><summary>stats</summary></details></span>`;
  } else {
    const score = j.score != null ? ` &middot; score ${esc(String(j.score))}` : '';
    const phase = j.kind === 'task' && j.phase > 1 ? ' &middot; phase 2' : '';
    sub = `${esc(KIND_LABEL[j.kind] || j.kind)} &middot; ${esc(j.project)}${score}${phase}`;
  }
  dHeader(j.title, j.status, links, sub, Number(j.stage) || 0, feature, data.live);

  const st = threadState();
  const pipeline = feature
    ? '<div class="daydiv">&mdash; pipeline &mdash;</div>' + stageCards(data)
    : '';
  // v1 items have no stage cards — the live run gets its own activity box
  // (the feature live-slot lives inside the current stage card instead), and
  // past runs replay from their transcripts (§13)
  const pastRuns = !feature ? transcriptAccordions(data, null) : '';
  const activity = (!feature && (data.live || pastRuns))
    ? '<div class="daydiv">&mdash; activity &mdash;</div>'
      + (data.live ? '<div class="stage live"><div class="inner" id="live-slot" style="border-top:0"></div></div>' : '')
      + (pastRuns ? `<div class="stage"><div class="inner" style="border-top:0">${pastRuns}</div></div>` : '')
    : '';
  const convo = data.chat_available
    ? '<div class="daydiv">&mdash; conversation &mdash;</div>'
      + (convoThread(data) || '<div class="lead">no messages yet &mdash; ask the engine anything about this work</div>')
    : '';
  document.getElementById('d-thread').innerHTML = pipeline + activity + prSection(data) + verdictCard(data) + convo + gatePacket(data);
  restoreThread(st);
  wireTranscripts(j.issue_id);

  // re-seat the persistent live log + streaming chat bubble after the rebuild
  const slot = document.getElementById('live-slot');
  if (slot) {
    if (!liveLog) {
      liveLog = document.createElement('div');
      liveLog.innerHTML = '<div class="lead" style="margin-top:8px">watching for activity&hellip;</div>';
    }
    slot.appendChild(liveLog);
  }
  if (chatLive) {
    const w = document.getElementById('convo-wait');
    if (w) w.replaceWith(chatLive); else {
      const gate = document.querySelector('#d-thread .gate');
      if (gate) gate.before(chatLive); else document.getElementById('d-thread').appendChild(chatLive);
    }
  }

  // stats loader (lazy, on toggle — reuses the features stats endpoint)
  const sd = document.querySelector('#d-sub details');
  if (sd && !sd.dataset.wired) {
    sd.dataset.wired = '1';
    sd.addEventListener('toggle', () => { if (sd.open) loadHeaderStats(j.issue_id, sd); });
  }

  // composer state — the backend's flags are the single source of truth:
  // chat_available gates the whole composer (kind-based); steer_available
  // gates the Steer tab (feature + running only — the answer box is the
  // correction channel at gates, and v1 items have no resumable runs).
  document.getElementById('composer').style.display = data.chat_available ? '' : 'none';
  const steerTab = document.getElementById('tab-steer');
  steerTab.style.display = feature ? '' : 'none';
  steerTab.disabled = !data.steer_available;
  if ((steerTab.disabled || !feature) && composerMode === 'steer') setMode('ask');
  updateComposerHint(data);
  const send = document.getElementById('c-send');
  send.disabled = composerMode === 'ask' && (data.chat_limit || data.chat_pending);
}

async function loadHeaderStats(id, det) {
  if (det.querySelector('.stats-body')) { return; }
  const body = document.createElement('div');
  body.className = 'stats-body';
  body.innerHTML = '<div class="empty">loading&hellip;</div>';
  det.appendChild(body);
  try {
    const r = await fetch('api/features/' + encodeURIComponent(id) + '/stats');
    const data = await r.json();
    body.innerHTML = r.ok ? statsTable(data) : '<div class="empty">Error: ' + esc(data.detail || r.status) + '</div>';
  } catch (e) { body.innerHTML = '<div class="empty">Error: ' + esc(String(e)) + '</div>'; }
}

function statsTable(data) {
  const runs = data.runs || [];
  if (!runs.length) return '<div class="empty">no stage runs yet</div>';
  const autoStages = new Set();
  for (const g of data.guidance || []) if (g.action === 'auto') autoStages.add(Number(g.stage));
  const by = {};
  for (const r of runs) {
    const s = by[r.stage] || (by[r.stage] = { n: 0, dur: 0, cost: 0, wait: 0, status: '', gate: '' });
    s.n += 1; s.dur += r.duration_ms || 0; s.cost += r.cost_usd || 0;
    if (r.gate_posted_at && r.gate_answered_at) s.wait += Math.max(0, r.gate_answered_at - r.gate_posted_at);
    if (r.result_status) s.status = r.result_status;
    s.gate = r.gate_action || '';
  }
  const rows = Object.keys(by).map(Number).sort((a, b) => a - b).map(st => {
    const s = by[st];
    const res = !s.gate && autoStages.has(st)
      ? '<span class="muted-t" title="auto-advanced (light gate mode)">auto</span>' : esc(s.status);
    return `<tr><td>P${st} ${esc(STAGE_NAMES[st] || '')}</td><td>${s.n}</td><td>${fmtMMSS(s.dur)}</td>
      <td>$${s.cost.toFixed(2)}</td><td>${fmtWait(s.wait)}</td><td>${res}</td></tr>`;
  }).join('');
  return `<table><tr><th>stage</th><th>attempts</th><th>duration</th><th>cost</th><th>gate wait</th><th>result</th></tr>${rows}</table>`;
}

// (non-feature items render through renderDetail too — the session snapshot
// serves all kinds; chat_available / steer_available flags shape the composer)

// ---------- run transcripts (§13): replayable Activity accordions ----------
// Every run records its status/delta events write-through on the server; these
// accordions replay them after the live stream is gone — the fix for "the chat
// only shows messages, not what the system actually did".

function transcriptAccordions(data, stg) {
  const list = (data.transcripts || []).filter(t => {
    const h = t.header || {};
    return stg == null ? h.kind !== 'stage' : Number(h.stage) === stg;
  });
  return list.map(t => {
    const h = t.header || {};
    const label = h.kind === 'stage'
      ? 'activity &middot; attempt ' + esc(String(h.attempt || '?'))
      : 'activity &middot; ' + (Number(h.phase) > 1 ? 'implementation run' : 'analysis run');
    const when = t.mtime ? ' &middot; ' + esc(fmtAgo(t.mtime)) : '';
    return `<details class="sub" data-key="tr-${esc(t.key)}" data-tr="${esc(t.key)}">
      <summary>${label}${when}</summary><div class="tr-body"></div></details>`;
  }).join('');
}

function wireTranscripts(jobId) {
  for (const d of document.querySelectorAll('#d-thread details[data-tr]')) {
    if (d.dataset.wired) continue;
    d.dataset.wired = '1';
    d.addEventListener('toggle', () => { if (d.open) loadTranscript(jobId, d); });
    if (d.open) loadTranscript(jobId, d);  // reopened by restoreThread across rebuilds
  }
}

async function loadTranscript(jobId, det) {
  const body = det.querySelector('.tr-body');
  if (!body || body.dataset.loaded) return;
  body.dataset.loaded = '1';
  body.innerHTML = '<div class="empty">loading&hellip;</div>';
  try {
    const r = await fetch('api/jobs/' + encodeURIComponent(jobId)
      + '/transcripts/' + encodeURIComponent(det.dataset.tr));
    const data = await r.json();
    body.innerHTML = r.ok
      ? (renderTranscript(data.events || []) || '<div class="empty">no recorded activity</div>')
      : '<div class="empty">Error: ' + esc(data.detail || r.status) + '</div>';
  } catch (e) {
    body.dataset.loaded = '';  // transient — retry on the next toggle
    body.innerHTML = '<div class="empty">Error: ' + esc(String(e)) + '</div>';
  }
}

function renderTranscript(events) {
  // consecutive deltas merge into one text block, mirroring the live log
  let h = '', text = '';
  const flush = () => { if (text.trim()) h += `<div class="ev text">${esc(text)}</div>`; text = ''; };
  for (const ev of events) {
    if (ev.e === 'delta') { text += ev.d || ''; continue; }
    flush();
    if (ev.e === 'status') h += `<div class="ev status">${esc(ev.d || '')}</div>`;
    else if (ev.e === 'end') h += `<div class="ev done">run finished &middot; ${esc(ev.d || '')}</div>`;
    else if (ev.e === 'truncated') h += `<div class="ev status">${esc(ev.d || '(truncated)')}</div>`;
  }
  flush();
  return h;
}

// ---------- live stage stream (api/jobs/{id}/session/stream) ----------

let stageES = null;

function startStageStream(id) {
  if (stageES && stageES._job === id) return;
  stopStageStream();
  if (typeof EventSource === 'undefined') return;
  const es = new EventSource('api/jobs/' + encodeURIComponent(id) + '/session/stream');
  es._job = id;
  stageES = es;
  es.addEventListener('status', (e) => stageEv('status', sseData(e)));
  es.addEventListener('delta', (e) => stageEv('delta', sseData(e)));
  es.addEventListener('done', () => { stageEv('done', ''); stopStageStream(); loadDetail(id); });
  es.onerror = () => { /* the 5s snapshot poll keeps the frame fresh */ };
}
function stopStageStream() {
  if (stageES) { try { stageES.close(); } catch (e) { /* closed */ } stageES = null; }
}
function sseData(e) { try { return JSON.parse(e.data).t || ''; } catch (x) { return ''; } }

let liveText = null;
function stageEv(kind, t) {
  if (!liveLog) return;
  const lead = liveLog.querySelector('.lead');
  if (lead && kind !== 'done') lead.remove();
  if (kind === 'status' && t) {
    liveText = null;
    const d = document.createElement('div'); d.className = 'ev status'; d.textContent = t;
    liveLog.appendChild(d);
  } else if (kind === 'delta' && t) {
    if (!liveText) { liveText = document.createElement('div'); liveText.className = 'ev text'; liveLog.appendChild(liveText); }
    liveText.textContent += t;
  } else if (kind === 'done') {
    liveText = null;
    const d = document.createElement('div'); d.className = 'ev done'; d.textContent = 'run finished';
    liveLog.appendChild(d);
  }
  const th = document.getElementById('d-thread');
  if (th) th.scrollTop = th.scrollHeight;
}

// ---------- composer: Ask (chat) / Steer ----------

function composerError(text) {
  document.getElementById('c-err').textContent = text || '';
  document.getElementById('c-hint').style.display = text ? 'none' : '';
}

function setMode(m) {
  composerMode = m;
  composerError('');
  document.getElementById('tab-ask').classList.toggle('on', m === 'ask');
  document.getElementById('tab-steer').classList.toggle('on', m === 'steer');
  const box = document.getElementById('c-in');
  box.placeholder = m === 'ask'
    ? 'Ask about this work — answers read-only from the latest artifacts + repo…'
    : 'Steer the run — describe the course correction…';
  document.getElementById('c-send').className = 'send' + (m === 'steer' ? ' steer' : '');
  document.getElementById('c-send').textContent = m === 'steer' ? 'Steer' : 'Send';
  updateComposerHint(null);
}

function updateComposerHint(data) {
  const el = document.getElementById('c-hint');
  if (composerMode === 'steer') {
    el.innerHTML = 'Interrupts the running stage and resumes it with your note (or queues to the next checkpoint).';
  } else if (data && data.chat_limit) {
    el.innerHTML = 'Chat limit reached for this gate — answer with Proceed / Redo / Skip.';
  } else if (data && data.chat_pending) {
    el.innerHTML = 'An answer is in flight&hellip;';
  } else {
    el.innerHTML = 'Ask anytime — mid-run, at a gate, or after the work lands. Switch to <b>Steer</b> to redirect a running stage.';
  }
}

async function sendComposer() {
  if (!sel) return;
  const box = document.getElementById('c-in');
  const text = (box.value || '').trim();
  if (!text) return;
  const btn = document.getElementById('c-send');
  btn.disabled = true;
  composerError('');
  const msg = document.getElementById('msg');
  try {
    if (composerMode === 'steer') {
      const r = await fetch('api/jobs/' + encodeURIComponent(sel) + '/session/steer', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({note: text}),
      });
      let data = {}; try { data = await r.json(); } catch (e2) { /* non-JSON */ }
      if (r.status === 202) {
        box.value = ''; localStorage.removeItem(box.dataset.draft);
        msg.textContent = data.status === 'interrupting'
          ? 'Steering — the run is being interrupted and will resume with your note.'
          : 'Steer saved — it will be applied at the next checkpoint.';
        lastSnap = ''; loadDetail(sel);
      } else composerError(data.detail || ('steer failed (' + r.status + ')'));
    } else {
      const r = await fetch('api/jobs/' + encodeURIComponent(sel) + '/chat', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({message: text}),
      });
      let data = {}; try { data = await r.json(); } catch (e2) { /* non-JSON */ }
      if (r.status === 202) {
        box.value = ''; localStorage.removeItem(box.dataset.draft);
        startChatStream(sel);
        lastSnap = ''; loadDetail(sel);
      } else {
        composerError(data.detail || ('send failed (' + r.status + ')'));
        if (r.status === 409) { lastSnap = ''; loadDetail(sel); }
      }
    }
  } catch (e) { composerError('network error — not sent: ' + e); }
  btn.disabled = false;
}

// live answer stream (api/jobs/{id}/chat/stream, SSE) — pure UX; the 5s
// snapshot poll still delivers the persisted reply if the stream dies.
let chatES = null;

function startChatStream(id) {
  stopChatStream();
  if (typeof EventSource === 'undefined') return;
  chatLive = document.createElement('div');
  chatLive.className = 'turn engine';
  chatLive.innerHTML = '<div class="b"></div><div class="c"><span class="spin"></span>engine &middot; thinking&hellip;</div>';
  const gate = document.querySelector('#d-thread .gate');
  if (gate) gate.before(chatLive); else document.getElementById('d-thread').appendChild(chatLive);
  const es = new EventSource('api/jobs/' + encodeURIComponent(id) + '/chat/stream');
  chatES = es;
  es.addEventListener('delta', (e) => {
    const t = sseData(e); if (!t) return;
    chatLive.querySelector('.b').textContent += t;
    chatLive.querySelector('.c').innerHTML = 'engine &middot; answering&hellip;';
    const th = document.getElementById('d-thread'); th.scrollTop = th.scrollHeight;
  });
  es.addEventListener('status', (e) => {
    const t = sseData(e); if (!t) return;
    chatLive.querySelector('.c').textContent = 'engine · ' + t;
  });
  es.addEventListener('done', () => { stopChatStream(); if (sel) { lastSnap = ''; loadDetail(sel); } });
  es.onerror = () => { stopChatStream(); };
}
function stopChatStream() {
  if (chatES) { try { chatES.close(); } catch (e) { /* closed */ } chatES = null; }
  if (chatLive && chatLive.parentNode) chatLive.parentNode.removeChild(chatLive);
  chatLive = null;
}

// ---------- gate answers (proceed / redo / skip + error re-kick) ----------

async function answer(id, action, btn) {
  const msg = document.getElementById('msg');
  const box = document.getElementById('ans-' + id);
  const text = box ? box.value.trim() : '';
  if (action === 'skip' && !confirm('Abort this job? (feature branches are left intact)')) return;
  const ovr = document.getElementById('ovr-' + id);
  btn.disabled = true; msg.textContent = 'Recording decision…';
  try {
    const r = await fetch('api/jobs/' + encodeURIComponent(id) + '/answer', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action, answer: text, override: !!(ovr && ovr.checked)}),
    });
    const data = await r.json();
    if (r.ok) {
      localStorage.removeItem('gb-draft-' + id);
      msg.textContent = { proceed: 'Answer recorded — next run queued.',
                          redo: 'Redo queued.', skip: 'Skipped.' }[action] || ('OK: ' + data.status);
      lastSnap = ''; refresh(true); if (sel) loadDetail(sel);
    } else {
      msg.textContent = 'Error: ' + (data.detail || r.status);
      if (r.status === 409) { lastSnap = ''; refresh(true); if (sel) loadDetail(sel); }
    }
  } catch (e) { msg.textContent = 'Error: ' + e; }
  btn.disabled = false;
}

// persist drafts as they are typed (delegated: nodes are re-created on render)
document.addEventListener('input', (e) => {
  const t = e.target;
  if (t && t.matches && t.matches('textarea[data-draft]') && t.dataset.draft)
    localStorage.setItem(t.dataset.draft, t.value);
});

// ---------- Product brain (api/memory) ----------

let lastMem = '';
let lastMemData = null;
const pendingBoot = new Set();

async function refreshMemory(force) {
  try {
    const mem = await (await fetch('api/memory')).json();
    const sig = JSON.stringify(mem);
    if (force || sig !== lastMem) { lastMem = sig; lastMemData = mem; renderMemory(mem); }
  } catch (e) { /* keep the previous table */ }
}

function renderMemory(mem) {
  const rows = Object.entries(mem).map(([p, m]) => {
    const exists = m.exists ? '<span class="ok-t">&#10003;</span>' : '<span class="err-t">&#10007;</span>';
    const stale = !m.exists ? '&mdash;'
      : (m.staleness_commits == null ? '?'
        : (m.staleness_commits > 0
          ? esc(String(m.staleness_commits)) + (m.staleness_commits === 1 ? ' commit behind' : ' commits behind')
          : 'fresh'));
    const fetched = m.fetched_at ? new Date(m.fetched_at * 1000).toLocaleString() : '&mdash;';
    const dis = pendingBoot.has(p) ? ' disabled' : '';
    return `<tr><td>${esc(p)}</td><td>${exists}</td><td>${stale}</td><td>${fetched}</td>
      <td><button onclick="bootstrap('${esc(p)}',this)"${dis} title="Bootstrap engine memory via a draft PR">Bootstrap</button></td></tr>`;
  }).join('');
  document.getElementById('brain-body').innerHTML = rows
    ? `<table><tr><th>project</th><th>memory</th><th>staleness</th><th>fetched</th><th></th></tr>${rows}</table>`
    : '<div class="empty">no projects configured</div>';
}

async function bootstrap(project, btn) {
  const msg = document.getElementById('msg');
  btn.disabled = true; pendingBoot.add(project);
  msg.textContent = 'Bootstrapping memory for ' + project + '…';
  try {
    const r = await fetch('api/memory/' + encodeURIComponent(project) + '/bootstrap', { method: 'POST' });
    const data = await r.json();
    msg.textContent = r.ok ? 'memory ' + project + ': ' + (data.decision || 'queued')
                           : 'Error: ' + (data.detail || r.status);
    if (r.ok) refresh(true);
  } catch (e) { msg.textContent = 'Error: ' + e; }
  pendingBoot.delete(project);
  if (lastMemData) renderMemory(lastMemData); else btn.disabled = false;
}

// ---------- outcome ledger (api/outcomes, Epic B5) ----------

async function loadOutcomes() {
  try {
    const r = await fetch('api/outcomes');
    if (!r.ok) return;
    renderOutcomes(await r.json());
  } catch (e) { /* keep the previous view */ }
}

function renderOutcomes(data) {
  const el = document.getElementById('outcomes-body');
  if (!el) return;
  const v = data.verdicts || {};
  const chips = ['moved', 'flat', 'regressed', 'unmeasured'].map(
    (k) => `<span class="vchip v-${k}">${esc(k)} ${Number(v[k] || 0)}</span>`).join(' ');
  const rows = (data.outcomes || []).map((o) => {
    const when = o.decided_at || o.created_at;
    return `<tr>
      <td><a href="#/job/${esc(o.feature_id)}">${esc(o.feature_id || '?')}</a></td>
      <td>${esc(o.metric || '')}</td>
      <td>${esc(o.target || '')}</td>
      <td>${o.observed == null ? '&mdash;' : esc(String(o.observed))}</td>
      <td><span class="vchip v-${esc(o.verdict || 'unmeasured')}">${esc(o.verdict || 'unmeasured')}</span></td>
      <td>${esc((o.learning || '').slice(0, 120))}</td>
      <td>${esc(o.decided_by || '')}${when ? ' &middot; ' + fmtAgo(when) : ''}</td>
    </tr>`;
  }).join('');
  el.innerHTML = `<div class="vchips" style="margin-top:10px">${chips}</div>`
    + (rows
      ? `<table><tr><th>feature</th><th>metric</th><th>target</th><th>observed</th>
         <th>verdict</th><th>learning</th><th>decided</th></tr>${rows}</table>`
      : '<div class="empty">no measured outcomes yet &mdash; ship a feature with a success metric</div>');
}

// ---------- project context (api/context) ----------

let ctxData = null;

// esc() leaves quotes alone (it round-trips through textContent), so values
// interpolated into value="…" attributes need the quote entity too
function attr(s) { return esc(s).replace(new RegExp(String.fromCharCode(34), 'g'), '&quot;'); }

async function loadContext() {
  try {
    const r = await fetch('api/context');
    if (!r.ok) return;
    ctxData = await r.json();
    renderContext();
  } catch (e) { /* keep the previous form */ }
}

function ctxRepoRow(slug, e) {
  e = e || {};
  return `<div class="ctx-row">
    <input class="cr-slug" placeholder="project slug" value="${attr(slug || '')}">
    <input class="cr-repo" placeholder="owner/name" value="${attr(e.repo || '')}">
    <input class="cr-base" placeholder="base branch" value="${attr(e.base || '')}">
    <input class="cr-setup" placeholder="setup cmd (optional)" value="${attr(e.setup_cmd || '')}">
    <input class="cr-test" placeholder="test cmd (optional)" value="${attr(e.test_cmd || '')}">
    <input class="cr-allow" placeholder="extra allowed tools, comma-sep" value="${attr((e.allow || []).join(', '))}">
    <button type="button" onclick="this.parentNode.remove()" title="Remove this repo">&#10007;</button>
  </div>`;
}

function renderContext() {
  const c = ctxData.context;
  const over = ctxData.overridden || [];
  const note = over.length
    ? 'Customized (' + esc(over.join(', ')) + ') &mdash; saved in the engine DB, survives restarts.'
    : 'Running on the built-in defaults.';
  document.getElementById('ctx-body').innerHTML = `
    <div class="hint" style="margin-top:10px">${note} The BUSINESS layer of every run's briefing: who the company is and how it works. Repos, canonical memory repo and per-surface context live on each workspace (Settings &rarr; Workspaces).</div>
    <div class="ctx-grid">
      <div><label>Default product name</label><input id="ctx-name" value="${attr(c.product_name)}"></div>
    </div>
    <label>Business context (stacked above each workspace's context in every prompt)</label>
    <textarea id="ctx-biz" placeholder="&lt;product&gt; is &lt;one-line description&gt; for &lt;who it's for&gt;, built across these repositories:
- &lt;slug&gt; (&lt;owner/repo&gt;) — &lt;what this repo is: backend/API, web client, …&gt;
How the pieces relate (e.g. clients consume the backend's API; cross-repo features ship server-first).
Anything every automated run should know about the business or the codebase conventions.">${esc(c.business_context)}</textarea>
    <div style="display:flex; gap:8px; margin-top:12px">
      <button type="button" class="save" onclick="saveContext(this)">Save context</button>
      <button type="button" onclick="resetContext(this)">Reset to defaults</button>
    </div>`;
}

function ctxAddRow() {
  document.getElementById('ctx-repos').insertAdjacentHTML('beforeend', ctxRepoRow('', {}));
}

async function saveContext(btn) {
  const msg = document.getElementById('msg');
  // only send what actually changed: untouched fields must NOT become DB
  // overrides, or they would pin today's defaults forever (shadowing future
  // env/default improvements)
  const cur = ctxData.context;
  const body = {};
  const name = document.getElementById('ctx-name').value.trim();
  if (name !== cur.product_name) body.product_name = name;
  const biz = document.getElementById('ctx-biz').value;
  if (biz !== cur.business_context) body.business_context = biz;
  if (!Object.keys(body).length) { msg.textContent = 'No changes to save.'; return; }
  btn.disabled = true; msg.textContent = 'Saving project context…';
  try {
    const r = await fetch('api/context', {
      method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    const data = await r.json();
    if (r.ok) {
      ctxData = data; renderContext();
      msg.textContent = 'Project context saved — new runs use it immediately.'
        + (data.warning ? ' ' + data.warning : '');
      loadProjects(); refreshMemory(true);
    } else msg.textContent = 'Error: ' + (data.detail || r.status);
  } catch (e) { msg.textContent = 'Error: ' + e; }
  btn.disabled = false;
}

async function resetContext(btn) {
  if (!confirm('Reset the project context to the built-in defaults? Saved customizations are removed.')) return;
  const msg = document.getElementById('msg');
  btn.disabled = true;
  try {
    const r = await fetch('api/context', { method: 'DELETE' });
    const data = await r.json();
    if (r.ok) {
      ctxData = data; renderContext();
      msg.textContent = 'Project context reset to defaults.'
        + (data.warning ? ' ' + data.warning : '');
      loadProjects(); refreshMemory(true);
    } else msg.textContent = 'Error: ' + (data.detail || r.status);
  } catch (e) { msg.textContent = 'Error: ' + e; }
  btn.disabled = false;
}

// ---------- intake forms ----------

async function loadProjects() {
  try {
    const ps = await (await fetch('api/projects')).json();
    window.PROJECTS_CACHE = ps;
    renderProjectOptions(ps);
  } catch (e) { /* keep empty selects; submit will fail loudly */ }
}

function renderProjectOptions(ps) {
  const scoped = (activeWs && WORKSPACES.length > 1)
    ? ps.filter((p) => p.workspace_id === activeWs) : ps;
  const opts = '<option value="" disabled selected>Project&hellip;</option>' +
    scoped.map(p => `<option value="${esc(p.slug)}">${esc(p.slug)} (${esc(p.repo)})</option>`).join('');
  document.getElementById('task-project').innerHTML = opts;
  document.getElementById('feat-project').innerHTML = opts;
}

async function trigger(ev) {
  ev.preventDefault();
  const btn = document.getElementById('go'), msg = document.getElementById('msg');
  btn.disabled = true; msg.textContent = 'Grading + creating ticket…';
  try {
    const r = await fetch('api/trigger', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({issue: document.getElementById('ref').value.trim()}),
    });
    const data = await r.json();
    msg.innerHTML = r.ok
      ? `Queued <b>${esc(data.title)}</b>` + (data.clickup_task_url ? ` &mdash; <a href="${esc(data.clickup_task_url)}" target="_blank">ClickUp ticket</a>` : '')
      : 'Error: ' + esc(data.detail || r.status);
    if (r.ok) { document.getElementById('ref').value = ''; refresh(true); }
  } catch (e) { msg.textContent = 'Error: ' + e; }
  btn.disabled = false;
  return false;
}

async function submitTask(ev) {
  ev.preventDefault();
  const btn = document.getElementById('task-go'), msg = document.getElementById('msg');
  const body = {
    project: document.getElementById('task-project').value,
    clickup: document.getElementById('task-clickup').value.trim(),
    title: document.getElementById('task-title').value.trim(),
    summary: document.getElementById('task-summary').value.trim(),
  };
  if (!body.project) { msg.textContent = 'Pick a project first.'; return false; }
  if (!body.clickup && !body.title) { msg.textContent = 'Give a ClickUp URL or a title.'; return false; }
  btn.disabled = true; msg.textContent = 'Creating ticket + queueing analysis…';
  try {
    const r = await fetch('api/tasks', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await r.json();
    msg.innerHTML = r.ok
      ? `Queued <b>${esc(data.title)}</b>` + (data.clickup_task_url ? ` &mdash; <a href="${esc(data.clickup_task_url)}" target="_blank">ClickUp ticket</a>` : '')
      : 'Error: ' + esc(data.detail || r.status);
    if (r.ok) {
      for (const id of ['task-clickup', 'task-title', 'task-summary']) document.getElementById(id).value = '';
      refresh(true);
    }
  } catch (e) { msg.textContent = 'Error: ' + e; }
  btn.disabled = false;
  return false;
}

async function submitFeature(ev) {
  ev.preventDefault();
  const btn = document.getElementById('feat-go'), msg = document.getElementById('msg');
  const body = {
    project: document.getElementById('feat-project').value,
    clickup: document.getElementById('feat-clickup').value.trim(),
    title: document.getElementById('feat-title').value.trim(),
    summary: document.getElementById('feat-summary').value.trim(),
    founder_dri: document.getElementById('feat-founder-dri').value.trim(),
    dev_dri: document.getElementById('feat-dev-dri').value.trim(),
    gate_mode: document.getElementById('feat-gatemode').value,
    related_to: document.getElementById('feat-related').value.trim(),
    success_metric: document.getElementById('feat-metric').value.trim(),
    metric_target: document.getElementById('feat-target').value.trim(),
  };
  const windowRaw = document.getElementById('feat-window').value.trim();
  if (windowRaw) {
    const w = Number(windowRaw);
    if (!Number.isInteger(w) || w < 1 || w > 365) {
      msg.textContent = 'Measurement window must be a whole number of days, 1-365.';
      return false;
    }
    body.metric_window_days = w;
  }
  if (!body.project) { msg.textContent = 'Pick a project first.'; return false; }
  if (!body.clickup && !body.title) { msg.textContent = 'Give a ClickUp URL or a title.'; return false; }
  btn.disabled = true; msg.textContent = 'Creating ticket + starting P0 Intake…';
  try {
    const r = await fetch('api/features', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await r.json();
    msg.innerHTML = r.ok
      ? `Pipeline started for <b>${esc(data.title)}</b>` + (data.clickup_task_url ? ` &mdash; <a href="${esc(data.clickup_task_url)}" target="_blank">ClickUp ticket</a>` : '')
      : 'Error: ' + esc(data.detail || r.status);
    if (r.ok) {
      for (const id of ['feat-clickup', 'feat-title', 'feat-summary',
                        'feat-founder-dri', 'feat-dev-dri', 'feat-related',
                        'feat-metric', 'feat-target', 'feat-window'])
        document.getElementById(id).value = '';
      refresh(true);
    }
  } catch (e) { msg.textContent = 'Error: ' + e; }
  btn.disabled = false;
  return false;
}

// ---------- keyboard + init ----------

document.addEventListener('keydown', (e) => {
  // Enter sends (chat convention); Shift+Enter makes a newline
  if (e.key === 'Enter' && !e.shiftKey && document.activeElement
      && document.activeElement.id === 'c-in') { e.preventDefault(); sendComposer(); }
  else if (e.key === 'Escape' && sel
      && (!document.activeElement || document.activeElement.id !== 'c-in')) closeItem();
});
window.addEventListener('hashchange', routeHash);

loadProjects(); refresh(true); refreshMemory(true); loadContext(); loadOutcomes(); routeHash();
setInterval(() => refresh(false), 10000);
setInterval(() => refreshMemory(false), 60000);
setInterval(() => loadOutcomes(), 60000);

// ---------- auth: who am I, sign-out, expired-session redirect ----------

async function loadMe() {
  try {
    const r = await fetch('api/me');
    if (r.status === 401) { location.href = 'login'; return; }
    if (!r.ok) return;
    ME = await r.json();
    document.getElementById('me-chip').textContent = ME.username + ' · ' + ME.role;
    if (ME.role === 'admin') {
      document.getElementById('nav-settings').style.display = '';
      document.getElementById('sp-users').style.display = '';
      document.getElementById('sp-workspaces').style.display = '';
      renderWorkspacesAdmin();
      loadSetup();
    } else {
      // configuration is admin-only: hide the Project context editor for members
      const ctx = document.querySelector('details.ctx');
      if (ctx) ctx.style.display = 'none';
    }
    if (ME.must_change_pw) openAccount();
  } catch (e) { /* transient; refresh() handles persistent 401s */ }
}

// ---------- first-run setup wizard (§14, admins only) ----------
// A checklist, not a form maze: every step auto-detects from live state and
// points at the existing UI (or env) where the work actually happens.

const SETUP_STEPS = [
  ['business_context', 'Set your product name and business context',
   'Open the Project context editor below and describe YOUR product — new runs use it immediately.'],
  ['repos', 'Point a workspace at your repositories',
   'Settings → Workspaces: replace the default repos with your own (slug, owner/repo, base branch, test command).'],
  ['github_token', 'Provide a GitHub token',
   'Set GITHUB_TOKEN in the deploy environment and restart — the engine clones and pushes with it.'],
  ['memory', 'Bootstrap product memory',
   'In the Project memory panel, Bootstrap reads a repo and opens its engine-memory PR.'],
  ['team', 'Invite your team',
   'Settings → Users: add members with temporary passwords, then assign them to workspaces.'],
];

async function loadSetup() {
  try {
    const r = await fetch('api/setup');
    if (!r.ok) return;
    const data = await r.json();
    const card = document.getElementById('setup-card');
    if (!data.needed) { card.style.display = 'none'; return; }
    card.style.display = '';
    document.getElementById('setup-steps').innerHTML = SETUP_STEPS.map(([k, title, how]) =>
      `<li class="${data.steps[k] ? 'done' : ''}"><span class="s-tick">${data.steps[k] ? '&#10003;' : '&#9675;'}</span>
       <div><div class="s-t">${esc(title)}</div><div class="s-h">${esc(how)}</div></div></li>`).join('');
  } catch (e) { /* advisory only */ }
}

async function dismissSetup() {
  try { await fetch('api/setup/dismiss', { method: 'POST' }); } catch (e) {}
  document.getElementById('setup-card').style.display = 'none';
}

async function signOut() {
  try { await fetch('api/logout', { method: 'POST' }); } catch (e) {}
  location.href = 'login';
}

// ---------- settings pane (Phase 1: Users for admins + Account) ----------

// messages must surface where the person actually is: #msg lives in the
// dashboard toolbar, which is display:none while the settings pane is open —
// writing errors there made a failed password change look like success
function uiMsg(text) {
  const sp = document.getElementById('settings-pane');
  const el = (sp && sp.style.display !== 'none')
    ? document.getElementById('sp-msg') : document.getElementById('msg');
  if (el) el.textContent = text;
}

function openSettings() {
  // forced = signed in on a temporary password. The screen must SAY so and
  // demand exactly one thing: the banner explains, every other panel hides,
  // and the back button goes with them (closeSettings refuses anyway).
  // Derived from ME and owned HERE — every entry point (Settings/Account
  // buttons, forced sign-in) goes through this function, so the state can
  // never depend on which one was clicked.
  const forced = !!(ME && ME.must_change_pw);
  document.body.classList.toggle('forced-pw', forced);
  document.getElementById('pw-banner').style.display = forced ? '' : 'none';
  document.getElementById('shell').style.display = 'none';
  document.getElementById('settings-pane').style.display = '';
  document.getElementById('sp-title').textContent = forced ? 'Set your password' : 'Settings';
  if (ME && ME.role === 'admin') { loadUsers(); renderWorkspacesAdmin(); }
}
function openAccount() {
  openSettings();
  if (ME && ME.must_change_pw) { document.getElementById('pw-cur').focus(); return; }
  // the Account panel sits below Users/Workspaces for admins — bring the
  // password form into view. The panels above populate async and shift the
  // layout, so anchor again after they settle.
  const showForm = () => {
    document.getElementById('sp-account').scrollIntoView({ block: 'start' });
    document.getElementById('pw-cur').focus();
  };
  showForm();
  setTimeout(showForm, 500);
}

function goHome() {
  // the brand is the universal way back: dismiss settings, close any open
  // item, land on the plain dashboard
  closeSettings();
  if (typeof closeItem === 'function') closeItem();
}
function closeSettings() {
  // a temporary password blocks everything else — the forced screen can't be
  // dismissed, only completed (or sign out)
  if (ME && ME.must_change_pw) return;
  document.getElementById('settings-pane').style.display = 'none';
  document.getElementById('shell').style.display = '';
  if (ME && ME.role === 'admin') loadSetup();  // refresh ticks after settings work
}

async function loadUsers() {
  try {
    const r = await fetch('api/users');
    if (!r.ok) return;
    const users = await r.json();
    const rows = users.map((u) => {
      const name = u.disabled ? `<span class="u-dis">${esc(u.username)}</span>` : esc(u.username);
      const flags = [u.disabled ? 'disabled' : '', u.must_change_pw ? 'temp pw' : '']
        .filter(Boolean).join(' · ');
      const self = ME && u.username === ME.username;
      const cu = u.clickup_user_id
        ? esc(u.clickup_user_id) : '<span class="hint" style="margin:0">not linked</span>';
      // no self-reset: the admin reset ARMS the temp-pw flag (it hands out a
      // new temporary credential) — resetting yourself just loops the forced
      // change forever. Your own password changes live in Account below.
      return `<tr><td>${name}</td><td>${esc(u.role)}</td><td>${cu}</td><td>${esc(flags)}</td><td>
        <button onclick="linkClickUp('${esc(u.username)}')">${u.clickup_user_id ? 'Edit' : 'Link'} ClickUp id</button>
        ${self ? '<span class="hint" style="margin:0">you &mdash; change your password in Account below</span>'
               : `<button onclick="resetUserPw('${esc(u.username)}')">Reset password</button>
        <button onclick="toggleUser('${esc(u.username)}', ${u.disabled ? 'false' : 'true'})">${u.disabled ? 'Enable' : 'Disable'}</button>
        <button onclick="setUserRole('${esc(u.username)}', '${u.role === 'admin' ? 'member' : 'admin'}')">Make ${u.role === 'admin' ? 'member' : 'admin'}</button>`}
      </td></tr>`;
    }).join('');
    document.getElementById('users-list').innerHTML =
      `<table><tr><th>user</th><th>role</th><th>ClickUp id</th><th>flags</th><th></th></tr>${rows}</table>`;
  } catch (e) {}
}

async function patchUser(username, body, okMsg) {
  try {
    const r = await fetch('api/users/' + encodeURIComponent(username), {
      method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    const data = await r.json();
    uiMsg(r.ok ? okMsg : 'Error: ' + (data.detail || r.status));
    if (r.ok) loadUsers();
  } catch (e) { uiMsg('Error: ' + e); }
}

function linkClickUp(username) {
  // Epic A1: ClickUp identity ↔ CtrlLoop user — gate verbs by comment are
  // attributed (and refused when strictness demands a mapped commenter)
  const cu = prompt('ClickUp user id for ' + username
    + ' (numeric — see the member profile in ClickUp; empty clears the link):');
  if (cu === null) return;
  patchUser(username, { clickup_user_id: cu.trim() },
            'ClickUp link updated for ' + username + '.');
}

function resetUserPw(username) {
  const pw = prompt('New temporary password for ' + username + ' (min 8 chars) - they must change it at next sign-in:');
  if (pw) patchUser(username, { password: pw }, 'Password reset for ' + username + '.');
}
function toggleUser(username, disable) {
  if (disable && !confirm('Disable ' + username + '? Their sessions are revoked immediately.')) return;
  patchUser(username, { disabled: disable }, (disable ? 'Disabled ' : 'Enabled ') + username + '.');
}
function setUserRole(username, role) {
  patchUser(username, { role }, username + ' is now ' + role + '.');
}

async function createUser(ev) {
  ev.preventDefault();
  const btn = document.getElementById('nu-go');
  btn.disabled = true;
  try {
    const r = await fetch('api/users', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        username: document.getElementById('nu-name').value.trim(),
        password: document.getElementById('nu-pass').value,
        role: document.getElementById('nu-role').value,
      }),
    });
    const data = await r.json();
    uiMsg(r.ok ? 'User created - share the temporary password with them.'
               : 'Error: ' + (data.detail || r.status));
    if (r.ok) {
      document.getElementById('nu-name').value = '';
      document.getElementById('nu-pass').value = '';
      loadUsers();
    }
  } catch (e) { uiMsg('Error: ' + e); }
  btn.disabled = false;
  return false;
}

async function changePassword(ev) {
  ev.preventDefault();
  const btn = document.getElementById('pw-go');
  btn.disabled = true;
  try {
    const r = await fetch('api/me/password', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        current: document.getElementById('pw-cur').value,
        new: document.getElementById('pw-new').value,
      }),
    });
    const data = await r.json();
    // the server rotates this session's cookie in the response — the person
    // stays signed in and lands straight on the dashboard, temp flag cleared
    if (r.ok) { location.href = '.'; return false; }
    uiMsg('Error: ' + (data.detail || r.status));
  } catch (e) { uiMsg('Error: ' + e); }
  btn.disabled = false;
  return false;
}

loadMe();

// ---------- workspaces (Phase 2): switcher + scoping + admin editor ----------

let activeWs = parseInt(localStorage.getItem('cl-ws') || '0', 10) || null;

async function loadWorkspaces() {
  try {
    const r = await fetch('api/workspaces');
    if (!r.ok) return;
    WORKSPACES = await r.json();
    if (!WORKSPACES.length) return;
    // 0 = "All workspaces" (also where unowned/legacy jobs surface); a
    // specific selection filters STRICTLY (sentry finding 1595858)
    const valid = (activeWs === 0 && WORKSPACES.length > 1)
      || WORKSPACES.some((w) => w.id === activeWs);
    if (activeWs == null || !valid) activeWs = WORKSPACES.length > 1 ? 0 : WORKSPACES[0].id;
    // the switcher is also the "which workspace am I in" indicator, so it
    // stays visible with a single workspace; "All" only exists with several
    const sw = document.getElementById('ws-switch');
    sw.innerHTML = (WORKSPACES.length > 1
        ? '<option value="0"' + (activeWs === 0 ? ' selected' : '') + '>All workspaces</option>' : '') +
      WORKSPACES.map((w) =>
        `<option value="${w.id}" ${w.id === activeWs ? 'selected' : ''}>${esc(w.name)}</option>`).join('');
    sw.style.display = '';
    scopeProjectPickers();
    if (ME && ME.role === 'admin') renderWorkspacesAdmin();
  } catch (e) {}
}

function setWorkspace(id) {
  activeWs = parseInt(id, 10);
  localStorage.setItem('cl-ws', String(activeWs));
  scopeProjectPickers();
  lastJobs = ''; refresh(true); refreshMemory(true);
}

function jobInActiveWs(j) {
  if (!activeWs || WORKSPACES.length < 2) return true;  // "All", or single-workspace instance
  return (j.workspace_id || null) === activeWs;  // strict: unowned jobs live under "All" only
}

function scopeProjectPickers() {
  // re-filter the intake pickers to the active workspace's slugs
  if (window.PROJECTS_CACHE) renderProjectOptions(window.PROJECTS_CACHE);
}

async function renderWorkspacesAdmin() {
  const box = document.getElementById('ws-admin-list');
  if (!box) return;
  let users = [];
  try { users = await (await fetch('api/users')).json(); } catch (e) {}
  box.innerHTML = WORKSPACES.map((w) => {
    const repos = Object.entries(w.repos).map(([s, e]) => wsRepoRow(s, e)).join('');
    const members = users.map((u) => `<label style="display:inline-flex;gap:4px;align-items:center">
      <input type="checkbox" data-ws-member="${w.id}" value="${esc(u.username)}"
        ${w.members.includes(u.username) ? 'checked' : ''}
        onchange="setWsMember(${w.id}, '${esc(u.username)}', this.checked)">${esc(u.username)}</label>`).join('');
    return `<div class="ws-card" data-ws="${w.id}">
      <h3>${esc(w.name)} <span class="chip">${esc(w.slug)}</span></h3>
      <div class="ws-inline">
        <div><label>Product name</label><input class="w-product" value="${attr(w.product_name)}"></div>
        <div><label>Canonical project (hosts product-scope memory)</label><input class="w-canon" value="${attr(w.canonical_project)}"></div>
      </div>
      <label>Repositories (slug &rarr; repo &middot; base &middot; setup &middot; test)</label>
      <div class="ws-repos">${repos}</div>
      <button type="button" onclick="this.previousElementSibling.insertAdjacentHTML('beforeend', wsRepoRow('', {}))">+ Add repo</button>
      <label>Workspace context (stacked under the business context in every run)</label>
      <textarea class="w-ctx">${esc(w.workspace_context)}</textarea>
      <div class="ws-inline" style="margin-top:8px">
        <div><label>ClickUp list id (optional)</label><input class="w-culist" value="${attr(w.clickup_list_id)}"></div>
        <div><label>Slack webhook URL (gate nudges, optional)</label><input class="w-slack" value="${attr(w.slack_webhook_url)}"></div>
      </div>
      <div class="ws-flags">
        <label style="display:inline-flex;gap:5px;align-items:center">
          <input type="checkbox" class="w-cuon" ${w.clickup_enabled ? 'checked' : ''}> ClickUp mirroring</label>
        <span class="hint">members:</span><span class="ws-members">${members || '<span class="hint">none</span>'}</span>
      </div>
      <button type="button" class="save" onclick="saveWorkspace(${w.id}, this)">Save workspace</button>
    </div>`;
  }).join('');
}

function wsRepoRow(slug, e) {
  e = e || {};
  return `<div class="ws-repo-row">
    <input class="wr-slug" placeholder="slug" value="${attr(slug)}">
    <input class="wr-repo" placeholder="owner/name" value="${attr(e.repo || '')}">
    <input class="wr-base" placeholder="base" value="${attr(e.base || '')}">
    <input class="wr-setup" placeholder="setup cmd" value="${attr(e.setup_cmd || '')}">
    <input class="wr-test" placeholder="test cmd" value="${attr(e.test_cmd || '')}">
    <button type="button" onclick="this.parentNode.remove()" title="Remove">&#10007;</button>
  </div>`;
}

async function saveWorkspace(id, btn) {
  const card = document.querySelector(`.ws-card[data-ws="${id}"]`);
  const v = (cls) => card.querySelector('.' + cls).value.trim();
  const repos = [];
  for (const row of card.querySelectorAll('.ws-repo-row')) {
    const rv = (cls) => row.querySelector('.' + cls).value.trim();
    if (!rv('wr-slug')) continue;
    repos.push({ slug: rv('wr-slug'), repo: rv('wr-repo'), base: rv('wr-base') || 'main',
                 setup_cmd: rv('wr-setup') || null, test_cmd: rv('wr-test') || null });
  }
  btn.disabled = true; uiMsg('Saving workspace…');
  try {
    const r = await fetch('api/workspaces/' + id, {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        product_name: v('w-product'), canonical_project: v('w-canon'),
        workspace_context: card.querySelector('.w-ctx').value,
        clickup_list_id: v('w-culist'), slack_webhook_url: v('w-slack'),
        clickup_enabled: card.querySelector('.w-cuon').checked, repos,
      }),
    });
    const data = await r.json();
    if (r.ok) {
      uiMsg('Workspace saved.' + (data.warning ? ' ' + data.warning : ''));
      await loadWorkspaces(); loadProjects(); refreshMemory(true);
    } else uiMsg('Error: ' + (data.detail || r.status));
  } catch (e) { uiMsg('Error: ' + e); }
  btn.disabled = false;
}

async function createWorkspace(ev) {
  ev.preventDefault();
  try {
    const r = await fetch('api/workspaces', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ slug: document.getElementById('nw-slug').value.trim(),
                             name: document.getElementById('nw-name').value.trim() }),
    });
    const data = await r.json();
    uiMsg(r.ok ? 'Workspace created — add repos below.' : 'Error: ' + (data.detail || r.status));
    if (r.ok) { document.getElementById('nw-slug').value = ''; document.getElementById('nw-name').value = ''; loadWorkspaces(); }
  } catch (e) { uiMsg('Error: ' + e); }
  return false;
}

async function setWsMember(wsId, username, member) {
  try {
    await fetch(`api/workspaces/${wsId}/members`, {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ username, member }),
    });
    loadWorkspaces();
  } catch (e) {}
}

loadWorkspaces();
