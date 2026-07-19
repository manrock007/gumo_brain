"""Run sandbox seam (Epic F3, FLAG — local default).

Mirrors the analytics/secrets/db provider pattern: a base class, concrete
drivers, a factory keyed on a settings field, fail-closed to the working local
path on an unknown backend.

``spawn(cmd, cwd, env)`` returns an object with the exact
``asyncio.subprocess.Process`` surface fixer relies on — ``.communicate()``,
``.kill()``, ``.wait()``, ``.stdout``, ``.stderr``, ``.returncode`` — so
fixer's raw + streaming runners are unchanged apart from the spawn indirection.

- LocalRunner (DEFAULT): ``asyncio.create_subprocess_exec`` verbatim —
  byte-for-byte today.
- ContainerRunner (FLAG): each run in a disposable ``docker run --rm`` with the
  clone bind-mounted, NO ambient env (only the already-G2-allow-listed dict, via
  a 0600 --env-file), and an operator-provided egress-allowlist network. An
  empty network config FAILS CLOSED (refuses to start rather than granting full
  egress). Kill/timeout also issues ``docker kill`` so a killed client never
  leaks a live container; the env-file is unlinked in cleanup.
"""

import asyncio
import logging
import os
import subprocess
import tempfile
import uuid

log = logging.getLogger("brain.runner")

RUNNER_BACKENDS = ("local", "container")


class RunnerBackend:
    name = "base"

    async def spawn(self, cmd: list[str], cwd: str, env: dict,
                    limit: int | None = None):  # pragma: no cover - abstract
        raise NotImplementedError


class LocalRunner(RunnerBackend):
    """Default: the exact subprocess exec the engine has always used."""

    name = "local"

    async def spawn(self, cmd: list[str], cwd: str, env: dict,
                    limit: int | None = None):
        kwargs = {}
        if limit is not None:
            kwargs["limit"] = limit
        return await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs)


class _ContainerProcess:
    """Wraps the docker CLIENT process so the caller sees a normal Process while
    kill/cleanup also tears down the container and the temp env-file."""

    def __init__(self, proc, container_name: str, env_file: str, docker_cmd: str):
        self._proc = proc
        self._name = container_name
        self._env_file = env_file
        self._docker = docker_cmd
        self._cleaned = False

    # transparent proxies to the underlying docker client process
    @property
    def stdout(self):
        return self._proc.stdout

    @property
    def stderr(self):
        return self._proc.stderr

    @property
    def returncode(self):
        return self._proc.returncode

    async def communicate(self):
        try:
            return await self._proc.communicate()
        finally:
            self._unlink_env()

    async def wait(self):
        try:
            return await self._proc.wait()
        finally:
            self._unlink_env()

    def kill(self):
        # kill the docker client AND the container by name so a killed/timed-out
        # run never leaves a live container behind.
        try:
            self._proc.kill()
        except ProcessLookupError:
            pass
        # Schedule the async container kill on the RUNNING loop (get_event_loop()
        # raises on 3.12 when no loop is current, silently leaking the container).
        # If kill() is called off the loop (sync caller / loop already stopped),
        # get_running_loop() raises — fall back to a BLOCKING docker kill so the
        # container is still reaped rather than leaked (the method's whole promise).
        try:
            asyncio.get_running_loop().create_task(self._docker_kill())
        except RuntimeError:
            self._docker_kill_sync()
        self._unlink_env()

    async def _docker_kill(self):
        try:
            p = await asyncio.create_subprocess_exec(
                self._docker, "kill", self._name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            await p.wait()
        except Exception:
            log.warning("docker kill %s failed", self._name)

    def _docker_kill_sync(self):
        """Blocking container kill for the no-running-loop path — cleanup must
        happen even when kill() is invoked outside the event loop."""
        try:
            subprocess.run([self._docker, "kill", self._name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15, check=False)
        except Exception:
            log.warning("docker kill %s failed (sync)", self._name)

    def _unlink_env(self):
        if self._cleaned:
            return
        self._cleaned = True
        try:
            os.unlink(self._env_file)
        except OSError:
            pass


class ContainerRunner(RunnerBackend):
    """FLAG: sandbox each run in a disposable container. Off by default."""

    name = "container"

    def __init__(self, settings):
        self.docker = settings.runner_container_cmd or "docker"
        self.image = (settings.runner_container_image or "").strip()
        self.network = (settings.runner_container_network or "").strip()
        self.extra_args = (settings.runner_container_extra_args or "").split()

    def build_argv(self, cmd: list[str], cwd: str, env_file: str,
                   container_name: str) -> list[str]:
        # egress allowlist is mandatory — an empty network config FAILS CLOSED
        # (refuse rather than grant full egress).
        if not self.network:
            raise RuntimeError(
                "runner_backend=container requires RUNNER_CONTAINER_NETWORK "
                "(egress allowlist) — refusing to start with full egress")
        if not self.image:
            raise RuntimeError(
                "runner_backend=container requires RUNNER_CONTAINER_IMAGE")
        return [
            self.docker, "run", "--rm",
            "--name", container_name,
            "--network", self.network,
            "-v", f"{cwd}:{cwd}",
            "-w", cwd,
            "--env-file", env_file,
            *self.extra_args,
            self.image,
            *cmd,
        ]

    def _write_env_file(self, env: dict) -> str:
        fd, path = tempfile.mkstemp(prefix="ctl-run-env-")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            for k, v in env.items():
                if v is None:
                    continue
                # --env-file is KEY=VALUE per line; the allow-listed values are
                # single-line tokens/paths by construction.
                f.write(f"{k}={v}\n")
        return path

    async def spawn(self, cmd: list[str], cwd: str, env: dict,
                    limit: int | None = None):
        container_name = f"ctl-run-{uuid.uuid4().hex[:16]}"
        env_file = self._write_env_file(env)
        try:
            argv = self.build_argv(cmd, cwd, env_file, container_name)
        except Exception:
            try:
                os.unlink(env_file)
            except OSError:
                pass
            raise
        kwargs = {}
        if limit is not None:
            kwargs["limit"] = limit
        # the docker client inherits NO ambient env beyond what it needs to talk
        # to the daemon — the CONTAINER's env comes solely from --env-file.
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs)
        except Exception:
            # spawn failed (e.g. docker binary absent -> FileNotFoundError, or
            # OSError/EMFILE): no _ContainerProcess is returned, so its _unlink_env
            # cleanup can never run. Unlink the secret-bearing env-file here or a
            # misconfigured container deployment leaks a 0600 credential file on
            # every attempt. Mirrors the build_argv guard above.
            try:
                os.unlink(env_file)
            except OSError:
                pass
            raise
        return _ContainerProcess(proc, container_name, env_file, self.docker)


def resolve_runner(settings) -> RunnerBackend:
    """Factory keyed on runner_backend. Fail closed: unknown → LocalRunner."""
    backend = (getattr(settings, "runner_backend", "local") or "local").strip()
    if backend == "container":
        return ContainerRunner(settings)
    if backend != "local":
        log.warning("unknown runner_backend '%s' — falling back to local", backend[:40])
    return LocalRunner()
