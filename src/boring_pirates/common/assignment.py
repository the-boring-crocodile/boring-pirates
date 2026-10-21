"""One bounded assignment: the model loop, its file tools and its transcript."""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from pathlib import Path

from .files import CheckoutFiles
from .history import FileHistory
from .images import attach, read_image
from .limits import MAX_ANSWER, MAX_CONVERSATION, MAX_TOOL_RESULT
from .profiles import ResolvedProfile, TOOLS
from .providers import ProviderFailure
from .runtime import ToolRuntime, settled
from boring_worker_kit.outcomes import Refusal
from boring_worker_kit.services import ServiceFailure
from boring_worker_kit.task_client import ContainmentError
from boring_worker_kit.execution import ForegroundInterrupted

MAX_WRITE_ACTIONS = 32
MAX_TOOL_ERRORS = 32
MAX_TOOL_ERROR_TEXT = 256  # characters; the tool name is cut to 64


async def settled_thread(function, *args, on_cancelled_result=None):
    return await settled(asyncio.to_thread(function, *args), on_cancelled_result)


async def run(provider, checkout: Path, profile: ResolvedProfile, history: FileHistory,
              prompt: str, timeout: float, runtime: ToolRuntime | None = None) -> dict:
    """Return the outcome as data; raise only for failures (and cancellation)."""
    started = time.monotonic()
    usage: dict = {"provider": profile.spec.provider, "requested_model": profile.spec.model,
                   "reported_model": None, "model_calls": 0, "input_tokens": 0, "output_tokens": 0,
                   "cached_input_tokens": 0, "tool_calls": 0, "finish_reason": None}
    models: set[str] = set()
    writes: list[dict] = []
    errors: list[dict] = []
    pending = False

    def add(key: str, count) -> None:
        known = type(count) is int and count >= 0 and usage[key] is not None
        usage[key] = usage[key] + count if known else None

    def finish(state: str, reason: str | None = None, answer: str | None = None) -> dict:
        if pending:  # a provider call was unanswered: its tokens are unknown
            usage.update(input_tokens=None, output_tokens=None, cached_input_tokens=None)
        usage["reported_model"] = next(iter(models)) if len(models) == 1 else None
        history.append("outcome", {"state": state, "reason": reason})
        return {"state": state, "reason": reason, "answer": answer, "usage": usage,
                "write_actions": writes[:MAX_WRITE_ACTIONS], "write_actions_count": len(writes),
                "tool_errors": errors[:MAX_TOOL_ERRORS], "tool_errors_count": len(errors),
                "elapsed_seconds": round(time.monotonic() - started, 3)}

    own_runtime = runtime is None
    runtime = runtime or ToolRuntime()
    files = CheckoutFiles(checkout, profile.spec.files, profile.spec.workdir)
    deadline = asyncio.get_running_loop().time() + timeout
    messages = [{"role": "system", "content": profile.context + "\n\nConfigured tool policy: " + json.dumps(profile.spec.summary(), ensure_ascii=True)}, {"role": "user", "content": prompt}]
    seen: set[str] = set()

    async def steps() -> dict:
        nonlocal pending
        for _ in range(profile.spec.max_turns):
            if len(json.dumps(messages, ensure_ascii=False).encode()) > MAX_CONVERSATION:
                return finish("incomplete", "conversation_limit")
            pending = True
            try:
                turn = await provider.complete(messages)
            except ProviderFailure as failure:  # tokens of the unanswered call stay unknown
                history.append("provider_failure", {"reason": failure.reason, "detail": failure.detail[:2000]})
                return finish("failed", "provider_" + failure.reason)
            pending = False
            usage["model_calls"] += 1
            usage["finish_reason"] = turn.finish_reason
            if turn.model is not None:
                models.add(turn.model)
            add("input_tokens", turn.usage.get("prompt_tokens"))
            add("output_tokens", turn.usage.get("completion_tokens"))
            details = turn.usage.get("prompt_tokens_details")
            add("cached_input_tokens", details.get("cached_tokens") if isinstance(details, dict) else None)
            history.append("provider_response", {"message": turn.message, "actual_model": turn.model,
                                                 "finish_reason": turn.finish_reason, "usage": turn.usage})
            messages.append(turn.message)
            if turn.finish_reason in ("length", "model_length"):
                return finish("incomplete", "truncated")
            if turn.finish_reason not in ("stop", "tool_calls"):
                return finish("incomplete", "finish_reason_" + turn.finish_reason)
            if not turn.calls:
                if len(turn.text.encode()) > MAX_ANSWER:
                    return finish("incomplete", "answer_limit")
                return finish("completed", answer=turn.text)
            # A turn's calls run in order. One that failed (it may have had effects) stops the
            # rest; a refusal does not (nothing ran, and writes are hash-checked anyway).
            failed_before: str | None = None
            image: tuple[str, str] | None = None  # (path, data URL) to show after this turn's results
            for call in turn.calls:
                if call["id"] in seen:
                    raise ValueError("duplicate tool-call ID")
                seen.add(call["id"])
                if failed_before is not None:
                    # Every call needs an answer; this one was never started.
                    outcome = {"ok": False, "status": "not_started",
                               "error": f"not run: call {failed_before} before it in this turn failed"}
                    history.append("tool_result", {"id": call["id"], **outcome})
                    messages.append({"role": "tool", "tool_call_id": call["id"],
                                     "content": json.dumps(outcome, ensure_ascii=True)})
                    continue
                usage["tool_calls"] += 1
                history.append("tool_intent", call)
                try:
                    if "error" in call:  # malformed by the model; the provider says what
                        raise ValueError(call["error"])
                    if call["name"] not in profile.spec.tools:
                        allowed = ", ".join(sorted(profile.spec.tools)) or "none"
                        known = "is disabled by profile" if call["name"] in TOOLS else "is unknown"
                        raise ValueError(f"tool {call['name']!r} {known}; allowed tools: {allowed}")
                    # Bound now: a cancelled call may settle after the loop moved on.
                    def record_write(value, call=call):
                        if call["name"] in {"file_write", "file_patch", "file_write_bytes"}:
                            writes.append({"root": call["arguments"].get("root", "work"),
                                           "path": call["arguments"]["path"], **value})
                    def cancelled_result(value, call=call, record_write=record_write):
                        record_write(value)
                        history.append("tool_result", {"id": call["id"], "ok": True, "value": value,
                                                       "settled_after_cancellation": True})
                    if call["name"] == "view_image":
                        args = call["arguments"]
                        if set(args) != {"root", "path"} or not all(isinstance(args[k], str) for k in args):
                            raise ValueError("view_image arguments: exactly root and path, both strings")
                        value, url = await read_image(files, call["id"], args["root"], args["path"])
                        image = (args["path"], url)
                    else:
                        value = await runtime.dispatch(files, profile.spec, call, deadline, cancelled_result)
                    record_write(value)
                    if len(json.dumps(value, ensure_ascii=True).encode()) > MAX_TOOL_RESULT:
                        raise ServiceFailure({"status": "failed", "code": "limit", "message": "tool result exceeds limit",
                                              "effects": "unknown" if call["name"] == "service_call" else "completed"})
                    outcome = {"ok": True, "value": value}

                except Refusal as error:
                    outcome = {"ok": False, "error": str(error), "status": "refused", **error.as_data()}
                    errors.append({"tool": call["name"][:64], "error": str(error)[:MAX_TOOL_ERROR_TEXT]})
                except ContainmentError:
                    raise  # admission is poisoned by the shared executor; fatal cleanup failure
                except (ServiceFailure, ForegroundInterrupted, RuntimeError, OSError) as error:
                    data = error.data if isinstance(error, ServiceFailure) else {
                        "status": "failed", "effects": "unknown", "message": str(error)}
                    outcome = {"ok": False, "error": str(error), "failure": data}
                    errors.append({"tool": call["name"][:64], "error": str(error)[:MAX_TOOL_ERROR_TEXT], "status": "failed"})
                except (ValueError, UnicodeError, TypeError) as error:
                    # The model sees the refusal (stale edit, bad path) and can act on it.
                    outcome = {"ok": False, "error": f"{type(error).__name__}: {error}"}
                    name = call["name"][:64] if isinstance(call["name"], str) else None
                    errors.append({"tool": name, "error": outcome["error"][:MAX_TOOL_ERROR_TEXT]})
                history.append("tool_result", {"id": call["id"], **outcome})
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": json.dumps(outcome, ensure_ascii=True)})
                if "failure" in outcome:
                    failed_before = call["id"]
            if image is not None:  # after every tool result of the turn, as the next user message
                attach(messages, *image)
        return finish("incomplete", "turn_limit")

    try:
        try:
            return await asyncio.wait_for(steps(), timeout)
        except asyncio.TimeoutError:
            return finish("timed_out", "deadline")
    except BaseException as error:
        state = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
        with suppress(OSError, ValueError):  # the original error is raised regardless
            history.append("outcome", {"state": state, "reason": str(error) or type(error).__name__,
                                       "write_actions_count": len(writes)})
        raise
    finally:
        await settled_thread(files.close)
        if own_runtime:
            await runtime.close()
