"""Pirate adapters to maintained file, execution and service owners."""
from __future__ import annotations
import asyncio
from typing import Any
from collections.abc import Callable
from .files import CheckoutFiles
from .limits import MAX_TOOL_RESULT
from .profiles import ProfileSpec
from boring_worker_kit.settings import ExecutionSettings

from boring_worker_kit.settlement import settled
from boring_worker_kit.batch_contracts import admit_single, ExecArgs
from boring_worker_kit.deadlines import left
from boring_worker_kit.file_refs import call_service, check_save_to, has_references
from boring_worker_kit.execution import EnvironmentExecutor
from boring_worker_kit.host_limits import read_host_limits
from boring_worker_kit.local_authority import LocalAuthority
from boring_worker_kit.outcomes import Refusal
from boring_worker_kit.services import Services
from boring_worker_kit.tools import FILE_MODELS

class ToolRuntime:
    def __init__(self, execution: ExecutionSettings | None = None, authority: LocalAuthority | None = None,
                 *, executor: EnvironmentExecutor | None = None) -> None:
        if execution is not None and execution.task_socket_path is None:
            raise ValueError("Pirate exec requires per-command systemd containment")
        self.executor = executor or (EnvironmentExecutor(execution, read_host_limits(execution)) if execution else None)
        self.authority: LocalAuthority | None = authority
        # Made with the first token answer: the authority names the caller (pirate/NAME,
        # fixed by this Pirate's uid), which services see as the client's name.
        self.services: Services | None = None

    def _services(self, caller: object) -> Services:
        if self.services is None:
            if not isinstance(caller, str) or not caller:
                raise RuntimeError("the service authority named no caller")
            self.services = Services(caller)
        elif self.services.pond != caller:
            raise RuntimeError(f"the service authority named caller {caller!r}, before {self.services.pond!r}")
        return self.services

    async def dispatch(self, files: CheckoutFiles, spec: ProfileSpec, call: dict[str, Any], deadline: float,
                       on_cancelled_result: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        name, args = call["name"], call["arguments"]
        if name in FILE_MODELS:
            admitted = admit_single(name, args).arguments.model_dump(exclude_unset=True)
            return await settled(files.tools.call(name, {"operation": name, "request_id": call["id"], **admitted}),
                                 on_cancelled_result)
        if name == "exec":
            step = admit_single(name, args)
            if self.executor is None:
                raise Refusal("forbidden", "exec is not configured for this Pirate instance")
            arguments = step.arguments
            assert isinstance(arguments, ExecArgs)
            return await self.executor.execute(arguments.command, arguments.cwd or str(spec.workdir), arguments.timeout_seconds,
                                               foreground_deadline=deadline)
        if name.startswith("service_"):
            if self.authority is None:
                raise Refusal("forbidden", "service authority is not configured for this Pirate")
            fields = {"service_list": set(), "service_describe": {"service"},
                      "service_call": {"service", "tool", "arguments"}}
            optional = {"service_call": {"save_to"}}.get(name, set())
            if name not in fields:
                raise Refusal("invalid_request", "unknown service tool")
            if not fields[name] <= set(args) <= fields[name] | optional:
                # Names only, never values: what the tool takes, what was missing, what was extra.
                takes = ", ".join(sorted(fields[name])) or "no arguments"
                missing, extra = sorted(fields[name] - set(args)), sorted(set(args) - fields[name] - optional)
                said = "; ".join(part for part in (
                    "missing " + ", ".join(missing) if missing else "",
                    "not arguments of " + name + ": " + ", ".join(extra) if extra else "") if part)
                nest = (" The service tool's own arguments go inside `arguments`."
                        if name == "service_call" and extra else "")
                raise Refusal("invalid_request", f"invalid service arguments: {name} takes {takes} ({said}).{nest}")
            if name == "service_list":
                result = await self.authority.request("list")
                return {"caller": result["caller"], "services": [row for row in result["services"]
                                                               if row["name"] in spec.allowed_services]}
            service = args["service"]
            if not isinstance(service, str) or service not in spec.allowed_services:
                raise Refusal("forbidden", "service is not allowed by this flavour")
            check_save_to(args.get("save_to"))  # a malformed save_to costs no token
            # save_to writes and $file reads: the flavour's tools allow them, not only its file policy.
            if args.get("save_to") is not None and "file_write" not in spec.tools:
                raise Refusal("forbidden", "save_to writes a file, and this flavour has no file_write")
            if has_references(args.get("arguments")) and not {"file_read", "file_read_bytes"} & spec.tools:
                raise Refusal("forbidden", "a $file reference reads a file, and this flavour has neither "
                              "file_read nor file_read_bytes")
            # Identity/URL/token are obtained afresh from SO_PEERCRED authority, right before
            # the request is sent (after file references and the save target are checked), so a
            # slow read cannot outlast the token. Neither assigner credentials nor
            # model-provided endpoints enter here.
            async def token_for() -> tuple[Services, str, dict[str, Any]]:
                assert self.authority is not None
                result = await self.authority.request("token", service)
                return self._services(result["caller"]), result["token"], result["grant"]
            if name == "service_describe":
                services, token, grant = await token_for()
                return await services.describe(service, token, grant=grant, timeout=left(deadline))

            async def send(arguments: object, timeout: float) -> dict[str, Any]:
                started = asyncio.get_running_loop().time()
                services, token, grant = await token_for()
                spent = asyncio.get_running_loop().time() - started  # the token's time is the call's
                return await services.call(service, args["tool"], arguments, token, grant=grant,
                                           timeout=timeout - spent)
            # The worker kit's file references and save_to, through this flavour's own file roots,
            # within the assignment's deadline (the same event-loop clock). The save is settled like
            # the file_write tool (the assignment owns its lifecycle): a save that lands as the
            # assignment stops is waited for and reported through on_cancelled_result, so it counts
            # as a write before the Pirate answers. Inline answers stay within the tool result limit.
            return await call_service(files.tools, args["arguments"], args.get("save_to"), send, deadline,
                                      max_inline_answer=MAX_TOOL_RESULT - 4096, settle_save=True,
                                      on_late_save=on_cancelled_result)
        raise Refusal("invalid_request", "unknown tool")

    async def close(self) -> None:
        if self.services is not None:
            await self.services.close()
