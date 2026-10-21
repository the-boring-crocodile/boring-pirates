"""A Pirate's guest socket (ping, describe, run), its registrations with the Ponds and a thin CLI."""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import logging
import math
import os
import secrets
import signal
import stat
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from boring_registry import Watchdog

from . import assignment, history
from .history import JOB_ID, FileHistory
from .limits import (MAX_ANSWER, MAX_CONTEXT, MAX_CONVERSATION, MAX_FRAME,
                     MAX_PROMPT, MAX_TOOL_RESULT, DEFAULT_PROVIDER_TIMEOUT_SECONDS, positive_seconds)
from .profiles import LAYOUT, ProfileSpec, parse_profiles
from .providers import provider as provider_folder
from .protocol_json import decode_object
from .runtime import ToolRuntime
from boring_worker_kit.settings import EnvironmentSettings
from boring_worker_kit.local_authority import LocalAuthority
from boring_worker_kit.framing import peer_uid

REGISTER_GLOB = "/run/boring-crocodile/guests/*/register.sock"
SCAN_SECONDS = 3.0
PRUNE_SECONDS = 3600.0
CLIENT_TIMEOUT = 5.0
REQUIRED = {"provider", "socket", "checkout", "history_dir", "key_file", "client_uids", "descriptor_file",
            "default_timeout_seconds", "max_timeout_seconds", "retention_seconds", "profiles"}
OPTIONAL = {"provider_request_timeout_seconds", "execution", "service_authority"}
SHAPES = {"ping": {"operation", "flavour"}, "describe": {"operation", "flavour"},
          "run": {"operation", "flavour", "job_id", "assignment", "timeout_seconds"}}
DESCRIPTOR = {"id", "name", "description", "capabilities", "workspace_scope"}
log = logging.getLogger("boring_pirates")


@dataclass(frozen=True)
class Config:
    provider: str
    socket: Path
    checkout: Path
    history_dir: Path
    key_file: Path
    client_uids: frozenset[int]
    descriptor: dict
    default_timeout: float
    max_timeout: float
    retention_seconds: int
    provider_request_timeout: float
    profiles: dict[str, ProfileSpec]
    execution: EnvironmentSettings | None = None
    service_authority: LocalAuthority | None = None


def settings(path: Path, *, credential_loaded: bool = True) -> Config:
    """The Pirate's validated config.json; `serve` starts from it and the release check
    (deploy/steps.py) runs it as the Pirate with the new code, so the two cannot drift.

    credential_loaded=False is that check: it runs outside the unit, where systemd's
    LoadCredential= file does not exist, so the key file need not exist; its directory
    is protected all the same. The check sets STATE_DIRECTORY as the unit would."""
    raw = decode_object(path.read_bytes())
    if "provider" not in raw:
        raise ValueError(LAYOUT)
    missing, unexpected = REQUIRED - set(raw), set(raw) - REQUIRED - OPTIONAL
    if missing or unexpected:
        raise ValueError(f"configuration keys: missing {sorted(missing)}, unexpected {sorted(unexpected)}")
    if any(not isinstance(raw[k], str) or not raw[k] for k in ("socket", "checkout", "history_dir", "key_file", "descriptor_file")):
        raise ValueError("paths must be nonempty strings")
    checkout = Path(raw["checkout"]).resolve(strict=True)
    history_dir, descriptor_file = (Path(raw[k]).resolve(strict=True) for k in ("history_dir", "descriptor_file"))
    key_file = Path(raw["key_file"]).resolve(strict=credential_loaded)
    if any(p == checkout or checkout in p.parents for p in (history_dir, key_file, descriptor_file)):
        raise ValueError("history, credential and descriptor cannot be inside the writable checkout")
    info = os.lstat(raw["history_dir"])
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError("history_dir must be a private directory owned by the Pirate")
    uids = raw["client_uids"]
    if not isinstance(uids, list) or not uids or any(type(uid) is not int or uid < 1 for uid in uids):
        raise ValueError("client_uids must be a nonempty list of positive UIDs")
    default_timeout = positive_seconds(raw["default_timeout_seconds"], "default_timeout_seconds")
    max_timeout = positive_seconds(raw["max_timeout_seconds"], "max_timeout_seconds")
    if default_timeout > max_timeout:
        raise ValueError("default_timeout_seconds exceeds max_timeout_seconds")
    retention = raw["retention_seconds"]
    if type(retention) is not int or retention < max_timeout:
        # A transcript is pruned only after no write for this long: never a running one.
        raise ValueError("retention_seconds must be an integer of at least max_timeout_seconds")
    descriptor = decode_object(descriptor_file.read_bytes())
    if not DESCRIPTOR <= set(descriptor):
        raise ValueError(f"descriptor needs {sorted(DESCRIPTOR)}")
    execution = EnvironmentSettings.from_mapping(raw["execution"]) if "execution" in raw else None
    authority = None
    if "service_authority" in raw:
        value = raw["service_authority"]
        if not isinstance(value, dict) or set(value) != {"socket", "uid"} or not isinstance(value["socket"], str):
            raise ValueError("service_authority needs exactly socket and uid")
        authority = LocalAuthority(Path(value["socket"]), value["uid"])
    # No file root may reach the key, the transcripts or the unit's state directory
    # (systemd's $STATE_DIRECTORY; installed, that is history_dir).
    state = tuple(Path(p) for p in os.environ.get("STATE_DIRECTORY", "").split(":") if p)
    protected = (key_file.parent, history_dir, *state)
    return Config(raw["provider"], Path(raw["socket"]), checkout, history_dir, key_file, frozenset(uids), descriptor,
                  default_timeout, max_timeout, retention,
                  positive_seconds(raw.get("provider_request_timeout_seconds", DEFAULT_PROVIDER_TIMEOUT_SECONDS),
                                   "provider_request_timeout_seconds"),
                  parse_profiles(raw["profiles"], checkout, raw["provider"], protected=protected), execution, authority)


def refusal(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


def encode(message: dict) -> bytes:
    return (json.dumps(message, allow_nan=False, ensure_ascii=True, separators=(",", ":")) + "\n").encode()


async def exchange(path: Path, payload: dict, timeout: float | None = CLIENT_TIMEOUT) -> dict:
    """One request, one response envelope (returned as data, whatever it says)."""
    async def go() -> dict:
        reader, writer = await asyncio.open_unix_connection(path, limit=MAX_FRAME)
        try:
            writer.write(encode(payload))
            await writer.drain()
            return decode_object(await reader.readline())
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
    return await asyncio.wait_for(go(), timeout)


class Pirate:
    def __init__(self, config: Config, providers: dict, register_glob: str = REGISTER_GLOB):
        self.config = config
        self.providers = providers
        self.runtime = ToolRuntime(config.execution, config.service_authority)
        self.flavours = {name: spec for name, spec in config.profiles.items() if spec.active}
        self.model_cards: dict[str, dict | None] = {}  # the provider's card per flavour (inspect)
        self.busy: set[str] = set()
        self.handlers: set[asyncio.Task] = set()
        self.register_glob = register_glob
        # register socket -> (its owner uid, one watchdog per flavour, their tasks)
        self.ponds: dict[str, tuple[int, dict[str, Watchdog], list[asyncio.Task]]] = {}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            await self._handle(reader, writer)
        finally:
            writer.close()
            with suppress(ConnectionError, asyncio.TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 2)
            self.handlers.discard(task)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            response = await self.answer(reader, writer)
        except Exception as error:
            log.exception("request failed")
            response = {"ok": False, "failure": f"{type(error).__name__}: {error}"}
        try:
            if response is not None:
                data = encode(response)
                if len(data) > MAX_FRAME:
                    data = encode({"ok": False, "failure": "response exceeds frame limit"})
                writer.write(data)
                await writer.drain()
        except ConnectionError:
            pass  # the client left; nothing to tell it
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    async def answer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> dict | None:
        uid = peer_uid(writer)
        if uid not in self.config.client_uids:
            return refusal("forbidden", f"uid {uid} is not a client of this Pirate")
        try:
            request = decode_object(await reader.readline())
        except ValueError as error:
            return refusal("invalid_request", f"send one JSON object line: {error}")
        operation, name = request.get("operation"), request.get("flavour")
        if not isinstance(operation, str) or set(request) != SHAPES.get(operation):
            return refusal("invalid_request", "operations are ping, describe and run, each with exactly its fields")
        if not isinstance(name, str) or name not in self.flavours:
            return refusal("not_found", f"no active flavour {name!r}")
        if operation == "ping":
            # Each Pond's worker runs as its own uid, the owner of its register.sock.
            # One watchdog per (register socket, flavour) has one source: that worker.
            for owner, watchdogs, _ in self.ponds.values():
                if owner == uid:
                    watchdogs[name].pinged()
            return {"ok": True, "result": {"flavour": name}}
        if operation == "describe":
            return {"ok": True, "result": self.describe(name)}
        return await self.run(reader, uid, name, request)

    def describe(self, name: str) -> dict:
        c, spec = self.config, self.flavours[name]
        profile = {**spec.summary(), "context": spec.context_identity()}
        if name in self.model_cards:
            profile[spec.provider] = {**spec.settings, "model_card": self.model_cards[name]}
        return {**c.descriptor, "flavour": name, "profile": profile,
                "tool_runtime": {"exec_configured": self.runtime.executor is not None,
                    "exec_limits": self.runtime.executor.limits.public() if self.runtime.executor else None,
                    "services_configured": self.runtime.authority is not None}, "limits": {
            "default_timeout_seconds": c.default_timeout, "max_timeout_seconds": c.max_timeout,
            "provider_request_timeout_seconds": c.provider_request_timeout,
            "max_assignment_bytes": MAX_PROMPT, "max_answer_bytes": MAX_ANSWER,
            "max_context_bytes": MAX_CONTEXT, "max_conversation_bytes": MAX_CONVERSATION,
            "max_file_bytes": spec.files.limits["max_file_bytes"],
            "max_list_entries": spec.files.limits["max_list_entries"], "max_tool_result_bytes": MAX_TOOL_RESULT}}

    async def run(self, reader: asyncio.StreamReader, uid: int, name: str, request: dict) -> dict | None:
        job_id, prompt, timeout = request["job_id"], request["assignment"], request["timeout_seconds"]
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            return refusal("invalid_request", "job_id must be 32 lowercase hex characters")
        if not isinstance(prompt, str) or not prompt or len(prompt.encode()) > MAX_PROMPT:
            return refusal("invalid_request", f"assignment must be 1..{MAX_PROMPT} UTF-8 bytes")
        if timeout is None:
            timeout = self.config.default_timeout
        elif type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= self.config.max_timeout:
            return refusal("invalid_request", f"timeout_seconds must be null or in (0, {self.config.max_timeout:g}]")
        if name in self.busy:
            return refusal("busy", f"flavour {name} is running another assignment")
        self.busy.add(name)
        try:
            work = asyncio.create_task(self.execute(uid, name, job_id, prompt, float(timeout)))
            gone = asyncio.create_task(closed(reader))
            try:
                await asyncio.wait({work, gone}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                gone.cancel()
                if not work.done():
                    work.cancel("client disconnected")
                await asyncio.gather(work, return_exceptions=True)
            if work.cancelled():
                return None
            return {"ok": True, "result": work.result()}
        finally:
            self.busy.discard(name)

    async def execute(self, uid: int, name: str, job_id: str, prompt: str, timeout: float) -> dict:
        profile = self.flavours[name].resolve()
        transcript = FileHistory(self.config.history_dir, job_id, name, uid, profile.spec.provider, profile.spec.model)
        try:
            transcript.append("assignment", {"profile": profile.public(), "prompt": prompt, "timeout_seconds": timeout})
            return await assignment.run(self.providers[name], self.config.checkout, profile, transcript, prompt, timeout, self.runtime)
        finally:
            transcript.close()

    def scan(self) -> None:
        """Register where a Pond's register.sock is owned by a client uid."""
        found = {}
        for path in glob.glob(self.register_glob):
            with suppress(OSError):
                info = os.lstat(path)
                if stat.S_ISSOCK(info.st_mode) and info.st_uid in self.config.client_uids:
                    found[path] = info.st_uid
        for path, (owner, _, tasks) in list(self.ponds.items()):
            if found.get(path) != owner:
                for task in tasks:
                    task.cancel()
                del self.ponds[path]
                log.info("stopped registering at %s", path)
        for path, owner in found.items():
            if path not in self.ponds:
                watchdogs = {name: Watchdog(self.registrar(path, name)) for name in self.flavours}
                self.ponds[path] = (owner, watchdogs, [asyncio.create_task(w.run()) for w in watchdogs.values()])
                log.info("registering %s at %s", ", ".join(watchdogs), path)

    def registrar(self, path: str, name: str):
        last = "unknown"

        async def register() -> dict | None:
            """The ack for the watchdog, or None. The Pirate keeps only the interval: each
            watchdog serves one register socket, so one implicit source."""
            nonlocal last
            ack = None
            try:
                reply = await exchange(Path(path), {"operation": "register", "kind": "pirate", "flavour": name,
                                                    "socket_path": str(self.config.socket)})
                if reply.get("ok") is True:
                    ack = {"ping_interval_seconds": positive_seconds(reply["result"]["ping_interval_seconds"],
                                                                     "ping_interval_seconds")}
                    note = "registered"
                else:
                    note = f"refused: {reply.get('error', reply.get('failure'))}"
            except Exception as error:  # unreachable or malformed; the watchdog tries again
                note = f"failed: {type(error).__name__}: {error}"
            if note != last:  # report changes, not every retry
                log.log(logging.INFO if ack else logging.WARNING, "flavour %s at %s: %s", name, path, note)
                last = note
            return ack
        return register

    async def keep_registered(self) -> None:
        try:
            while True:
                self.scan()
                await asyncio.sleep(SCAN_SECONDS)
        finally:
            tasks = [task for _, _, group in self.ponds.values() for task in group]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def keep_pruned(self) -> None:
        while True:
            try:
                removed = await asyncio.to_thread(history.prune, self.config.history_dir, self.config.retention_seconds)
                if removed:
                    log.info("deleted %d transcripts older than %d s", len(removed), self.config.retention_seconds)
            except OSError as error:
                log.error("transcript pruning failed: %s", error)
            await asyncio.sleep(PRUNE_SECONDS)


async def closed(reader: asyncio.StreamReader) -> None:
    """Return when the client closes (or sends anything after its one request)."""
    with suppress(ConnectionError):
        await reader.read(1)


async def serve(config_path: Path) -> None:
    if os.getuid() == 0:
        raise SystemExit("Pirate service must run as an ordinary user")
    config = settings(config_path)
    key = config.key_file.read_text().strip()
    if not key:
        raise ValueError("empty provider credential")
    folder = provider_folder(config.provider)
    client = folder.client(key, config.provider_request_timeout)
    pirate = Pirate(config, {name: folder.Provider(spec.model, client, config.provider_request_timeout,
                                           spec.tools, spec.settings)
                     for name, spec in config.profiles.items() if spec.active})
    async with client:
        await inspect_flavours(pirate, folder, client)
        await serve_instance(pirate)


async def inspect_flavours(pirate: Pirate, folder, client) -> None:
    """Ask the provider about each flavour's model once, at start: a flavour whose
    model cannot do what it allows is not offered (logged); warnings are logged;
    no answer from the provider is no reason to refuse."""
    for name, spec in list(pirate.flavours.items()):
        problems, warnings, card = await folder.inspect(client, spec.model, spec.tools)
        for warning in warnings:
            log.warning("flavour %s: %s", name, warning)
        if problems:
            log.error("flavour %s is not offered: %s", name, "; ".join(problems))
            del pirate.flavours[name]
            continue
        pirate.model_cards[name] = card
    if not pirate.flavours:
        raise SystemExit("no flavour can be offered (see the errors above)")


async def serve_instance(pirate: Pirate) -> None:
    """Graceful Unix shutdown; provider context stays open until assignments settle."""
    config = pirate.config
    if os.path.lexists(config.socket):
        raise FileExistsError("socket path already exists; inspect before removing it")
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    server = await asyncio.start_unix_server(pirate.handle, path=config.socket, limit=MAX_FRAME)
    os.chmod(config.socket, 0o660)
    for sig in previous:
        loop.add_signal_handler(sig, stopping.set)
    background = [asyncio.create_task(pirate.keep_registered()), asyncio.create_task(pirate.keep_pruned())]
    try:
        await stopping.wait()
    finally:
        # Stop admission before cancelling assignments. Each handler awaits its
        # contained command/file settlement before its terminal transcript record.
        server.close()
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        handlers = list(pirate.handlers)
        for task in handlers:
            task.cancel("Pirate service stopping")
        await asyncio.gather(*handlers, return_exceptions=True)
        # Since Python 3.12 wait_closed() also waits for open connections, so it
        # comes after the handlers have settled and closed theirs.
        await server.wait_closed()
        await pirate.runtime.close()
        config.socket.unlink(missing_ok=True)
        for sig, handler in previous.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="A Boring Pirate: serve its flavours, or ask one as a client.")
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("serve").add_argument("--config", type=Path, required=True)
    for name in SHAPES:
        command = sub.add_parser(name)
        command.add_argument("--socket", type=Path, required=True)
        command.add_argument("--flavour", required=True)
        if name == "run":
            command.add_argument("--prompt-file", type=Path, default=Path("-"))
            command.add_argument("--timeout-seconds", type=float)
    args = parser.parse_args()
    if args.mode == "serve":
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
        asyncio.run(serve(args.config))
        return
    payload: dict = {"operation": args.mode, "flavour": args.flavour}
    timeout: float | None = CLIENT_TIMEOUT
    if args.mode == "run":
        source = sys.stdin.buffer if str(args.prompt_file) == "-" else args.prompt_file.open("rb")
        with source:
            prompt = source.read(MAX_PROMPT + 1).decode("utf-8")
        payload.update(job_id=secrets.token_hex(16), assignment=prompt, timeout_seconds=args.timeout_seconds)
        print("job_id " + payload["job_id"], file=sys.stderr)
        timeout = None  # the run holds the connection until it ends
    try:
        reply = asyncio.run(exchange(args.socket, payload, timeout))
    except (OSError, ValueError, asyncio.TimeoutError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(json.dumps(reply, ensure_ascii=False))
    raise SystemExit(0 if reply.get("ok") is True else 1)


if __name__ == "__main__":
    main()
