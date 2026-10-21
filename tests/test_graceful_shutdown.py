"""Real SIGTERM against an isolated service and a fake provider-owned child."""
import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

from test_pirates import make_site
from boring_pirates.common.ipc import Pirate, encode, serve_instance, settings

async def fixture(root):
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
    parrot = Pirate(settings(make_site(root)), {"alpha": Provider(), "beta": Provider()},
            register_glob=str(root / "no-registration" / "*"))
    await serve_instance(parrot)

def test_sigterm_settles_assignment_before_exiting(tmp_path):
    process = subprocess.Popen([sys.executable, __file__, str(tmp_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 30  # a loaded host starts Python slowly
        client = socket.socket(socket.AF_UNIX)
        while True:  # the socket file appears at bind, before listen
            assert process.poll() is None
            try:
                client.connect(str(tmp_path / "parrot.sock"))
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
            process.send_signal(signal.SIGTERM)
            _, error = process.communicate(timeout=30)
        assert process.returncode == 0, error.decode()[-2000:]
        assert (tmp_path / "settled").read_text() == "yes"
        records = [json.loads(line) for line in (tmp_path / "history" / ("a" * 32 + ".jsonl")).read_text().splitlines()]
        assert records[-1]["kind"] == "outcome"
        assert records[-1]["data"]["state"] == "cancelled"
        assert not (tmp_path / "parrot.sock").exists()
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
    asyncio.run(fixture(Path(sys.argv[1])))
