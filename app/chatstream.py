"""In-memory SSE broker for gate-chat streaming (docs/CONVERSATIONS.md §5).

Single-process by design, like every lock in this service: the broker holds the
CURRENT turn's event buffer per job so a subscriber that connects late — or
reconnects mid-answer — replays what it missed, then follows live. The
persisted gate_chat row remains the durable record; the stream is pure UX and
losing it costs nothing (the dashboard's polling still picks the reply up).
"""

import asyncio
import time

# Event buffer cap per turn. Once full, consecutive text deltas coalesce and
# other events drop — a replay is then coarser, never wrong (the final text
# comes from the persisted row anyway).
MAX_EVENTS = 800
HEARTBEAT_SECONDS = 15


class ChatBroker:
    def __init__(self):
        # job_id -> {"events": [(event, data)], "done": bool,
        #            "queues": set[asyncio.Queue], "started": float}
        self._turns: dict[str, dict] = {}

    def start(self, job_id: str):
        """Begin a new turn: replaces the previous turn's buffer (single-flight
        per gate means at most one live turn per job)."""
        old = self._turns.get(job_id)
        if old is not None:
            for q in list(old["queues"]):
                q.put_nowait(("done", ""))
        self._turns[job_id] = {"events": [], "done": False,
                               "queues": set(), "started": time.time()}

    def publish(self, job_id: str, event: str, data: str):
        """Synchronous: safe to call from any coroutine without awaiting."""
        t = self._turns.get(job_id)
        if t is None or t["done"]:
            return
        if len(t["events"]) >= MAX_EVENTS:
            if event == "delta" and t["events"] and t["events"][-1][0] == "delta":
                t["events"][-1] = ("delta", t["events"][-1][1] + data)
            # non-delta events past the cap are dropped from the replay buffer
        else:
            t["events"].append((event, data))
        for q in list(t["queues"]):
            q.put_nowait((event, data))

    def finish(self, job_id: str):
        t = self._turns.get(job_id)
        if t is None or t["done"]:
            return
        t["done"] = True
        for q in list(t["queues"]):
            q.put_nowait(("done", ""))

    def active(self, job_id: str) -> bool:
        t = self._turns.get(job_id)
        return t is not None and not t["done"]

    async def subscribe(self, job_id: str, max_seconds: float = 900):
        """Async generator of (event, data): replay the buffered turn, then
        follow live until the turn finishes. Emits ("ping", "") on quiet spells
        so proxies keep the connection open; always terminates with ("done", "")
        so a client never hangs on a dead turn."""
        t = self._turns.get(job_id)
        if t is None:
            yield ("done", "")
            return
        q: asyncio.Queue = asyncio.Queue()
        # register BEFORE snapshotting, with no await in between: an event is
        # either in the snapshot (published before) or in the queue (after),
        # never both, never neither
        t["queues"].add(q)
        snapshot = list(t["events"])
        done = t["done"]
        try:
            for ev in snapshot:
                yield ev
            if done:
                yield ("done", "")
                return
            deadline = time.monotonic() + max_seconds
            while True:
                timeout = min(HEARTBEAT_SECONDS, deadline - time.monotonic())
                if timeout <= 0:
                    yield ("done", "")
                    return
                try:
                    event, data = await asyncio.wait_for(q.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    yield ("ping", "")
                    continue
                yield (event, data)
                if event == "done":
                    return
        finally:
            t["queues"].discard(q)
