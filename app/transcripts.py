"""Run transcripts (docs/ENGINE.md §13): the replayable record of what the
engine actually DID during a run — tool calls and assistant text, the same
events the live session stream shows — persisted write-through as JSONL under
data_dir/transcripts/<job_id>/<key>.jsonl.

The live stream (chatstream broker) is ephemeral: subscribe late or reload and
the activity is gone, which is exactly the "chat only shows messages between
the user and the system" gap. Transcripts close it: every stage run and every
v1 fix run writes its events through here as they happen, so the dashboard can
replay a run's activity at any time after the fact.

Invariants:
- Write-through, never buffered: a crash mid-run keeps everything up to the
  crash. Each line is flushed as written.
- Capped (TRANSCRIPT_CAP_BYTES): one explicit truncation marker, then silence —
  a runaway run cannot fill the volume.
- Fail-open: transcript I/O errors disable the writer and never break the run.
- TTL-pruned by the session janitor alongside CLI session transcripts.

File format: one JSON object per line, {"at": epoch, "e": event, "d": data}.
Events: "start" (header: stage/attempt/kind), "status" (one tool call or
progress line), "delta" (assistant text chunk), "end" (result status).
"""

import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger("brain.transcripts")

TRANSCRIPT_CAP_BYTES = 2 * 1024 * 1024  # 2MB per run
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")  # path-traversal guard


def transcripts_root(settings) -> Path:
    return Path(settings.data_dir) / "transcripts"


def _job_dir(settings, job_id: str) -> Path | None:
    """Transcript directory for a job; None when the id can't be a safe path
    segment (never raise — transcripts are best-effort by design)."""
    job_id = str(job_id or "")
    if not KEY_RE.match(job_id):
        return None
    return transcripts_root(settings) / job_id


class TranscriptWriter:
    """Appends run events to one JSONL file, flushing per line. All methods
    swallow I/O errors: a full disk or bad mount must never fail a run."""

    def __init__(self, path: Path | None):
        self._fh = None
        self._bytes = 0
        self._truncated = False
        self.path = path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8")
            self._bytes = path.stat().st_size
        except OSError:
            log.warning("transcript disabled — cannot open %s", path, exc_info=True)
            self._fh = None

    def write(self, event: str, data):
        if self._fh is None or self._truncated:
            return
        try:
            if self._bytes >= TRANSCRIPT_CAP_BYTES:
                self._truncated = True
                marker = json.dumps({"at": time.time(), "e": "truncated",
                                     "d": f"transcript capped at {TRANSCRIPT_CAP_BYTES // (1024 * 1024)}MB"})
                self._fh.write(marker + "\n")
                self._fh.flush()
                return
            line = json.dumps({"at": time.time(), "e": str(event), "d": data},
                              ensure_ascii=False, default=str)
            self._fh.write(line + "\n")
            self._fh.flush()
            self._bytes += len(line) + 1
        except (OSError, ValueError):
            log.warning("transcript write failed — disabling for this run")
            self.close()

    def close(self, status: str | None = None):
        if self._fh is None:
            return
        if status and not self._truncated:
            try:
                self._fh.write(json.dumps({"at": time.time(), "e": "end", "d": status}) + "\n")
            except (OSError, ValueError):
                pass
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None


def open_writer(settings, job_id: str, key: str, header: dict) -> TranscriptWriter:
    """A writer for one run. key names the file (stage runs: 'P4-run17';
    v1 runs: 'v1-p1-<ts>'); header lands as the first 'start' event."""
    base = _job_dir(settings, job_id)
    path = (base / f"{key}.jsonl") if (base is not None and KEY_RE.match(key or "")) else None
    writer = TranscriptWriter(path)
    writer.write("start", header)
    return writer


def list_for_job(settings, job_id: str) -> list[dict]:
    """The job's recorded runs, oldest first: key + header + size, enough for
    the dashboard to build the Activity accordion without reading every file."""
    base = _job_dir(settings, job_id)
    if base is None or not base.is_dir():
        return []
    files = []
    for f in base.glob("*.jsonl"):
        try:
            st = f.stat()  # ONE stat per file: sort key and entry must agree
        except OSError:
            continue  # pruned between glob and stat — absent, not an error
        files.append((st.st_mtime, st.st_size, f))
    out = []
    for mtime, size, f in sorted(files, key=lambda t: t[0]):
        entry = {"key": f.stem, "size": size, "mtime": mtime, "header": {}}
        try:
            with open(f, encoding="utf-8") as fh:
                first = json.loads(fh.readline() or "{}")
                if first.get("e") == "start" and isinstance(first.get("d"), dict):
                    entry["header"] = first["d"]
        except (OSError, json.JSONDecodeError):
            pass
        out.append(entry)
    return out


def read_events(settings, job_id: str, key: str) -> list[dict] | None:
    """Parsed events of one run transcript; None when it doesn't exist (the
    API surfaces that as 404). Unparsable lines are skipped, not fatal —
    a torn final line after a crash must not hide the rest of the run."""
    base = _job_dir(settings, job_id)
    if base is None or not KEY_RE.match(key or ""):
        return None
    path = base / f"{key}.jsonl"
    if not path.is_file():
        return None
    events = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return None
    return events


def prune(settings, ttl_days: int) -> int:
    """Janitor hook: drop transcripts whose file mtime is beyond the TTL, then
    empty job directories. Mtime, not job status — mirrors the CLI-session
    janitor's reasoning (abandoned gates, unattributed v1 traffic)."""
    root = transcripts_root(settings)
    if not root.is_dir():
        return 0
    cutoff = time.time() - ttl_days * 86400
    pruned = 0
    for f in root.glob("*/*.jsonl"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                pruned += 1
        except OSError:
            continue
    for d in root.iterdir():
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            continue
    return pruned
