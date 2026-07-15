"""Single-file dashboard for gumo_brain v2 — the Gumo Engine (docs/ENGINE.md §6).

Three intake panels (Sentry fix / request / feature pipeline P0-P9), a Product
brain panel (per-repo memory freshness + bootstrap), and the four-column job
queue. Feature cards carry a P0-P9 stage strip, a lazy per-stage stats table,
and gates with Proceed / Redo / Skip, plus a per-gate chat with the engine
(GET/POST api/jobs/{id}/chat, feature gates only). Typed gate answers and chat
drafts survive re-renders (in-memory snapshot + localStorage drafts keyed
`gb-draft-<job_id>` / `gb-chat-<job_id>`).

NOTE: this module is one big Python string. It deliberately contains NO
backslashes — JS regexes are built with `new RegExp` + `String.fromCharCode`
— so Python escape handling can never mangle the emitted HTML/JS.
"""

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>gumo_brain</title>
<style>
  :root { color-scheme: light dark; --bg:#fff; --fg:#1a1a2e; --muted:#667; --card:#f4f5f7; --accent:#5b5bd6; --ok:#1a7f37; --warn:#b35900; --err:#c0392b; --line:#d8dae0; }
  @media (prefers-color-scheme: dark) { :root { --bg:#101014; --fg:#e8e8ee; --muted:#99a; --card:#1c1c24; --line:#33343e; } }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.5 -apple-system,system-ui,sans-serif; background:var(--bg); color:var(--fg); padding:24px; }
  h1 { font-size:20px; margin:0 0 4px; } h1 small { color:var(--muted); font-weight:400; }
  #msg { margin:8px 0 16px; color:var(--muted); min-height:1.2em; }
  .intake { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:16px; margin:16px 0 24px; }
  .panel { background:var(--card); border-radius:12px; padding:14px 16px; }
  .panel h2 { font-size:13px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:0 0 10px; }
  .panel input, .panel textarea, .panel select { width:100%; padding:8px 12px; margin-bottom:8px; border:1px solid var(--line); border-radius:8px; background:var(--bg); color:var(--fg); font:inherit; }
  .panel textarea { resize:vertical; min-height:64px; }
  .panel button { padding:8px 16px; border:0; border-radius:8px; background:var(--accent); color:#fff; cursor:pointer; font:inherit; }
  .panel button:disabled { opacity:.5; }
  .hint { font-size:12px; color:var(--muted); margin:2px 0 8px; }
  .cols { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:16px; }
  .col h2 { font-size:13px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:0 0 8px; }
  .job { background:var(--card); border-radius:10px; padding:10px 12px; margin-bottom:8px; }
  .job .t { font-weight:600; font-size:14px; overflow:hidden; text-overflow:ellipsis; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; }
  .job .m { font-size:12px; color:var(--muted); margin-top:4px; }
  .job a { color:var(--accent); text-decoration:none; margin-right:10px; }
  .badge { display:inline-block; font-size:11px; padding:1px 8px; border-radius:99px; background:var(--muted); color:var(--bg); margin-right:6px; }
  .badge.pr_opened { background:var(--ok); } .badge.awaiting_input { background:var(--warn); }
  .badge.error,.badge.timeout { background:var(--err); }
  .badge.kind { background:transparent; color:var(--muted); border:1px solid var(--line); }
  .chip { display:inline-block; font-size:11px; padding:0 8px; border-radius:99px; background:var(--warn); color:#fff; margin-left:8px; }
  .empty { color:var(--muted); font-size:13px; }
  .q { white-space:pre-wrap; font-size:13px; background:var(--bg); border:1px solid var(--line); border-radius:8px; padding:8px 10px; margin-top:8px; max-height:180px; overflow:auto; }
  details { margin-top:6px; font-size:12px; }
  details summary { cursor:pointer; color:var(--muted); }
  details pre { white-space:pre-wrap; overflow-wrap:anywhere; font:12px/1.5 ui-monospace,monospace; background:var(--bg); border:1px solid var(--line); border-radius:8px; padding:8px 10px; max-height:320px; overflow:auto; }
  .strip { display:flex; gap:2px; margin-top:8px; }
  .seg { flex:1; text-align:center; font-size:9px; line-height:15px; border-radius:3px; border:1px solid var(--line); color:var(--muted); }
  .seg.done { background:var(--accent); border-color:var(--accent); color:#fff; opacity:.55; }
  .seg.cur { background:var(--accent); border-color:var(--accent); color:#fff; animation:pulse 1.4s ease-in-out infinite; }
  .seg.cur.still { animation:none; }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.4; } }
  .stagetext { font-size:12px; color:var(--muted); margin-top:2px; }
  .answer textarea { width:100%; margin-top:8px; padding:6px 10px; border:1px solid var(--line); border-radius:8px; background:var(--bg); color:var(--fg); font:inherit; font-size:13px; resize:vertical; min-height:44px; }
  .answer .btns { display:flex; gap:8px; margin-top:6px; }
  .answer button { flex:1; padding:6px 0; border:0; border-radius:8px; cursor:pointer; font:inherit; font-size:13px; color:#fff; }
  .answer button.go { background:var(--ok); } .answer button.no { background:var(--err); }
  .answer button.redo { background:var(--warn); }
  .answer button:disabled { opacity:.5; }
  .answer .hint { margin:4px 0 0; }
  .answer button.ask { background:var(--accent); }
  .chat-log { display:flex; flex-direction:column; gap:6px; margin-top:6px; max-height:260px; overflow:auto; font-size:13px; }
  .chat-log .turn { max-width:92%; align-self:flex-start; }
  .chat-log .turn.human { align-self:flex-end; }
  .chat-log .turn .b { white-space:pre-wrap; overflow-wrap:anywhere; border:1px solid var(--line); border-radius:8px; padding:6px 10px; background:var(--bg); }
  .chat-log .turn.human .b { background:var(--accent); border-color:var(--accent); color:#fff; }
  .chat-log .turn .c { font-size:11px; color:var(--muted); margin-top:1px; }
  .chat-log .turn.human .c { text-align:right; }
  .chat-log .turn.wait { color:var(--muted); font-size:12px; }
  .spin { display:inline-block; width:10px; height:10px; border:2px solid var(--muted); border-top-color:transparent; border-radius:50%; margin-right:6px; vertical-align:-1px; animation:rot .9s linear infinite; }
  @keyframes rot { to { transform:rotate(360deg); } }
  .brain { margin:0 0 24px; }
  .brain > summary { cursor:pointer; font-size:13px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }
  .brain table, .stats-body table { border-collapse:collapse; font-size:12px; margin-top:8px; }
  .brain th, .brain td, .stats-body th, .stats-body td { border:1px solid var(--line); padding:3px 8px; text-align:left; }
  .brain th, .stats-body th { color:var(--muted); font-weight:600; }
  .brain button { padding:3px 10px; border:0; border-radius:6px; background:var(--accent); color:#fff; cursor:pointer; font:inherit; font-size:12px; }
  .brain button:disabled { opacity:.5; }
  .ok-t { color:var(--ok); } .err-t { color:var(--err); } .muted-t { color:var(--muted); }
</style>
</head>
<body>
<h1>gumo_brain <small>the Gumo Engine &mdash; Sentry fixes, requests &amp; feature pipelines</small></h1>
<div id="msg"></div>

<div class="intake">
  <div class="panel">
    <h2>Fix a Sentry issue</h2>
    <form onsubmit="return trigger(event)">
      <input id="ref" placeholder="Sentry issue id, short id (GUMO-1A) or URL" required>
      <button id="go">Fix it</button>
    </form>
  </div>
  <div class="panel">
    <h2>Submit a request (bug fix / change)</h2>
    <form onsubmit="return submitTask(event)">
      <select id="task-project" required></select>
      <input id="task-clickup" placeholder="ClickUp task URL (optional — adopts that ticket)">
      <div class="hint">&hellip;or describe it and a ClickUp ticket is created for you:</div>
      <input id="task-title" placeholder="Title">
      <textarea id="task-summary" placeholder="What's wrong / what do you need? Steps, links, expected behaviour&hellip;"></textarea>
      <div class="hint">Claude analyses first and comes back with its plan + questions below before touching code.</div>
      <button id="task-go">Submit</button>
    </form>
  </div>
  <div class="panel">
    <h2>Feature pipeline (P0&ndash;P9)</h2>
    <form onsubmit="return submitFeature(event)">
      <select id="feat-project" required></select>
      <input id="feat-clickup" placeholder="ClickUp task URL (optional — adopts that ticket)">
      <div class="hint">&hellip;or describe the feature and a ClickUp ticket is created:</div>
      <input id="feat-title" placeholder="Title">
      <textarea id="feat-summary" placeholder="What should be built? Goal, users, constraints, links&hellip;"></textarea>
      <input id="feat-owner" placeholder="Owner — ClickUp user id (optional, gets gate notifications)">
      <select id="feat-gatemode">
        <option value="full" selected>full &mdash; pause at every stage (default)</option>
        <option value="light">light &mdash; pause at P0/P1/P3/P9 + questions</option>
      </select>
      <input id="feat-related" placeholder="Related pipeline job id(s) (optional, comma-separated)">
      <div class="hint">P0 Intake &rarr; P9 Ship; every stage parks below (and on ClickUp) for your Proceed / Redo / Skip.</div>
      <button id="feat-go">Start pipeline</button>
    </form>
  </div>
</div>

<details class="panel brain">
  <summary>Product brain <span class="hint" style="display:inline">&mdash; per-repo memory freshness</span></summary>
  <div id="brain-body"><div class="empty">loading&hellip;</div></div>
</details>

<div class="cols">
  <div class="col"><h2>Pending</h2><div id="pending"></div></div>
  <div class="col"><h2>In progress</h2><div id="progress"></div></div>
  <div class="col"><h2>Awaiting input</h2><div id="awaiting"></div></div>
  <div class="col"><h2>Completed</h2><div id="completed"></div></div>
</div>
<script>
const GROUPS = {
  pending: ['received', 'queued'],
  progress: ['running'],
  awaiting: ['awaiting_input'],
  completed: ['pr_opened', 'no_fix', 'skipped', 'error', 'timeout'],
};
const STAGE_NAMES = ['Intake', 'PRD', 'Recon', 'Design', 'Plan', 'Build 1', 'Build 2+', 'Test', 'Review', 'Ship'];
const KIND_LABEL = { sentry: 'sentry', task: 'request', feature: 'feature', memory: 'memory' };
const LIVE = ['received', 'queued', 'running', 'awaiting_input'];

function esc(s) { const d = document.createElement('span'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }

// URL matcher built WITHOUT regex-literal escapes: this file ships inside a
// Python string, so backslashes are banned. fromCharCode(9,10,13,34,39) puts
// tab, LF, CR, double-quote and single-quote in the negated class.
const URL_RE = new RegExp('https://[^ <>)' + String.fromCharCode(9, 10, 13, 34, 39) + ']+', 'g');
function linkify(s) {
  // esc() first: matched URLs then contain no <>"' and are attribute-safe
  // (& is already &amp;, which parses back to & inside the href attribute).
  return esc(s).replace(URL_RE, (u) => `<a href="${u}" target="_blank">${u}</a>`);
}

// ---------- cards ----------

function stageStrip(j) {
  const cur = Number(j.stage) || 0;
  const still = LIVE.includes(j.status) ? '' : ' still'; // no pulse on finished pipelines
  let segs = '';
  for (let i = 0; i < 10; i++) {
    const cls = i < cur ? 'done' : (i === cur ? 'cur' + still : 'todo');
    segs += `<span class="seg ${cls}" title="P${i} ${esc(STAGE_NAMES[i])}">P${i}</span>`;
  }
  const mirror = j.mirror_ok ? '' :
    '<span class="chip" title="ClickUp artifact mirror unhealthy — artifacts live in git; answer gates here">mirror off</span>';
  const askq = j.status === 'awaiting_input' && j.gate_kind === 'ask'
    ? '<span class="chip" title="the engine paused mid-stage to ask a question — answering resumes from the pause point">question &mdash; resumes in place</span>'
    : '';
  return `<div class="strip">${segs}</div>
    <div class="stagetext">P${cur} ${esc(j.stage_name || STAGE_NAMES[cur] || '')}${askq}${mirror}</div>`;
}

function answerBlock(j) {
  const id = esc(j.issue_id);
  const feature = j.kind === 'feature';
  // STAGE_ASK gates: the engine paused mid-work to ask — 'proceed' delivers the
  // answer and resumes in place, so the button reads Answer; Redo discards the
  // pause point and restarts the stage.
  const isAsk = j.gate_kind === 'ask';
  const goLabel = isAsk ? 'Answer' : 'Proceed';
  const redoTitle = isAsk ? ' title="restarts the stage fresh (discards the pause point)"' : '';
  const placeholder = isAsk ? 'Your answer&hellip;' : 'Your answer / guidance&hellip;';
  const btns = feature
    ? `<button class="go" onclick="answer('${id}','proceed',this)">${goLabel}</button>
       <button class="redo"${redoTitle} onclick="answer('${id}','redo',this)">Redo</button>
       <button class="no" onclick="answer('${id}','skip',this)">Skip</button>`
    : `<button class="go" onclick="answer('${id}','proceed',this)">${goLabel}</button>
       <button class="no" onclick="answer('${id}','skip',this)">Skip</button>`;
  const hint = feature
    ? `<div class="hint">Redo re-runs this stage with your notes as corrections &mdash; optionally prefix 'P3 ' to redo an earlier stage.</div>`
    : '';
  const chat = feature
    ? `<details class="chat" data-key="${id}:chat" data-job="${id}" data-updated="${esc(j.updated_at)}">
        <summary>chat with the engine</summary>
        <div class="chat-log" id="chat-log-${id}"><div class="empty">&hellip;</div></div>
        <textarea id="chat-in-${id}" data-draft="gb-chat-${id}" placeholder="Ask about this gate&hellip;"></textarea>
        <div class="btns"><button class="ask" id="chat-ask-${id}" onclick="askChat('${id}',this)">Ask</button></div>
        <div class="hint">Answers come from the gate's documents and the repo (read-only). Typically 15&ndash;90s.</div>
      </details>`
    : '';
  return `<div class="answer"><textarea id="ans-${id}" data-job="${id}" data-draft="gb-draft-${id}" placeholder="${placeholder}"></textarea>
    ${chat}${hint}<div class="btns">${btns}</div></div>`;
}

function card(j) {
  const id = esc(j.issue_id);
  let links = '';
  if (j.issue_url) links += `<a href="${esc(j.issue_url)}" target="_blank">Sentry</a>`;
  if (j.clickup_task_url) links += `<a href="${esc(j.clickup_task_url)}" target="_blank">ClickUp</a>`;
  if (j.pr_url) links += `<a href="${esc(j.pr_url)}" target="_blank">PR</a>`;
  const when = new Date(j.updated_at * 1000).toLocaleString();
  const score = j.score != null ? ` &middot; score ${j.score}` : '';
  const phase = j.kind === 'task' && j.phase > 1 ? ' &middot; phase 2' : '';
  const owner = j.kind === 'feature' && j.owner ? ` &middot; owner ${esc(j.owner)}` : '';
  const kind = `<span class="badge kind">${esc(KIND_LABEL[j.kind] || j.kind)}</span>`;
  const strip = j.kind === 'feature' ? stageStrip(j) : '';

  let ask = '';
  if (j.status === 'awaiting_input') {
    ask = `<div class="q">${esc(j.question || (j.detail || '').slice(0, 500))}</div>`
      + (j.evidence ? `<details data-key="${id}:ev"><summary>evidence</summary><pre>${linkify(j.evidence)}</pre></details>` : '')
      + (j.analysis ? `<details data-key="${id}:an"><summary>full analysis</summary><pre>${esc(j.analysis)}</pre></details>` : '')
      + answerBlock(j);
  }
  let rekick = '';
  if (j.kind === 'feature' && (j.status === 'error' || j.status === 'timeout')) {
    rekick = `<div class="answer"><div class="btns">
      <button class="redo" onclick="answer('${id}','redo',this)">Re-kick (redo)</button></div></div>`;
  }
  let stats = '';
  if (j.kind === 'feature') {
    stats = `<details class="stats" data-key="${id}:stats" data-job="${id}"><summary>stats</summary>
      <div class="stats-body" id="stats-${id}"><div class="empty">&hellip;</div></div></details>`;
  }
  return `<div class="job"><div class="t">${esc(j.title || j.issue_id)}</div>
    <div class="m"><span class="badge ${esc(j.status)}">${esc(j.status)}</span>${kind}${esc(j.project)}${score}${phase}${owner}</div>
    ${strip}<div class="m">${links}</div><div class="m">${when}</div>${ask}${rekick}${stats}</div>`;
}

// ---------- render, with draft + <details> state preservation (§6: typed
// input must survive re-renders, even when the jobs payload changed) ----------

function captureDrafts() {
  // every draft-bearing textarea (gate answers gb-draft-<id>, gate chat
  // gb-chat-<id>) carries its localStorage key in data-draft.
  const snap = {};
  let focus = null;
  for (const t of document.querySelectorAll('textarea[data-draft]')) {
    snap[t.dataset.draft] = t.value;
    if (t === document.activeElement)
      focus = { key: t.dataset.draft, start: t.selectionStart, end: t.selectionEnd };
  }
  return { snap, focus };
}

function restoreDrafts(state) {
  for (const t of document.querySelectorAll('textarea[data-draft]')) {
    const k = t.dataset.draft;
    // in-memory snapshot (freshest) first, then the localStorage draft
    // (survives reloads; written on every input, cleared on answered)
    const v = state.snap[k] !== undefined ? state.snap[k] : localStorage.getItem(k);
    if (v) t.value = v;
    if (state.focus && state.focus.key === k) {
      t.focus();
      try { t.setSelectionRange(state.focus.start, state.focus.end); } catch (e) { /* ok */ }
    }
  }
}

function renderJobs(jobs) {
  const drafts = captureDrafts();
  const open = new Set();
  for (const d of document.querySelectorAll('details[data-key]')) if (d.open) open.add(d.dataset.key);

  for (const [div, statuses] of Object.entries(GROUPS)) {
    const items = jobs.filter(j => statuses.includes(j.status));
    document.getElementById(div).innerHTML =
      items.length ? items.map(card).join('') : '<div class="empty">none</div>';
  }

  // re-open evidence/analysis/stats/chat panels; stats + chat bodies refill
  // from cache first (no flicker), then the toggle event triggers a fetch.
  for (const d of document.querySelectorAll('details[data-key]')) {
    if (!open.has(d.dataset.key)) continue;
    if (d.classList.contains('stats') && statsCache[d.dataset.job])
      d.querySelector('.stats-body').innerHTML = statsCache[d.dataset.job];
    if (d.classList.contains('chat') && chatCache[d.dataset.job])
      paintChat(d.dataset.job);
    d.open = true;
  }
  restoreDrafts(drafts);
}

// persist drafts as they are typed (delegated: cards are re-created on render)
document.addEventListener('input', (e) => {
  const t = e.target;
  if (t && t.matches && t.matches('textarea[data-draft]'))
    localStorage.setItem(t.dataset.draft, t.value);
});

let lastJobs = '';
async function refresh(force) {
  try {
    const jobs = await (await fetch('api/jobs')).json();
    const sig = JSON.stringify(jobs);
    // skip the re-render entirely when nothing changed (10s poll)
    if (force || sig !== lastJobs) { lastJobs = sig; renderJobs(jobs); }
  } catch (e) { document.getElementById('msg').textContent = 'refresh failed: ' + e; }
}

// ---------- per-feature stats (api/features/{id}/stats) ----------

const statsCache = {};        // job id -> rendered table html (for re-renders)
const statsInflight = new Set();

// <details> toggle does not bubble — capture at the document instead.
document.addEventListener('toggle', (e) => {
  const d = e.target;
  if (!d || !d.dataset || !d.dataset.key) return;
  if (d.open && d.classList.contains('stats')) loadStats(d.dataset.job); // refetch on each open
  if (d.open && d.classList.contains('chat')) openChat(d.dataset.job);   // lazy fetch, cached
}, true);

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

function statsTable(data) {
  const runs = data.runs || [];
  if (!runs.length) return '<div class="empty">no stage runs yet</div>';
  const autoStages = new Set();  // stages the engine advanced without a human gate (light mode)
  for (const g of data.guidance || []) if (g.action === 'auto') autoStages.add(Number(g.stage));
  const by = {};
  for (const r of runs) {  // aggregate per stage; runs arrive ordered, last status wins
    const s = by[r.stage] || (by[r.stage] = { n: 0, dur: 0, cost: 0, wait: 0, status: '', gate: '' });
    s.n += 1;
    s.dur += r.duration_ms || 0;
    s.cost += r.cost_usd || 0;
    if (r.gate_posted_at && r.gate_answered_at) s.wait += Math.max(0, r.gate_answered_at - r.gate_posted_at);
    if (r.result_status) s.status = r.result_status;
    s.gate = r.gate_action || '';  // latest run's gate_action wins ('' = never answered by a human)
  }
  const rows = Object.keys(by).map(Number).sort((a, b) => a - b).map(st => {
    const s = by[st];
    const res = !s.gate && autoStages.has(st)
      ? '<span class="muted-t" title="auto-advanced (light gate mode)">auto</span>'
      : esc(s.status);
    return `<tr><td>P${st} ${esc(STAGE_NAMES[st] || '')}</td><td>${s.n}</td><td>${fmtMMSS(s.dur)}</td>
      <td>$${s.cost.toFixed(2)}</td><td>${fmtWait(s.wait)}</td><td>${res}</td></tr>`;
  }).join('');
  return `<table><tr><th>stage</th><th>attempts</th><th>duration</th><th>cost</th><th>gate wait</th><th>result</th></tr>${rows}</table>`;
}

async function loadStats(id) {
  if (statsInflight.has(id)) return;
  statsInflight.add(id);
  const body = () => document.getElementById('stats-' + id);
  if (body() && !statsCache[id]) body().innerHTML = '<div class="empty">loading&hellip;</div>';
  try {
    const r = await fetch('api/features/' + encodeURIComponent(id) + '/stats');
    const data = await r.json();
    statsCache[id] = r.ok ? statsTable(data)
                          : '<div class="empty">Error: ' + esc(data.detail || r.status) + '</div>';
  } catch (e) {
    statsCache[id] = '<div class="empty">Error: ' + esc(String(e)) + '</div>';
  }
  statsInflight.delete(id);
  if (body()) body().innerHTML = statsCache[id];
}

// ---------- gate chat (api/jobs/{id}/chat — feature gates only) ----------

const chatCache = {};      // job id -> { key: updated_at at fetch, html, pending, limit }
const chatInflight = new Set();
const chatPollers = {};    // job id -> 5s interval handle while an answer is pending

function chatTranscript(data) {
  const turns = data.turns || [];
  let h = '';
  for (const t of turns) {
    const human = t.role === 'human';
    let meta = '';
    if (t.degraded) meta += '(from documents only) ';
    if (t.cost_usd != null) meta += '$' + Number(t.cost_usd).toFixed(2);
    h += '<div class="turn ' + (human ? 'human' : 'engine') + '"><div class="b">' + esc(t.text) + '</div>'
      + (meta ? '<div class="c">' + esc(meta) + '</div>' : '') + '</div>';
    if (t.pending) h += '<div class="turn wait"><span class="spin"></span>answering&hellip;</div>';
  }
  return h || '<div class="empty">no messages yet &mdash; ask the engine about this gate</div>';
}

function paintChat(id) {
  const c = chatCache[id];
  const log = document.getElementById('chat-log-' + id);
  if (!c || !log) return;
  log.innerHTML = c.html;
  log.scrollTop = log.scrollHeight;
  const box = document.getElementById('chat-in-' + id);
  const btn = document.getElementById('chat-ask-' + id);
  if (box) {
    box.disabled = c.limit;
    if (c.limit) box.placeholder = 'chat limit reached for this gate — answer with Proceed/Redo/Skip';
  }
  if (btn) btn.disabled = c.limit;
}

async function loadChat(id) {
  if (chatInflight.has(id)) return;
  chatInflight.add(id);
  const el = document.querySelector('details.chat[data-job="' + id + '"]');
  const log = document.getElementById('chat-log-' + id);
  if (log && !chatCache[id]) log.innerHTML = '<div class="empty">loading&hellip;</div>';
  try {
    const r = await fetch('api/jobs/' + encodeURIComponent(id) + '/chat');
    const data = await r.json();
    if (r.ok) {
      chatCache[id] = { key: el ? el.dataset.updated : '', html: chatTranscript(data),
                        pending: !!data.pending, limit: !!data.limit_reached };
      if (data.pending) startChatPoll(id); else stopChatPoll(id);
    } else {
      chatCache[id] = { key: '', html: '<div class="empty">Error: ' + esc(data.detail || r.status) + '</div>',
                        pending: false, limit: false };
      stopChatPoll(id);
    }
  } catch (e) {
    chatCache[id] = { key: '', html: '<div class="empty">Error: ' + esc(String(e)) + '</div>',
                      pending: false, limit: false };
  }
  chatInflight.delete(id);
  paintChat(id);
}

function openChat(id) {
  // cache keyed by job + updated_at: reuse only while the card has not moved
  // on and no answer is pending; otherwise (re)fetch.
  const el = document.querySelector('details.chat[data-job="' + id + '"]');
  const c = chatCache[id];
  if (c && el && c.key === el.dataset.updated && !c.pending) { paintChat(id); return; }
  loadChat(id);
}

async function askChat(id, btn) {
  const msg = document.getElementById('msg');
  const box = document.getElementById('chat-in-' + id);
  const text = box ? box.value.trim() : '';
  if (!text) return;
  btn.disabled = true;
  try {
    const r = await fetch('api/jobs/' + encodeURIComponent(id) + '/chat', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text}),
    });
    let data = {};
    try { data = await r.json(); } catch (e2) { /* non-JSON error body */ }
    if (r.status === 202) {
      if (box) box.value = '';
      localStorage.removeItem('gb-chat-' + id);
      loadChat(id); // shows the pending turn + starts the 5s poll
    } else {
      // 409 = answer already in flight / chat limit reached; 404/500 = error
      msg.textContent = 'Chat: ' + (data.detail || r.status);
      if (r.status === 409) loadChat(id);
    }
  } catch (e) { msg.textContent = 'Chat error: ' + e; }
  btn.disabled = false;
}

function startChatPoll(id) {
  if (chatPollers[id]) return;
  chatPollers[id] = setInterval(() => {
    // stop when the card left awaiting_input (the chat panel is gone)
    if (!document.getElementById('chat-log-' + id)) { stopChatPoll(id); return; }
    loadChat(id);
  }, 5000);
}

function stopChatPoll(id) {
  if (chatPollers[id]) { clearInterval(chatPollers[id]); delete chatPollers[id]; }
}

// ---------- Product brain (api/memory) ----------

let lastMem = '';
let lastMemData = null;
const pendingBoot = new Set();

async function refreshMemory(force) {
  try {
    const mem = await (await fetch('api/memory')).json();
    const sig = JSON.stringify(mem);
    if (force || sig !== lastMem) { lastMem = sig; lastMemData = mem; renderMemory(mem); }
  } catch (e) { /* keep the previous table; jobs poll reports connectivity */ }
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
      <td><button onclick="bootstrap('${esc(p)}',this)"${dis} title="Bootstrap .gumo memory via a draft PR">Bootstrap</button></td></tr>`;
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
    if (r.ok) refresh(true); // the memory job shows up in the queue
  } catch (e) { msg.textContent = 'Error: ' + e; }
  pendingBoot.delete(project);
  if (lastMemData) renderMemory(lastMemData); else btn.disabled = false;
}

// ---------- intake forms ----------

async function loadProjects() {
  try {
    const ps = await (await fetch('api/projects')).json();
    const opts = '<option value="" disabled selected>Project&hellip;</option>' +
      ps.map(p => `<option value="${esc(p.slug)}">${esc(p.slug)} (${esc(p.repo)})</option>`).join('');
    document.getElementById('task-project').innerHTML = opts;
    document.getElementById('feat-project').innerHTML = opts;
  } catch (e) { /* keep empty selects; submit will fail loudly */ }
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
    owner: document.getElementById('feat-owner').value.trim(),
    gate_mode: document.getElementById('feat-gatemode').value,
    related_to: document.getElementById('feat-related').value.trim(),
  };
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
      for (const id of ['feat-clickup', 'feat-title', 'feat-summary', 'feat-owner', 'feat-related'])
        document.getElementById(id).value = '';
      refresh(true);
    }
  } catch (e) { msg.textContent = 'Error: ' + e; }
  btn.disabled = false;
  return false;
}

// ---------- gate answers (proceed / redo / skip + error re-kick) ----------

async function answer(id, action, btn) {
  const msg = document.getElementById('msg');
  const box = document.getElementById('ans-' + id);
  const text = box ? box.value.trim() : '';
  if (action === 'skip' && !confirm('Abort this job? (feature branches are left intact)')) return;
  btn.disabled = true; msg.textContent = 'Recording decision…';
  try {
    const r = await fetch('api/jobs/' + encodeURIComponent(id) + '/answer', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action, answer: text}),
    });
    const data = await r.json();
    if (r.ok) {
      localStorage.removeItem('gb-draft-' + id); // answered — drop the draft
      localStorage.removeItem('gb-chat-' + id);  // the gate chat is over too
      stopChatPoll(id); delete chatCache[id];
      msg.textContent = { proceed: 'Answer recorded — next run queued.',
                          redo: 'Redo queued.', skip: 'Skipped.' }[action] || ('OK: ' + data.status);
      refresh(true);
    } else {
      // 409 = lost race (e.g. already answered via ClickUp) or wrong state —
      // surface the server's explanation and re-sync the board.
      msg.textContent = 'Error: ' + (data.detail || r.status);
      if (r.status === 409) refresh(true);
    }
  } catch (e) { msg.textContent = 'Error: ' + e; }
  btn.disabled = false;
}

loadProjects(); refresh(true); refreshMemory(true);
setInterval(() => refresh(false), 10000);
setInterval(() => refreshMemory(false), 60000);
</script>
</body>
</html>"""
