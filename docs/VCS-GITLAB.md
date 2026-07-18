# VCS seam (H2) — GitHub → GitLab mapping

The VCS seam (`app/vcs.py`) abstracts the version-control / PR host behind the
`VCS` ABC. The **current and default** driver is GitHub (`app/github.py`,
`GitHubVCS`); a `GitLabVCS` scaffold ships inert (`enabled=False`, PR ops are
no-ops; `clone_url` still returns a valid HTTPS URL so a checkout never wedges;
`mint_git_token` returns None → PAT fallback). This doc is the concept map a
real GitLab driver must satisfy — **driver-config**, not an engine change.

## Interface

Two responsibilities:

- **Repo plumbing** — `clone_url(repo)` (used by `fixer.prepare_workspace` /
  `prepare_feature_workspace`) and `async mint_git_token(repo)` (Epic G1
  per-repo installation token; None → the run uses the PAT env default).
- **PR lifecycle** — `get_pr`, `create_pr`, `mark_ready`, `comment` (issue
  comment = the `@sentry review` trigger channel), `reply_to_review_comment`,
  `list_comments`, `get_review_comments`, `get_comment_reactions`.

Best-effort throughout: every method returns `None`/`False` on failure and
never raises. `enabled` gates the PR shepherd (`worker.py` reads
`self.engine.github.enabled`).

## Concept map

| GitHub | GitLab | Notes |
| --- | --- | --- |
| Pull request | Merge request | `create_pr` → `POST /projects/:id/merge_requests`. Returns `web_url`. |
| Draft PR (`draft: true`) | Draft MR | GitLab marks draft via a `Draft:` title prefix or the `draft` param. |
| `markPullRequestReadyForReview` (GraphQL mutation + node_id) | `PUT /merge_requests/:iid?draft=false` (REST) | GitLab un-drafts via REST — no GraphQL node-id dance. |
| Issue comment (`/issues/:n/comments`) | MR note (`/merge_requests/:iid/notes`) | The `@sentry review` trigger channel. |
| Line review comment + reply (`/pulls/:n/comments`, `/replies`) | MR discussion + note reply (`/discussions`, `/discussions/:id/notes`) | Threaded review = GitLab discussions. |
| 🎉 reaction on the trigger comment (`/reactions`) | Award emoji on the note (`/award_emoji`) | The bot's clean-pass signal maps to `tada` award emoji. |
| PR facts: `draft`, `state`, `merged`, `node_id` | MR: `work_in_progress`/`draft`, `state`, `merged_at`, `iid` | `get_pr` shape must be normalized so shepherd logic reads the same keys, or the shepherd adapts per-driver. |
| GitHub App installation token | GitLab project/group access token or CI `CI_JOB_TOKEN` | `mint_git_token` maps to minting a short-lived project access token; None → PAT. |
| PR **number** | MR **iid** | Identifier semantics differ (repo-scoped iid vs global). |

## No clean GitLab analogue

- **The `sentry[bot]` review loop** (CLAUDE.md "Sentry PR review loop") is a
  GitHub-App code-review integration. There is no drop-in GitLab equivalent;
  wiring an equivalent bot is a **driver-config concern**, not engine code. The
  seam only needs the comment/reply/reaction surface to drive whatever review
  bot the GitLab deployment uses.

## Enabling a real driver

1. Implement `class GitLabVCS(VCS)` against the surface above, normalizing the
   `get_pr` dict keys the shepherd reads.
2. Set `enabled` from real credential presence.
3. `vcs_for` already routes `gitlab`; set `VCS_PROVIDER=gitlab`.
