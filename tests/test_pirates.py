from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from boring_pirates.common import assignment
from boring_pirates.common.files import CheckoutFiles
from boring_pirates.common.profiles import default_files
from boring_pirates.common.history import FileHistory, prune
from boring_pirates.common.ipc import Pirate, exchange, settings
from boring_pirates.common.runtime import ToolRuntime
from boring_worker_kit.outcomes import Refusal
from boring_pirates.common.providers import Turn
from boring_pirates.mistral import provider as mistral
from boring_pirates.mistral.provider import MistralProvider

UID = os.getuid()
ALL_TOOLS = ["file_list", "file_read", "file_write"]


def mistral_answer(reason, content="", calls=None):
    """A whole chat completion as Mistral's API returns it."""
    return {
        "id": "synthetic", "object": "chat.completion", "model": "test-model",
        "created": 1, "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        "choices": [{"index": 0, "finish_reason": reason, "message": {
            "role": "assistant", "content": content,
            "tool_calls": [{"id": cid, "type": "function", "function": {
                "name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}} for cid, name, args in (calls or [])]}}],
    }


def stream_chunks(body):
    """A whole answer as the chunks Mistral streams: text split over two chunks,
    tool calls with their index, finish reason and usage in the last chunk."""
    body = json.loads(json.dumps(body))
    choice = body["choices"][0]
    delta = {k: v for k, v in choice["message"].items() if k != "role"}
    for index, call in enumerate(delta.get("tool_calls") or []):
        call["index"] = index
    chunks = []
    if isinstance(delta.get("content"), str) and len(delta["content"]) > 1:
        chunks.append({"id": body["id"], "model": body["model"],
                       "choices": [{"index": 0, "delta": {"content": delta["content"][:1]}, "finish_reason": None}]})
        delta["content"] = delta["content"][1:]
    chunks.append({"id": body["id"], "model": body["model"], "usage": body["usage"],
                   "choices": [{"index": 0, "delta": delta, "finish_reason": choice["finish_reason"]}]})
    return chunks


class Events(httpx.AsyncByteStream):
    """Server-sent events, `pause` seconds before each; `closed` once the client let go."""

    def __init__(self, chunks, pause=0.0):
        self.chunks, self.pause, self.closed = list(chunks), pause, False

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(self.pause)
            yield f"data: {json.dumps(chunk)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed = True


class FakeMistral(httpx.AsyncClient):
    """Mistral's API over (mocked) HTTP, as the provider's client: each request gets
    the next answer, streamed (the default) or whole. `requests` keeps the bodies sent."""

    def __init__(self, answers, pause=0.0):
        self.answers = iter(answers)
        self.requests, self.streams, self.pause = [], [], pause
        super().__init__(base_url=mistral.BASE + "/", headers={"Authorization": "Bearer synthetic"},
                         transport=httpx.MockTransport(self.respond))

    async def respond(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        answer = next(self.answers)
        if body.get("stream") is True:
            self.streams.append(Events(stream_chunks(answer), self.pause))
            return httpx.Response(200, stream=self.streams[-1], headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=answer)


def answer(text):
    return Turn({"role": "assistant", "content": text}, [], text, "stop",
                {"prompt_tokens": 1, "completion_tokens": 1}, "m")


def tool_call(cid, name, **args):
    return Turn({"role": "assistant", "content": None}, [{"id": cid, "name": name, "arguments": args}],
                "", "tool_calls", {}, "m")


class Script:
    """A fake provider: returns scripted turns, optionally after a gate opens."""

    def __init__(self, *turns, gate=None, model="m"):
        self.turns = list(turns)
        self.gate = gate
        self.model = model
        self.started = asyncio.Event()

    async def complete(self, messages):
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


def make_site(root: Path, profiles=None, **overrides) -> Path:
    for name in ("checkout", "context"):
        (root / name).mkdir(exist_ok=True)
    (root / "history").mkdir(mode=0o700, exist_ok=True)
    (root / "context" / "INSTRUCTIONS.md").write_text("Stay inside checkout.")
    for provider in ("mistral", "openai_compat"):  # in $CREDENTIALS_DIRECTORY, as LoadCredential= puts it
        (root / "context" / f"{provider}-api-key").write_text("synthetic")
    (root / "context" / "descriptor.json").write_text(json.dumps({
        "id": "parrot", "name": "Parrot", "description": "test", "capabilities": [], "workspace_scope": "work"}))
    config = {
        "provider": "mistral", "checkout": str(root / "checkout"),
        "client_uids": [UID], "descriptor_file": str(root / "context" / "descriptor.json"),
        "default_timeout_seconds": 5, "max_timeout_seconds": 10, "retention_seconds": 60,
        "profiles": profiles if profiles is not None else {
            "alpha": {"active": True, "model": "model-a", "allowed_tools": ALL_TOOLS,
                      "handbook": str(root / "context" / "INSTRUCTIONS.md")},
            "beta": {"active": True, "model": "model-b", "max_turns": 2, "allowed_tools": ALL_TOOLS},
            "gamma": {"active": False, "model": "model-c"}},
        **overrides}
    path = root / "config.json"
    path.write_text(json.dumps(config))
    return path


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def unit_environment(root: Path) -> dict[str, str]:
    """The directories systemd names to the Pirate's unit, for a test site at root."""
    return {"RUNTIME_DIRECTORY": str(root), "STATE_DIRECTORY": str(root / "history"),
            "CREDENTIALS_DIRECTORY": str(root / "context")}


class TempCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory(dir="/tmp")  # short paths for Unix sockets
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        environment = patch.dict(os.environ, unit_environment(self.root))
        environment.start()
        self.addCleanup(environment.stop)


class ConfigTest(TempCase):
    async def test_check_config_reads_what_serve_reads(self):
        from boring_pirates.common.ipc import check_config
        path = make_site(self.root)
        report = check_config(path)
        self.assertEqual((report["ok"], report["uid"], report["credential"]), (True, UID, "present"))
        self.assertEqual(sorted(report["flavours"]), ["alpha", "beta"])
        self.assertEqual(report["inactive"], ["gamma"])
        self.assertEqual(report["flavours"]["alpha"]["context"][0]["path"], str(self.root / "context" / "INSTRUCTIONS.md"))
        (self.root / "context" / "mistral-api-key").unlink()
        self.assertEqual(check_config(path)["credential"], "absent here (only inside the unit)")
        (self.root / "context" / "INSTRUCTIONS.md").unlink()  # a context file gone: serve would fail too
        with self.assertRaises(OSError):
            check_config(path)
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "boring_pirates.common.ipc", "check-config", "--config", str(path),
            stdout=asyncio.subprocess.PIPE)
        out, _ = await process.communicate()
        self.assertEqual(process.returncode, 1)
        self.assertFalse(json.loads(out)["ok"])

    async def test_flavours_are_active_profiles(self):
        config = settings(make_site(self.root))
        self.assertEqual({n for n, s in config.profiles.items() if s.active}, {"alpha", "beta"})
        self.assertEqual(config.profiles["alpha"].model, "model-a")
        self.assertEqual(config.profiles["alpha"].tools, frozenset(ALL_TOOLS))
        self.assertEqual(config.profiles["gamma"].tools, frozenset())  # omitted: no tools
        self.assertEqual(config.profiles["gamma"].summary()["allowed_tools"], [])

    async def test_refusals(self):
        good = {"active": True, "model": "m"}
        cases = [
            ({"default_profile": "alpha"}, None, "unexpected \\['default_profile'\\]"),
            ({"max_stored_assignments": 5}, None, "unexpected \\['max_stored_assignments'\\]"),
            ({}, {"alpha": {"model": "m"}}, '"active" must be true or false'),
            ({}, {"alpha": {"active": "yes", "model": "m"}}, '"active" must be true or false'),
            ({}, {"alpha": {"active": True, "model": "m", "max_service_calls": 0}}, "max_service_calls must be a positive integer"),
            ({}, {"alpha": {"active": True, "model": "m", "write_reminder_after": "5"}}, "write_reminder_after must be a positive integer"),
            ({}, {"alpha": {"active": True, "model": "m", "digest_file": 5}}, "invalid digest_file"),
            ({}, {"Alpha": good}, "must match"),
            ({}, {"-alpha": good}, "must match"),
            ({}, {"a" * 64: good}, "must match"),
            ({}, {"alpha": {"active": False, "model": "m"}}, "no profile is active"),
            ({}, {"alpha": {**good, "top_p": 1}}, "unknown settings"),
            ({}, {"alpha": {**good, "mistral": {"temperature": 2}}}, "mistral.temperature must be a number from 0 to 1.5"),
            ({}, {"alpha": {**good, "mistral": {"temperature": True}}}, "mistral.temperature must be a number"),
            ({}, {"alpha": {**good, "mistral": {"reasoning_effort": "max"}}}, "mistral.reasoning_effort must be one of"),
            ({}, {"alpha": {**good, "mistral": {"top_k": 1}}}, "the mistral block takes"),
            ({}, {"alpha": {**good, "mistral": {"top_p": 2}}}, "mistral.top_p must be a number from 0 to 1"),
            ({}, {"alpha": {**good, "mistral": {"random_seed": -1}}}, "mistral.random_seed must be an integer"),
            ({}, {"alpha": {**good, "mistral": {"stop": []}}}, "mistral.stop must be a text or a list"),
            ({}, {"alpha": {**good, "mistral": {"service_tier": "fast"}}}, "mistral.service_tier must be one of"),
            ({}, {"alpha": {**good, "mistral": {"safe_prompt": "yes"}}}, "mistral.safe_prompt must be true or false"),
            ({}, {"alpha": {"active": True}}, "model name"),
            # provider settings outside the provider's block, and a flavour of another provider
            ({}, {"alpha": {**good, "temperature": 0.1}}, "unknown settings"),
            ({}, {"alpha": {**good, "max_output_tokens": 10}}, "unknown settings"),
            ({"provider": None}, None, "names its provider at the top"),
            ({"provider": "openai"}, None, "provider must be one of"),
            ({"retention_seconds": 9}, None, "retention_seconds"),
            ({"client_uids": []}, None, "client_uids"),
        ]
        for overrides, profiles, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                settings(make_site(self.root, profiles, **overrides))
        settings(make_site(self.root, {"a" * 63: good, "0-x": good}))
        tuned = settings(make_site(self.root, {"alpha": {**good, "mistral": {"temperature": 0, "reasoning_effort": "low"}}}))
        self.assertEqual(tuned.profiles["alpha"].settings,
                         {"max_tokens": 8192, "temperature": 0.0, "reasoning_effort": "low"})
        summary = tuned.profiles["alpha"].summary()
        self.assertEqual((summary["provider"], summary["mistral"]["temperature"]), ("mistral", 0.0))
        self.assertNotIn("temperature", summary)
        (self.root / "history").chmod(0o750)
        with self.assertRaisesRegex(PermissionError, "private"):
            settings(make_site(self.root))

    async def test_file_roots_never_reach_the_key_history_state_or_credentials(self):
        def site(path):
            return make_site(self.root, {"alpha": {"active": True, "model": "m", "files": {
                "roots": {"work": {"path": str(path), "read": True, "write": False}}}}})
        make_site(self.root)  # the key lives in context/, transcripts in history/ (the state directory)
        for name in ("context/inner", "history/inner", "shared", "history-notes"):
            (self.root / name).mkdir()
        (self.root / "shared" / "to-history").symlink_to(self.root / "history")
        inside = [self.root / "context", self.root / "context" / "inner", self.root / "history",
                  self.root / "history" / "inner",
                  self.root / "shared" / "to-history", Path("/run/credentials/boring-pirate@mo.service"),
                  Path("/proc"), Path("/proc/self"), Path("/sys/kernel"), Path("/dev")]
        for path in inside:
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "file root work .* is inside"):
                settings(site(path))
        for path in (self.root / "checkout", self.root / "shared", self.root / "history-notes"):
            with self.subTest(path=path):
                root = settings(site(path)).profiles["alpha"].files.roots["work"]
                self.assertEqual((root.path, root.deny), (path, ()))
        # A root containing protected directories gets them denied (PF1), not refused.
        relative = self.root.relative_to("/").parts
        for path, denied in ((self.root, {("context",), ("history",)}),
                             (Path("/run"), {("credentials",)}),
                             (Path("/"), {("run", "credentials"), ("proc",), ("sys",), ("dev",),
                                          (*relative, "context"), (*relative, "history")})):
            with self.subTest(path=path):
                root = settings(site(path)).profiles["alpha"].files.roots["work"]
                self.assertEqual(set(root.deny), denied)
        # the default root (work at the workdir) passes the same check
        self.assertEqual(settings(make_site(self.root)).profiles["beta"].files.roots["work"].path,
                         (self.root / "checkout").resolve())

    async def test_the_unit_names_the_socket_state_and_key(self):
        config = settings(make_site(self.root))
        self.assertEqual((config.socket, config.history_dir, config.key_file),
                         (self.root / "pirate.sock", self.root / "history", self.root / "context" / "mistral-api-key"))
        for key in ("socket", "history_dir", "key_file"):  # config.json no longer repeats them
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, f"unexpected \\['{key}'\\]"):
                settings(make_site(self.root, **{key: "/somewhere"}))
        for variable in ("RUNTIME_DIRECTORY", "STATE_DIRECTORY", "CREDENTIALS_DIRECTORY"):
            for value in ("", "relative", "/a:/b"):
                with self.subTest(variable=variable, value=value), \
                        self.assertRaisesRegex(ValueError, f"\\${variable} must name one directory"):
                    settings(make_site(self.root), environment={**unit_environment(self.root), variable: value})
        outside = {k: v for k, v in unit_environment(self.root).items() if k != "CREDENTIALS_DIRECTORY"}
        self.assertIsNone(settings(make_site(self.root), environment=outside, credential_loaded=False).key_file)

    async def test_protected_directories_stay_out_of_reach_through_the_file_tools(self):
        """A root of / reads ordinary files but not the key, the history, nor the
        Pirate process's own memory and environment under /proc/<pid>/."""
        policy = settings(make_site(self.root, {"alpha": {"active": True, "model": "m", "files": {
            "roots": {"fs": {"path": "/", "read": True, "write": False}}}}})).profiles["alpha"]
        files = CheckoutFiles(self.root / "checkout", policy.files, self.root / "checkout")
        self.addCleanup(files.close)

        async def call(name, **args):
            deadline = asyncio.get_running_loop().time() + 5
            return await ToolRuntime().dispatch(files, None, {"id": "t", "name": name,
                                                              "arguments": {"root": "fs", **args}}, deadline)
        relative = str(self.root.relative_to("/"))
        (self.root / "checkout" / "note.txt").write_text("hello")
        self.assertEqual((await call("file_read", path=relative + "/checkout/note.txt"))["content"], "hello")
        pid = os.getpid()
        for name, path in (("file_read", relative + "/context/key"), ("file_list", relative + "/history"),
                           ("file_read_bytes", f"proc/{pid}/mem"), ("file_read", f"proc/{pid}/environ"),
                           ("file_list", "run/credentials"), ("file_list", "sys/kernel"), ("file_read", "dev/null")):
            with self.subTest(path=path), self.assertRaises(Refusal) as refused:
                await call(name, path=path)
            self.assertEqual(refused.exception.code, "forbidden")

    async def test_the_transcript_names_the_flavours_provider(self):
        import dataclasses
        config = settings(make_site(self.root))
        parrot = Pirate(config, {"alpha": Script(answer("done"))})
        parrot.flavours["alpha"] = dataclasses.replace(parrot.flavours["alpha"], provider="otherprovider")
        job_id = secrets.token_hex(16)
        result = await parrot.execute(UID, "alpha", job_id, "Do it", 5)
        await parrot.runtime.close()
        self.assertEqual(result["state"], "completed")
        trail = records(self.root / "history" / (job_id + ".jsonl"))
        self.assertEqual({record["provider"] for record in trail}, {"otherprovider"})
        self.assertEqual(trail[0]["model"], "model-a")


class SocketCase(TempCase):
    async def start(self, providers, **overrides):
        self.config = settings(make_site(self.root, **overrides))
        self.parrot = Pirate(self.config, providers, register_glob=str(self.root / "ponds" / "*" / "register.sock"))
        self.path = self.root / "listen.sock"
        server = await asyncio.start_unix_server(self.parrot.handle, path=self.path)
        await server.__aenter__()
        self.addAsyncCleanup(server.__aexit__, None, None, None)

    async def ask(self, operation, flavour, **arguments):
        return await exchange(self.path, {"operation": operation, "flavour": flavour, **arguments}, None)

    async def run_job(self, flavour, text="Do it", timeout=None, job_id=None):
        job_id = job_id or secrets.token_hex(16)
        return job_id, await self.ask("run", flavour, job_id=job_id, assignment=text, timeout_seconds=timeout)


class ProtocolTest(SocketCase):
    async def test_ping_describe_run(self):
        await self.start({"alpha": Script(answer("Bonjour.")), "beta": Script()})
        self.assertEqual(await self.ask("ping", "alpha"), {"ok": True, "result": {"flavour": "alpha"}})
        described = (await self.ask("describe", "beta"))["result"]
        self.assertEqual((described["flavour"], described["profile"]["model"], described["id"]),
                         ("beta", "model-b", "parrot"))
        self.assertEqual(described["profile"]["max_turns"], 2)
        self.assertEqual(described["profile"]["context"], [])
        handbook = self.root / "context" / "INSTRUCTIONS.md"
        self.assertEqual((await self.ask("describe", "alpha"))["result"]["profile"]["context"], [
            {"path": str(handbook), "sha256": hashlib.sha256(b"Stay inside checkout.").hexdigest()}])
        handbook.write_text("Edited handbook.")  # read at describe time, like at run time
        self.assertEqual((await self.ask("describe", "alpha"))["result"]["profile"]["context"][0]["sha256"],
                         hashlib.sha256(b"Edited handbook.").hexdigest())
        handbook.write_text("Stay inside checkout.")
        job_id, reply = await self.run_job("alpha")
        result = reply["result"]
        self.assertEqual((result["state"], result["reason"], result["answer"]), ("completed", None, "Bonjour."))
        self.assertEqual(result["usage"]["model_calls"], 1)
        trail = records(self.root / "history" / (job_id + ".jsonl"))
        self.assertEqual([r["kind"] for r in trail], ["assignment", "provider_response", "outcome"])
        self.assertTrue(all(r["job_id"] == job_id and r["flavour"] == "alpha" and r["client_uid"] == UID
                            for r in trail))
        self.assertEqual(trail[0]["data"]["profile"]["model"], "model-a")
        self.assertEqual(trail[0]["data"]["profile"]["context"][0]["path"],
                         str(self.root / "context" / "INSTRUCTIONS.md"))
        for operation in ("ping", "describe"):
            for flavour in ("gamma", "nope", 3):
                reply = await self.ask(operation, flavour)
                self.assertEqual(reply["error"]["code"], "not_found", (operation, flavour))
        self.assertEqual((await self.run_job("gamma"))[1]["error"]["code"], "not_found")

    async def test_invalid_requests_are_refusals(self):
        await self.start({"alpha": Script(answer("still here")), "beta": Script()})
        job = secrets.token_hex(16)
        lines = [b"", b"null", b"[]", b'{"operation":"ping","flavour":"alpha","x":NaN}',
                 b'{"operation":"ping","operation":"ping","flavour":"alpha"}',
                 b'{"operation":"submit","flavour":"alpha"}', b'{"operation":"ping"}',
                 b'{"operation":"ping","flavour":"alpha","version":1}',
                 b'{"operation":["ping"],"flavour":"alpha"}']
        runs = [{"job_id": job.upper()}, {"job_id": job[:-1]}, {"job_id": "../" + job[3:]},
                {"assignment": ""}, {"assignment": "x" * 40000}, {"timeout_seconds": 0},
                {"timeout_seconds": 11}, {"timeout_seconds": True}, {"timeout_seconds": "5"}]
        base = {"operation": "run", "flavour": "alpha", "job_id": job, "assignment": "a", "timeout_seconds": None}
        lines += [json.dumps({**base, **change}).encode() for change in runs]
        for line in lines:
            with self.subTest(line=line[:80]):
                reader, writer = await asyncio.open_unix_connection(self.path)
                writer.write(line + b"\n")
                await writer.drain()
                reply = json.loads(await asyncio.wait_for(reader.readline(), 2))
                writer.close()
                self.assertEqual(reply["error"]["code"], "invalid_request")
        self.assertEqual(list((self.root / "history").iterdir()), [])
        self.assertEqual((await self.run_job("alpha"))[1]["result"]["answer"], "still here")

    async def test_peer_uid_must_be_a_client(self):
        await self.start({"alpha": Script(), "beta": Script()}, client_uids=[UID + 1])
        reply = await self.ask("ping", "alpha")
        self.assertEqual(reply["error"]["code"], "forbidden")

    async def test_busy_per_flavour_while_other_flavour_runs(self):
        gate = asyncio.Event()
        alpha = Script(answer("alpha done"), gate=gate)
        await self.start({"alpha": alpha, "beta": Script(answer("beta done"))})
        first = asyncio.create_task(self.run_job("alpha"))
        await asyncio.wait_for(alpha.started.wait(), 2)
        busy_job, busy = await self.run_job("alpha")
        self.assertEqual(busy["error"]["code"], "busy")
        self.assertFalse((self.root / "history" / (busy_job + ".jsonl")).exists())
        self.assertEqual((await self.run_job("beta"))[1]["result"]["answer"], "beta done")
        self.assertFalse(first.done())
        gate.set()
        self.assertEqual((await first)[1]["result"]["answer"], "alpha done")
        self.assertEqual(self.parrot.busy, set())

    async def test_client_disconnect_cancels_run(self):
        gate = asyncio.Event()
        alpha = Script(gate=gate)
        await self.start({"alpha": alpha, "beta": Script()})
        job_id = secrets.token_hex(16)
        reader, writer = await asyncio.open_unix_connection(self.path)
        writer.write(json.dumps({"operation": "run", "flavour": "alpha", "job_id": job_id,
                                 "assignment": "wait", "timeout_seconds": None}).encode() + b"\n")
        await writer.drain()
        await asyncio.wait_for(alpha.started.wait(), 2)
        writer.close()
        await writer.wait_closed()
        for _ in range(200):
            if not self.parrot.busy:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.parrot.busy, set())
        last = records(self.root / "history" / (job_id + ".jsonl"))[-1]
        self.assertEqual((last["kind"], last["data"]["state"], last["data"]["reason"]),
                         ("outcome", "cancelled", "client disconnected"))
        alpha.gate, alpha.turns = None, [answer("again")]
        self.assertEqual((await self.run_job("alpha"))[1]["result"]["answer"], "again")

    async def test_provider_failure_is_a_failure_and_transcribed(self):
        await self.start({"alpha": Script(RuntimeError("synthetic outage")), "beta": Script()})
        with self.assertLogs("boring_pirates", "ERROR"):
            job_id, reply = await self.run_job("alpha")
        self.assertEqual(reply, {"ok": False, "failure": "RuntimeError: synthetic outage"})
        last = records(self.root / "history" / (job_id + ".jsonl"))[-1]["data"]
        self.assertEqual((last["state"], last["reason"]), ("failed", "synthetic outage"))
        self.assertEqual(self.parrot.busy, set())

    async def test_reused_job_id_is_a_failure_not_an_overwrite(self):
        await self.start({"alpha": Script(answer("one"), answer("two")), "beta": Script()})
        job_id, first = await self.run_job("alpha")
        before = (self.root / "history" / (job_id + ".jsonl")).read_bytes()
        with self.assertLogs("boring_pirates", "ERROR"):
            _, second = await self.run_job("alpha", job_id=job_id)
        self.assertIn("FileExistsError", second["failure"])
        self.assertEqual((self.root / "history" / (job_id + ".jsonl")).read_bytes(), before)

    async def test_cli(self):
        await self.start({"alpha": Script(), "beta": Script()})
        # Keep the same explicitly selected dependency sources as this test process.
        paths = [str(Path(__file__).parents[1] / "src"), os.environ.get("PYTHONPATH", "")]
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in paths if p)}
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "boring_pirates.common.ipc", "describe", "--socket", str(self.path),
            "--flavour", "beta", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        out, err = await process.communicate()
        self.assertEqual(process.returncode, 0, err.decode())
        self.assertEqual(json.loads(out)["result"]["flavour"], "beta")


class RegistrationTest(SocketCase):
    async def fake_pond(self, name, reply):
        directory = self.root / "ponds" / name
        directory.mkdir(parents=True)
        received = []

        async def handle(reader, writer):
            received.append(json.loads(await reader.readline()))
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
            writer.close()
        server = await asyncio.start_unix_server(handle, path=directory / "register.sock")
        await server.__aenter__()
        self.addAsyncCleanup(server.__aexit__, None, None, None)
        return directory / "register.sock", received

    async def eventually(self, condition):
        for _ in range(200):
            if condition():
                return
            await asyncio.sleep(0.01)
        self.fail("condition not reached")

    async def test_registers_each_active_flavour_and_pings_refresh(self):
        await self.start({"alpha": Script(), "beta": Script()})
        path, received = await self.fake_pond("pond", {"ok": True, "result": {"ping_interval_seconds": 0.5}})
        self.parrot.scan()
        await self.eventually(lambda: len(received) == 2)
        self.assertEqual(sorted(received, key=lambda m: m["flavour"]), [
            {"operation": "register", "kind": "pirate", "flavour": f, "socket_path": str(self.root / "pirate.sock")}
            for f in ("alpha", "beta")])
        owner, watchdogs, _ = self.parrot.ponds[str(path)]
        self.assertEqual((owner, sorted(watchdogs)), (UID, ["alpha", "beta"]))
        await self.eventually(lambda: all(w.registered for w in watchdogs.values()))
        self.assertEqual(watchdogs["alpha"].interval, 0.5)
        self.assertEqual(watchdogs["alpha"]._sources.keys(), {""})  # one implicit source per register socket
        watchdogs["alpha"]._sources[""] = watchdogs["beta"]._sources[""] = time.monotonic() - 5
        self.assertTrue(watchdogs["alpha"].due())
        await self.ask("ping", "alpha")
        self.assertFalse(watchdogs["alpha"].due())
        self.assertTrue(watchdogs["beta"].due())
        path.unlink()
        self.parrot.scan()
        self.assertEqual(self.parrot.ponds, {})

    async def test_registrar_returns_the_ack_with_only_the_interval(self):
        await self.start({"alpha": Script(), "beta": Script()})
        path, _ = await self.fake_pond("acks", {"ok": True, "result": {
            "ping_interval_seconds": 2, "sources": ["pond", "garden"]}})
        self.assertEqual(await self.parrot.registrar(str(path), "alpha")(), {"ping_interval_seconds": 2.0})

    async def test_owner_uid_must_be_a_client(self):
        await self.start({"alpha": Script(), "beta": Script()}, client_uids=[UID + 1])
        _, received = await self.fake_pond("other", {"ok": True, "result": {"ping_interval_seconds": 3}})
        self.parrot.scan()
        await asyncio.sleep(0.05)
        self.assertEqual((self.parrot.ponds, received), ({}, []))

    async def test_a_pond_added_later_after_client_uids_gains_its_worker_and_a_restart(self):
        """The documented way to offer an installed Pirate a new Pond (after the
        Crocodile's pirate-add): its worker's uid joins client_uids in config.json,
        the Pirate restarts (reads config.json at start) and registers there too."""
        site = make_site(self.root, client_uids=[UID + 1])
        providers = {"alpha": Script(), "beta": Script()}
        glob = str(self.root / "ponds" / "*" / "register.sock")
        before = Pirate(settings(site), providers, register_glob=glob)
        path, received = await self.fake_pond("garden", {"ok": True, "result": {"ping_interval_seconds": 3}})
        before.scan()
        self.assertEqual(before.ponds, {})
        raw = json.loads(site.read_text())
        raw["client_uids"].append(UID)
        site.write_text(json.dumps(raw))
        before.scan()  # a running Pirate keeps the client_uids it started with
        self.assertEqual(before.ponds, {})
        after = Pirate(settings(site), providers, register_glob=glob)  # the restart
        after.scan()
        await self.eventually(lambda: len(received) == 2)
        self.assertEqual(sorted(m["flavour"] for m in received), ["alpha", "beta"])
        for _, _, tasks in after.ponds.values():
            for task in tasks:
                task.cancel()
        self.assertEqual(list(after.ponds), [str(path)])

    async def test_refused_or_unreachable_registration_returns_none(self):
        await self.start({"alpha": Script(), "beta": Script()})
        path, received = await self.fake_pond("refuses", {"ok": False, "error": {
            "code": "forbidden", "message": "not a pirate"}})
        with self.assertLogs("boring_pirates", "WARNING") as logs:
            self.assertIsNone(await self.parrot.registrar(str(path), "alpha")())
        self.assertIn("forbidden", logs.output[0])
        stale = self.root / "ponds" / "stale.sock"
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(stale))
        sock.close()
        with self.assertLogs("boring_pirates", "WARNING") as logs:
            self.assertIsNone(await self.parrot.registrar(str(stale), "alpha")())
        self.assertIn("ConnectionRefusedError", logs.output[0])


class AssignmentTest(TempCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = settings(make_site(self.root))

    async def run_with(self, provider, flavour="alpha", timeout=5.0, **spec_changes):
        import dataclasses
        spec = dataclasses.replace(self.config.profiles[flavour], **spec_changes)
        self.job_id = secrets.token_hex(16)
        history = FileHistory(self.root / "history", self.job_id, flavour, UID, spec.provider, spec.model)
        try:
            return await assignment.run(provider, self.config.checkout, spec.resolve(), history, "Do it", timeout)
        finally:
            history.close()

    def trail(self):
        return records(self.root / "history" / (self.job_id + ".jsonl"))

    def last_outcome(self):
        """The transcript's closing record as (state, reason); it must be the outcome."""
        last = self.trail()[-1]
        self.assertEqual(last["kind"], "outcome")
        return last["data"]["state"], last["data"]["reason"]

    async def test_mistral_edit_journey(self):
        (self.root / "checkout" / "note.txt").write_text("initial")
        initial = hashlib.sha256(b"initial").hexdigest()
        client = FakeMistral([
            mistral_answer("tool_calls", calls=[("a", "file_read", {"root": "work", "path": "note.txt"}),
                                              ("b", "file_write", {"root": "work", "path": "note.txt", "content": "edited",
                                                                   "expected_sha256": initial})]),
            mistral_answer("stop", "Done.")])
        result = await self.run_with(MistralProvider("model-a", client))
        self.assertEqual((result["state"], result["answer"]), ("completed", "Done."))
        self.assertEqual((result["usage"]["model_calls"], result["usage"]["input_tokens"],
                          result["usage"]["tool_calls"], result["usage"]["reported_model"]), (2, 10, 2, "test-model"))
        self.assertEqual(result["write_actions"], [{"root": "work", "path": "note.txt", "bytes": 6, "encoding": "utf-8", "bom": False, "newline": "lf",
                                                    "sha256": hashlib.sha256(b"edited").hexdigest()}])
        self.assertEqual((self.root / "checkout" / "note.txt").read_text(), "edited")
        self.assertTrue(client.requests[1]["messages"][0]["content"].startswith("Stay inside checkout.\n\nConfigured tool policy:"))
        self.assertTrue(client.requests[0]["parallel_tool_calls"])

    async def test_several_calls_in_a_turn_run_in_order_and_stop_at_a_failure(self):
        calls = [{"id": str(n), "name": "file_write",
                  "arguments": {"root": "work", "path": name, "content": name, "expected_sha256": None}}
                 for n, name in ((1, "a.txt"), (2, "stale.txt"), (3, "b.txt"), (4, "c.txt"))]
        calls[1]["arguments"]["expected_sha256"] = "0" * 64  # a refusal: the turn goes on
        seen = []
        real_dispatch = ToolRuntime.dispatch

        async def flaky(runtime, files, spec, call, deadline, on_cancelled_result=None):
            if call["arguments"]["path"] == "b.txt":
                raise OSError("disk gone")  # a failure: may have had effects
            return await real_dispatch(runtime, files, spec, call, deadline, on_cancelled_result)

        class Recording(Script):
            async def complete(self, messages):
                seen.append([m for m in messages if m["role"] == "tool"])
                return await super().complete(messages)
        script = Recording(Turn({"role": "assistant", "content": None}, calls, "", "tool_calls", {}, "m"), answer("ok"))
        with patch.object(ToolRuntime, "dispatch", flaky):
            result = await self.run_with(script)
        answered = seen[1]
        self.assertEqual([m["tool_call_id"] for m in answered], ["1", "2", "3", "4"])  # all answered, in order
        outcomes = [json.loads(m["content"]) for m in answered]
        self.assertEqual([o["ok"] for o in outcomes], [True, False, False, False])
        self.assertNotIn("failure", outcomes[1])
        self.assertEqual(outcomes[2]["failure"]["effects"], "unknown")
        self.assertEqual((outcomes[3]["status"], outcomes[3]["error"]),
                         ("not_started", "not run: call 3 before it in this turn failed"))
        self.assertTrue((self.root / "checkout" / "a.txt").exists())
        self.assertEqual((self.root / "checkout" / "a.txt").read_bytes(), b"a.txt")
        self.assertFalse((self.root / "checkout" / "stale.txt").exists())  # refused: nothing written
        self.assertFalse((self.root / "checkout" / "c.txt").exists())  # never started
        self.assertEqual((result["usage"]["tool_calls"], result["write_actions_count"]), (3, 1))

    async def test_file_write_hash_rule(self):
        (self.root / "checkout" / "exists.txt").write_text("old")
        script = Script(
            tool_call("1", "file_write", root="work", path="exists.txt", content="new", expected_sha256=None),
            tool_call("2", "file_write", root="work", path="absent.txt", content="new", expected_sha256="0" * 64),
            tool_call("3", "file_write", root="work", path="exists.txt", content="new",
                      expected_sha256=hashlib.sha256(b"old").hexdigest()),
            tool_call("4", "file_write", root="work", path="absent.txt", content="created", expected_sha256=None),
            answer("ok"))
        result = await self.run_with(script)
        tool_results = [r["data"] for r in self.trail() if r["kind"] == "tool_result"]
        self.assertEqual([r["ok"] for r in tool_results], [False, False, True, True])
        self.assertIn("stale edit: expected hash differs", tool_results[0]["error"])
        self.assertIn("stale edit: file is absent", tool_results[1]["error"])
        # The model is told the next step, with the current hash it already gets in the refusal.
        self.assertIn("Read it with file_read first", tool_results[0]["hint"])
        self.assertIn(hashlib.sha256(b"old").hexdigest(), tool_results[0]["hint"])
        self.assertIn("expected_sha256 null", tool_results[1]["hint"])
        self.assertNotIn("hint", tool_results[2])
        self.assertEqual(result["write_actions_count"], 2)
        self.assertEqual(result["tool_errors_count"], 2)
        self.assertEqual(result["tool_errors"], [{"tool": "file_write", "error": e["error"]} for e in tool_results[:2]])
        self.assertEqual((self.root / "checkout" / "exists.txt").read_text(), "new")
        self.assertEqual((self.root / "checkout" / "absent.txt").read_text(), "created")

    async def test_tool_argument_errors_say_what_to_fix(self):
        script = Script(
            tool_call("1", "file_write", root="work", path="new.txt", content="x"),
            tool_call("2", "delete_file", path="new.txt"),
            tool_call("3", "file_list", root="work", path=".", recursive=True),
            tool_call("4", "file_read", root="work"),
            tool_call("5", "file_write", root="work", path="new.txt", content="x", expected_sha256=None),
            answer("ok"))
        result = await self.run_with(script)
        results = [r["data"] for r in self.trail() if r["kind"] == "tool_result"]
        # The shared tools name the field that is wrong (missing or not allowed).
        self.assertEqual(results[0]["fields"], ["file_write.arguments.expected_sha256"])
        self.assertEqual(results[1]["error"], "ValueError: tool 'delete_file' is unknown; "
                                              "allowed tools: file_list, file_read, file_write")
        self.assertEqual(results[2]["fields"], ["file_list.arguments.recursive"])
        self.assertEqual(results[3]["fields"], ["file_read.arguments.path"])
        self.assertTrue(results[4]["ok"])
        self.assertEqual(result["write_actions_count"], 1)  # strict: no silent default for expected_sha256
        self.assertEqual((self.root / "checkout" / "new.txt").read_text(), "x")

    async def test_malformed_tool_calls_go_back_to_the_model(self):
        client = FakeMistral([
            mistral_answer("tool_calls", calls=[("a", "file_write", '{"root": "work", "path": "x.txt", "content": '),
                                              ("b", "", {"path": "."}),
                                              ("c", "file_list", "[1]")]),
            mistral_answer("stop", "Retried.")])
        result = await self.run_with(MistralProvider("model-a", client))
        self.assertEqual((result["state"], result["answer"], result["usage"]["tool_calls"]),
                         ("completed", "Retried.", 3))
        replies = [m for m in client.requests[1]["messages"] if m.get("role") == "tool"]
        self.assertEqual([m["tool_call_id"] for m in replies], ["a", "b", "c"])
        errors = [json.loads(m["content"])["error"] for m in replies]
        self.assertTrue(errors[0].startswith("ValueError: tool arguments are not valid JSON: "), errors[0])
        self.assertEqual(errors[1:], ["ValueError: tool call has no function name",
                                      "ValueError: tool arguments must be a JSON object"])
        self.assertFalse((self.root / "checkout" / "x.txt").exists())
        # A response whose finish reason disagrees with its calls is still broken: a failure.
        broken = FakeMistral([mistral_answer("stop", calls=[("d", "file_list", {"root": "work", "path": "."})])])
        result = await self.run_with(MistralProvider("model-a", broken))
        self.assertEqual((result["state"], result["reason"]), ("failed", "provider_invalid_response"))

    async def test_sampling_settings_cache_key_and_structured_content(self):
        thinking = [{"type": "thinking", "thinking": [{"type": "text", "text": "hmm"}]},
                    {"type": "text", "text": "Done"}, {"type": "text", "text": "."}]
        client = FakeMistral([mistral_answer("tool_calls", calls=[("a", "file_list", {"root": "work", "path": "."})]),
                            mistral_answer("stop", thinking)])
        provider = MistralProvider("model-a", client, options={"temperature": 0.05, "reasoning_effort": "low"})
        result = await self.run_with(provider)
        self.assertEqual((result["state"], result["answer"]), ("completed", "Done."))
        first, second = client.requests
        self.assertEqual((first["temperature"], first["reasoning_effort"]), (0.05, "low"))
        self.assertRegex(first["prompt_cache_key"], r"^[0-9a-f]{32}$")
        self.assertEqual(first["prompt_cache_key"], second["prompt_cache_key"])  # one key per run
        plain = FakeMistral([mistral_answer("stop", "ok")])
        await self.run_with(MistralProvider("model-a", plain))
        self.assertNotIn("temperature", plain.requests[0])
        self.assertNotIn("reasoning_effort", plain.requests[0])
        odd = FakeMistral([mistral_answer("stop", [{"type": "image_url", "image_url": "https://x"}])])
        result = await self.run_with(MistralProvider("model-a", odd))
        self.assertEqual((result["state"], result["reason"]), ("failed", "provider_invalid_response"))
        self.assertIn("unsupported completion part 'image_url'", json.dumps(self.trail()))

    async def test_tool_errors_are_capped_in_the_result(self):
        bad = [tool_call(str(i), "file_read", root="work", path="absent-%d.txt" % i) for i in range(34)]
        result = await self.run_with(Script(*bad, tool_call("x", "y" * 300), answer("ok")), max_turns=40)
        self.assertEqual((result["state"], result["tool_errors_count"]), ("completed", 35))
        self.assertEqual(len(result["tool_errors"]), assignment.MAX_TOOL_ERRORS)
        self.assertEqual(result["tool_errors"][0]["tool"], "file_read")
        self.assertIn("file does not exist", result["tool_errors"][0]["error"])
        unknown = [r["data"] for r in self.trail() if r["kind"] == "tool_result"][-1]["error"]
        self.assertGreater(len(unknown), assignment.MAX_TOOL_ERROR_TEXT)  # the model sees it whole
        result = await self.run_with(Script(tool_call("x", "y" * 300), answer("ok")))
        self.assertEqual(result["tool_errors"], [{"tool": "y" * 64, "error": unknown[:assignment.MAX_TOOL_ERROR_TEXT]}])
        self.assertEqual((await self.run_with(Script(answer("ok"))))["tool_errors"], [])

    async def test_an_empty_turn_is_asked_again_a_bounded_number_of_times(self):
        # A server that drops an unparsable tool call returns neither text nor calls.
        seen = []
        class Recorder(Script):
            async def complete(self, messages):
                seen.append(len(messages))
                return await super().complete(messages)
        result = await self.run_with(Recorder(answer(""), answer(" "), answer("Done.")), max_turns=5)
        self.assertEqual((result["state"], result["answer"], result["usage"]["empty_turn_retries"]),
                         ("completed", "Done.", 2))
        self.assertEqual(len(set(seen)), 1)  # the empty turns are not kept in the conversation
        self.assertEqual([r["data"] for r in self.trail() if r["kind"] == "empty_turn"], [{"retry": 1}, {"retry": 2}])
        empties = [answer("") for _ in range(assignment.EMPTY_TURN_RETRIES + 1)]
        result = await self.run_with(Script(*empties), max_turns=10)
        self.assertEqual((result["state"], result["answer"], result["usage"]["empty_turn_retries"]),
                         ("completed", "", assignment.EMPTY_TURN_RETRIES))

    async def test_service_call_budget_and_write_reminder(self):
        calls = [tool_call(str(i), "service_call", service="knowledge", tool="kb_search", arguments={}) for i in range(2)]
        result = await self.run_with(Script(*calls, answer("ok")), max_turns=5,
                                     tools=frozenset(ALL_TOOLS) | {"service_call"}, max_service_calls=1)
        self.assertEqual(result["usage"]["service_calls"], 1)
        self.assertIn("service_call budget of 1", result["tool_errors"][-1]["error"])
        seen = []
        class Recorder(Script):
            async def complete(self, messages):
                seen.append([m.get("content") for m in messages if m.get("role") == "user"])
                return await super().complete(messages)
        lists = [tool_call(str(i), "file_list", root="work", path=".") for i in range(3)]
        result = await self.run_with(Recorder(*lists, answer("ok")), max_turns=6, write_reminder_after=2)
        self.assertEqual(result["usage"]["write_reminders"], 1)
        self.assertEqual([len(users) for users in seen], [1, 1, 2, 2])  # once, after the second call
        self.assertIn("written no file", seen[2][-1])
        self.assertEqual([r["data"] for r in self.trail() if r["kind"] == "write_reminder"], [{"tool_calls": 2}])

    async def test_digest_ends_every_call_inside_the_last_message(self):
        digest = self.root / "digest.md"
        digest.write_text("Conditions go into the where clause.\n")
        seen = []
        class Recorder(Script):
            async def complete(self, messages):
                seen.append([dict(m) for m in messages])
                return await super().complete(messages)
        lists = [tool_call(str(i), "file_list", root="work", path=".") for i in range(2)]
        result = await self.run_with(Recorder(*lists, answer("ok")), max_turns=5, digest_file=digest)
        self.assertEqual(result["usage"]["digest_calls"], 3)
        # First call: at the end of the prompt, so a one-shot answer sees it too.
        self.assertEqual([m["role"] for m in seen[0]], ["system", "user"])
        self.assertTrue(seen[0][1]["content"].startswith("Do it\n\n---\nKey rules"))
        for sent in seen[1:]:  # later: at the end of the latest tool result, never a user turn of its own
            self.assertEqual(sent[-1]["role"], "tool")
            self.assertTrue(sent[-1]["content"].endswith("Conditions go into the where clause."))
            self.assertEqual(sent[1]["content"], "Do it")  # earlier copies are not kept
        self.assertEqual(len(seen[2]), len(seen[1]) + 2)  # +1 turn, +1 tool result
        self.assertEqual(sum("where clause" in json.dumps(m) for m in seen[2]), 1)
        history = json.dumps(self.trail())
        self.assertNotIn("where clause", history)  # the transcript keeps the conversation without it
        # After an image (list content) the digest is a text part of that message, not a turn of its own.
        image = [{"role": "user", "content": [{"type": "image_url", "image_url": "data:"}]}]
        sent = assignment.with_digest(image, "R")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["content"][-1], {"type": "text", "text": assignment.DIGEST.format(digest="R").lstrip()})
        self.assertEqual(len(image[0]["content"]), 1)  # the conversation itself is not changed
        digest.write_bytes(b"x" * 5000)
        with self.assertRaisesRegex(ValueError, "digest file exceeds limit"):
            await self.run_with(Script(answer("ok")), digest_file=digest)

    async def test_the_result_checks_the_answers_files_line(self):
        script = Script(tool_call("1", "file_write", root="work", path="a.txt", content="a", expected_sha256=None),
                        answer("Wrote them.\nFILES: a.txt, b.txt"))
        result = await self.run_with(script)
        self.assertEqual(result["files_check"], {"claimed": ["a.txt", "b.txt"], "claimed_not_written": ["b.txt"],
                                                 "written_not_claimed": []})
        self.assertNotIn("files_check", await self.run_with(Script(answer("ok"))))

    async def test_model_failures_are_outcomes(self):
        loop = [tool_call(str(i), "file_list", root="work", path=".") for i in range(3)]
        result = await self.run_with(Script(*loop), max_turns=2)
        self.assertEqual((result["state"], result["reason"], result["answer"]), ("incomplete", "turn_limit", None))
        self.assertEqual(result["usage"]["model_calls"], 2)
        self.assertEqual(self.trail()[-1]["data"], {"state": "incomplete", "reason": "turn_limit"})
        truncated = MistralProvider("model-a", FakeMistral([mistral_answer("length", "partial")]))
        result = await self.run_with(truncated)
        self.assertEqual((result["state"], result["reason"], result["usage"]["finish_reason"]),
                         ("incomplete", "truncated", "length"))
        self.assertEqual(self.last_outcome(), ("incomplete", "truncated"))
        result = await self.run_with(Script(answer("x" * 40000)))
        self.assertEqual((result["state"], result["reason"]), ("incomplete", "answer_limit"))
        self.assertEqual(self.last_outcome(), ("incomplete", "answer_limit"))
        result = await self.run_with(Script(answer("late"), gate=asyncio.Event()), timeout=0.05)
        self.assertEqual((result["state"], result["reason"]), ("timed_out", "deadline"))
        self.assertEqual(self.last_outcome(), ("timed_out", "deadline"))
        self.assertIsNone(result["usage"]["input_tokens"])  # the unanswered call's usage is unknown

    async def test_disabled_tool_is_a_tool_refusal(self):
        result = await self.run_with(Script(tool_call("1", "file_write", root="work", path="x", content="y",
                                                      expected_sha256=None), answer("ok")),
                                     tools=frozenset({"file_read"}))
        self.assertEqual(result["state"], "completed")
        self.assertEqual([r for r in self.trail() if r["kind"] == "tool_result"][0]["data"]["error"],
                         "ValueError: tool 'file_write' is disabled by profile; allowed tools: file_read")
        self.assertFalse((self.root / "checkout" / "x").exists())

    async def test_cancel_waits_for_a_file_write_in_progress(self):
        entered, release = threading.Event(), threading.Event()
        from boring_worker_kit.files import FileTools
        original = FileTools.call

        async def slow(tools, name, request):  # inside the settled call: cancellation waits for it
            entered.set()
            await asyncio.to_thread(release.wait, 2)
            return await original(tools, name, request)
        script = Script(tool_call("1", "file_write", root="work", path="slow.txt", content="settled", expected_sha256=None))
        with patch.object(FileTools, "call", slow):
            running = asyncio.create_task(self.run_with(script))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            running.cancel("client disconnected")
            await asyncio.sleep(0.02)
            self.assertFalse(running.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await running
        self.assertEqual((self.root / "checkout" / "slow.txt").read_text(), "settled")
        self.assertEqual(self.trail()[-1]["data"]["state"], "cancelled")

    async def test_history_failure_stops_before_the_write(self):
        class Broken(FileHistory):
            def append(self, kind, data):
                if kind == "tool_intent":
                    raise OSError("synthetic log failure")
                super().append(kind, data)
        spec = self.config.profiles["alpha"]
        history = Broken(self.root / "history", secrets.token_hex(16), "alpha", UID, spec.provider, spec.model)
        with self.assertRaisesRegex(OSError, "synthetic"):
            await assignment.run(Script(tool_call("1", "file_write", root="work", path="f.txt", content="bad",
                                                  expected_sha256=None)),
                                 self.config.checkout, spec.resolve(), history, "Do it", 5)
        history.close()
        self.assertFalse((self.root / "checkout" / "f.txt").exists())

    async def test_mistral_http_request(self):
        seen = []

        def respond(request):
            seen.append(request)
            events = "".join(f"data: {json.dumps(c)}\n\n" for c in stream_chunks(mistral_answer("stop", "ok")))
            return httpx.Response(200, content=(events + "data: [DONE]\n\n").encode(),
                                  headers={"content-type": "text/event-stream"})
        async with mistral.client("synthetic", 5, transport=httpx.MockTransport(respond)) as client:
            provider = MistralProvider("test-model", client, options={"max_tokens": 4096},
                                       allowed_tools=frozenset({"file_read"}))
            turn = await provider.complete([{"role": "user", "content": "check"}])
        self.assertEqual(turn.text, "ok")
        request = seen[0]
        self.assertEqual((str(request.url), request.headers["authorization"], request.headers["accept"]),
                         ("https://api.mistral.ai/v1/chat/completions", "Bearer synthetic", "text/event-stream"))
        body = json.loads(request.content)
        self.assertEqual([t["function"]["name"] for t in body["tools"]], ["file_read"])
        self.assertEqual((body["max_tokens"], body["parallel_tool_calls"], body["stream"], body["tool_choice"]),
                         (4096, True, True, "auto"))
        self.assertNotIn("stream_options", body)


class TranscriptTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_naming_and_validation(self):
        job_id = secrets.token_hex(16)
        history = FileHistory(self.root, job_id, "alpha", 1000, "mistral", "m")
        history.append("assignment", {"prompt": "p"})
        history.close()
        record = records(self.root / (job_id + ".jsonl"))[0]
        self.assertEqual({k: record[k] for k in ("version", "sequence", "job_id", "flavour", "client_uid")},
                         {"version": 2, "sequence": 0, "job_id": job_id, "flavour": "alpha", "client_uid": 1000})
        self.assertEqual((self.root / (job_id + ".jsonl")).stat().st_mode & 0o777, 0o600)
        for bad in (job_id.upper(), job_id[:-1], job_id + "0", "../" + job_id[3:], None):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "32 lowercase hex"):
                FileHistory(self.root, bad, "alpha", 1000, "mistral", "m")
        with self.assertRaises(FileExistsError):
            FileHistory(self.root, job_id, "alpha", 1000, "mistral", "m")
        link = secrets.token_hex(16)
        (self.root / (link + ".jsonl")).symlink_to(self.root / "elsewhere")
        with self.assertRaises(FileExistsError):
            FileHistory(self.root, link, "alpha", 1000, "mistral", "m")
        self.assertFalse((self.root / "elsewhere").exists())

    def test_prune_deletes_only_old_transcripts(self):
        old, new = secrets.token_hex(16) + ".jsonl", secrets.token_hex(16) + ".jsonl"
        for name in (old, new, "assignments.sqlite3", "notes.jsonl"):
            (self.root / name).write_text("{}\n")
        link = secrets.token_hex(16) + ".jsonl"
        (self.root / link).symlink_to(self.root / "assignments.sqlite3")
        for name in (old, "assignments.sqlite3", "notes.jsonl"):
            os.utime(self.root / name, (0, 0))
        self.assertEqual(prune(self.root, 60), [old])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         sorted([new, link, "assignments.sqlite3", "notes.jsonl"]))


class FilesTest(unittest.IsolatedAsyncioTestCase):
    """The flavour's file roots through the shared tools, as the model calls them."""

    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "sub").mkdir()
        self.files = CheckoutFiles(self.root, default_files(self.root))
        self.addCleanup(self.files.close)

    async def call(self, name, **args):
        deadline = asyncio.get_running_loop().time() + 5
        return await ToolRuntime().dispatch(self.files, None, {"id": "t", "name": name,
                                                               "arguments": {"root": "work", **args}}, deadline)

    async def test_edit_and_scope(self):
        self.assertEqual([e["name"] for e in (await self.call("file_list", path="."))["entries"]], ["sub"])
        receipt = await self.call("file_write", path="sub/note.txt", content="one", expected_sha256=None)
        self.assertEqual((await self.call("file_read", path="sub/note.txt"))["content"], "one")
        with self.assertRaisesRegex(Refusal, "stale edit"):
            await self.call("file_write", path="sub/note.txt", content="two", expected_sha256=None)
        with self.assertRaisesRegex(Refusal, "stale edit"):
            await self.call("file_write", path="sub/note.txt", content="two", expected_sha256="0" * 64)
        self.assertEqual((self.root / "sub" / "note.txt").read_text(), "one")  # the refusals wrote nothing
        await self.call("file_write", path="sub/note.txt", content="two", expected_sha256=receipt["sha256"])
        self.assertEqual((self.root / "sub" / "note.txt").read_text(), "two")
        (self.root / "sub" / "link").symlink_to("/etc/passwd")
        (self.root / "sub" / "dirlink").symlink_to("/etc")
        for name in ("../escape", ".git/config", "sub/link", "sub/dirlink/passwd"):
            with self.subTest(name=name), self.assertRaises((Refusal, ValueError, OSError)):
                await self.call("file_read", path=name)
        with self.assertRaises((Refusal, ValueError, OSError)):
            await self.call("file_write", path="sub/link", content="bad", expected_sha256=None)
        self.assertTrue((self.root / "sub" / "link").is_symlink())
        self.assertEqual(os.readlink(self.root / "sub" / "link"), "/etc/passwd")

    async def test_errors_are_plain_sentences(self):
        # The model gets a sentence, not "[Errno 40] ...".
        (self.root / "sub" / "link").symlink_to("/etc/passwd")
        (self.root / "sub" / "f").write_text("x")
        cases = (("sub/missing", "file does not exist"), ("sub/link", "not an allowed regular file"),
                 ("sub/f/x", "not a directory"), ("sub", "not an allowed regular file"),
                 ("a" * 256, "too long"))
        for path, text in cases:
            with self.subTest(path=path), self.assertRaisesRegex(Refusal, text):
                await self.call("file_read", path=path)
        (self.root / "sub" / "bin").write_bytes(b"\xff")
        with self.assertRaisesRegex(Refusal, "text"):
            await self.call("file_read", path="sub/bin")
        with self.assertRaisesRegex(Refusal, "does not exist"):
            await self.call("file_list", path="nowhere")

    async def test_hardlinks_and_special_files(self):
        (self.root / "sub" / "a").write_text("data")
        os.link(self.root / "sub" / "a", self.root / "sub" / "b")
        os.mkfifo(self.root / "sub" / "pipe")
        # Same MCP policy: reads may inspect hardlinks; mutations must not affect aliases.
        for name in ("a", "b"):
            self.assertEqual((await self.call("file_read", path="sub/" + name))["content"], "data")
            with self.assertRaises(Refusal):
                await self.call("file_write", path="sub/" + name, content="changed",
                                expected_sha256=hashlib.sha256(b"data").hexdigest())
        a, b = self.root / "sub" / "a", self.root / "sub" / "b"
        self.assertEqual((a.read_bytes(), b.read_bytes()), (b"data", b"data"))  # the refusals changed neither
        self.assertEqual(a.stat().st_ino, b.stat().st_ino)
        self.assertEqual(a.stat().st_nlink, 2)
        with self.assertRaises(Refusal):
            await self.call("file_read", path="sub/pipe")
        (self.root / "sub" / "script").write_text("#!")
        (self.root / "sub" / "script").chmod(0o750)
        await self.call("file_write", path="sub/script", content="changed",
                        expected_sha256=hashlib.sha256(b"#!").hexdigest())
        self.assertEqual((self.root / "sub/script").stat().st_mode & 0o777, 0o750)


if __name__ == "__main__":
    unittest.main()
