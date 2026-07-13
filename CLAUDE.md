# gumo_brain — notes for Claude

Self-hosted Sentry → headless Claude Code autofix service. See README.md for
architecture; app code in `app/`, deploy wiring lives in the gumoiac repo.

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
