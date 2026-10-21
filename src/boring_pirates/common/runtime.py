"""Pirate adapters to maintained file, execution and service owners."""
from __future__ import annotations
from typing import Any
from collections.abc import Callable
from .files import CheckoutFiles
from .profiles import ProfileSpec
from boring_worker_kit.settings import ExecutionSettings

from boring_worker_kit.settlement import settled
from boring_worker_kit.batch_contracts import admit_single, ExecArgs
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
            if name not in fields or set(args) != fields[name]:
                raise Refusal("invalid_request", "invalid service arguments")
            if name == "service_list":
                result = await self.authority.request("list")
                return {"caller": result["caller"], "services": [row for row in result["services"]
                                                               if row["name"] in spec.allowed_services]}
            service = args["service"]
            if not isinstance(service, str) or service not in spec.allowed_services:
                raise Refusal("forbidden", "service is not allowed by this flavour")
            # Identity/URL/token are obtained afresh from SO_PEERCRED authority.
            # Neither assigner credentials nor model-provided endpoints enter here.
            result = await self.authority.request("token", service)
            services = self._services(result["caller"])
            if name == "service_describe":
                return await services.describe(service, result["token"], grant=result["grant"])
            return await services.call(service, args["tool"], args["arguments"], result["token"], grant=result["grant"])
        raise Refusal("invalid_request", "unknown tool")

    async def close(self) -> None:
        if self.services is not None:
            await self.services.close()
