"""Single-file dashboard for gumo_brain — the Gumo Engine (docs/ENGINE.md §6).

A professional, theme-aware console (dark-first, light via
prefers-color-scheme): a sticky top bar, three intake panels (Sentry fix /
request / feature pipeline P0-P9), a Product brain panel (per-repo memory
freshness + bootstrap), and the four-column job queue with live counts.
Feature cards carry a status-accented rail, a P0-P9 stage strip, a lazy
per-stage stats table, and gates with Proceed / Redo / Skip, plus a per-gate
chat with the engine (GET/POST api/jobs/{id}/chat, feature gates only) with
live SSE streaming. Typed gate answers and chat drafts survive re-renders
(in-memory snapshot + localStorage drafts keyed
`gb-draft-<job_id>` / `gb-chat-<job_id>`).

The visual system is a single set of CSS custom properties (surfaces, lines,
semantic status colors, radii). Card status classes stay the raw job status
(e.g. `awaiting_input`) so the CSS hooks match; only the human-facing label is
prettified via STATUS_LABEL. A dedicated live-session page (drop into a
running stage, watch it work, steer mid-run) is the next increment and slots
onto the same card IA.

NOTE: this module is one big Python string. It deliberately contains NO
backslashes — JS regexes are built with `new RegExp` + `String.fromCharCode`,
and CSS uses HTML entities / empty `content:""` rather than unicode escapes —
so Python escape handling can never mangle the emitted HTML/JS.
"""

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>gumo_brain</title>
<style>
  :root {
    color-scheme: dark light;
    --bg:#0a0b0f; --bg-2:#0d0f15; --surface:#13151d; --surface-2:#191c26; --surface-3:#20242f;
    --line:#242835; --line-2:#333a49;
    --fg:#e7e9f0; --fg-2:#a9b0c0; --muted:#6f778a;
    --accent:#8b7bff; --accent-2:#6d5efc; --accent-weak:rgba(124,108,255,.15);
    --ok:#3fb950; --ok-weak:rgba(63,185,80,.15);
    --warn:#e3a72c; --warn-weak:rgba(227,167,44,.15);
    --err:#f56565; --err-weak:rgba(245,101,101,.15);
    --info:#4d9fff; --info-weak:rgba(77,159,255,.15);
    --r:10px; --r-sm:7px; --r-lg:14px;
    --shadow:0 1px 2px rgba(0,0,0,.35), 0 10px 30px rgba(0,0,0,.22);
    --font:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg:#f5f6f8; --bg-2:#eceef2; --surface:#ffffff; --surface-2:#f5f6f9; --surface-3:#eceef3;
      --line:#e4e7ec; --line-2:#d3d8e0;
      --fg:#161922; --fg-2:#4a5163; --muted:#8b93a5;
      --accent:#6d5efc; --accent-2:#5b4ce0; --accent-weak:rgba(109,94,252,.1);
      --ok:#1a7f37; --ok-weak:rgba(26,127,55,.1);
      --warn:#b26a00; --warn-weak:rgba(178,106,0,.12);
      --err:#d33f3f; --err-weak:rgba(211,63,63,.1);
      --info:#2563eb; --info-weak:rgba(37,99,235,.1);
      --shadow:0 1px 2px rgba(20,25,40,.06), 0 10px 30px rgba(20,25,40,.08);
    }
  }
  * { box-sizing:border-box; }
  html { -webkit-text-size-adjust:100%; }
  body { margin:0; font:14.5px/1.6 var(--font); background:var(--bg); color:var(--fg); -webkit-font-smoothing:antialiased; }
  body::before { content:""; position:fixed; inset:0; z-index:-1; pointer-events:none;
    background:radial-gradient(900px 480px at 80% -10%, var(--accent-weak), transparent 70%); }
  a { color:var(--accent); text-decoration:none; }
  a:hover { text-decoration:underline; }
  ::selection { background:var(--accent-weak); }
  ::-webkit-scrollbar { width:10px; height:10px; }
  ::-webkit-scrollbar-thumb { background:var(--line-2); border-radius:99px; border:2px solid transparent; background-clip:content-box; }
  ::-webkit-scrollbar-thumb:hover { background:var(--muted); background-clip:content-box; }

  .topbar { position:sticky; top:0; z-index:20; display:flex; align-items:center; justify-content:space-between; gap:16px;
    padding:11px 24px; background:var(--bg); background:color-mix(in srgb, var(--bg) 82%, transparent);
    -webkit-backdrop-filter:saturate(150%) blur(12px); backdrop-filter:saturate(150%) blur(12px); border-bottom:1px solid var(--line); }
  .brand { display:flex; align-items:center; gap:11px; min-width:0; }
  .logo { display:grid; place-items:center; width:30px; height:30px; border-radius:9px; font:700 12px/1 var(--mono); letter-spacing:-.02em;
    color:#fff; background:linear-gradient(135deg,var(--accent),var(--accent-2)); box-shadow:0 3px 12px var(--accent-weak); }
  .brand-name { font-weight:650; font-size:15px; letter-spacing:-.01em; }
  .brand-sub { color:var(--muted); font-size:13px; }
  .topbar-meta { display:flex; align-items:center; gap:14px; color:var(--muted); font-size:12.5px; }
  .live { display:inline-flex; align-items:center; gap:7px; }
  .live-dot { width:7px; height:7px; border-radius:50%; background:var(--ok); animation:ping 2.4s ease-out infinite; }
  @keyframes ping { 0%{ box-shadow:0 0 0 0 rgba(63,185,80,.5); } 70%{ box-shadow:0 0 0 6px rgba(63,185,80,0); } 100%{ box-shadow:0 0 0 0 rgba(63,185,80,0); } }
  @media (max-width:600px) { .brand-sub { display:none; } }

  .wrap { max-width:1560px; margin:0 auto; padding:22px 24px 64px; }
  #msg { min-height:1.3em; margin:0 0 14px; color:var(--fg-2); font-size:13px; }
  #msg:not(:empty) { padding:9px 13px; background:var(--surface); border:1px solid var(--line); border-radius:var(--r-sm); box-shadow:var(--shadow); }
  .section-label { font-size:11.5px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); font-weight:650; margin:0 0 13px; }

  .intake { display:grid; grid-template-columns:repeat(auto-fit,minmax(330px,1fr)); gap:16px; margin:0 0 30px; }
  .panel { background:var(--surface); border:1px solid var(--line); border-radius:var(--r-lg); padding:16px 18px; box-shadow:var(--shadow); }
  .panel h2 { font-size:13px; font-weight:650; letter-spacing:-.01em; color:var(--fg); margin:0 0 13px; }
  .panel input, .panel textarea, .panel select { width:100%; padding:9px 12px; margin-bottom:9px; border:1px solid var(--line);
    border-radius:var(--r-sm); background:var(--surface-2); color:var(--fg); font:inherit; font-size:13.5px; transition:border-color .12s, box-shadow .12s; }
  .panel input::placeholder, .panel textarea::placeholder { color:var(--muted); }
  .panel input:focus, .panel textarea:focus, .panel select:focus { outline:0; border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-weak); }
  .panel textarea { resize:vertical; min-height:70px; }
  .panel button { padding:9px 18px; border:0; border-radius:var(--r-sm); background:linear-gradient(135deg,var(--accent),var(--accent-2));
    color:#fff; cursor:pointer; font:inherit; font-size:13.5px; font-weight:600; box-shadow:0 2px 10px var(--accent-weak); transition:transform .1s, opacity .12s; }
  .panel button:hover:not(:disabled) { transform:translateY(-1px); }
  .panel button:disabled { opacity:.5; cursor:default; }
  .hint { font-size:12px; color:var(--muted); margin:2px 0 9px; line-height:1.5; }

  .cols { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:16px; align-items:start; }
  .col { background:var(--bg-2); border:1px solid var(--line); border-radius:var(--r-lg); padding:12px; min-height:84px; }
  .col h2 { display:flex; align-items:center; gap:8px; font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--fg-2); font-weight:650; margin:2px 4px 12px; }
  .count { display:inline-grid; place-items:center; min-width:20px; height:20px; padding:0 6px; border-radius:99px; background:var(--surface-3); color:var(--muted); font:600 11px/1 var(--mono); }

  .job { position:relative; background:var(--surface); border:1px solid var(--line); border-radius:var(--r); padding:12px 13px 12px 16px;
    margin-bottom:10px; box-shadow:0 1px 2px rgba(0,0,0,.14); transition:border-color .12s; }
  .job::before { content:""; position:absolute; left:0; top:11px; bottom:11px; width:3px; border-radius:99px; background:var(--line-2); }
  .job[data-status="running"]::before { background:var(--accent); }
  .job[data-status="awaiting_input"]::before { background:var(--warn); }
  .job[data-status="pr_opened"]::before { background:var(--ok); }
  .job[data-status="error"]::before, .job[data-status="timeout"]::before { background:var(--err); }
  .job:hover { border-color:var(--line-2); }
  .job-head { display:flex; align-items:flex-start; justify-content:space-between; gap:10px; }
  .job .t { font-weight:600; font-size:13.5px; line-height:1.4; letter-spacing:-.01em; overflow:hidden; text-overflow:ellipsis;
    display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; }
  .job .m { font-size:12px; color:var(--muted); margin-top:6px; line-height:1.7; }
  .job .m .badge, .job .m .proj { vertical-align:middle; }
  .job .m.when { font-family:var(--mono); font-size:11px; opacity:.85; }
  .job .m.links a { margin-right:13px; font-weight:550; }
  .proj { font-family:var(--mono); font-size:11.5px; color:var(--fg-2); }

  .badge { display:inline-flex; align-items:center; gap:6px; white-space:nowrap; font-size:11px; font-weight:600; padding:2px 9px; border-radius:99px;
    background:var(--surface-3); color:var(--fg-2); margin-right:2px; }
  .job-head .badge { flex-shrink:0; }
  .badge::before { content:""; width:6px; height:6px; border-radius:50%; background:currentColor; }
  .badge.running { color:var(--accent); background:var(--accent-weak); }
  .badge.running::before { animation:pulse 1.4s ease-in-out infinite; }
  .badge.awaiting_input { color:var(--warn); background:var(--warn-weak); }
  .badge.pr_opened { color:var(--ok); background:var(--ok-weak); }
  .badge.error, .badge.timeout { color:var(--err); background:var(--err-weak); }
  .badge.queued, .badge.received { color:var(--info); background:var(--info-weak); }
  .badge.no_fix, .badge.skipped { color:var(--muted); background:var(--surface-3); }
  .badge.kind { background:transparent; color:var(--muted); border:1px solid var(--line-2); text-transform:lowercase; font-weight:550; }
  .badge.kind::before { display:none; }
  .chip { display:inline-flex; align-items:center; font-size:10.5px; font-weight:600; padding:1px 8px; border-radius:99px; background:var(--warn-weak); color:var(--warn); margin-left:8px; }

  .q { white-space:pre-wrap; font-size:12.5px; line-height:1.55; background:var(--bg-2); border:1px solid var(--line); border-radius:var(--r-sm);
    padding:9px 11px; margin-top:9px; max-height:190px; overflow:auto; }
  details { margin-top:8px; font-size:12px; }
  details summary { cursor:pointer; color:var(--fg-2); font-weight:550; user-select:none; list-style:none; display:inline-flex; align-items:center; gap:6px; }
  details summary::-webkit-details-marker { display:none; }
  details summary::before { content:"+"; font:700 12px/1 var(--mono); color:var(--muted); }
  details[open] > summary::before { content:"-"; }
  details summary:hover { color:var(--fg); }
  details pre { white-space:pre-wrap; overflow-wrap:anywhere; font:11.5px/1.55 var(--mono); background:var(--bg-2); border:1px solid var(--line);
    border-radius:var(--r-sm); padding:10px 11px; max-height:340px; overflow:auto; margin:8px 0 0; color:var(--fg-2); }

  .strip { display:flex; gap:3px; margin-top:11px; }
  .seg { flex:1; height:7px; border-radius:99px; background:var(--surface-3); font-size:0; overflow:hidden; transition:background .2s; }
  .seg.done { background:var(--accent); opacity:.85; }
  .seg.cur { background:var(--accent); box-shadow:0 0 0 2px var(--accent-weak); animation:pulse 1.5s ease-in-out infinite; }
  .seg.cur.still { animation:none; }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.35; } }
  .stagetext { font-size:11.5px; color:var(--fg-2); margin-top:8px; font-weight:550; }

  .answer { margin-top:12px; padding-top:12px; border-top:1px solid var(--line); }
  .answer textarea { width:100%; padding:8px 11px; border:1px solid var(--line); border-radius:var(--r-sm); background:var(--surface-2); color:var(--fg);
    font:inherit; font-size:13px; resize:vertical; min-height:46px; transition:border-color .12s, box-shadow .12s; }
  .answer textarea:focus { outline:0; border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-weak); }
  .answer .btns { display:flex; gap:8px; margin-top:8px; }
  .answer button { flex:1; padding:8px 0; border:0; border-radius:var(--r-sm); cursor:pointer; font:inherit; font-size:12.5px; font-weight:600; color:#fff; transition:transform .1s, opacity .12s; }
  .answer button:hover:not(:disabled) { transform:translateY(-1px); }
  .answer button.go { background:linear-gradient(135deg,#3fb950,#2ea043); }
  .answer button.no { background:var(--surface-3); color:var(--fg-2); }
  .answer button.redo { background:linear-gradient(135deg,var(--warn),#c8860f); }
  .answer button.ask { background:linear-gradient(135deg,var(--accent),var(--accent-2)); }
  .answer button:disabled { opacity:.5; cursor:default; }
  .answer .hint { margin:6px 0 0; }

  .chat { margin-top:10px; }
  .chat-log { display:flex; flex-direction:column; gap:8px; margin-top:10px; max-height:300px; overflow:auto; font-size:13px; padding:2px; }
  .chat-log .turn { max-width:88%; align-self:flex-start; }
  .chat-log .turn.human { align-self:flex-end; }
  .chat-log .turn .b { white-space:pre-wrap; overflow-wrap:anywhere; border:1px solid var(--line); border-radius:12px 12px 12px 4px; padding:8px 11px; background:var(--surface-2); line-height:1.5; }
  .chat-log .turn.human .b { background:linear-gradient(135deg,var(--accent),var(--accent-2)); border-color:transparent; color:#fff; border-radius:12px 12px 4px 12px; }
  .chat-log .turn .c { font-size:10.5px; color:var(--muted); margin-top:3px; padding:0 4px; }
  .chat-log .turn.human .c { text-align:right; }
  .chat-log .turn.wait { color:var(--muted); font-size:12px; align-self:flex-start; }
  .spin { display:inline-block; width:11px; height:11px; border:2px solid var(--muted); border-top-color:transparent; border-radius:50%; margin-right:6px; vertical-align:-1px; animation:rot .8s linear infinite; }
  @keyframes rot { to { transform:rotate(360deg); } }

  .brain { margin:0 0 30px; background:var(--surface); border:1px solid var(--line); border-radius:var(--r-lg); padding:15px 18px; box-shadow:var(--shadow); }
  .brain > summary { font-size:13px; font-weight:650; color:var(--fg); letter-spacing:-.01em; }
  .brain > summary .hint { display:inline; }
  .brain table, .stats-body table { border-collapse:collapse; font-size:12px; margin-top:12px; width:100%; }
  .brain th, .brain td, .stats-body th, .stats-body td { border-bottom:1px solid var(--line); padding:7px 10px; text-align:left; }
  .brain tr:last-child td, .stats-body tr:last-child td { border-bottom:0; }
  .brain th, .stats-body th { color:var(--muted); font-weight:600; font-size:10.5px; text-transform:uppercase; letter-spacing:.05em; }
  .stats-body { overflow-x:auto; }
  .stats-body td:nth-child(n+2), .stats-body th:nth-child(n+2) { font-family:var(--mono); }
  .stats-body th { white-space:nowrap; }
  .brain button { padding:5px 12px; border:1px solid var(--line-2); border-radius:var(--r-sm); background:var(--surface-3); color:var(--fg);
    cursor:pointer; font:inherit; font-size:12px; font-weight:600; transition:border-color .12s; }
  .brain button:hover:not(:disabled) { border-color:var(--accent); }
  .brain button:disabled { opacity:.5; }
  .empty { color:var(--muted); font-size:12.5px; padding:8px 4px; }
  .ok-t { color:var(--ok); } .err-t { color:var(--err); } .muted-t { color:var(--muted); }
</style>
</head>
<body>
<header class="topbar">
  <div class="brand">
    <span class="logo">gb</span>
    <span class="brand-name">gumo_brain</span>
    <span class="brand-sub">the Gumo Engine</span>
  </div>
  <div class="topbar-meta">
    <span class="live"><span class="live-dot"></span>live</span>
  </div>
</header>
<main class="wrap">
<div id="msg"></div>

<div class="section-label">New work</div>
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

<div class="section-label">Pipeline queue</div>
<div class="cols">
  <div class="col"><h2>Pending <span class="count" id="count-pending">0</span></h2><div id="pending"></div></div>
  <div class="col"><h2>In progress <span class="count" id="count-progress">0</span></h2><div id="progress"></div></div>
  <div class="col"><h2>Awaiting input <span class="count" id="count-awaiting">0</span></h2><div id="awaiting"></div></div>
  <div class="col"><h2>Completed <span class="count" id="count-completed">0</span></h2><div id="completed"></div></div>
</div>
</main>
<script>
const GROUPS = {
  pending: ['received', 'queued'],
  progress: ['running'],
  awaiting: ['awaiting_input'],
  completed: ['pr_opened', 'no_fix', 'skipped', 'error', 'timeout'],
};
const STAGE_NAMES = ['Intake', 'PRD', 'Recon', 'Design', 'Plan', 'Build 1', 'Build 2+', 'Test', 'Review', 'Ship'];
const KIND_LABEL = { sentry: 'sentry', task: 'request', feature: 'feature', memory: 'memory' };
const STATUS_LABEL = { received: 'Received', queued: 'Queued', running: 'Running',
  awaiting_input: 'Awaiting input', pr_opened: 'PR opened', no_fix: 'No fix',
  skipped: 'Skipped', error: 'Error', timeout: 'Timeout' };
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
  return `<div class="job" data-status="${esc(j.status)}" data-kind="${esc(j.kind)}">
    <div class="job-head">
      <div class="t">${esc(j.title || j.issue_id)}</div>
      <span class="badge ${esc(j.status)}">${esc(STATUS_LABEL[j.status] || j.status)}</span>
    </div>
    <div class="m">${kind}<span class="proj">${esc(j.project)}</span>${score}${phase}${owner}</div>
    ${strip}${links ? `<div class="m links">${links}</div>` : ''}<div class="m when">${when}</div>${ask}${rekick}${stats}</div>`;
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
    const cnt = document.getElementById('count-' + div);
    if (cnt) cnt.textContent = items.length;
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
    if (t.lane === 'fast') meta += '⚡ ';
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
  // a live-streaming bubble survives transcript repaints: re-attach the node
  const s = chatStreams[id];
  if (s && s.live) log.appendChild(s.live);
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
      if (data.pending) startChatPoll(id); else { stopChatPoll(id); stopChatStream(id); }
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
      startChatStream(id); // live tokens via SSE; polling stays as the fallback
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

// ---------- live answer stream (api/jobs/{id}/chat/stream, SSE) ----------
// Instant-messaging feel: the fast lane streams first tokens in ~1-2s; slow
// (code-reading) runs stream progress lines. Losing the stream costs nothing —
// the 5s poll above still delivers the persisted reply.

const chatStreams = {};    // job id -> { es: EventSource, live: bubble element }

function startChatStream(id) {
  stopChatStream(id);
  if (typeof EventSource === 'undefined') return;
  const log = document.getElementById('chat-log-' + id);
  if (!log) return;
  const live = document.createElement('div');
  live.className = 'turn engine';
  live.innerHTML = '<div class="b"></div><div class="c"><span class="spin"></span>engine &middot; thinking&hellip;</div>';
  log.appendChild(live);
  log.scrollTop = log.scrollHeight;
  const es = new EventSource('api/jobs/' + encodeURIComponent(id) + '/chat/stream');
  chatStreams[id] = { es: es, live: live };
  es.addEventListener('delta', (e) => {
    let t = '';
    try { t = JSON.parse(e.data).t || ''; } catch (e2) { return; }
    const b = live.querySelector('.b');
    if (b) { b.textContent += t; }
    const c = live.querySelector('.c');
    if (c) c.innerHTML = 'engine &middot; answering&hellip;';
    log.scrollTop = log.scrollHeight;
  });
  es.addEventListener('status', (e) => {
    let t = '';
    try { t = JSON.parse(e.data).t || ''; } catch (e2) { return; }
    const c = live.querySelector('.c');
    if (c) { c.textContent = 'engine · ' + t; }
    log.scrollTop = log.scrollHeight;
  });
  es.addEventListener('done', () => { stopChatStream(id); loadChat(id); });
  es.onerror = () => { stopChatStream(id); };  // 5s polling remains the fallback
}

function stopChatStream(id) {
  const s = chatStreams[id];
  if (!s) return;
  delete chatStreams[id];
  try { s.es.close(); } catch (e) { /* already closed */ }
  if (s.live && s.live.parentNode) s.live.parentNode.removeChild(s.live);
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
