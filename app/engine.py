"""Feature pipeline orchestration (docs/ENGINE.md §2, §5).

One call to `run_stage` = one stage execution: resume branch → pull human
edits → record baseline → run Claude → engine checkpoint (commit+push, even on
failure) → mirror artifacts → park at the gate. Fail-closed everywhere:
nothing advances without an explicit STAGE_DONE and a human answer.
"""

import asyncio
import json
import logging
import re
import time
import uuid
from pathlib import Path

from .artifacts import ArtifactSync, artifact_path, feature_dir, list_artifacts, normalize
from .chatstream import ChatBroker
from .clickup import ClickUp
from .config import Settings
from .db import JobStore
from . import fastlane
from .feature_prompts import (
    STAGES,
    build_bootstrap_prompt,
    build_chat_prompt,
    build_fastlane_messages,
    build_fastlane_system,
    build_stage_prompt,
    stage_artifact,
    stage_kind,
    stage_name,
)
from .fixer import (
    BASE_ALLOWED_TOOLS,
    PR_LINE_RE,
    TRANSIENT_ERROR_RE,
    BranchLostError,
    git,
    prepare_feature_workspace,
    prepare_workspace,
    run_claude_raw,
    run_claude_stream,
    session_transcript_exists,
)
from .github import GitHub
from .memory import MemoryReader
from .prompts import _test_block, build_v1_chat_prompt, build_v1_fastlane_system

log = logging.getLogger("brain.engine")

GATE_PREFIX = "**[gumo_brain]**"
DOC_STAGE_TOOLS = ["Read", "Grep", "Glob"]
# chat runs are read-only by DENY, not just allow: --allowedTools is additive to any
# settings-file grants living in the (persistent) workspace, so write tools must be
# explicitly disallowed (docs/CONVERSATIONS.md §2)
CHAT_TOOLS = ["Read", "Grep", "Glob"]
CHAT_DENIED_TOOLS = ["Edit", "Write", "NotebookEdit", "Bash", "WebFetch", "WebSearch"]


class RepoLocks:
    """One asyncio.Lock per repo workspace. Every workspace toucher — stage runs,
    sentry/task fixes, memory bootstraps, chat runs, canonical product-scope reads —
    must hold the repo's lock. In-process: the service MUST run single-process
    (uvicorn workers=1; see deploy wiring)."""

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}
        # Concurrent claude invocations sharing a config dir must serialize — the
        # CLI read-modify-writes its state files per invocation (docs/
        # CONVERSATIONS.md §4). claude_global guards the stage/default store
        # (which is ~/.claude when session_persistence is off — and chat is the
        # first concurrent invoker this service ever had); chat_global guards the
        # dedicated artifact-primed chat store used when persistence is on.
        self.claude_global = asyncio.Lock()
        self.chat_global = asyncio.Lock()

    def for_repo(self, repo: str) -> asyncio.Lock:
        if repo not in self._locks:
            self._locks[repo] = asyncio.Lock()
        return self._locks[repo]

    def is_busy(self, repo: str) -> bool:
        lock = self._locks.get(repo)
        return bool(lock and lock.locked())

# PR_LINE_RE (the strict `PR_URL:` line matcher) lives in fixer.py — one
# definition for both the feature pipeline and the v1 lifecycle capture.
QUESTION_HEADING_RE = re.compile(r"^#{1,4}\s*(?:open\s+)?questions?\b.*$", re.IGNORECASE | re.MULTILINE)
BUILD_GROUP_RE = re.compile(r"^#{1,4}\s*build\s+group\b", re.IGNORECASE | re.MULTILINE)


def all_pr_urls(text: str) -> list[str]:
    """Every PR_URL line in a run's output, de-duplicated in order — one work
    packet can open several PRs (P5 + per-build-group in P6) and stages re-print
    earlier URLs, so the prs table is fed from ALL of them, idempotently."""
    return list(dict.fromkeys(PR_LINE_RE.findall(text or "")))


def parse_stage_output(text: str) -> tuple[str, str, str | None]:
    """(marker, payload, pr_url). Marker: done | fail | ask | unparsed.
    End-anchored: the LAST line-start occurrence wins; a bare URL elsewhere
    never counts."""
    text = text or ""
    pr_matches = PR_LINE_RE.findall(text)
    pr_url = pr_matches[-1] if pr_matches else None

    candidates = []
    for marker, tag in (("done", "STAGE_DONE:"), ("fail", "STAGE_FAIL:"), ("ask", "STAGE_ASK:")):
        positions = list(re.finditer(rf"^{tag}", text, re.MULTILINE))
        if positions:
            candidates.append((positions[-1].start(), marker, tag))
    if not candidates:
        return "unparsed", text.strip(), pr_url
    pos, marker, tag = max(candidates)
    payload = text[pos + len(tag):].strip()
    return marker, payload, pr_url


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
    def __init__(self, settings: Settings, store: JobStore, clickup: ClickUp,
                 locks: RepoLocks | None = None):
        self.settings = settings
        self.store = store
        self.clickup = clickup
        self.locks = locks or RepoLocks()
        self.sync = ArtifactSync(store, clickup, settings.clickup_mirror_max_chars)
        self.memory = MemoryReader(settings, locks=self.locks)
        self.github = GitHub(settings)
        # live session observation: a stage run streams its tool calls / text here,
        # keyed by job id. A SECOND broker instance (chat has its own) so a gate
        # chat turn and a live stage run never clobber each other's buffer.
        self.stage_broker = ChatBroker()
        # per-job mid-run steer handles: job_id -> {event, stage}. Populated while a
        # stage runs; an HTTP steer request sets the event to interrupt in place.
        self._steer: dict[str, dict] = {}

    def request_steer(self, job_id: str, note: str) -> str:
        """Human course-correction from the live session page. Returns the outcome:
        'interrupting' — a stage is running and session persistence is on, so the
        run is interrupted and will resume the same session with `note` folded in;
        'queued' — otherwise (no persistence, or not running): the note is recorded
        as guidance and applied at the next stage/gate (the safe fallback)."""
        note = (note or "").strip()
        if not note:
            return "empty"
        handle = self._steer.get(job_id)
        if handle and self.settings.session_persistence and not handle["event"].is_set():
            self.store.set_fields(job_id, steer_note=note)
            handle["event"].set()
            log.info("job %s: steer requested mid-P%s", job_id, handle["stage"])
            return "interrupting"
        # fallback: record as guidance so the next run/gate sees it
        stage = handle["stage"] if handle else int(self.store.get(job_id).get("stage") or 0)
        self.store.guidance_add(job_id, stage, "steer", note, "dashboard")
        log.info("job %s: steer queued to next checkpoint (P%s)", job_id, stage)
        return "queued"

    async def record_prs(self, job_id: str, urls: list[str], kickoff: bool = True):
        """Track every PR a run opened and, for NEW ones (pr_add is idempotent
        by URL), run the lifecycle kickoff: flip the draft to ready-for-review
        and post the first `@sentry review` (the bot ignores plain pushes).
        kickoff=False records only (memory-bootstrap PRs are doc drafts — a code
        review bot on them is noise). Best-effort throughout — a GitHub hiccup
        never fails the run; the shepherd re-drives whatever this leaves."""
        from .db import PR_URL_PARTS_RE

        for url in urls or []:
            try:
                if not self.store.pr_add(job_id, url):
                    # already tracked — kickoff happened (or shepherd owns it).
                    # One case still needs action: the bot APPROVED this PR and
                    # a later run (e.g. build group 2 on the same branch) just
                    # pushed more commits. 'approved' is outside the shepherd's
                    # scan states, so without a re-kick those commits would ship
                    # unreviewed. Head-compare so a run that merely re-prints
                    # the PR_URL line never burns a review round.
                    if kickoff and self.settings.pr_auto_ready:
                        await self._rekick_if_approved_head_moved(job_id, url)
                    continue
                m = PR_URL_PARTS_RE.search(url)
                if m:  # conveyor mirror: Backend PR / Web PR / App PR by repo
                    await self.sync_pr_field(job_id, m.group(1), url)
                if not m or not kickoff or not self.settings.pr_auto_ready:
                    continue
                repo, number = m.group(1), int(m.group(2))
                if await self.github.mark_ready(repo, number):
                    self.store.pr_set(url, state="ready")
                    log.info("job %s: PR %s#%s marked ready", job_id, repo, number)
                if await self.github.comment(repo, number, "@sentry review"):
                    self.store.pr_set(url, state="in_review", review_rounds=1)
                    log.info("job %s: review requested on %s#%s", job_id, repo, number)
            except Exception:
                log.exception("PR lifecycle kickoff failed for %s (%s)", url, job_id)

    async def _rekick_if_approved_head_moved(self, job_id: str, url: str):
        from .db import PR_URL_PARTS_RE

        row = self.store.pr_get(url)
        if not row or row.get("state") != "approved":
            return
        m = PR_URL_PARTS_RE.search(url)
        if not m:
            return
        repo, number = m.group(1), int(m.group(2))
        info = await self.github.get_pr(repo, number)
        if info is None:
            return  # unknown — the next run mentioning this PR retries
        head = ((info.get("head") or {}).get("sha") or "").strip()
        if not head or head == (row.get("approved_head") or ""):
            return  # nothing new since the clean pass
        if await self.github.comment(repo, number, "@sentry review"):
            rounds = int(row.get("review_rounds") or 0) + 1
            self.store.pr_set(url, state="in_review", review_rounds=rounds,
                              detail=f"round {rounds}: new commits after approval — "
                                     "re-review requested")
            log.info("job %s: %s#%s re-kicked after post-approval push", job_id, repo, number)

    # ---------- the one entry point ----------

    async def run_stage(self, job: dict, queued_at: float | None = None):
        job_id = job["issue_id"]
        stage = int(job.get("stage") or 0)
        target = self.settings.repo_for_project(job.get("project") or "")
        if target is None:
            self.store.set_status(job_id, "skipped", detail=f"no repo mapped for '{job.get('project')}'")
            return
        branch = f"brain/feat-{job_id}"
        # conveyor mirror: slide the ticket's Stage card + link the live view
        await self.sync_stage_field(job, str(stage))
        await self.sync_dashboard_field(job)

        state = self.store.stage_state_get(job_id, stage) or {"attempts": 0, "base_sha": ""}
        # a STAGE_ASK resume continues the SAME attempt — no bump, no rewind
        resuming = self._resume_intended(job, stage)
        if resuming:
            attempt = int(job.get("resume_attempt") or state["attempts"] or 1)
        else:
            attempt = int(state["attempts"]) + 1
        run_id = self.store.stage_run_open(job_id, stage, attempt, queued_at)
        # NOTE: resumed=1 is stamped by the inner run only once the resume
        # invocation actually succeeds — an intended resume can still downgrade
        # to a fresh run (head moved, budget spent, transcript pruned)
        self.store.set_fields(job_id, run_started_at=time.time(), stage_attempts=attempt)
        job["stage_attempts"] = attempt  # keep the local view consistent for _park

        # register the live-session hooks: an interrupt event the session page can
        # trip to steer mid-run, and a broker turn the page streams tool calls from.
        steer_ev = asyncio.Event()
        self._steer[job_id] = {"event": steer_ev, "stage": stage}
        self.stage_broker.start(job_id)

        def publish(event, data):
            self.stage_broker.publish(job_id, event, data)

        self.store.set_status(job_id, "running")
        await self.clickup.set_status(job.get("clickup_task_id") or "", "running")

        try:
            return await self._run_stage_inner(job, stage, run_id, target, branch,
                                               queued_at, resuming,
                                               publish=publish, interrupt_event=steer_ev)
        except BranchLostError as e:
            self.store.stage_run_close(run_id, "branch_lost")
            self.store.set_status(job_id, "error", detail=str(e))
            await self._comment(job, f"Pipeline halted at P{stage}: {e}")
            return
        except Exception:
            # close the telemetry row before the worker's generic error handling
            self.store.stage_run_close(run_id, "exception")
            raise
        finally:
            self._steer.pop(job_id, None)
            self.stage_broker.finish(job_id)

    async def _run_stage_inner(self, job: dict, stage: int, run_id: int, target,
                               branch: str, queued_at: float | None,
                               resuming: bool = False, publish=None, interrupt_event=None):
        job_id = job["issue_id"]
        publish = publish or (lambda event, data: None)
        state = self.store.stage_state_get(job_id, stage) or {"attempts": 0, "base_sha": ""}
        attempt = int(job.get("stage_attempts") or int(state["attempts"]) + 1)
        publish("status", "preparing the branch workspace")
        workspace = await prepare_feature_workspace(self.settings, target, branch, stage)

        # STAGE_ASK resume validation happens against the PRE-pull origin head so a
        # third-party push invalidates, but engine-authored post-park commits
        # (edit pulls, guidance) never do — they are enumerated in the resume message.
        resume_reason = ""
        if resuming:
            code, oh = await git(workspace, "rev-parse", f"origin/{branch}")
            if code != 0 or oh.strip() != (job.get("resume_head") or ""):
                resuming, resume_reason = False, "the branch moved while parked"
            elif (job.get("gate_kind") != "steer"
                  and int(job.get("ask_count") or 0) >= self.settings.max_asks_per_stage):
                # a human steer is not rate-limited by the STAGE_ASK budget
                resuming, resume_reason = False, "the ask budget for this stage is spent"
            elif not session_transcript_exists(self.settings, job.get("resume_session_id") or ""):
                resuming, resume_reason = False, "the session transcript is gone (restart/prune)"

        # An explicit redo of THIS code stage rewinds to the stage baseline,
        # preserving the rejected attempt under refs/gumo/. This keys off the
        # pending_redo_stage flag set by the human's answer — `attempt > 1` alone
        # also fires when the pipeline merely re-advances through this stage after
        # an earlier-stage redo, which would hard-reset to a now-stale baseline.
        redo_notes = self._pending_redo_notes(job_id, stage)
        if (not resuming and job.get("pending_redo_stage") == stage
                and stage_kind(stage) == "code" and state.get("base_sha")):
            await git(workspace, "update-ref",
                      f"refs/gumo/{job_id}/P{stage}-attempt-{max(1, attempt - 1)}", "HEAD")
            await git(workspace, "push", "origin",
                      f"refs/gumo/{job_id}/P{stage}-attempt-{max(1, attempt - 1)}")
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
        if stage == 6 and not resuming and not self._plan_has_multiple_groups(workspace, job_id):
            self.store.stage_run_close(run_id, "skipped_single_group")
            self.store.set_fields(job_id, stage=7, stage_attempts=0, ask_count=0)
            self.store.set_status(job_id, "queued")
            log.info("job %s: P6 auto-skipped (single build group)", job_id)
            await self._comment(job, "P6 auto-skipped — the plan has a single build group. Queued P7.")
            return "requeue"

        if resuming:
            # a resumed run continues the SAME attempt against the SAME baseline
            base_sha = state.get("base_sha") or ""
        else:
            code, head = await git(workspace, "rev-parse", "HEAD")
            base_sha = head.strip() if code == 0 else ""
            self.store.stage_state_set(job_id, stage, base_sha=base_sha, bump_attempts=True)

        kind = stage_kind(stage)
        tools = DOC_STAGE_TOOLS if kind == "doc" else BASE_ALLOWED_TOOLS + target.allow
        timeout = (self.settings.doc_stage_timeout_seconds if kind == "doc"
                   else self.settings.claude_timeout_seconds)

        ask_answer = (job.get("resume_answer") or "").strip()
        resume_sid = (job.get("resume_session_id") or "").strip()
        resume_kind = job.get("gate_kind")  # 'ask' | 'steer' — captured before the clear
        # consume the pending resume exactly once, whichever path runs
        if resume_sid or resume_kind in ("ask", "steer"):
            self.store.set_fields(job_id, resume_session_id="", resume_stage=None,
                                  resume_attempt=None, resume_head="", resume_answer="",
                                  gate_kind="")
        if resuming:
            prompt = self._resume_message(job, stage, ask_answer, edited, resume_kind)
            log.info("job %s: resuming P%s session %s (%s)", job_id, stage, resume_sid[:8],
                     resume_kind or "ask")
            publish("status", "resuming the session with your steer"
                    if resume_kind == "steer" else "resuming the session with your answer")
            raw = await self._invoke(workspace, prompt, tools, timeout, resume_session=resume_sid,
                                     publish=publish, interrupt_event=interrupt_event)
            if raw.status == "session_lost":
                resuming, resume_reason = False, "the session could not be resumed"
            else:
                # only now is this run truly a continuation of the parked session.
                # A STAGE_ASK resume consumes the ask budget; a human steer does not.
                self.store.stage_run_mark_resumed(run_id)
                if resume_kind != "steer":
                    self.store.set_fields(job_id, ask_count=int(job.get("ask_count") or 0) + 1)
        if not resuming:
            if resume_reason and ask_answer:
                # the human's answer must survive the downgrade to a fresh re-run
                redo_notes = (f"(Your earlier STAGE_ASK could not resume: {resume_reason}. "
                              f"Re-run the stage; the human's answer to your question was: "
                              f"{ask_answer})\n\n{redo_notes}").strip()
            prompt = await self._build_prompt(job, stage, target, branch, workspace,
                                              redo_notes, edited)
            log.info("job %s: running P%s (%s) attempt %s", job_id, stage, stage_name(stage), attempt)
            publish("status", f"running P{stage} {stage_name(stage)} (attempt {attempt})")
            raw = await self._invoke(workspace, prompt, tools, timeout,
                                     publish=publish, interrupt_event=interrupt_event)

        # a human tripped the steer event mid-run: the CLI was stopped with the
        # session intact — checkpoint the work so far and re-enqueue to resume it
        # with the steer note folded in (reuses the STAGE_ASK resume machinery).
        if raw and raw.status == "interrupted":
            return await self._steer_reenqueue(job, stage, run_id, attempt, workspace,
                                               branch, raw, publish)

        try:
            # propagate the result: a light-mode auto-advance returns "requeue",
            # which the worker must see to re-enqueue the job (the finally still runs)
            return await self._after_run(job, stage, run_id, target, branch, workspace, raw, base_sha)
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
            # ONE automatic retry for upstream hiccups (API 5xx, overloaded…):
            # the work is fine, the transport failed. Errors the run produced
            # itself — and every timeout — still park for a human /redo. The
            # budget is 1 until a gate parks or a human redo resets it, so a
            # genuinely broken stage can never retry-loop.
            if (raw.status == "error" and TRANSIENT_ERROR_RE.search(raw.text or "")
                    and int(job.get("auto_retries") or 0) < 1):
                self.store.set_fields(job_id, auto_retries=1)
                self.store.set_status(job_id, "queued",
                                      detail=f"transient upstream error — retrying "
                                             f"automatically: {raw.text[:300]}")
                await self._comment(
                    job, f"P{stage} ({stage_name(stage)}) hit a transient upstream error — "
                         "retrying automatically (1/1). ♻️")
                return "requeue"
            self.store.set_status(job_id, raw.status, detail=raw.text[:2000])
            await self._comment(
                job, f"P{stage} ({stage_name(stage)}) ended with `{raw.status}`: "
                     f"{raw.text[:400]}\n\nRe-kick with `/redo <notes>` here or on the dashboard.")
            return

        marker, payload, pr_url = parse_stage_output(raw.text)
        # track EVERY PR this run mentioned (not just the last) + lifecycle kickoff
        await self.record_prs(job_id, all_pr_urls(raw.text))

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
        if marker == "ask":
            if stage_kind(stage) == "code" and stage != 9:
                self.store.stage_run_close(run_id, "stage_ask", **self._meta(raw))
                await self._park(job, stage, run_id, workspace, target, base_sha,
                                 payload or "(no question given)", pr_url,
                                 conflicted=conflicted,
                                 ask_session=raw.meta.get("session_id") or "")
                return
            marker = "unparsed"  # doc stages / P9 must not ask — fail closed
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

        # light gate mode: checkpoint stages always park; the rest auto-advance
        # on a clean STAGE_DONE unless a guard trips (docs/CONVERSATIONS.md §1)
        if self._auto_advance_ok(job, stage, payload, pr_url, conflicted):
            self.store.guidance_add(job_id, stage, "auto",
                                    "auto-advanced (light gate mode)", "engine")
            evidence = await self._evidence(workspace, target, base_sha, stage, pr_url)
            await self._comment(
                job, f"P{stage} ({stage_name(stage)}) complete — auto-advanced "
                     f"(light gate mode).\n\n{payload[:3000]}{evidence}")
            self.store.set_fields(job_id, stage=stage + 1, stage_attempts=0, question="",
                                  ask_count=0)
            self.store.set_status(job_id, "queued")
            log.info("job %s: P%s auto-advanced (light mode)", job_id, stage)
            return "requeue"

        await self._park(job, stage, run_id, workspace, target, base_sha,
                         payload, pr_url, conflicted=conflicted)

    # light mode parks unconditionally at P0/P1/P3/P9; other stages may advance
    LIGHT_MODE_AUTO_STAGES = {2, 4, 5, 6, 7, 8}
    BOILERPLATE_Q_RE = re.compile(r"approve|continue|proceed|look good|lgtm", re.IGNORECASE)

    def _auto_advance_ok(self, job, stage, payload, pr_url, conflicted) -> bool:
        if (job.get("gate_mode") or "full") != "light":
            return False
        if stage not in self.LIGHT_MODE_AUTO_STAGES:
            return False
        if conflicted:  # a human edited mid-run — they must see the warning
            return False
        if not job.get("mirror_ok", 1):
            return False
        if stage == 5 and not (pr_url or job.get("pr_url")):
            return False  # P5 without a captured draft PR must park
        # the first clean run after an explicit /redo of this stage must park
        for e in reversed(self.store.guidance_for(job["issue_id"])):
            if e["stage"] == stage and e["action"] in ("redo", "proceed", "auto"):
                if e["action"] == "redo":
                    return False
                break
        # only a boilerplate approval question may auto-advance
        questions = extract_questions_last(payload)
        items = [l.strip() for l in questions.splitlines()
                 if re.match(r"^\d+[.)]", l.strip())]
        if len(items) > 1:
            return False
        probe = items[0] if items else questions.strip()
        return bool(len(probe) <= 160 and self.BOILERPLATE_Q_RE.search(probe))

    async def _park(self, job, stage, run_id, workspace, target, base_sha,
                    payload, pr_url, flag: str = "", conflicted: list[str] | None = None,
                    ask_session: str = ""):
        """Gate-park, crash-safe ordering: DB transition (with marker) BEFORE the
        ClickUp comment; the poller only reacts to /verb comments so ours are inert.
        ask_session marks a STAGE_ASK gate: the answer resumes that session in place."""
        job_id = job["issue_id"]
        is_ask = bool(ask_session)
        # a stage reached a gate: restore the transient-retry budget (it is
        # per-stage-attempt-chain, not per-pipeline)
        self.store.set_fields(job_id, auto_retries=0)
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
        if is_ask and int(job.get("ask_count") or 0) + 1 >= self.settings.max_asks_per_stage:
            warnings += ("\n\n⚠️ This stage keeps asking — consider `/redo` with clearer "
                         "guidance or a sharper plan.")

        question = extract_questions_last(payload)
        comments = await self.clickup.comments(job.get("clickup_task_id") or "")
        marker = comments[-1]["id"] if comments else ""
        code, head = await git(workspace, "rev-parse", "HEAD")
        head = head.strip() if code == 0 else ""
        fields = dict(
            analysis=payload,
            question=question,
            evidence=(evidence + warnings).strip(),
            comment_marker=marker,
            parked_head=head,
        )
        if is_ask:
            # resume state and the ask flag land in the SAME update, before the
            # ClickUp comment — a crash in between leaves a fully answerable gate
            fields.update(gate_kind="ask", resume_session_id=ask_session,
                          resume_stage=stage, resume_attempt=attempts or 1,
                          resume_head=head, resume_answer="")
            self.store.guidance_add(job_id, stage, "ask", question[:1500], "engine", head)
        self.store.set_fields(job_id, **fields)
        self.store.set_status(job_id, "awaiting_input", detail=payload[:2000])
        self.store.stage_run_gate_posted(run_id)
        await self.clickup.set_status(job.get("clickup_task_id") or "", "awaiting_input")

        if is_ask:
            header = f"**Question: P{stage} {stage_name(stage)} — work paused, resumes in place.**"
            actions = (f"Reply `/proceed <your answer>` — the stage picks up exactly where it "
                       f"stopped. `/redo <notes>` restarts the stage fresh; `/skip` aborts. "
                       f"Here, on any artifact subtask, or on the dashboard.")
        else:
            header = (f"**Gate: P{stage} {stage_name(stage)} — "
                      f"{'attempt ' + str(job.get('stage_attempts')) if int(job.get('stage_attempts') or 0) > 1 else 'complete'}.**")
            actions = (f"Reply `/proceed <guidance>` to continue to P{min(stage + 1, 9)}, "
                       f"`/redo <notes>` to re-run this stage (or `/redo P<k> <notes>` for an earlier one), "
                       f"or `/skip` to abort — here, on any artifact subtask, or on the dashboard.")
        gate_body = (
            f"{GATE_PREFIX} {header}\n\n"
            f"{payload[:6000]}\n"
            f"{evidence}{warnings}\n\n---\n{actions}"
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
            "session_id": raw.meta.get("session_id"),
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

    async def _invoke(self, workspace, prompt, tools, timeout,
                      resume_session: str | None = None, fork_session: bool = False,
                      config_dir: str | None = None, publish=None, interrupt_event=None):
        """All stage/session claude invocations funnel here: the engine owns the
        session id on fresh runs (timeout/error runs still have a resumable id on
        record), and invocations sharing the session store serialize globally.

        When publish/interrupt_event are supplied (stage runs viewed on the live
        session page) the run streams via run_claude_stream — same return contract
        as run_claude_raw — so tool calls surface live and a human can steer it."""
        sid = None
        if resume_session is None and self.settings.session_persistence:
            sid = str(uuid.uuid4())
        observed = publish is not None or interrupt_event is not None

        async def _go(cdir):
            if observed:
                return await run_claude_stream(
                    self.settings, workspace, prompt, tools, timeout,
                    resume_session=resume_session, fork_session=fork_session,
                    session_id=sid, config_dir=cdir,
                    on_event=publish, interrupt_event=interrupt_event)
            return await run_claude_raw(self.settings, workspace, prompt, tools, timeout,
                                        resume_session=resume_session,
                                        fork_session=fork_session, session_id=sid,
                                        config_dir=cdir)

        if config_dir is None:
            # stage/bootstrap runs touch the stage/default store — ALWAYS serialize:
            # with persistence off that store is ~/.claude, shared with any
            # concurrent chat run (the worker itself is serial, so this only ever
            # waits on chats, never on other stages)
            async with self.locks.claude_global:
                return await _go(None)
        return await _go(config_dir)

    async def _steer_reenqueue(self, job, stage, run_id, attempt, workspace, branch, raw, publish):
        """A human steered mid-run: the CLI was stopped with its session intact.
        Checkpoint the work-in-progress to origin, then set up a resume of that same
        session with the steer note folded in and re-enqueue. Continuity over a clean
        slate — the model keeps its half-done work and adjusts course."""
        job_id = job["issue_id"]
        sid = (raw.meta or {}).get("session_id") or ""
        note = (self.store.get(job_id) or {}).get("steer_note") or ""
        publish("status", "steer received — checkpointing work so far")
        pushed = await self._checkpoint(workspace, branch, job_id, stage)
        code, head = await git(workspace, "rev-parse", f"origin/{branch}")
        head = head.strip() if code == 0 else ""
        self.store.stage_run_close(run_id, "interrupted", **self._meta(raw))
        if not (pushed and sid and head):
            # can't resume safely (push failed / no session / no head) — fall back to
            # a fresh re-run carrying the note as guidance, so the steer is never lost
            self.store.guidance_add(job_id, stage, "steer", note, "dashboard")
            self.store.set_fields(job_id, steer_note="")
            self.store.set_status(job_id, "queued")
            await self._comment(job, f"Steer at P{stage}: could not resume in place; "
                                     "re-running the stage with your note as guidance.")
            return "requeue"
        self.store.set_fields(job_id, resume_session_id=sid, resume_stage=stage,
                              resume_attempt=attempt, resume_head=head, resume_answer=note,
                              gate_kind="steer", steer_note="")
        self.store.set_status(job_id, "queued")
        publish("status", "resuming with your steer")
        await self._comment(job, f"Steering P{stage} ({stage_name(stage)}) — interrupted mid-run "
                                 "and resuming the same session with your note.")
        log.info("job %s: steer-reenqueue P%s session %s", job_id, stage, sid[:8])
        return "requeue"

    def _resume_intended(self, job: dict, stage: int) -> bool:
        """A pending STAGE_ASK answer or mid-run steer exists for exactly this gate
        (head/budget/transcript validation happens later, in the workspace)."""
        return bool(job.get("gate_kind") in ("ask", "steer")
                    and (job.get("resume_session_id") or "").strip()
                    and job.get("resume_stage") == stage
                    and (job.get("resume_answer") or "").strip())

    def _resume_message(self, job: dict, stage: int, answer: str, edited: list[str],
                        kind: str = "ask") -> str:
        if kind == "steer":
            parts = [
                f"The human interrupted you mid-run at P{stage} to steer your work:",
                "",
                answer,
                "",
                "Adjust course accordingly and continue from where you stopped — keep "
                "the work already done that still applies; revise what the steer changes.",
            ]
        else:
            parts = [
                f"The human answered your STAGE_ASK question at the P{stage} gate:",
                "",
                answer,
            ]
        chat = self.store.chat_for(job["issue_id"], stage)
        if chat:
            lines, total = [], 0
            for t in chat:
                line = f"{'Reviewer' if t['role'] == 'human' else 'Engine'}: {(t['text'] or '').strip()[:600]}"
                if total + len(line) > 3000:
                    break
                lines.append(line)
                total += len(line)
            parts += ["", "While parked, this conversation happened at the gate "
                          "(a read-only copy of you answered — treat it as context):",
                      *lines]
        if edited:
            parts += ["", "While parked, humans edited these artifacts (authoritative, "
                          "already committed): " + ", ".join(f"`{a}`" for a in edited)]
        parts += ["", "NOTE: other runs may have used this workspace while you were parked. "
                      "Tracked files match your branch, but ignored files (node_modules, "
                      "build outputs, caches) may have changed — re-run installs/builds "
                      "before trusting earlier test results.",
                  "", "Continue the stage exactly where you stopped. The output protocol "
                      "is unchanged: end with STAGE_DONE:, STAGE_FAIL:, or (if another "
                      "human decision surfaces) STAGE_ASK:."]
        return "\n".join(parts)

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

    # ---------- gumo-speed conveyor mirror (ClickUp custom fields) ----------

    async def sync_stage_field(self, job: dict, key: str):
        """Mirror the pipeline position onto the ticket's `Stage` dropdown —
        the original workflow's board contract (the card slides across the
        columns as the feature advances; ClickUp automations key off it).
        key: '0'…'9' | 'shipped' | 'merged'. Best-effort display only; the
        engine's own store stays the record (ENGINE.md §7)."""
        if not self.settings.clickup_field_sync_enabled:
            return
        task_id = job.get("clickup_task_id") or ""
        if not task_id or (job.get("kind") or "") != "feature":
            return
        try:
            option = json.loads(self.settings.clickup_stage_field_map).get(str(key), "")
            if option == "build":  # the build columns are per-surface (per repo)
                target = self.settings.repo_for_project(job.get("project") or "")
                option = json.loads(self.settings.clickup_repo_stage_map).get(
                    target.repo if target else "", "")
            if option:
                await self.clickup.field_set(task_id, "Stage", option)
        except Exception:
            log.exception("Stage field sync failed for %s (non-fatal)", job.get("issue_id"))

    async def sync_dashboard_field(self, job: dict):
        """Point the ticket's `Dashboard` url field at this job's inbox deep
        link — one click from the board to the live session view."""
        if not self.settings.clickup_field_sync_enabled:
            return
        task_id = job.get("clickup_task_id") or ""
        if not task_id:
            return
        try:
            await self.clickup.field_set(
                task_id, "Dashboard",
                f"{self.settings.public_base_url}/#/job/{job['issue_id']}")
        except Exception:
            log.exception("Dashboard field sync failed for %s (non-fatal)", job.get("issue_id"))

    async def sync_pr_field(self, job_id: str, repo: str, url: str):
        """Fill the per-repo PR url field (`Backend PR` / `Web PR` / `App PR`)
        the moment a run opens that repo's PR — the Tech Review stage of the
        original workflow reads these fields."""
        if not self.settings.clickup_field_sync_enabled:
            return
        try:
            field = json.loads(self.settings.clickup_pr_field_map).get(repo, "")
            job = self.store.get(job_id) or {}
            if field and job.get("clickup_task_id"):
                await self.clickup.field_set(job["clickup_task_id"], field, url)
        except Exception:
            log.exception("PR field sync failed for %s (non-fatal)", job_id)

    async def _comment(self, job, text, raw: bool = False):
        # ClickUp is best-effort visibility and NEVER drives progress (ENGINE.md
        # §7) — a comment failure must never propagate and corrupt control flow
        # (e.g. flip a just-requeued steer to error, or overwrite closed telemetry).
        body = text if raw else f"{GATE_PREFIX} {text}"
        try:
            await self.clickup.comment(job.get("clickup_task_id") or "", body)
        except Exception:
            log.exception("clickup comment failed for %s (non-fatal)", job.get("issue_id"))

    # ---------- gate chat (artifact-primed, read-only — docs/CONVERSATIONS.md §2) ----------

    async def chat(self, job: dict, message: str, publish=None):
        """Answer one gate-chat question. The human turn is already persisted (the
        endpoint returns 202 immediately); this runs as a background task and
        appends the engine turn — persist-then-poll, so the dashboard's ordinary
        polling picks up the reply and nothing is lost if the client disconnects.
        `publish(event, data)` (optional, sync) streams live progress to the SSE
        broker: 'delta' text chunks and 'status' progress lines. Streaming is
        pure UX — the persisted row is the contract."""
        job_id = job["issue_id"]
        stage = int(job.get("stage") or 0)
        attempt = max(1, int(job.get("stage_attempts") or 1))
        target = self.settings.repo_for_project(job.get("project") or "")
        publish = publish or (lambda event, data: None)

        v1 = (job.get("kind") or "sentry") != "feature"
        try:
            reply, meta, degraded = await self._chat_dispatch(job, stage, message,
                                                              target, publish, v1=v1)
        except asyncio.CancelledError:
            # shutdown (or task cancellation): leave a tombstone engine turn so
            # the human turn isn't orphaned — an orphan blocks this gate's chat
            # for the stale-pending window after restart. The write is
            # synchronous SQLite (safe in a cancelled task); skip the ClickUp
            # mirror and propagate the cancellation.
            self.store.chat_add(
                job_id, stage, attempt, "engine",
                "(the service shut down before this was answered — ask again, "
                "or answer the gate with Proceed/Redo/Skip)",
                degraded=True,
            )
            raise
        except Exception as e:
            log.exception("chat run failed for %s", job_id)
            reply, meta, degraded = (f"(chat failed: {str(e)[:300]} — the question is "
                                     "recorded; answer the gate with Proceed/Redo/Skip)"), {}, True

        self.store.chat_add(
            job_id, stage, attempt, "engine", reply,
            cost_usd=meta.get("cost_usd"), num_turns=meta.get("num_turns"),
            duration_ms=meta.get("duration_ms"), session_id=meta.get("session_id") or "",
            degraded=degraded, lane=meta.get("lane") or "",
        )
        # mirror the exchange to the ticket — ClickUp stays the record
        q_label = f"Q (P{stage} gate)" if not v1 else "Q"
        verbs = "/proceed, /redo, or /skip" if not v1 else "/proceed or /skip"
        await self.clickup.comment(
            job.get("clickup_task_id") or "",
            f"{GATE_PREFIX} 💬 **{q_label}:** {message[:800]}\n\n"
            f"**A:** {reply[:6000]}\n\n"
            f"_Replies here must start with {verbs}._",
        )

    async def _chat_dispatch(self, job, stage, message, target, publish,
                             v1: bool = False) -> tuple[str, dict, bool]:
        """Two-lane routing (docs/CONVERSATIONS.md §5): try the fast lane —
        a bundle-primed streaming API call — and fall through to the tool-run
        slow lane when it escalates (NEED_CODE_RUN) or errors. The fast lane
        never blocks on locks and never touches a workspace. v1 (sentry/task)
        items use the same lanes primed from their record instead of stage
        artifacts."""
        if self.settings.chat_fast_enabled:
            status, text, fmeta = await self._chat_fast(job, stage, message, publish, v1)
            if status == "ok":
                return text.strip(), fmeta, False
            if status == "escalate":
                reason = text.split(":", 1)[1].strip()[:120] if ":" in text else ""
                publish("status", "needs the repository — starting a code run"
                        + (f" ({reason})" if reason else ""))
                log.info("chat %s P%s: fast lane escalated: %s",
                         job["issue_id"], stage, reason or text[:120])
            else:
                # error: the slow lane still answers; the fast lane stays UX-only
                log.warning("chat %s P%s: fast lane error: %s",
                            job["issue_id"], stage, text[:200])
        if v1:
            return await self._chat_inner_v1(job, stage, message, target, publish)
        return await self._chat_inner(job, stage, message, target, publish)

    async def _chat_fast(self, job, stage, message, publish, v1: bool = False):
        job_id = job["issue_id"]
        if v1:
            system = build_v1_fastlane_system(job, self.store.guidance_for(job_id))
        else:
            names = list(dict.fromkeys([stage_artifact(stage), "P1-prd.md"]))
            system = build_fastlane_system(
                job=job, stage=stage,
                inline_artifacts=self.store.artifact_contents(job_id, names),
                guidance_entries=self.store.guidance_for(job_id),
            )
        messages = build_fastlane_messages(
            self.store.chat_for(job_id, stage)[:-1],  # exclude the turn being answered
            message,
        )
        return await fastlane.stream_answer(
            self.settings, system, messages,
            on_delta=lambda chunk: publish("delta", chunk),
        )

    async def _chat_inner_v1(self, job, stage, message, target, publish=None) -> tuple[str, dict, bool]:
        """Slow lane for sentry/task items: a read-only run on a fresh checkout
        of the BASE branch (v1 items have no feature branch or stage artifacts —
        the record travels in the prompt). Session continuity: resume the item's
        previous chat session when persistence is on; else artifact-primed fresh."""
        publish = publish or (lambda event, data: None)
        job_id = job["issue_id"]
        if target is None:
            return "(no repo is mapped for this project — cannot answer from code)", {}, True
        publish("status", "preparing a read-only workspace")

        # a DEDICATED clone + lock, NOT the main repo lock: a v1 item holds the
        # main lock for its entire run and lands terminal right after — waiting
        # on it would make mid-run chat unreachable. Chat reads the BASE branch,
        # so its own clone is always safe; the chat lock only serializes chats.
        async with self.locks.for_repo(f"chat:{target.repo}"):
            # no status re-check: v1 chat is valid for the item's whole life —
            # a question queued mid-run stays answerable after the run lands
            # (post-mortems read the BASE branch, never the run's residue)
            fresh = self.store.get(job_id)
            if not fresh:
                return ("This item no longer exists — the question stays on "
                        "the record."), {}, True
            try:
                workspace = await prepare_workspace(
                    self.settings, target, f"brain/chat-{job_id}",
                    workspace_root=f"{self.settings.workspaces_dir}/chat")
            except Exception as e:
                return (f"(cannot check out the repository to answer from code: "
                        f"{str(e)[:200]} — answering is limited to the record)"), {}, True

            resume_sid = None
            if self.settings.session_persistence:
                for t in reversed(self.store.chat_for(job_id, stage)):
                    # v1 chat runs live in the DEDICATED chat store (they invoke
                    # with config_dir=claude_chat_config_dir) — check THAT store,
                    # not the default stage store, or resume never fires
                    if (t["role"] == "engine" and t.get("session_id") and not t.get("degraded")
                            and session_transcript_exists(
                                self.settings, t["session_id"],
                                config_dir=self.settings.claude_chat_config_dir)):
                        resume_sid = t["session_id"]
                        break
            if resume_sid:
                prompt = (f"The reviewer asks a follow-up about this item (READ-ONLY — "
                          f"do not modify anything):\n\n{message.strip()[:4000]}")
            else:
                prompt = build_v1_chat_prompt(
                    target=target, job=job, message=message,
                    transcript=self.store.chat_for(job_id, stage)[:-1],
                )
            chat_dir = (self.settings.claude_chat_config_dir
                        if self.settings.session_persistence else None)
            store_lock = (self.locks.chat_global if chat_dir else self.locks.claude_global)
            publish("status", "reading the code")
            async with store_lock:
                raw = await run_claude_stream(
                    self.settings, workspace, prompt,
                    allowed_tools=CHAT_TOOLS,
                    timeout=self.settings.chat_timeout_seconds,
                    disallowed_tools=CHAT_DENIED_TOOLS,
                    resume_session=resume_sid, config_dir=chat_dir,
                    on_event=publish,
                )
            if raw.status == "session_lost":
                # pruned transcript: fall back to a fresh primed run, same locks
                prompt = build_v1_chat_prompt(
                    target=target, job=job, message=message,
                    transcript=self.store.chat_for(job_id, stage)[:-1],
                )
                async with store_lock:
                    raw = await run_claude_stream(
                        self.settings, workspace, prompt,
                        allowed_tools=CHAT_TOOLS,
                        timeout=self.settings.chat_timeout_seconds,
                        disallowed_tools=CHAT_DENIED_TOOLS,
                        config_dir=chat_dir,
                        on_event=publish,
                    )
            # hygiene: a chat run must never leave residue in the shared workspace
            code, status = await git(workspace, "status", "--porcelain")
            dirty = code == 0 and bool(status.strip())
            if dirty:
                await git(workspace, "reset", "--hard")
                await git(workspace, "clean", "-fd")

        if raw.status != "ok" or not raw.text.strip():
            return (f"(chat run ended with `{raw.status}` — try again, or answer the "
                    "item with Proceed/Skip)"), self._meta(raw), True
        reply = raw.text.strip()
        if dirty:
            reply += "\n\n_(note: the chat attempted writes; they were discarded)_"
        return reply, self._meta(raw), False

    async def _chat_inner(self, job, stage, message, target, publish=None) -> tuple[str, dict, bool]:
        publish = publish or (lambda event, data: None)
        job_id = job["issue_id"]
        if target is None:
            return "(no repo is mapped for this project — cannot answer from code)", {}, True
        branch = f"brain/feat-{job_id}"
        publish("status", "waiting for the repository workspace")

        async with self.locks.for_repo(target.repo):
            # re-validate under the lock: the gate may have been answered while
            # queued. Parked, running AND terminal are all answerable (post-
            # mortems included) — only a stage ADVANCE means the question's
            # moment passed, because the answer would describe superseded work.
            fresh = self.store.get(job_id)
            if not fresh or int(fresh.get("stage") or 0) != stage:
                return ("The gate was answered before I got to this — the pipeline has "
                        "moved on. This question stays on the record."), {}, True
            try:
                workspace = await prepare_feature_workspace(self.settings, target, branch, stage)
            except (BranchLostError, RuntimeError) as e:
                return (f"(cannot check out the feature branch to answer from code: "
                        f"{str(e)[:200]} — answering is limited to the gate summary)"), {}, True

            inline: dict[str, str] = {}
            for name in (stage_artifact(stage), "P1-prd.md"):
                p = artifact_path(workspace, job_id, name)
                if p.exists() and name not in inline:
                    text = p.read_text().strip()
                    inline[name] = text[:6000] + ("\n… (truncated — read the file)" if len(text) > 6000 else "")

            # Chat mode by gate class (docs/CONVERSATIONS.md §1): code gates fork the
            # stage session when possible — its memory of the exploration and test
            # output is the value there; everything else is artifact-primed (cheap).
            resume_sid, fork_new = self._chat_session_for(job, stage)
            if resume_sid:
                convo = ""
                if fork_new:
                    # a fresh fork has no memory of earlier chat turns (e.g. after a
                    # lost fork id) — re-prime it from the recorded transcript
                    for t in self.store.chat_for(job_id, stage)[:-1][-6:]:
                        who = "Reviewer" if t["role"] == "human" else "You"
                        convo += f"\n{who}: {(t['text'] or '').strip()[:600]}"
                    if convo:
                        convo = f"\n\nConversation so far at this gate:{convo}\n"
                prompt = (
                    f"PAUSE — you are at the P{stage} gate of this stage run, in READ-ONLY "
                    f"mode, answering the human reviewer's question below. This is a copy of "
                    f"your session: the working session will only see the reviewer's final "
                    f"Answer plus this conversation's transcript. Do NOT modify, create or "
                    f"delete anything. Answer directly and concisely (under 250 words unless "
                    f"the question demands more); cite files when referencing code; if an "
                    f"honest answer requires changing the work, say so and recommend /redo "
                    f"with concrete notes.{convo}\n\nThe reviewer asks:\n\n{message.strip()[:4000]}"
                )
                publish("status", "consulting the stage session")
                async with self.locks.claude_global:  # shares the session store
                    raw = await run_claude_stream(
                        self.settings, workspace, prompt,
                        allowed_tools=CHAT_TOOLS,
                        timeout=self.settings.chat_timeout_seconds,
                        disallowed_tools=CHAT_DENIED_TOOLS,
                        resume_session=resume_sid, fork_session=fork_new,
                        on_event=publish,
                    )
                if raw.status == "session_lost":
                    resume_sid = None  # fall through to artifact-primed below
            if not resume_sid:
                prompt = build_chat_prompt(
                    target=target, branch=branch, job=job, stage=stage, message=message,
                    transcript=self.store.chat_for(job_id, stage)[:-1],  # exclude the turn being answered
                    inline_artifacts=inline,
                )
                # serialize on whichever store this run touches: the dedicated
                # chat store when persistence is on, else the shared default
                # store (which stage runs also use — see _invoke)
                chat_dir = (self.settings.claude_chat_config_dir
                            if self.settings.session_persistence else None)
                store_lock = (self.locks.chat_global if chat_dir
                              else self.locks.claude_global)
                publish("status", "reading the branch artifacts and code")
                async with store_lock:
                    raw = await run_claude_stream(
                        self.settings, workspace, prompt,
                        allowed_tools=CHAT_TOOLS,
                        timeout=self.settings.chat_timeout_seconds,
                        disallowed_tools=CHAT_DENIED_TOOLS,
                        config_dir=chat_dir,
                        on_event=publish,
                    )
            # hygiene: a chat run must never leave residue for _checkpoint to commit
            code, status = await git(workspace, "status", "--porcelain")
            dirty = code == 0 and bool(status.strip())
            if dirty:
                await git(workspace, "reset", "--hard")
                await git(workspace, "clean", "-fd")

        if raw.status != "ok" or not raw.text.strip():
            return (f"(chat run ended with `{raw.status}` — try again, or answer the "
                    "gate with Proceed/Redo/Skip)"), self._meta(raw), True
        reply = raw.text.strip()
        if dirty:
            reply += "\n\n_(note: the chat attempted writes; they were discarded)_"
        return reply, self._meta(raw), False

    def _chat_session_for(self, job: dict, stage: int) -> tuple[str | None, bool]:
        """(session_to_resume, fork_new): continue this gate's existing chat
        session, else fork the stage session — chat sessions are keyed by
        (job, stage, attempt) via the gate_chat rows, never a bare jobs column
        (a stale fork must never answer a later gate's questions)."""
        if stage_kind(stage) != "code" or not self.settings.session_persistence:
            return None, False
        attempt = int(job.get("stage_attempts") or 1)
        for t in reversed(self.store.chat_for(job["issue_id"], stage)):
            if (t["role"] == "engine" and t.get("session_id") and not t.get("degraded")
                    and int(t.get("attempt") or 0) == attempt
                    and session_transcript_exists(self.settings, t["session_id"])):
                return t["session_id"], False  # continue the gate's chat session
        runs = [r for r in self.store.stage_runs_for(job["issue_id"])
                if r["stage"] == stage and r.get("session_id")]
        if runs and session_transcript_exists(self.settings, runs[-1]["session_id"]):
            return runs[-1]["session_id"], True  # first message: fork the stage session
        return None, False

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
        pr_urls: list[str] = []  # every explicit PR_URL line across both runs
        for run in (1, 2):
            run_id = self.store.stage_run_open(job_id, stage=run, attempt=1)
            prompt = build_bootstrap_prompt(target=target, branch=branch, project=project,
                                            is_canonical=is_canonical, run=run)
            raw = await self._invoke(workspace, prompt,
                                     BASE_ALLOWED_TOOLS + target.allow,
                                     self.settings.claude_timeout_seconds)
            await self._checkpoint(workspace, branch, job_id, stage=0)
            # terminal outcomes comment the owning ticket: memory jobs are
            # ClickUp-adopted now, and a silent job is indistinguishable from a
            # stuck one from the outside (dogfood-found)
            if raw.status != "ok":
                self.store.stage_run_close(run_id, raw.status, **self._meta(raw))
                self.store.set_status(job_id, raw.status, detail=raw.text[:2000])
                await self._comment(job, f"memory bootstrap run {run} ended with "
                                         f"`{raw.status}`: {raw.text[:400]} — re-file the "
                                         "`[memory]` ticket to retry.")
                return
            marker, payload, found_pr = parse_stage_output(raw.text)
            self.store.stage_run_close(run_id, marker, **self._meta(raw))
            pr_url = found_pr or pr_url
            for u in all_pr_urls(raw.text):
                if u not in pr_urls:
                    pr_urls.append(u)
            if marker == "fail":
                self.store.set_status(job_id, "no_fix", detail=payload[:2000])
                await self._comment(job, f"memory bootstrap stopped (run {run} reported "
                                         f"STAGE_FAIL): {payload[:600]}")
                return
            if marker == "unparsed":  # fail closed — never advance/complete on an unmarked run
                self.store.set_status(job_id, "error",
                                      detail=f"bootstrap run {run} ended without STAGE_DONE/"
                                             f"STAGE_FAIL: {payload[-1500:]}")
                await self._comment(job, f"memory bootstrap run {run} ended without a "
                                         "STAGE_DONE/STAGE_FAIL marker — re-file the "
                                         "`[memory]` ticket to retry.")
                return
        await self.memory.refresh_cache(project, workspace, target.base)
        if pr_urls:  # tracked, but no auto-ready/review — memory PRs are doc drafts
            await self.record_prs(job_id, pr_urls, kickoff=False)
        self.store.set_status(job_id, "pr_opened" if pr_url else "no_fix",
                              pr_url=pr_url, detail="memory bootstrap complete")
        await self._comment(
            job, "✅ memory bootstrap complete"
                 + (f" — draft PR for review: {pr_url}" if pr_url
                    else " — no PR was opened (see the job detail)."))
