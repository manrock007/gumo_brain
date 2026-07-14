# gumo_brain — notes for Claude

Self-hosted software-development engine (Sentry fixes, manual requests, and
P0–P9 feature pipelines with human gates) built on headless Claude Code runs.
README.md for the overview; **docs/ENGINE.md is the authoritative spec** for
the feature pipeline, the artifact sync invariants (human edits always win,
fail closed), and product memory — read it before touching app/engine.py,
app/artifacts.py, app/worker.py or app/memory.py. App code in `app/`; deploy
wiring lives in the gumoiac repo.

Run the tests with `python -m pytest tests/ -q` (they need fastapi/httpx from
requirements.txt plus pytest; no network, no tokens).

## Sentry PR review loop

When a PR has findings from the Sentry review bot (`sentry[bot]`), work them ALL
sequentially without stopping until the PR is green (no unresolved bot findings):

- Verify each finding first — the bot may have reviewed a stale commit.
- Real issue → fix, commit, push, reply on the thread citing the commit
  (the push triggers the bot's next pass automatically).
- Not real / already fixed → reply explaining why, then post a PR comment
  containing exactly `@sentry review` to trigger another pass (required
  whenever no new commit landed).
- Poll for the bot's next pass after each push/trigger; repeat until a pass
  produces no new findings.
