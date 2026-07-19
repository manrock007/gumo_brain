"""Epic F3: sandboxed container runner seam (FLAG, off by default)."""

import asyncio
import os

import pytest

from app.config import Settings
from app.runner import (ContainerRunner, LocalRunner, resolve_runner)


def test_default_backend_is_local():
    assert resolve_runner(Settings()).name == "local"


def test_unknown_backend_falls_closed_to_local():
    assert resolve_runner(Settings(runner_backend="nope")).name == "local"


def test_container_backend_selected():
    s = Settings(runner_backend="container", runner_container_image="img",
                 runner_container_network="ctl-net")
    assert isinstance(resolve_runner(s), ContainerRunner)


def test_local_runner_is_passthrough_exec():
    async def go():
        proc = await LocalRunner().spawn(["/bin/echo", "hi"], cwd=os.getcwd(), env={})
        out, _ = await proc.communicate()
        assert out.strip() == b"hi"
        assert proc.returncode == 0
    asyncio.run(go())


def _crunner(**over):
    s = Settings(runner_backend="container",
                 runner_container_image=over.get("image", "ctl/runner:latest"),
                 runner_container_network=over.get("network", "ctl-egress"),
                 runner_container_extra_args=over.get("extra", ""))
    return ContainerRunner(s)


def test_container_argv_shape():
    r = _crunner(extra="--cpus 2")
    argv = r.build_argv(["claude", "-p", "go"], cwd="/data/ws/x",
                        env_file="/tmp/envf", container_name="ctl-run-abc")
    assert argv[:2] == ["docker", "run"]
    assert "--rm" in argv
    assert argv[argv.index("--name") + 1] == "ctl-run-abc"
    assert argv[argv.index("--network") + 1] == "ctl-egress"
    assert argv[argv.index("-v") + 1] == "/data/ws/x:/data/ws/x"
    assert argv[argv.index("-w") + 1] == "/data/ws/x"
    assert argv[argv.index("--env-file") + 1] == "/tmp/envf"
    assert "--cpus" in argv and "2" in argv
    # image precedes the actual command
    assert argv[-3:] == ["claude", "-p", "go"]
    assert "ctl/runner:latest" in argv
    assert argv.index("ctl/runner:latest") < argv.index("claude")


def test_container_empty_network_fails_closed():
    r = _crunner(network="")
    with pytest.raises(RuntimeError, match="egress"):
        r.build_argv(["claude"], cwd="/w", env_file="/tmp/e", container_name="n")


def test_container_missing_image_fails():
    r = _crunner(image="")
    with pytest.raises(RuntimeError, match="IMAGE"):
        r.build_argv(["claude"], cwd="/w", env_file="/tmp/e", container_name="n")


def test_env_file_is_written_0600_and_keyvalue(tmp_path):
    r = _crunner()
    path = r._write_env_file({"GH_TOKEN": "secret", "CLAUDE_CONFIG_DIR": "/cfg", "X": None})
    try:
        mode = oct(os.stat(path).st_mode & 0o777)
        assert mode == "0o600"
        body = open(path).read()
        assert "GH_TOKEN=secret" in body
        assert "CLAUDE_CONFIG_DIR=/cfg" in body
        assert "X=" not in body  # None values skipped
    finally:
        os.unlink(path)


def test_container_kill_issues_docker_kill(monkeypatch, tmp_path):
    """spawn's kill() must tear down the container by name (no leaked container)
    and unlink the env-file."""
    from app import runner as runner_mod

    calls = []

    class _FakeProc:
        returncode = None
        stdout = None
        stderr = None

        def kill(self):
            calls.append("client-kill")

    envf = tmp_path / "envf"
    envf.write_text("A=1")
    cp = runner_mod._ContainerProcess(_FakeProc(), "ctl-run-xyz", str(envf), "docker")

    async def go():
        killed = {}

        async def fake_docker_kill():
            killed["name"] = cp._name

        cp._docker_kill = fake_docker_kill
        cp.kill()
        await asyncio.sleep(0)  # let the scheduled docker-kill task run
        return killed

    killed = asyncio.run(go())
    assert "client-kill" in calls
    assert killed.get("name") == "ctl-run-xyz"
    assert not envf.exists()  # env-file unlinked


def test_container_kill_falls_back_to_sync_off_loop(tmp_path):
    """kill() called with NO running loop (sync caller / loop stopped) must still
    reap the container via the blocking path — get_running_loop() raises there,
    and a silent pass would leak the container (Seer 1603282)."""
    from app import runner as runner_mod

    class _FakeProc:
        returncode = None
        stdout = None
        stderr = None

        def kill(self):
            pass

    envf = tmp_path / "envf"
    envf.write_text("A=1")
    cp = runner_mod._ContainerProcess(_FakeProc(), "ctl-run-off", str(envf), "docker")
    reaped = {}
    cp._docker_kill_sync = lambda: reaped.setdefault("name", cp._name)

    cp.kill()  # no running loop here — must take the sync fallback

    assert reaped.get("name") == "ctl-run-off"
    assert not envf.exists()
