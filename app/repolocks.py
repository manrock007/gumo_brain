"""Per-repo serialization seam (Epic F2).

The default in-process backend is today's ``engine.RepoLocks`` (one
``asyncio.Lock`` per repo + the ``claude_global`` / ``chat_global`` locks
guarding the shared CLI config dir). It requires the service to run
single-process (uvicorn workers=1) and is unchanged for SQLite.

The Postgres backend maps the SAME interface onto session-level advisory locks,
so per-repo serialization spans PROCESSES and the workers=1 constraint lifts.
The ``claude_global`` / ``chat_global`` guards ALSO become cross-process
advisory locks (a plain ``asyncio.Lock`` is per-process and would let two worker
processes on one host race the shared ``~/.claude`` config dir — the blocker the
design's adversarial pass raised).

Interface preserved for both backends:
  - ``for_repo(repo)`` → an ``async with``-able context manager
  - ``claude_global`` / ``chat_global`` → ``async with``-able
  - ``is_busy(repo)`` → best-effort bool (dashboard hint)
"""

import asyncio
import hashlib
import logging

log = logging.getLogger("brain.repolocks")

# Fixed advisory-lock keys for the two global CLI-config-dir guards. Derived the
# same way as repo keys so they can never collide with a repo's key by accident.
_CLAUDE_GLOBAL_NAME = "ctrlloop:global:claude"
_CHAT_GLOBAL_NAME = "ctrlloop:global:chat"


def advisory_key(name: str) -> int:
    """Stable 64-bit signed int for a name (Postgres advisory-lock keys are
    bigint). blake2b keeps collisions negligible."""
    digest = hashlib.blake2b(name.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


class _PgAdvisoryLock:
    """One session-level ``pg_advisory_lock`` held on a dedicated standalone
    connection for the lifetime of the ``async with``. The blocking acquire/
    release run in a worker thread so the event loop is never stalled."""

    def __init__(self, driver, key: int):
        self._driver = driver
        self._key = key
        self._conn = None

    async def __aenter__(self):
        self._conn = await asyncio.to_thread(self._driver.lock_connection)
        try:
            await asyncio.to_thread(
                self._conn.execute, "SELECT pg_advisory_lock(%s)", (self._key,))
        except Exception:
            await asyncio.to_thread(self._conn.close)
            self._conn = None
            raise
        return self

    async def __aexit__(self, *exc):
        if self._conn is None:
            return
        try:
            await asyncio.to_thread(
                self._conn.execute, "SELECT pg_advisory_unlock(%s)", (self._key,))
        except Exception:  # closing the connection releases session locks anyway
            log.warning("advisory unlock failed for key %s — closing connection", self._key)
        finally:
            await asyncio.to_thread(self._conn.close)
            self._conn = None


class InProcessRepoLocks:
    """Today's behavior verbatim — one asyncio.Lock per repo, plus the two
    global CLI-config-dir guards. Single-process only (uvicorn workers=1)."""

    backend = "inprocess"

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}
        self.claude_global = asyncio.Lock()
        self.chat_global = asyncio.Lock()

    def for_repo(self, repo: str) -> asyncio.Lock:
        if repo not in self._locks:
            self._locks[repo] = asyncio.Lock()
        return self._locks[repo]

    def is_busy(self, repo: str) -> bool:
        lock = self._locks.get(repo)
        return bool(lock and lock.locked())


class PgAdvisoryRepoLocks:
    """Postgres backend: for_repo / claude_global / chat_global all resolve to
    cross-process advisory locks. A fresh ``async with``-able is returned each
    access, so ``store_lock = self.locks.claude_global; async with store_lock``
    keeps working unchanged."""

    backend = "postgres"

    def __init__(self, driver):
        self._driver = driver

    def for_repo(self, repo: str) -> _PgAdvisoryLock:
        return _PgAdvisoryLock(self._driver, advisory_key("repo:" + repo))

    @property
    def claude_global(self) -> _PgAdvisoryLock:
        return _PgAdvisoryLock(self._driver, advisory_key(_CLAUDE_GLOBAL_NAME))

    @property
    def chat_global(self) -> _PgAdvisoryLock:
        return _PgAdvisoryLock(self._driver, advisory_key(_CHAT_GLOBAL_NAME))

    def is_busy(self, repo: str) -> bool:
        """Best-effort: try to grab + immediately release the lock. True means
        someone else holds it. Never raises (dashboard hint only)."""
        key = advisory_key("repo:" + repo)
        conn = None
        try:
            conn = self._driver.lock_connection()
            got = conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()
            free = bool(got and list(got.values())[0] if isinstance(got, dict) else got[0])
            if free:
                conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
            return not free
        except Exception:
            return False
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def resolve_locks(store):
    """Factory: Postgres store → advisory-lock backend (cross-process); anything
    else → the in-process asyncio-lock backend (the SQLite default)."""
    driver = getattr(store, "_driver", None)
    if driver is not None and getattr(driver, "backend", "") == "postgres":
        return PgAdvisoryRepoLocks(driver)
    return InProcessRepoLocks()
