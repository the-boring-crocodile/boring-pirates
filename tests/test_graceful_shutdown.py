"""Real SIGTERM against an isolated service and a fake provider-owned child."""
import asyncio
import json
import os
from pathlib import Path
import signal
import shlex
import pytest
import socket
import subprocess
import sys
import time

from test_pirates import make_site, unit_environment, Script, tool_call
from boring_pirates.common.runtime import ToolRuntime
from boring_worker_kit.execution import EnvironmentExecutor
from boring_worker_kit.host_limits import HostLimits
from boring_worker_kit.settings import EnvironmentSettings
from boring_pirates.common.ipc import Pirate, encode, serve_instance, settings

async def fixture(root, mode):
    class Provider:
        async def complete(self, messages):
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time; time.sleep(120)",
                start_new_session=True)
            (root / "child.pid").write_text(str(child.pid))
            try:
                await asyncio.Event().wait()
            finally:
                # This represents asynchronous contained-runtime settlement.
                await asyncio.sleep(.05)
                os.killpg(child.pid, signal.SIGTERM)
                await child.wait()
                (root / "settled").write_text("yes")
    if mode == "generation":
        config = settings(make_site(root), environment=unit_environment(root))
        providers = {"alpha": Provider(), "beta": Provider()}
    else:
        config = settings(make_site(root, profiles={"alpha": {
            "active": True, "model": "synthetic", "allowed_tools": ["exec"]}}), environment=unit_environment(root))
        code = f"import os,time; open({str(root / 'child.pid')!r}, 'w').write(str(os.getpid())); time.sleep(120)"
        command = shlex.join([sys.executable, "-c", code])
        providers = {"alpha": Script(tool_call("exec", "exec", command=command, timeout_seconds=20))}
    parrot = Pirate(config, providers, register_glob=str(root / "no-registration" / "*"))
    if mode != "generation":
        # Explicit development-mode executor: real child/cleanup, no systemd authority.
        execution = EnvironmentSettings("parrot", config.checkout, (config.checkout,), None,
            "boring-pirate-task-parrot", Path("/system.slice/system-pirateparrot.slice"), 1024)
        parrot.runtime = ToolRuntime(executor=EnvironmentExecutor(execution,
            HostLimits(task_slots=1, job_timeout_seconds=30)))
    await serve_instance(parrot)

@pytest.mark.parametrize("mode", ["generation", "exec", "exec_disconnect"])
def test_sigterm_settles_assignment_before_exiting(tmp_path, mode):
    process = subprocess.Popen([sys.executable, __file__, str(tmp_path), mode],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 30  # a loaded host starts Python slowly
        client = socket.socket(socket.AF_UNIX)
        while True:  # the socket file appears at bind, before listen
            assert process.poll() is None
            try:
                client.connect(str(tmp_path / "pirate.sock"))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                assert time.monotonic() < deadline
                time.sleep(.01)
        with client:
            client.sendall(encode({"operation": "run", "flavour": "alpha",
                "job_id": "a" * 32, "assignment": "synthetic", "timeout_seconds": 10}))
            while not (tmp_path / "child.pid").exists():
                assert time.monotonic() < deadline
                time.sleep(.01)
            if mode == "exec_disconnect":
                client.shutdown(socket.SHUT_RDWR)
                client.close()
                trail = tmp_path / "history" / ("a" * 32 + ".jsonl")
                while True:
                    rows = [json.loads(line) for line in trail.read_text().splitlines()]
                    if rows[-1]["kind"] == "outcome":
                        break
                    assert time.monotonic() < deadline
                    time.sleep(.01)
            process.send_signal(signal.SIGTERM)
            _, error = process.communicate(timeout=30)
        assert process.returncode == 0, error.decode()[-2000:]
        if mode == "generation":
            assert (tmp_path / "settled").read_text() == "yes"
        records = [json.loads(line) for line in (tmp_path / "history" / ("a" * 32 + ".jsonl")).read_text().splitlines()]
        assert records[-1]["kind"] == "outcome"
        assert records[-1]["data"]["state"] == "cancelled"
        expected = "client disconnected" if mode == "exec_disconnect" else "Pirate service stopping"
        assert records[-1]["data"]["reason"] == expected
        assert not (tmp_path / "pirate.sock").exists()
        pid = int((tmp_path / "child.pid").read_text())
        assert not Path(f"/proc/{pid}").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        # Fixture cleanup is scoped to the child PID recorded by this subprocess.
        if (tmp_path / "child.pid").exists():
            try: os.kill(int((tmp_path / "child.pid").read_text()), signal.SIGKILL)
            except ProcessLookupError: pass

if __name__ == "__main__":
    asyncio.run(fixture(Path(sys.argv[1]), sys.argv[2]))
