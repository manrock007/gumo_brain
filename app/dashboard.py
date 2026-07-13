"""Single-file dashboard: pending / in-progress / awaiting-input / completed jobs."""

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>gumo_brain</title>
<style>
  :root { color-scheme: light dark; --bg:#fff; --fg:#1a1a2e; --muted:#667; --card:#f4f5f7; --accent:#5b5bd6; --ok:#1a7f37; --warn:#b35900; --err:#c0392b; }
  @media (prefers-color-scheme: dark) { :root { --bg:#101014; --fg:#e8e8ee; --muted:#99a; --card:#1c1c24; } }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.5 -apple-system,system-ui,sans-serif; background:var(--bg); color:var(--fg); padding:24px; }
  h1 { font-size:20px; margin:0 0 4px; } h1 small { color:var(--muted); font-weight:400; }
  .trigger { display:flex; gap:8px; margin:16px 0 24px; max-width:640px; }
  .trigger input { flex:1; padding:8px 12px; border:1px solid var(--muted); border-radius:8px; background:var(--bg); color:var(--fg); }
  .trigger button { padding:8px 16px; border:0; border-radius:8px; background:var(--accent); color:#fff; cursor:pointer; }
  .trigger button:disabled { opacity:.5; }
  #msg { margin:-12px 0 20px; color:var(--muted); min-height:1.2em; }
  .cols { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:16px; }
  .col h2 { font-size:13px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:0 0 8px; }
  .job { background:var(--card); border-radius:10px; padding:10px 12px; margin-bottom:8px; }
  .job .t { font-weight:600; font-size:14px; overflow:hidden; text-overflow:ellipsis; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; }
  .job .m { font-size:12px; color:var(--muted); margin-top:4px; }
  .job a { color:var(--accent); text-decoration:none; margin-right:10px; }
  .badge { display:inline-block; font-size:11px; padding:1px 8px; border-radius:99px; background:var(--muted); color:var(--bg); margin-right:6px; }
  .badge.pr_opened { background:var(--ok); } .badge.awaiting_input { background:var(--warn); }
  .badge.error,.badge.timeout { background:var(--err); }
  .empty { color:var(--muted); font-size:13px; }
</style>
</head>
<body>
<h1>gumo_brain <small>Sentry &rarr; Claude autofix</small></h1>
<div id="msg"></div>
<form class="trigger" onsubmit="return trigger(event)">
  <input id="ref" placeholder="Sentry issue id, short id (GUMO-1A) or URL" required>
  <button id="go">Fix it</button>
</form>
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
  return `<div class="job"><div class="t">${esc(j.title || j.issue_id)}</div>
    <div class="m"><span class="badge ${esc(j.status)}">${esc(j.status)}</span>${esc(j.project)}${score}${phase}</div>
    <div class="m">${links}</div><div class="m">${when}</div></div>`;
}
async function refresh() {
  try {
    const jobs = await (await fetch('api/jobs')).json();
    for (const [div, statuses] of Object.entries(GROUPS)) {
      const items = jobs.filter(j => statuses.includes(j.status));
      document.getElementById(div).innerHTML =
        items.length ? items.map(card).join('') : '<div class="empty">none</div>';
    }
  } catch (e) { document.getElementById('msg').textContent = 'refresh failed: ' + e; }
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
    if (r.ok) { document.getElementById('ref').value = ''; refresh(); }
  } catch (e) { msg.textContent = 'Error: ' + e; }
  btn.disabled = false;
  return false;
}
refresh(); setInterval(refresh, 10000);
</script>
</body>
</html>"""
