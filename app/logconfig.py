"""Structured logging + request/job correlation ids (Epic F4).

``LOG_FORMAT=text`` (default) keeps today's exact ``basicConfig`` format
byte-for-byte. ``LOG_FORMAT=json`` emits one JSON object per line carrying the
current ``request_id`` / ``job_id`` from contextvars (set by the HTTP
middleware and the ``logctx`` wrapper around each job).
"""

import contextlib
import contextvars
import datetime
import json
import logging

# correlation ids — read by JsonFormatter (and available to any handler via the
# filter). Absent (None) when no request/job is in scope.
request_id_var: contextvars.ContextVar = contextvars.ContextVar("request_id", default=None)
job_id_var: contextvars.ContextVar = contextvars.ContextVar("job_id", default=None)

TEXT_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


class _ContextFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id_var.get()
        record.job_id = job_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": datetime.datetime.fromtimestamp(
                record.created, tz=datetime.timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = getattr(record, "request_id", None)
        jid = getattr(record, "job_id", None)
        if rid:
            payload["request_id"] = rid
        if jid:
            payload["job_id"] = jid
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(settings) -> None:
    """Install the selected formatter on the root logger. Idempotent — replaces
    any handler we previously added so a re-call (tests) doesn't stack them."""
    fmt = (getattr(settings, "log_format", "text") or "text").strip().lower()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # drop handlers we own; leave foreign ones (e.g. pytest's) alone
    for h in list(root.handlers):
        if getattr(h, "_ctrlloop_owned", False):
            root.removeHandler(h)
    handler = logging.StreamHandler()
    handler._ctrlloop_owned = True
    handler.addFilter(_ContextFilter())
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(TEXT_FORMAT))
    root.addHandler(handler)


@contextlib.contextmanager
def logctx(*, request_id: str | None = None, job_id: str | None = None):
    """Bind correlation ids for the duration of a block (a job's processing, a
    request). Tokens are reset on exit so ids never leak across tasks."""
    rtok = request_id_var.set(request_id) if request_id is not None else None
    jtok = job_id_var.set(job_id) if job_id is not None else None
    try:
        yield
    finally:
        if jtok is not None:
            job_id_var.reset(jtok)
        if rtok is not None:
            request_id_var.reset(rtok)
