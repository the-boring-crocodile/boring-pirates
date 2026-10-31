"""A Pirate's guest socket (ping, describe, run), its registrations with the Ponds and a thin CLI."""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import glob
import hashlib
import json
import logging
import math
import os
import secrets
import signal
import stat
import sys
from contextlib import suppress
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from boring_registry import Watchdog

from . import assignment, history
from .history import JOB_ID, FileHistory
from .limits import (MAX_ANSWER, MAX_CONTEXT, MAX_CONVERSATION, MAX_FRAME,
                     MAX_PROMPT, MAX_TOOL_RESULT, DEFAULT_PROVIDER_IDLE_SECONDS, DEFAULT_PROVIDER_TIMEOUT_SECONDS,
                     positive_seconds)
from .overrides import Caps, apply as apply_overrides
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
REQUIRED = {"provider", "checkout", "client_uids", "descriptor_file",
            "default_timeout_seconds", "max_timeout_seconds", "retention_seconds", "profiles"}
SOCKET_NAME = "pirate.sock"  # in the unit's RuntimeDirectory= (deploy/steps.py pings it there)
OPTIONAL = {"provider_request_timeout_seconds", "provider_idle_timeout_seconds", "execution", "service_authority",
            "provider_endpoint", "caps"}
SHAPES = {"ping": {"operation", "flavour"}, "describe": {"operation", "flavour"},
          "run": {"operation", "flavour", "job_id", "assignment", "timeout_seconds"}}
OPTIONAL_FIELDS = {"run": {"overrides"}}  # PF1; an older Pirate refuses the field (exact shapes)
DESCRIPTOR = {"id", "name", "description", "capabilities", "workspace_scope"}
log = logging.getLogger("boring_pirates")


@dataclass(frozen=True)
class Config:
    provider: str
    socket: Path
    checkout: Path
    history_dir: Path
    key_file: Path | None  # None only for check-config outside the unit
    client_uids: frozenset[int]
    descriptor: dict
    default_timeout: float
    max_timeout: float
    retention_seconds: int
    provider_request_timeout: float
    profiles: dict[str, ProfileSpec]
    execution: EnvironmentSettings | None = None
    service_authority: LocalAuthority | None = None
    provider_endpoint: str | None = None
    provider_idle_timeout: float = DEFAULT_PROVIDER_IDLE_SECONDS
    caps: Caps = Caps()


def unit_directory(environment: Mapping[str, str], variable: str) -> Path:
    """One of the directories systemd makes for the Pirate's unit (RuntimeDirectory=,
    StateDirectory=, LoadCredential=) and names in the environment it starts with."""
    value = environment.get(variable, "")
    if not value or ":" in value or not os.path.isabs(value):
        raise ValueError(f"${variable} must name one directory: the Pirate runs in its unit "
                         "(boring-pirate@NAME.service), which sets it")
    return Path(value)


def settings(path: Path, *, environment: Mapping[str, str] | None = None, credential_loaded: bool = True) -> Config:
    """The Pirate's validated config.json; `serve` starts from it and the release check
    (deploy/steps.py) runs it as the Pirate with the new code, so the two cannot drift.

    The unit owns where the socket, the transcripts and the key are: systemd names
    its directories in the environment (RUNTIME_DIRECTORY, STATE_DIRECTORY,
    CREDENTIALS_DIRECTORY), so config.json does not repeat them.
    credential_loaded=False is check-config run without the unit's LoadCredential=:
    the key is then absent (key_file None)."""
    environment = os.environ if environment is None else environment
    raw = decode_object(path.read_bytes())
    if "provider" not in raw or raw["provider"] is None:
        raise ValueError(LAYOUT)
    missing, unexpected = REQUIRED - set(raw), set(raw) - REQUIRED - OPTIONAL
    if missing or unexpected:
        raise ValueError(f"configuration keys: missing {sorted(missing)}, unexpected {sorted(unexpected)}")
    endpoint = provider_folder(raw["provider"]).validate_endpoint(raw.get("provider_endpoint"))
    if any(not isinstance(raw[k], str) or not raw[k] for k in ("checkout", "descriptor_file")):
        raise ValueError("paths must be nonempty strings")
    socket = unit_directory(environment, "RUNTIME_DIRECTORY") / SOCKET_NAME
    history_dir = unit_directory(environment, "STATE_DIRECTORY")
    key_file = None
    if credential_loaded or environment.get("CREDENTIALS_DIRECTORY"):
        key_file = unit_directory(environment, "CREDENTIALS_DIRECTORY") / f"{raw['provider']}-api-key"
    checkout = Path(raw["checkout"]).resolve(strict=True)
    history_dir, descriptor_file = history_dir.resolve(strict=True), Path(raw["descriptor_file"]).resolve(strict=True)
    if any(p == checkout or checkout in p.parents for p in (history_dir, descriptor_file)):
        raise ValueError("history and descriptor cannot be inside the writable checkout")
    info = os.lstat(history_dir)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError("the state directory must be a private directory owned by the Pirate")
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
    # No file root may reach the transcripts (the unit's state directory) or the key.
    protected = (history_dir, *((key_file.parent,) if key_file is not None else ()))
    request_timeout = positive_seconds(raw.get("provider_request_timeout_seconds", DEFAULT_PROVIDER_TIMEOUT_SECONDS),
                                       "provider_request_timeout_seconds")
    idle_timeout = positive_seconds(raw.get("provider_idle_timeout_seconds",
                                            min(DEFAULT_PROVIDER_IDLE_SECONDS, request_timeout)),
                                    "provider_idle_timeout_seconds")
    if idle_timeout > request_timeout:
        raise ValueError("provider_idle_timeout_seconds exceeds provider_request_timeout_seconds")
    return Config(raw["provider"], socket, checkout, history_dir, key_file, frozenset(uids), descriptor,
                  default_timeout, max_timeout, retention, request_timeout,
                  parse_profiles(raw["profiles"], checkout, raw["provider"], protected=protected), execution, authority,
                  endpoint, idle_timeout, Caps.parse(raw.get("caps")))


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
    def __init__(self, config: Config, providers: dict, register_glob: str = REGISTER_GLOB,
                 make_provider=None):
        self.config = config
        self.providers = providers
        # spec -> provider, for a run whose overrides change the tools or the settings
        # (serve: the flavour's provider class on the Pirate's one client). None: the
        # flavour's own provider is used (tests with scripted providers).
        self.make_provider = make_provider
        self.runtime = ToolRuntime(config.execution, config.service_authority)
        self.flavours = {name: spec for name, spec in config.profiles.items() if spec.active}
        self.model_cards: dict[str, dict | None] = {}  # the provider's card per flavour (inspect)
        self.busy: set[str] = set()
        self.handlers: set[asyncio.Task] = set()
        self.register_glob = register_glob
        self.stopping = False  # set by serve_instance before it cancels the running assignments
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
        if (not isinstance(operation, str) or operation not in SHAPES
                or not SHAPES[operation] <= set(request) <= SHAPES[operation] | OPTIONAL_FIELDS.get(operation, set())):
            return refusal("invalid_request", "operations are ping, describe and run, each with exactly its fields "
                           "(run: and optionally overrides)")
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
                "allow_overrides": spec.allow_overrides, "caps": c.caps.public(),
                "tool_runtime": {"exec_configured": self.runtime.executor is not None,
                    "exec_limits": self.runtime.executor.limits.public() if self.runtime.executor else None,
                    "services_configured": self.runtime.authority is not None}, "limits": {
            "default_timeout_seconds": c.default_timeout, "max_timeout_seconds": c.max_timeout,
            "provider_request_timeout_seconds": c.provider_request_timeout,
            "provider_idle_timeout_seconds": c.provider_idle_timeout,
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
        overrides = request.get("overrides")
        spec, system = self.flavours[name], None
        if "overrides" in request:
            # Refused, never ignored: a caller must not believe a run used its prompt when it did not.
            if not spec.allow_overrides:
                return refusal("forbidden", f"flavour {name} does not accept overrides")
            try:
                spec, system = apply_overrides(spec, overrides, self.config.caps,
                                               provider_folder(spec.provider).settings)
            except ValueError as error:
                return refusal("invalid_request", str(error))
        if name in self.busy:
            return refusal("busy", f"flavour {name} is running another assignment")
        self.busy.add(name)
        cancel_reason: str | None = None
        try:
            work = asyncio.create_task(self.execute(uid, name, job_id, prompt, float(timeout),
                                                    spec, system, overrides,
                                                    cancellation_reason=lambda: cancel_reason))
            gone = asyncio.create_task(closed(reader))
            try:
                await asyncio.wait({work, gone}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                gone.cancel()
                if not work.done():
                    # The transcript's reason: the service stopping cancels this handler too.
                    # Keep the initiating cause outside the executor: its cleanup
                    # may re-raise cancellation without the original message.
                    cancel_reason = "Pirate service stopping" if self.stopping else "client disconnected"
                    work.cancel(cancel_reason)
                await asyncio.gather(work, return_exceptions=True)
            if work.cancelled():
                return None
            return {"ok": True, "result": work.result()}
        finally:
            self.busy.discard(name)

    async def execute(self, uid: int, name: str, job_id: str, prompt: str, timeout: float,
                      spec: ProfileSpec | None = None, system: str | None = None,
                      overrides: dict | None = None, *,
                      cancellation_reason: Callable[[], str | None] | None = None) -> dict:
        """`spec`/`system`: the flavour after `overrides` (apply_overrides), else the flavour."""
        own = self.flavours[name]
        spec = spec or own
        profile = spec.resolve(system)
        provider = self.providers[name]
        if self.make_provider is not None and (spec.tools != own.tools or spec.options != own.options):
            provider = self.make_provider(spec)
        system_sha256 = hashlib.sha256(profile.system_message().encode()).hexdigest()
        transcript = FileHistory(self.config.history_dir, job_id, name, uid, profile.spec.provider, profile.spec.model)
        try:
            record = {"profile": profile.public(), "prompt": prompt, "timeout_seconds": timeout,
                      "system_sha256": system_sha256}
            if overrides is not None:
                record["overrides"] = overrides  # in full, the system text included: the run's evidence
            transcript.append("assignment", record)
            result = await assignment.run(provider, self.config.checkout, profile, transcript, prompt, timeout, self.runtime,
                                          cancellation_reason=cancellation_reason)
            return {**result, "overrides_used": overrides is not None, "system_sha256": system_sha256}
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


PR_SET_DUMPABLE = 4


def not_dumpable() -> None:
    """Keep the Pirate's own UID out of this process: its commands (exec) run as that
    UID in other units, and /proc/<this pid>/ (root, mem, environ, fd) would let them
    reach the key and the transcripts past their own sandbox. A process that is not
    dumpable has those owned by root (the kernel checks this before Yama)."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_DUMPABLE, 0) failed")


async def serve(config_path: Path) -> None:
    if os.getuid() == 0:
        raise SystemExit("Pirate service must run as an ordinary user")
    not_dumpable()  # before the key is read
    config = settings(config_path)
    if config.key_file is None:  # not reached: with the credential required, settings names the key
        raise ValueError("no provider credential")
    key = config.key_file.read_text().strip()
    if not key:
        raise ValueError("empty provider credential")
    folder = provider_folder(config.provider)
    client = folder.client(key, config.provider_request_timeout, endpoint=config.provider_endpoint)
    def make_provider(spec: ProfileSpec):
        return folder.Provider(spec.model, client, config.provider_request_timeout,
                               spec.tools, spec.settings, idle_timeout=config.provider_idle_timeout)
    pirate = Pirate(config, {name: make_provider(spec) for name, spec in config.profiles.items() if spec.active},
                    make_provider=make_provider)
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
        pirate.stopping = True
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


def check_config(path: Path) -> dict:
    """What `serve` reads before it listens, checked as the account that runs this:
    the configuration, every active flavour's context and digest files, and the
    provider credential when it is there (inside the unit, LoadCredential= puts it
    there; outside, it is absent and reported so). Raises on the first problem."""
    not_dumpable()  # the release check runs as the Pirate with its key, like serve
    config = settings(path, credential_loaded=False)
    key = config.key_file if config.key_file is not None and config.key_file.exists() else None
    if key is not None and not key.read_text().strip():
        raise ValueError("empty provider credential")
    flavours = {}
    for name, spec in config.profiles.items():
        if spec.active:
            profile = spec.resolve()  # reads context and digest files now, as a run would
            flavours[name] = {"model": spec.model, "revision": profile.revision,
                              "context": list(profile.context_sources), "allowed_tools": sorted(spec.tools)}
    return {"ok": True, "uid": os.getuid(), "provider": config.provider, "credential": "present" if key is not None
            else "absent here (only inside the unit)", "provider_request_timeout_seconds": config.provider_request_timeout,
            "provider_idle_timeout_seconds": config.provider_idle_timeout, "flavours": flavours,
            "inactive": sorted(n for n, s in config.profiles.items() if not s.active)}


def main() -> None:
    parser = argparse.ArgumentParser(description="A Boring Pirate: serve its flavours, or ask one as a client.")
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("serve").add_argument("--config", type=Path, required=True)
    sub.add_parser("check-config", help="check a configuration as the account that runs this; "
                   "run it as the Pirate (see docs/CONFIGURATION.md)").add_argument("--config", type=Path, required=True)
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
    if args.mode == "check-config":
        try:
            report = check_config(args.config)
        except (OSError, ValueError) as error:
            print(json.dumps({"ok": False, "uid": os.getuid(), "error": f"{type(error).__name__}: {error}"}))
            raise SystemExit(1) from error
        print(json.dumps(report, indent=2))
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
