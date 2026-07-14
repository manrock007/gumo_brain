"""Feature pipeline orchestration (docs/ENGINE.md §2, §5).

One call to `run_stage` = one stage execution: resume branch → pull human
edits → record baseline → run Claude → engine checkpoint (commit+push, even on
failure) → mirror artifacts → park at the gate. Fail-closed everywhere:
nothing advances without an explicit STAGE_DONE and a human answer.
"""

import logging
import re
import time
from pathlib import Path

from .artifacts import ArtifactSync, artifact_path, feature_dir, list_artifacts, normalize
from .clickup import ClickUp
from .config import Settings
from .db import JobStore
from .feature_prompts import (
    STAGES,
    build_bootstrap_prompt,
    build_stage_prompt,
    stage_artifact,
    stage_kind,
    stage_name,
)
from .fixer import (
    BASE_ALLOWED_TOOLS,
    BranchLostError,
    git,
    prepare_feature_workspace,
    run_claude_raw,
)
from .memory import MemoryReader
from .prompts import _test_block

log = logging.getLogger("brain.engine")

GATE_PREFIX = "**[gumo_brain]**"
DOC_STAGE_TOOLS = ["Read", "Grep", "Glob"]

PR_LINE_RE = re.compile(
    r"^[\s`*>-]*PR_URL:\s*`?(https://github\.com/[\w./-]+/pull/\d+)`?[\s`]*$", re.MULTILINE
)
QUESTION_HEADING_RE = re.compile(r"^#{1,4}\s*(?:open\s+)?questions?\b.*$", re.IGNORECASE | re.MULTILINE)
BUILD_GROUP_RE = re.compile(r"^#{1,4}\s*build\s+group\b", re.IGNORECASE | re.MULTILINE)


def parse_stage_output(text: str) -> tuple[str, str, str | None]:
    """(marker, payload, pr_url). Marker: done | fail | unparsed. End-anchored:
    the LAST line-start occurrence wins; a bare URL elsewhere never counts."""
    text = text or ""
    pr_matches = PR_LINE_RE.findall(text)
    pr_url = pr_matches[-1] if pr_matches else None

    done_positions = [m for m in re.finditer(r"^STAGE_DONE:", text, re.MULTILINE)]
    fail_positions = [m for m in re.finditer(r"^STAGE_FAIL:", text, re.MULTILINE)]
    last_done = done_positions[-1].start() if done_positions else -1
    last_fail = fail_positions[-1].start() if fail_positions else -1

    if last_done == -1 and last_fail == -1:
        return "unparsed", text.strip(), pr_url
    if last_done >= last_fail:
        payload = text[last_done + len("STAGE_DONE:"):].strip()
        return "done", payload, pr_url
    payload = text[last_fail + len("STAGE_FAIL:"):].strip()
    return "fail", payload, pr_url


def extract_questions_last(analysis: str) -> str:
    """Take the LAST questions heading (stage payloads embed earlier artifacts)."""
    matches = list(QUESTION_HEADING_RE.finditer(analysis or ""))
    if matches:
        rest = analysis[matches[-1].end():]
        nxt = re.search(r"^#{1,4}\s", rest, re.MULTILINE)
        section = (rest[: nxt.start()] if nxt else rest).strip()
        if section:
            return section[:1500]
    return (analysis or "").strip()[-600:]


class Engine:
    def __init__(self, settings: Settings, store: JobStore, clickup: ClickUp):
        self.settings = settings
        self.store = store
        self.clickup = clickup
        self.sync = ArtifactSync(store, clickup, settings.clickup_mirror_max_chars)
        self.memory = MemoryReader(settings)

    # ---------- the one entry point ----------

    async def run_stage(self, job: dict, queued_at: float | None = None):
        job_id = job["issue_id"]
        stage = int(job.get("stage") or 0)
        target = self.settings.repo_for_project(job.get("project") or "")
        if target is None:
            self.store.set_status(job_id, "skipped", detail=f"no repo mapped for '{job.get('project')}'")
            return
        branch = f"brain/feat-{job_id}"

        state = self.store.stage_state_get(job_id, stage) or {"attempts": 0, "base_sha": ""}
        attempt = int(state["attempts"]) + 1
        run_id = self.store.stage_run_open(job_id, stage, attempt, queued_at)
        self.store.set_fields(job_id, run_started_at=time.time(), stage_attempts=attempt)
        job["stage_attempts"] = attempt  # keep the local view consistent for _park
        self.store.set_status(job_id, "running")
        await self.clickup.set_status(job.get("clickup_task_id") or "", "running")

        try:
            return await self._run_stage_inner(job, stage, run_id, target, branch, queued_at)
        except BranchLostError as e:
            self.store.stage_run_close(run_id, "branch_lost")
            self.store.set_status(job_id, "error", detail=str(e))
            await self._comment(job, f"Pipeline halted at P{stage}: {e}")
            return
        except Exception:
            # close the telemetry row before the worker's generic error handling
            self.store.stage_run_close(run_id, "exception")
            raise

    async def _run_stage_inner(self, job: dict, stage: int, run_id: int, target,
                               branch: str, queued_at: float | None):
        job_id = job["issue_id"]
        state = self.store.stage_state_get(job_id, stage) or {"attempts": 0, "base_sha": ""}
        attempt = int(state["attempts"]) + 1
        workspace = await prepare_feature_workspace(self.settings, target, branch, stage)

        # An explicit redo of THIS code stage rewinds to the stage baseline,
        # preserving the rejected attempt under refs/gumo/. This keys off the
        # pending_redo_stage flag set by the human's answer — `attempt > 1` alone
        # also fires when the pipeline merely re-advances through this stage after
        # an earlier-stage redo, which would hard-reset to a now-stale baseline.
        redo_notes = self._pending_redo_notes(job_id, stage)
        if (job.get("pending_redo_stage") == stage and stage_kind(stage) == "code"
                and state.get("base_sha")):
            await git(workspace, "update-ref",
                      f"refs/gumo/{job_id}/P{stage}-attempt-{attempt - 1}", "HEAD")
            await git(workspace, "push", "origin",
                      f"refs/gumo/{job_id}/P{stage}-attempt-{attempt - 1}")
            await git(workspace, "reset", "--hard", state["base_sha"])
        if job.get("pending_redo_stage") == stage:
            self.store.set_fields(job_id, pending_redo_stage=None)

        # Fold in human edits AFTER any rewind, so they land on the baseline and
        # can never be discarded by the reset. pull() pushes each edit to origin
        # before advancing its synced_hash, so a mid-run crash can't lose an edit
        # the bookkeeping already marked synced.
        edited = await self.sync.pull(workspace, job, branch=branch)
        await self._write_guidance_file(workspace, job)

        # P6 auto-skips when the (post-pull) plan has a single build group
        if stage == 6 and not self._plan_has_multiple_groups(workspace, job_id):
            self.store.stage_run_close(run_id, "skipped_single_group")
            self.store.set_fields(job_id, stage=7, stage_attempts=0)
            self.store.set_status(job_id, "queued")
            log.info("job %s: P6 auto-skipped (single build group)", job_id)
            await self._comment(job, "P6 auto-skipped — the plan has a single build group. Queued P7.")
            return "requeue"

        code, head = await git(workspace, "rev-parse", "HEAD")
        base_sha = head.strip() if code == 0 else ""
        self.store.stage_state_set(job_id, stage, base_sha=base_sha, bump_attempts=True)

        prompt = await self._build_prompt(job, stage, target, branch, workspace,
                                          redo_notes, edited)
        kind = stage_kind(stage)
        tools = DOC_STAGE_TOOLS if kind == "doc" else BASE_ALLOWED_TOOLS + target.allow
        timeout = (self.settings.doc_stage_timeout_seconds if kind == "doc"
                   else self.settings.claude_timeout_seconds)

        log.info("job %s: running P%s (%s) attempt %s", job_id, stage, stage_name(stage), attempt)
        raw = await run_claude_raw(self.settings, workspace, prompt, tools, timeout)

        try:
            await self._after_run(job, stage, run_id, target, branch, workspace, raw, base_sha)
        finally:
            # durability: whatever happened, nothing may exist only in the workspace.
            # Never raise from here — a hiccup after a successful gate park must not
            # flip the job to error and double-close the telemetry row.
            try:
                await self._checkpoint(workspace, branch, job_id, stage)
                await self.memory.refresh_cache(job.get("project") or "", workspace, target.base)
            except Exception:
                log.exception("post-run checkpoint/cache refresh failed for %s", job_id)

    # ---------- post-run handling ----------

    async def _after_run(self, job, stage, run_id, target, branch, workspace, raw, base_sha):
        job_id = job["issue_id"]
        if raw.status in ("timeout", "error"):
            self.store.stage_run_close(run_id, raw.status, **self._meta(raw))
            self.store.set_status(job_id, raw.status, detail=raw.text[:2000])
            await self._comment(
                job, f"P{stage} ({stage_name(stage)}) ended with `{raw.status}`: "
                     f"{raw.text[:400]}\n\nRe-kick with `/redo <notes>` here or on the dashboard.")
            return

        marker, payload, pr_url = parse_stage_output(raw.text)

        if marker == "done" and stage_kind(stage) == "doc":
            # engine owns the artifact for document stages
            content = payload if payload.strip() else "(empty stage output)"
            await self.sync.commit_file(
                workspace, job_id, stage_artifact(stage), content,
                f"P{stage} {stage_name(stage)}: artifact",
            )

        # push branch BEFORE mirroring so evidence links resolve. Fail closed:
        # a gate must never advertise (diffstat, compare link) work that never
        # reached origin, and the next stage reads origin/<branch>.
        if not await self._checkpoint(workspace, branch, job_id, stage):
            self.store.stage_run_close(run_id, "push_failed", **self._meta(raw))
            self.store.set_status(job_id, "error",
                                  detail="branch push to origin failed — stage output is only in "
                                         "the workspace; redo after fixing connectivity/auth")
            await self._comment(job, f"P{stage} finished but the branch push FAILED — not parking "
                                     "a gate on unpushed work. `/redo` once resolved.")
            return
        conflicted = await self.sync.push(workspace, job)

        if marker == "fail":
            self.store.stage_run_close(run_id, "stage_fail", **self._meta(raw))
            await self._park(job, stage, run_id, workspace, target, base_sha,
                             payload or "(no reason given)", pr_url,
                             flag="STAGE_FAIL — the stage reports it is blocked",
                             conflicted=conflicted)
            return
        if marker == "unparsed":
            self.store.stage_run_close(run_id, "unparsed", **self._meta(raw))
            await self._park(job, stage, run_id, workspace, target, base_sha,
                             payload[-4000:] if payload else "(empty output)", pr_url,
                             flag="UNPARSED — the run ended without STAGE_DONE/STAGE_FAIL; treat with suspicion",
                             conflicted=conflicted)
            return

        self.store.stage_run_close(run_id, "done", **self._meta(raw))
        if pr_url:
            self.store.set_fields(job_id, pr_url=pr_url)
        await self._park(job, stage, run_id, workspace, target, base_sha,
                         payload, pr_url, conflicted=conflicted)

    async def _park(self, job, stage, run_id, workspace, target, base_sha,
                    payload, pr_url, flag: str = "", conflicted: list[str] | None = None):
        """Gate-park, crash-safe ordering: DB transition (with marker) BEFORE the
        ClickUp comment; the poller only reacts to /verb comments so ours are inert."""
        job_id = job["issue_id"]
        evidence = await self._evidence(workspace, target, base_sha, stage, pr_url)
        warnings = ""
        if flag:
            warnings += f"\n\n⚠️ {flag}."
        for a in conflicted or []:
            warnings += (f"\n\n⚠️ You edited `{a}` while this stage ran; the stage did NOT "
                         "see that edit — `/redo` if it changes anything.")
        attempts = int(job.get("stage_attempts") or 0)
        if attempts >= 3:
            warnings += f"\n\n⚠️ This stage has been redone {attempts} times."

        question = extract_questions_last(payload)
        comments = await self.clickup.comments(job.get("clickup_task_id") or "")
        marker = comments[-1]["id"] if comments else ""
        code, head = await git(workspace, "rev-parse", "HEAD")
        self.store.set_fields(
            job_id,
            analysis=payload,
            question=question,
            evidence=(evidence + warnings).strip(),
            comment_marker=marker,
            parked_head=head.strip() if code == 0 else "",
        )
        self.store.set_status(job_id, "awaiting_input", detail=payload[:2000])
        self.store.stage_run_gate_posted(run_id)
        await self.clickup.set_status(job.get("clickup_task_id") or "", "awaiting_input")

        gate_body = (
            f"{GATE_PREFIX} **Gate: P{stage} {stage_name(stage)} — "
            f"{'attempt ' + str(job.get('stage_attempts')) if int(job.get('stage_attempts') or 0) > 1 else 'complete'}.**\n\n"
            f"{payload[:6000]}\n"
            f"{evidence}{warnings}\n\n---\n"
            f"Reply `/proceed <guidance>` to continue to P{min(stage + 1, 9)}, "
            f"`/redo <notes>` to re-run this stage (or `/redo P<k> <notes>` for an earlier one), "
            f"or `/skip` to abort — here, on any artifact subtask, or on the dashboard."
        )
        await self._comment(job, gate_body, raw=True)
        owner = (job.get("owner") or "").strip()
        if owner:
            await self.clickup.set_assignee(job.get("clickup_task_id") or "", owner)
        log.info("job %s parked at P%s gate", job_id, stage)

    # ---------- helpers ----------

    def _meta(self, raw) -> dict:
        return {
            "cost_usd": raw.meta.get("cost_usd"),
            "num_turns": raw.meta.get("num_turns"),
            "duration_ms": raw.meta.get("duration_ms"),
        }

    async def _evidence(self, workspace, target, base_sha, stage, pr_url) -> str:
        """Harness-captured evidence for code gates — never self-reported."""
        if stage_kind(stage) != "code" or not base_sha:
            return ""
        code, stat = await git(workspace, "diff", "--stat", f"{base_sha}..HEAD")
        code2, head = await git(workspace, "rev-parse", "HEAD")
        lines = ["\n\n**Evidence (harness-captured):**", "```", (stat.strip() or "(no changes)"), "```"]
        if code2 == 0 and base_sha != head.strip():
            lines.append(f"Compare: https://github.com/{target.repo}/compare/{base_sha[:12]}...{head.strip()[:12]}")
        if pr_url:
            lines.append(f"Draft PR: {pr_url}")
        return "\n".join(lines)

    async def _checkpoint(self, workspace, branch, job_id, stage) -> bool:
        """Engine-owned durability: commit anything loose, always push the branch."""
        code, status = await git(workspace, "status", "--porcelain")
        if code == 0 and status.strip():
            await git(workspace, "add", "-A")
            await git(workspace, "commit", "-m", f"P{stage}: engine checkpoint (uncommitted stage output)")
        ok = await self._push_branch(workspace, branch)
        if not ok:
            log.error("job %s: branch push failed", job_id)
        return ok

    async def _push_branch(self, workspace, branch) -> bool:
        """Push with --force-with-lease: a code-stage redo legitimately rewrites
        history (the rejected attempt is preserved under refs/gumo/), so a plain
        push's non-fast-forward rejection would silently discard human-approved
        redo work. The lease baseline is origin/<branch> fetched at stage start,
        so a third-party push mid-run still correctly fails the lease."""
        code, out = await git(workspace, "push", "--force-with-lease", "-u", "origin", branch,
                              timeout=300)
        if code != 0:
            log.error("push --force-with-lease origin %s failed: %s", branch, out[-500:])
        return code == 0

    def _plan_has_multiple_groups(self, workspace, job_id) -> bool:
        path = artifact_path(workspace, job_id, "P4-plan.md")
        if not path.exists():
            return True  # fail open to a gated P6 rather than skipping silently
        return len(BUILD_GROUP_RE.findall(path.read_text())) >= 2

    def _pending_redo_notes(self, job_id, stage) -> str:
        entries = [e for e in self.store.guidance_for(job_id)
                   if e["action"] == "redo" and e["stage"] == stage]
        return entries[-1]["text"] if entries else ""

    async def _write_guidance_file(self, workspace, job):
        """Mirror the guidance log into git — decisions belong to the branch."""
        entries = self.store.guidance_for(job["issue_id"])
        if not entries:
            return
        lines = ["# Gate decisions (machine-written; polished into ADRs at P9)\n"]
        for e in entries:
            when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(e["at"]))
            lines.append(f"## P{e['stage']} — {e['action']} ({e['via']}, {when} UTC, head {e['artifact_sha'][:12]})\n\n{e['text'] or '(no text)'}\n")
        path = Path(workspace) / feature_dir(job["issue_id"]) / "guidance.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join(lines)
        if path.exists() and normalize(path.read_text()) == normalize(content):
            return
        path.write_text(normalize(content))
        await git(workspace, "add", str(path))
        code, out = await git(workspace, "commit", "-m", "guidance: record gate decisions")
        if code != 0 and "nothing to commit" not in out:
            # non-fatal: the stage-end checkpoint sweeps up uncommitted files,
            # but a failing commit here should be visible, not silent
            log.error("guidance.md commit failed: %s", out[-500:])

    async def _build_prompt(self, job, stage, target, branch, workspace,
                            redo_notes, edited) -> str:
        job_id = job["issue_id"]
        memory_context = await self.memory.context_for_stage(stage, job.get("project") or "", workspace)
        names = list_artifacts(workspace, job_id)

        inline: dict[str, str] = {}
        def put(name: str, cap: int = 6000):
            p = artifact_path(workspace, job_id, name)
            if p.exists():
                text = p.read_text().strip()
                inline[name] = text[:cap] + ("\n… (truncated — read the file)" if len(text) > cap else "")

        # binding inputs per stage (docs/ENGINE.md §4), not recency
        if stage in (1, 2):
            put("P0-intake.md"); put("P1-prd.md")
        elif stage == 3:
            put("P1-prd.md"); put("P2-recon.md")
        elif stage == 4:
            put("P1-prd.md", 8000); put("P3-design.md", 8000)
        elif stage in (5, 6):
            put("P4-plan.md", 16000); put("P3-design.md", 4000)
        elif stage == 7:
            put("P4-plan.md", 8000); put("P1-prd.md", 6000)
        elif stage == 8:
            put("P1-prd.md", 6000)  # acceptance criteria; build narratives excluded
        elif stage == 9:
            put("P1-prd.md", 6000); put("P3-design.md", 6000)

        edited_note = ""
        if edited:
            edited_note = ("\n\nNOTE: a human edited these artifacts since the last stage — "
                           "they are AUTHORITATIVE and newer than anything above: "
                           + ", ".join(f"`{a}`" for a in edited))

        return build_stage_prompt(
            target=target, branch=branch, job=job, stage=stage,
            memory_context=memory_context,
            artifact_names=names,
            inline_artifacts=inline,
            guidance_entries=self.store.guidance_for(job_id),
            redo_notes=redo_notes,
            evidence_note=edited_note,
            test_block=_test_block(target) if stage in (7, 8) else "",
            canonical_project=self.settings.memory_canonical_project,
        )

    async def _comment(self, job, text, raw: bool = False):
        body = text if raw else f"{GATE_PREFIX} {text}"
        await self.clickup.comment(job.get("clickup_task_id") or "", body)

    # ---------- memory bootstrap (kind=memory) ----------

    async def run_memory_bootstrap(self, job: dict):
        job_id = job["issue_id"]
        project = job.get("project") or ""
        target = self.settings.repo_for_project(project)
        if target is None:
            self.store.set_status(job_id, "skipped", detail=f"no repo mapped for '{project}'")
            return
        branch = f"brain/memory-{project}"
        self.store.set_fields(job_id, run_started_at=time.time())
        self.store.set_status(job_id, "running")

        workspace = await prepare_feature_workspace(self.settings, target, branch, stage=0)
        is_canonical = project == self.settings.memory_canonical_project
        pr_url = None
        for run in (1, 2):
            run_id = self.store.stage_run_open(job_id, stage=run, attempt=1)
            prompt = build_bootstrap_prompt(target=target, branch=branch, project=project,
                                            is_canonical=is_canonical, run=run)
            raw = await run_claude_raw(self.settings, workspace, prompt,
                                       BASE_ALLOWED_TOOLS + target.allow,
                                       self.settings.claude_timeout_seconds)
            await self._checkpoint(workspace, branch, job_id, stage=0)
            if raw.status != "ok":
                self.store.stage_run_close(run_id, raw.status, **self._meta(raw))
                self.store.set_status(job_id, raw.status, detail=raw.text[:2000])
                return
            marker, payload, found_pr = parse_stage_output(raw.text)
            self.store.stage_run_close(run_id, marker, **self._meta(raw))
            pr_url = found_pr or pr_url
            if marker == "fail":
                self.store.set_status(job_id, "no_fix", detail=payload[:2000])
                return
            if marker == "unparsed":  # fail closed — never advance/complete on an unmarked run
                self.store.set_status(job_id, "error",
                                      detail=f"bootstrap run {run} ended without STAGE_DONE/"
                                             f"STAGE_FAIL: {payload[-1500:]}")
                return
        await self.memory.refresh_cache(project, workspace, target.base)
        self.store.set_status(job_id, "pr_opened" if pr_url else "no_fix",
                              pr_url=pr_url, detail="memory bootstrap complete")
