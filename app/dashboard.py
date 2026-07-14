"""Single-file dashboard: intake (Sentry issue or manual request) + job queue.

Awaiting-input jobs surface Claude's questions inline; answers are posted back to
the ClickUp ticket (keeper of record) and advance the job to phase 2.
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
  .empty { color:var(--muted); font-size:13px; }
  .q { white-space:pre-wrap; font-size:13px; background:var(--bg); border:1px solid var(--line); border-radius:8px; padding:8px 10px; margin-top:8px; max-height:180px; overflow:auto; }
  details { margin-top:6px; font-size:12px; }
  details summary { cursor:pointer; color:var(--muted); }
  details pre { white-space:pre-wrap; font:12px/1.5 ui-monospace,monospace; background:var(--bg); border:1px solid var(--line); border-radius:8px; padding:8px 10px; max-height:320px; overflow:auto; }
  .answer textarea { width:100%; margin-top:8px; padding:6px 10px; border:1px solid var(--line); border-radius:8px; background:var(--bg); color:var(--fg); font:inherit; font-size:13px; resize:vertical; min-height:44px; }
  .answer .btns { display:flex; gap:8px; margin-top:6px; }
  .answer button { flex:1; padding:6px 0; border:0; border-radius:8px; cursor:pointer; font:inherit; font-size:13px; color:#fff; }
  .answer button.go { background:var(--ok); } .answer button.no { background:var(--err); }
  .answer button:disabled { opacity:.5; }
</style>
</head>
<body>
<h1>gumo_brain <small>Sentry + requests &rarr; Claude autofix</small></h1>
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
</div>

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
function esc(s) { const d = document.createElement('span'); d.textContent = s || ''; return d.innerHTML; }
function card(j) {
  let links = '';
  if (j.issue_url) links += `<a href="${esc(j.issue_url)}" target="_blank">Sentry</a>`;
  if (j.clickup_task_url) links += `<a href="${esc(j.clickup_task_url)}" target="_blank">ClickUp</a>`;
  if (j.pr_url) links += `<a href="${esc(j.pr_url)}" target="_blank">PR</a>`;
  const when = new Date(j.updated_at * 1000).toLocaleString();
  const score = j.score != null ? ` &middot; score ${j.score}` : '';
  const phase = j.phase > 1 ? ' &middot; phase 2' : '';
  const kind = `<span class="badge kind">${j.kind === 'task' ? 'request' : 'sentry'}</span>`;
  let ask = '';
  if (j.status === 'awaiting_input') {
    const id = esc(j.issue_id);
    ask = `<div class="q">${esc(j.question || (j.detail || '').slice(0, 500))}</div>`
      + (j.analysis ? `<details><summary>full analysis</summary><pre>${esc(j.analysis)}</pre></details>` : '')
      + `<div class="answer"><textarea id="ans-${id}" placeholder="Your answer / guidance&hellip;"></textarea>
         <div class="btns"><button class="go" onclick="answer('${id}','proceed',this)">Proceed</button>
         <button class="no" onclick="answer('${id}','skip',this)">Skip</button></div></div>`;
  }
  return `<div class="job"><div class="t">${esc(j.title || j.issue_id)}</div>
    <div class="m"><span class="badge ${esc(j.status)}">${esc(j.status)}</span>${kind}${esc(j.project)}${score}${phase}</div>
    <div class="m">${links}</div><div class="m">${when}</div>${ask}</div>`;
}
function renderJobs(jobs) {
  for (const [div, statuses] of Object.entries(GROUPS)) {
    const items = jobs.filter(j => statuses.includes(j.status));
    document.getElementById(div).innerHTML =
      items.length ? items.map(card).join('') : '<div class="empty">none</div>';
  }
}
let lastJobs = '';
async function refresh(force) {
  try {
    const jobs = await (await fetch('api/jobs')).json();
    const sig = JSON.stringify(jobs);
    // don't wipe half-typed answers on the 10s poll unless something changed
    if (force || sig !== lastJobs) { lastJobs = sig; renderJobs(jobs); }
  } catch (e) { document.getElementById('msg').textContent = 'refresh failed: ' + e; }
}
async function loadProjects() {
  try {
    const ps = await (await fetch('api/projects')).json();
    document.getElementById('task-project').innerHTML =
      '<option value="" disabled selected>Project&hellip;</option>' +
      ps.map(p => `<option value="${esc(p.slug)}">${esc(p.slug)} (${esc(p.repo)})</option>`).join('');
  } catch (e) { /* keep empty select; submit will fail loudly */ }
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
async function answer(id, action, btn) {
  const msg = document.getElementById('msg');
  const box = document.getElementById('ans-' + id);
  const text = box ? box.value.trim() : '';
  if (action === 'skip' && !confirm('Drop this job?')) return;
  btn.disabled = true; msg.textContent = 'Recording decision on the ClickUp ticket…';
  try {
    const r = await fetch('api/jobs/' + encodeURIComponent(id) + '/answer', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action, answer: text}),
    });
    const data = await r.json();
    msg.textContent = r.ok
      ? (action === 'proceed' ? 'Answer recorded — fix queued.' : 'Skipped.')
      : 'Error: ' + (data.detail || r.status);
    if (r.ok) refresh(true);
  } catch (e) { msg.textContent = 'Error: ' + e; }
  btn.disabled = false;
}
loadProjects(); refresh(true); setInterval(() => refresh(false), 10000);
</script>
</body>
</html>"""
