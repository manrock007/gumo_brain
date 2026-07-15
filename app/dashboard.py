"""Single-file dashboard for gumo_brain — the Gumo Engine (docs/ENGINE.md §6).

An inbox + split view (theme-aware, dark-first): the LEFT pane lists every job
as an inbox row (newest activity first, All / Active / Awaiting-you filters,
mini P0-P9 strip on features). Selecting a row (hash-routed `#/job/<id>`)
opens the RIGHT pane — that item's whole world:

- header: title, status pill, stage strip, ClickUp/PR/Sentry links, stats;
- the work thread: every stage as a collapsible card (attempts, duration,
  cost, its artifact), with the CURRENT stage streaming live tool calls and
  text over SSE (`api/jobs/{id}/session/stream`);
- the conversation: the full chat transcript across all stages, streaming
  replies live (`api/jobs/{id}/chat/stream`);
- the gate packet when parked (question, evidence, analysis, Proceed / Redo /
  Skip with a guidance box);
- a composer with an explicit Ask / Steer toggle. Ask posts to
  `api/jobs/{id}/chat` (allowed mid-run AND at gates — the fast lane answers
  from persisted artifacts; a code-run escalation queues on the repo lock).
  Steer posts to `api/jobs/{id}/session/steer` — with session persistence on
  it interrupts the running stage and resumes it with the note folded in.

With nothing selected the right pane is the intake view (Sentry fix / request
/ feature pipeline panels + the Product brain table). Typed input survives
re-renders: the composer and gate-answer DOM are never rebuilt on polls, and
drafts persist to localStorage (`gb-draft-<job_id>` / `gb-chat-<job_id>`).

NOTE: this module is one big Python string. It deliberately contains NO
backslashes — JS regexes are built with `new RegExp` + `String.fromCharCode`,
newlines via template literals or `String.fromCharCode(10)`, and CSS uses
HTML entities / literal glyphs — so Python escape handling can never mangle
the emitted HTML/JS.
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
  body { margin:0; font:14.5px/1.6 var(--font); background:var(--bg); color:var(--fg);
    -webkit-font-smoothing:antialiased; height:100vh; display:flex; flex-direction:column; overflow:hidden; }
  a { color:var(--accent); text-decoration:none; }
  a:hover { text-decoration:underline; }
  ::selection { background:var(--accent-weak); }
  ::-webkit-scrollbar { width:10px; height:10px; }
  ::-webkit-scrollbar-thumb { background:var(--line-2); border-radius:99px; border:2px solid transparent; background-clip:content-box; }
  ::-webkit-scrollbar-thumb:hover { background:var(--muted); background-clip:content-box; }

  .topbar { display:flex; align-items:center; justify-content:space-between; gap:16px;
    padding:11px 22px; border-bottom:1px solid var(--line); flex-shrink:0; }
  .brand { display:flex; align-items:center; gap:11px; min-width:0; }
  .logo { display:grid; place-items:center; width:30px; height:30px; border-radius:9px; font:700 12px/1 var(--mono);
    color:#fff; background:linear-gradient(135deg,var(--accent),var(--accent-2)); box-shadow:0 3px 12px var(--accent-weak); }
  .brand-name { font-weight:650; font-size:15px; letter-spacing:-.01em; }
  .brand-sub { color:var(--muted); font-size:13px; }
  .topbar-meta { display:flex; align-items:center; gap:14px; color:var(--muted); font-size:12.5px; }
  .live-ind { display:inline-flex; align-items:center; gap:7px; }
  .live-dot { width:7px; height:7px; border-radius:50%; background:var(--ok); animation:ping 2.4s ease-out infinite; }
  @keyframes ping { 0%{ box-shadow:0 0 0 0 rgba(63,185,80,.5); } 70%{ box-shadow:0 0 0 6px rgba(63,185,80,0); } 100%{ box-shadow:0 0 0 0 rgba(63,185,80,0); } }

  #msg { flex-shrink:0; color:var(--fg-2); font-size:12.5px; padding:0 22px; }
  #msg:not(:empty) { padding:7px 22px; border-bottom:1px solid var(--line); background:var(--surface); }

  .shell { flex:1; display:grid; grid-template-columns:minmax(340px,42%) 1fr; overflow:hidden; }
  .shell.solo { grid-template-columns:1fr 0; }
  .shell.solo .detail { display:none; }
  @media (max-width:900px) {
    .shell { grid-template-columns:1fr; }
    .shell.split .inbox { display:none; }
  }

  /* ---------- inbox (left) ---------- */
  .inbox { border-right:1px solid var(--line); overflow:auto; background:var(--bg-2); display:flex; flex-direction:column; }
  .inbox-h { position:sticky; top:0; z-index:5; background:var(--bg-2); padding:12px 16px 9px;
    border-bottom:1px solid var(--line); display:flex; align-items:center; gap:10px; }
  .inbox-h h2 { font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--fg-2); font-weight:650; margin:0; }
  .filters { margin-left:auto; display:flex; gap:6px; }
  .fchip { font-size:11px; font-weight:600; padding:3px 10px; border-radius:99px; background:var(--surface-3);
    color:var(--muted); cursor:pointer; border:0; font-family:inherit; }
  .fchip.on { background:var(--accent-weak); color:var(--accent); }
  #inbox-list { flex:1; }
  .row { display:flex; gap:11px; padding:12px 16px; border-bottom:1px solid var(--line); cursor:pointer;
    border-left:3px solid transparent; }
  .row:hover { background:var(--surface); }
  .row.sel { background:var(--surface); border-left-color:var(--accent); }
  .row .rail { width:6px; height:6px; border-radius:50%; margin-top:7px; flex-shrink:0; background:var(--line-2); }
  .row[data-status="running"] .rail { background:var(--accent); }
  .row[data-status="awaiting_input"] .rail { background:var(--warn); }
  .row[data-status="pr_opened"] .rail { background:var(--ok); }
  .row[data-status="error"] .rail, .row[data-status="timeout"] .rail { background:var(--err); }
  .row .body { min-width:0; flex:1; }
  .row .t { font-weight:600; font-size:13.5px; line-height:1.35; overflow:hidden; text-overflow:ellipsis;
    display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; }
  .row .m { font-size:11.5px; color:var(--muted); margin-top:4px; display:flex; gap:8px; align-items:center;
    font-family:var(--mono); flex-wrap:wrap; }
  .row .when { margin-left:auto; font-size:11px; color:var(--muted); white-space:nowrap; font-family:var(--mono); }
  .mini { display:flex; gap:2px; margin-top:7px; }
  .mini i { flex:1; height:4px; border-radius:2px; background:var(--surface-3); }
  .mini i.d { background:var(--accent); opacity:.8; }
  .mini i.c { background:var(--accent); box-shadow:0 0 0 2px var(--accent-weak); animation:pulse 1.5s ease-in-out infinite; }
  .mini i.c.still { animation:none; }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.35; } }
  .newbtn { margin:10px 16px 16px; display:block; width:calc(100% - 32px); padding:9px; border:1px dashed var(--line-2);
    border-radius:var(--r-sm); background:transparent; color:var(--muted); font:inherit; font-size:12.5px; cursor:pointer; }
  .newbtn:hover { border-color:var(--accent); color:var(--accent); }
  .empty { color:var(--muted); font-size:12.5px; padding:14px 16px; }

  .badge { display:inline-flex; align-items:center; gap:6px; white-space:nowrap; font-size:11px; font-weight:600;
    padding:2px 9px; border-radius:99px; background:var(--surface-3); color:var(--fg-2); }
  .badge::before { content:""; width:6px; height:6px; border-radius:50%; background:currentColor; }
  .badge.running { color:var(--accent); background:var(--accent-weak); }
  .badge.running::before { animation:pulse 1.4s ease-in-out infinite; }
  .badge.awaiting_input { color:var(--warn); background:var(--warn-weak); }
  .badge.pr_opened { color:var(--ok); background:var(--ok-weak); }
  .badge.error, .badge.timeout { color:var(--err); background:var(--err-weak); }
  .badge.queued, .badge.received { color:var(--info); background:var(--info-weak); }
  .badge.no_fix, .badge.skipped { color:var(--muted); background:var(--surface-3); }
  .badge.kind { background:transparent; color:var(--muted); border:1px solid var(--line-2); font-weight:550; }
  .badge.kind::before { display:none; }
  .chip { display:inline-flex; align-items:center; font-size:10.5px; font-weight:600; padding:1px 8px;
    border-radius:99px; background:var(--warn-weak); color:var(--warn); margin-left:8px; }

  /* ---------- detail (right) ---------- */
  .detail { display:flex; flex-direction:column; overflow:hidden; }
  .d-top { display:flex; align-items:center; gap:12px; padding:13px 20px; border-bottom:1px solid var(--line); flex-wrap:wrap; }
  .d-back { display:none; }
  @media (max-width:900px) { .d-back { display:inline-flex; } }
  .d-back, .d-x { align-items:center; padding:5px 11px; border:1px solid var(--line-2); border-radius:var(--r-sm);
    background:var(--surface-2); color:var(--fg); cursor:pointer; font:inherit; font-size:12.5px; font-weight:600; }
  .d-title { font-weight:650; font-size:14.5px; letter-spacing:-.01em; min-width:0; overflow:hidden;
    text-overflow:ellipsis; white-space:nowrap; flex:1; }
  .d-strip { display:flex; gap:3px; width:170px; flex-shrink:0; }
  .d-strip i { flex:1; height:6px; border-radius:99px; background:var(--surface-3); }
  .d-strip i.d { background:var(--accent); opacity:.85; }
  .d-strip i.c { background:var(--accent); box-shadow:0 0 0 2px var(--accent-weak); animation:pulse 1.5s ease-in-out infinite; }
  .d-strip i.c.still { animation:none; }
  .d-links { display:flex; gap:13px; font-size:12.5px; font-weight:550; flex-shrink:0; }
  .d-sub { width:100%; display:flex; gap:10px; align-items:center; font-size:11.5px; color:var(--muted); font-family:var(--mono); }

  .thread { flex:1; overflow:auto; padding:16px 20px; display:flex; flex-direction:column; gap:12px; }
  .thread > * { flex-shrink:0; } /* children must never compress to fit — the thread scrolls */
  .daydiv { text-align:center; color:var(--muted); font-size:11px; font-family:var(--mono); margin:2px 0; }
  .stage { border:1px solid var(--line); border-radius:var(--r); background:var(--surface); overflow:hidden; }
  .stage > summary { list-style:none; cursor:pointer; padding:9px 13px; display:flex; align-items:center; gap:10px;
    font-size:12.5px; user-select:none; }
  .stage > summary::-webkit-details-marker { display:none; }
  .stage .sx { font-family:var(--mono); color:var(--muted); font-size:11px; width:22px; }
  .stage .snm { font-weight:600; }
  .stage .tick { color:var(--ok); font-size:12px; }
  .stage .sm { margin-left:auto; color:var(--muted); font-size:11px; font-family:var(--mono); }
  .stage .inner { padding:2px 13px 12px; border-top:1px solid var(--line); font-size:12.5px; color:var(--fg-2); }
  .stage .art { margin-top:9px; font:11.5px/1.55 var(--mono); background:var(--bg-2); border:1px solid var(--line);
    border-radius:var(--r-sm); padding:10px; color:var(--fg-2); white-space:pre-wrap; overflow-wrap:anywhere;
    max-height:320px; overflow:auto; }
  .stage.live { border-color:var(--accent); }
  .stage.live > summary { background:var(--accent-weak); }
  .stage.live .sm { color:var(--accent); }
  .ev { font-size:12.5px; line-height:1.5; margin-top:7px; }
  .ev.status { font-family:var(--mono); font-size:11.5px; color:var(--muted); display:flex; gap:8px; align-items:baseline; }
  .ev.status::before { content:"›"; color:var(--accent); font-weight:700; }
  .ev.text { white-space:pre-wrap; overflow-wrap:anywhere; color:var(--fg); background:var(--bg-2);
    border:1px solid var(--line); border-radius:var(--r-sm); padding:9px 12px; }
  .ev.done { color:var(--ok); font-size:12px; font-weight:650; }
  .lead { color:var(--muted); font-size:12.5px; }

  .turn { max-width:86%; font-size:13px; align-self:flex-start; }
  .turn.human { align-self:flex-end; }
  .turn .b { white-space:pre-wrap; overflow-wrap:anywhere; border:1px solid var(--line);
    border-radius:12px 12px 12px 4px; padding:8px 12px; background:var(--surface-2); line-height:1.5; }
  .turn.human .b { background:linear-gradient(135deg,var(--accent),var(--accent-2)); color:#fff;
    border-color:transparent; border-radius:12px 12px 4px 12px; }
  .turn.steer .b { background:var(--warn-weak); border-color:var(--warn); color:var(--fg); }
  .turn .c { font-size:10.5px; color:var(--muted); margin-top:3px; padding:0 4px; }
  .turn.human .c { text-align:right; }
  .turn.wait { color:var(--muted); font-size:12px; }
  .spin { display:inline-block; width:10px; height:10px; border:2px solid var(--muted); border-top-color:transparent;
    border-radius:50%; margin-right:6px; vertical-align:-1px; animation:rot .8s linear infinite; }
  @keyframes rot { to { transform:rotate(360deg); } }

  .gate { border:1px solid var(--warn); border-radius:var(--r); background:var(--surface); padding:12px 14px; }
  .gate .g-h { font-size:11.5px; font-weight:650; color:var(--warn); text-transform:uppercase; letter-spacing:.06em; margin-bottom:8px; }
  .q { white-space:pre-wrap; font-size:12.5px; line-height:1.55; background:var(--bg-2); border:1px solid var(--line);
    border-radius:var(--r-sm); padding:9px 11px; max-height:190px; overflow:auto; }
  details.sub { margin-top:8px; font-size:12px; }
  details.sub summary { cursor:pointer; color:var(--fg-2); font-weight:550; user-select:none; list-style:none;
    display:inline-flex; align-items:center; gap:6px; }
  details.sub summary::-webkit-details-marker { display:none; }
  details.sub summary::before { content:"+"; font:700 12px/1 var(--mono); color:var(--muted); }
  details.sub[open] > summary::before { content:"-"; }
  details.sub pre { white-space:pre-wrap; overflow-wrap:anywhere; font:11.5px/1.55 var(--mono); background:var(--bg-2);
    border:1px solid var(--line); border-radius:var(--r-sm); padding:10px 11px; max-height:340px; overflow:auto; margin:8px 0 0; color:var(--fg-2); }
  .answer textarea { width:100%; padding:8px 11px; border:1px solid var(--line); border-radius:var(--r-sm);
    background:var(--surface-2); color:var(--fg); font:inherit; font-size:13px; resize:vertical; min-height:46px; margin-top:9px; }
  .answer textarea:focus { outline:0; border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-weak); }
  .answer .btns { display:flex; gap:8px; margin-top:8px; }
  .answer button { flex:1; padding:8px 0; border:0; border-radius:var(--r-sm); cursor:pointer; font:inherit;
    font-size:12.5px; font-weight:600; color:#fff; }
  .answer button.go { background:linear-gradient(135deg,#3fb950,#2ea043); }
  .answer button.no { background:var(--surface-3); color:var(--fg-2); }
  .answer button.redo { background:linear-gradient(135deg,var(--warn),#c8860f); }
  .answer button:disabled { opacity:.5; cursor:default; }
  .hint { font-size:12px; color:var(--muted); margin:6px 0 0; line-height:1.5; }

  .composer { border-top:1px solid var(--line); padding:11px 20px 13px; flex-shrink:0; }
  .composer .tabs { display:flex; gap:8px; margin-bottom:8px; }
  .composer .tab { font-size:12px; font-weight:600; padding:4px 12px; border-radius:99px; cursor:pointer;
    background:var(--surface-3); color:var(--muted); border:0; font-family:inherit; }
  .composer .tab.on { background:var(--accent-weak); color:var(--accent); }
  .composer .tab.steer.on { background:var(--warn-weak); color:var(--warn); }
  .composer textarea { width:100%; padding:9px 12px; border:1px solid var(--line); border-radius:var(--r-sm);
    background:var(--surface-2); color:var(--fg); font:inherit; font-size:13px; resize:none; min-height:46px; }
  .composer textarea:focus { outline:0; border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-weak); }
  .composer .row2 { display:flex; align-items:center; gap:12px; margin-top:8px; }
  .composer .send { padding:8px 20px; border:0; border-radius:var(--r-sm);
    background:linear-gradient(135deg,var(--accent),var(--accent-2)); color:#fff; font:inherit; font-size:13px;
    font-weight:650; cursor:pointer; }
  .composer .send.steer { background:linear-gradient(135deg,var(--warn),#c8860f); }
  .composer .send:disabled { opacity:.5; cursor:default; }
  .composer .chint { font-size:11.5px; color:var(--muted); }

  /* ---------- welcome / intake (right pane, nothing selected) ---------- */
  .welcome { flex:1; overflow:auto; padding:20px 22px; }
  .section-label { font-size:11.5px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted);
    font-weight:650; margin:0 0 13px; }
  .intake { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:14px; margin:0 0 26px; }
  .panel { background:var(--surface); border:1px solid var(--line); border-radius:var(--r-lg); padding:15px 17px; box-shadow:var(--shadow); }
  .panel h2 { font-size:13px; font-weight:650; letter-spacing:-.01em; color:var(--fg); margin:0 0 12px; }
  .panel input, .panel textarea, .panel select { width:100%; padding:9px 12px; margin-bottom:9px; border:1px solid var(--line);
    border-radius:var(--r-sm); background:var(--surface-2); color:var(--fg); font:inherit; font-size:13.5px; }
  .panel input:focus, .panel textarea:focus, .panel select:focus { outline:0; border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-weak); }
  .panel textarea { resize:vertical; min-height:66px; }
  .panel button { padding:9px 18px; border:0; border-radius:var(--r-sm);
    background:linear-gradient(135deg,var(--accent),var(--accent-2)); color:#fff; cursor:pointer; font:inherit;
    font-size:13.5px; font-weight:600; }
  .panel button:disabled { opacity:.5; }
  .panel .hint { margin:2px 0 9px; }
  .brain { background:var(--surface); border:1px solid var(--line); border-radius:var(--r-lg); padding:15px 17px; box-shadow:var(--shadow); }
  .brain > summary { cursor:pointer; font-size:13px; font-weight:650; color:var(--fg); list-style:none; }
  .brain > summary::-webkit-details-marker { display:none; }
  .brain table, .stats-body table { border-collapse:collapse; font-size:12px; margin-top:12px; width:100%; }
  .brain th, .brain td, .stats-body th, .stats-body td { border-bottom:1px solid var(--line); padding:7px 10px; text-align:left; }
  .brain tr:last-child td, .stats-body tr:last-child td { border-bottom:0; }
  .brain th, .stats-body th { color:var(--muted); font-weight:600; font-size:10.5px; text-transform:uppercase; letter-spacing:.05em; white-space:nowrap; }
  .stats-body { overflow-x:auto; }
  .stats-body td:nth-child(n+2), .stats-body th:nth-child(n+2) { font-family:var(--mono); }
  .brain button { padding:5px 12px; border:1px solid var(--line-2); border-radius:var(--r-sm); background:var(--surface-3);
    color:var(--fg); cursor:pointer; font:inherit; font-size:12px; font-weight:600; }
  .brain button:disabled { opacity:.5; }
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
    <span class="live-ind"><span class="live-dot"></span>live</span>
  </div>
</header>
<div id="msg"></div>

<div class="shell solo" id="shell">
  <aside class="inbox">
    <div class="inbox-h">
      <h2>Inbox</h2>
      <div class="filters">
        <button class="fchip on" data-f="all" onclick="setFilter('all')">All</button>
        <button class="fchip" data-f="active" onclick="setFilter('active')">Active</button>
        <button class="fchip" data-f="await" onclick="setFilter('await')">Awaiting you</button>
      </div>
    </div>
    <div id="inbox-list"><div class="empty">loading&hellip;</div></div>
    <button class="newbtn" onclick="openNew()">+ New work &mdash; Sentry fix / request / feature pipeline</button>
  </aside>

  <section class="detail">
    <div class="welcome" id="welcome">
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
            <div class="hint">Claude analyses first and comes back with its plan + questions before touching code.</div>
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
            <div class="hint">P0 Intake &rarr; P9 Ship; every stage parks in the inbox (and on ClickUp) for your Proceed / Redo / Skip.</div>
            <button id="feat-go">Start pipeline</button>
          </form>
        </div>
      </div>
      <details class="brain">
        <summary>Product brain <span class="hint" style="display:inline">&mdash; per-repo memory freshness</span></summary>
        <div id="brain-body"><div class="empty">loading&hellip;</div></div>
      </details>
    </div>

    <div id="dpane" style="display:none; flex:1; min-height:0; flex-direction:column;">
      <div class="d-top">
        <button class="d-back" onclick="closeItem()">&larr;</button>
        <span class="d-title" id="d-title">&hellip;</span>
        <span class="badge" id="d-status"></span>
        <span class="d-strip" id="d-strip"></span>
        <span class="d-links" id="d-links"></span>
        <div class="d-sub" id="d-sub"></div>
      </div>
      <div class="thread" id="d-thread"></div>
      <div class="composer" id="composer">
        <div class="tabs">
          <button class="tab on" id="tab-ask" onclick="setMode('ask')">Ask</button>
          <button class="tab steer" id="tab-steer" onclick="setMode('steer')">Steer the run</button>
        </div>
        <textarea id="c-in" data-draft="" placeholder=""></textarea>
        <div class="row2">
          <button class="send" id="c-send" onclick="sendComposer()">Send</button>
          <span class="chint" id="c-hint"></span>
        </div>
      </div>
    </div>
  </section>
</div>

<script>
const STAGE_NAMES = ['Intake', 'PRD', 'Recon', 'Design', 'Plan', 'Build 1', 'Build 2+', 'Test', 'Review', 'Ship'];
const KIND_LABEL = { sentry: 'sentry', task: 'request', feature: 'feature', memory: 'memory' };
const STATUS_LABEL = { received: 'Received', queued: 'Queued', running: 'Running',
  awaiting_input: 'Awaiting you', pr_opened: 'PR opened', no_fix: 'No fix',
  skipped: 'Skipped', error: 'Error', timeout: 'Timeout' };
const LIVE = ['received', 'queued', 'running', 'awaiting_input'];
const NL = String.fromCharCode(10);

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
  return `<div class="row${j.issue_id === sel ? ' sel' : ''}" data-status="${esc(j.status)}" onclick="openItem('${esc(j.issue_id)}')">
    <span class="rail"></span>
    <div class="body">
      <div class="t">${esc(j.title || j.issue_id)}</div>
      <div class="m"><span class="badge ${esc(j.status)}">${esc(STATUS_LABEL[j.status] || j.status)}</span>${esc(j.project)}${stageTxt}</div>
      ${j.kind === 'feature' ? rowMini(j) : ''}
    </div>
    <span class="when">${fmtAgo(j.updated_at)}</span>
  </div>`;
}

function renderInbox() {
  let items = jobsCache.slice().sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
  if (filter === 'active') items = items.filter(j => LIVE.includes(j.status));
  if (filter === 'await') items = items.filter(j => j.status === 'awaiting_input');
  document.getElementById('inbox-list').innerHTML =
    items.length ? items.map(row).join('') : '<div class="empty">nothing here</div>';
}

let lastJobs = '';
async function refresh(force) {
  try {
    const jobs = await (await fetch('api/jobs')).json();
    const sig = JSON.stringify(jobs);
    if (force || sig !== lastJobs) {
      lastJobs = sig; jobsCache = jobs; renderInbox();
      // a selected non-feature item renders from the jobs row — keep it fresh
      if (sel) { const j = jobsCache.find(x => x.issue_id === sel); if (j && j.kind !== 'feature') renderV1Detail(j); }
    }
  } catch (e) { document.getElementById('msg').textContent = 'refresh failed: ' + e; }
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
  const j = jobsCache.find(x => x.issue_id === id);
  if (j && j.kind !== 'feature') { renderV1Detail(j); return; }
  try {
    const r = await fetch('api/jobs/' + encodeURIComponent(id) + '/session');
    if (!r.ok) {
      if (r.status === 404) closeItem();
      if (r.status === 409) { const jj = jobsCache.find(x => x.issue_id === id); if (jj) renderV1Detail(jj); }
      return;
    }
    const data = await r.json();
    const sig = JSON.stringify(data);
    if (sig === lastSnap) return;
    lastSnap = sig;
    renderFeatureDetail(data);
    if (data.live) startStageStream(id);
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
      : `<div class="inner">${a ? `<div class="art">${esc(a.content || '(empty)')}${a.truncated ? '… (truncated)' : ''}</div>` : '<div class="lead" style="margin-top:8px">no artifact for this stage</div>'}</div>`;
    h += `<details class="stage${isLive ? ' live' : ''}" data-key="st-${stg}"${isLive ? ' open' : ''}>
      <summary><span class="sx">P${stg}</span><span class="snm">${esc(STAGE_NAMES[stg] || '')}</span>
      ${!isLive && s.status === 'done' ? '<span class="tick">&#10003;</span>' : ''}<span class="sm">${meta}</span></summary>
      ${inner}</details>`;
  }
  return h || '<div class="lead">no stage runs yet</div>';
}

function convoTurns(data) {
  const turns = data.chat || [];
  let h = '';
  for (const t of turns) {
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

function steerTurns(data) {
  // steers live in the guidance log — show them in the conversation flow
  let h = '';
  for (const g of data.guidance || []) {
    if (g.action !== 'steer') continue;
    h += `<div class="turn human steer"><div class="b">&#8618; Steer: ${esc(g.text)}</div><div class="c">steer &middot; P${esc(String(g.stage))}</div></div>`;
  }
  return h;
}

function gatePacket(data) {
  const j = data.job;
  if (j.status !== 'awaiting_input' && j.status !== 'error' && j.status !== 'timeout') return '';
  const id = esc(j.issue_id);
  if (j.status !== 'awaiting_input') {
    return `<div class="gate"><div class="g-h">Stage ${j.status} — re-kick</div>
      <div class="answer"><div class="btns">
      <button class="redo" onclick="answer('${id}','redo',this)">Re-kick (redo)</button></div></div></div>`;
  }
  const isAsk = j.gate_kind === 'ask';
  const goLabel = isAsk ? 'Answer' : 'Proceed';
  return `<div class="gate"><div class="g-h">${isAsk ? 'The engine asks — resumes in place' : 'Gate — P' + j.stage + ' ' + esc(j.stage_name || '')}</div>
    <div class="q">${esc(j.question || '(see analysis)')}</div>
    ${j.evidence ? `<details class="sub" data-key="g-ev"><summary>evidence</summary><pre>${linkify(j.evidence)}</pre></details>` : ''}
    ${j.analysis ? `<details class="sub" data-key="g-an"><summary>full analysis</summary><pre>${esc(j.analysis)}</pre></details>` : ''}
    <div class="answer"><textarea id="ans-${id}" data-draft="gb-draft-${id}" placeholder="Your answer / guidance&hellip;"></textarea>
    <div class="hint">Redo re-runs this stage with your notes as corrections &mdash; optionally prefix 'P3 ' to redo an earlier stage.</div>
    <div class="btns">
      <button class="go" onclick="answer('${id}','proceed',this)">${goLabel}</button>
      <button class="redo" onclick="answer('${id}','redo',this)">Redo</button>
      <button class="no" onclick="answer('${id}','skip',this)">Skip</button>
    </div></div></div>`;
}

function renderFeatureDetail(data) {
  const j = data.job;
  let links = '';
  if (j.clickup_task_url) links += `<a href="${esc(j.clickup_task_url)}" target="_blank">ClickUp</a>`;
  if (j.pr_url) links += `<a href="${esc(j.pr_url)}" target="_blank">PR</a>`;
  const sub = `P${j.stage} ${esc(j.stage_name || '')} &middot; ${esc(j.project)} &middot; ${esc(j.gate_mode)} gates`
    + `<span style="margin-left:auto"><details class="sub" style="margin:0" data-key="d-stats"><summary>stats</summary></details></span>`;
  dHeader(j.title, j.status, links, sub, Number(j.stage) || 0, true, data.live);

  const st = threadState();
  document.getElementById('d-thread').innerHTML =
    '<div class="daydiv">&mdash; pipeline &mdash;</div>'
    + stageCards(data)
    + '<div class="daydiv">&mdash; conversation &mdash;</div>'
    + (convoTurns(data) + steerTurns(data) || '<div class="lead">no messages yet &mdash; ask the engine anything about this work</div>')
    + gatePacket(data);
  restoreThread(st);

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

  // composer state
  const running = j.status === 'running' || data.live;
  const steerTab = document.getElementById('tab-steer');
  steerTab.style.display = '';
  if (!running && composerMode === 'steer') setMode('ask');
  steerTab.disabled = !running && j.status !== 'awaiting_input';
  updateComposerHint(data);
  const send = document.getElementById('c-send');
  send.disabled = composerMode === 'ask' && (data.chat_limit || data.chat_pending);
  document.getElementById('composer').style.display = '';
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

// non-feature (sentry / request / memory) detail: question + evidence + answer
function renderV1Detail(j) {
  const id = esc(j.issue_id);
  let links = '';
  if (j.issue_url) links += `<a href="${esc(j.issue_url)}" target="_blank">Sentry</a>`;
  if (j.clickup_task_url) links += `<a href="${esc(j.clickup_task_url)}" target="_blank">ClickUp</a>`;
  if (j.pr_url) links += `<a href="${esc(j.pr_url)}" target="_blank">PR</a>`;
  const score = j.score != null ? ` &middot; score ${j.score}` : '';
  const phase = j.kind === 'task' && j.phase > 1 ? ' &middot; phase 2' : '';
  dHeader(j.title || j.issue_id, j.status, links,
          `${esc(KIND_LABEL[j.kind] || j.kind)} &middot; ${esc(j.project)}${score}${phase}`, 0, false, false);
  const st = threadState();
  let h = '';
  if (j.status === 'awaiting_input') {
    h += `<div class="gate"><div class="g-h">Needs your input</div>
      <div class="q">${esc(j.question || (j.detail || '').slice(0, 500))}</div>
      ${j.evidence ? `<details class="sub" data-key="g-ev"><summary>evidence</summary><pre>${linkify(j.evidence)}</pre></details>` : ''}
      ${j.analysis ? `<details class="sub" data-key="g-an"><summary>full analysis</summary><pre>${esc(j.analysis)}</pre></details>` : ''}
      <div class="answer"><textarea id="ans-${id}" data-draft="gb-draft-${id}" placeholder="Your answer / guidance&hellip;"></textarea>
      <div class="btns">
        <button class="go" onclick="answer('${id}','proceed',this)">Proceed</button>
        <button class="no" onclick="answer('${id}','skip',this)">Skip</button>
      </div></div></div>`;
  } else {
    h += `<div class="lead">${esc(STATUS_LABEL[j.status] || j.status)}${j.detail ? ' &mdash; ' + esc(String(j.detail).slice(0, 600)) : ''}</div>`;
    if (j.analysis) h += `<details class="sub" data-key="g-an"><summary>analysis</summary><pre>${esc(j.analysis)}</pre></details>`;
  }
  document.getElementById('d-thread').innerHTML = h;
  restoreThread(st);
  document.getElementById('composer').style.display = 'none';  // chat is feature-only
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

function setMode(m) {
  composerMode = m;
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
    el.innerHTML = 'Ask anytime (mid-run or at a gate). Switch to <b>Steer</b> to redirect the running stage.';
  }
}

async function sendComposer() {
  if (!sel) return;
  const box = document.getElementById('c-in');
  const text = (box.value || '').trim();
  if (!text) return;
  const btn = document.getElementById('c-send');
  btn.disabled = true;
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
      } else msg.textContent = 'Steer: ' + (data.detail || r.status);
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
        msg.textContent = 'Chat: ' + (data.detail || r.status);
        if (r.status === 409) { lastSnap = ''; loadDetail(sel); }
      }
    }
  } catch (e) { msg.textContent = 'Error: ' + e; }
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
  btn.disabled = true; msg.textContent = 'Recording decision…';
  try {
    const r = await fetch('api/jobs/' + encodeURIComponent(id) + '/answer', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action, answer: text}),
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
    if (r.ok) refresh(true);
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

// ---------- keyboard + init ----------

document.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && document.activeElement
      && document.activeElement.id === 'c-in') { e.preventDefault(); sendComposer(); }
  else if (e.key === 'Escape' && sel
      && (!document.activeElement || document.activeElement.id !== 'c-in')) closeItem();
});
window.addEventListener('hashchange', routeHash);

loadProjects(); refresh(true); refreshMemory(true); routeHash();
setInterval(() => refresh(false), 10000);
setInterval(() => refreshMemory(false), 60000);
</script>
</body>
</html>"""
