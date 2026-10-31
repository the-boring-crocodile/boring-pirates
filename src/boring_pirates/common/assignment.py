"""One bounded assignment: the model loop, its file tools and its transcript."""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from collections.abc import Callable
from pathlib import Path

from .claims import check as check_claims
from .files import CheckoutFiles
from .hints import hint
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
SAVE_PROBLEMS = ("save_to_refused", "save_to_failed")  # a service_call save that did not land
# A turn with neither text nor a tool call is asked again, at most this often per
# assignment. Local servers can drop a tool call they fail to parse (observed:
# Ollama 0.40.10 with Gemma 4, about every second turn after a file read); a new
# sample usually parses. An empty final answer after the retries stays empty.
EMPTY_TURN_RETRIES = 3
WRITE_REMINDER = ("You have made {calls} tool calls and written no file yet. Write the file the "
                  "assignment asks for now, with what you have; list what you could not settle "
                  "under DOUBTS. Do not look up more first.")
DIGEST = "\n\n---\nKey rules from the handbook (a reminder, not a new task):\n\n{digest}"


def with_digest(messages: list[dict], digest: str) -> list[dict]:
    """The conversation to send, with the digest at the very end; history keeps none of it.

    Appended to the last message (the prompt on the first call, then the latest tool
    result), not sent as a user message of its own: Gemma 4 answered such a message
    as a new task ("ready for the task details") after its work was done. After an
    image the digest is a text part of that message."""
    block = DIGEST.format(digest=digest)
    last = messages[-1]
    if last.get("role") in ("user", "tool") and isinstance(last.get("content"), str):
        return [*messages[:-1], {**last, "content": last["content"] + block}]
    if last.get("role") == "user" and isinstance(last.get("content"), list):  # an image (view_image)
        return [*messages[:-1], {**last, "content": [*last["content"], {"type": "text", "text": block.lstrip()}]}]
    return [*messages, {"role": "user", "content": block.lstrip()}]


async def settled_thread(function, *args, on_cancelled_result=None):
    return await settled(asyncio.to_thread(function, *args), on_cancelled_result)


async def run(provider, checkout: Path, profile: ResolvedProfile, history: FileHistory,
              prompt: str, timeout: float, runtime: ToolRuntime | None = None, *,
              cancellation_reason: Callable[[], str | None] | None = None) -> dict:
    """Return the outcome as data; raise only for failures (and cancellation)."""
    started = time.monotonic()
    usage: dict = {"provider": profile.spec.provider, "requested_model": profile.spec.model,
                   "reported_model": None, "model_calls": 0, "input_tokens": 0, "output_tokens": 0,
                   "cached_input_tokens": 0, "tool_calls": 0, "finish_reason": None,
                   "empty_turn_retries": 0, "service_calls": 0, "write_reminders": 0, "digest_calls": 0}
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
        result = {"state": state, "reason": reason, "answer": answer, "usage": usage,
                  "write_actions": writes[:MAX_WRITE_ACTIONS], "write_actions_count": len(writes),
                  "tool_errors": errors[:MAX_TOOL_ERRORS], "tool_errors_count": len(errors),
                  "elapsed_seconds": round(time.monotonic() - started, 3)}
        files = check_claims(answer, writes)  # only when the answer has a FILES line
        if files is not None:
            result["files_check"] = files
        return result

    own_runtime = runtime is None
    runtime = runtime or ToolRuntime()
    files = CheckoutFiles(checkout, profile.spec.files, profile.spec.workdir)
    deadline = asyncio.get_running_loop().time() + timeout
    messages = [{"role": "system", "content": profile.system_message()}, {"role": "user", "content": prompt}]
    seen: set[str] = set()

    async def steps() -> dict:
        nonlocal pending
        for _ in range(profile.spec.max_turns):
            if len(json.dumps(messages, ensure_ascii=False).encode()) > MAX_CONVERSATION:
                return finish("incomplete", "conversation_limit")
            pending = True
            sent = messages
            if profile.digest:  # on every call, at the end; not kept in history
                sent = with_digest(messages, profile.digest)
                usage["digest_calls"] += 1
            try:
                turn = await provider.complete(sent)
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
            if not turn.calls and not turn.text.strip() and usage["empty_turn_retries"] < EMPTY_TURN_RETRIES:
                # Ask again without keeping the empty turn; it costs a turn of max_turns.
                usage["empty_turn_retries"] += 1
                messages.pop()
                history.append("empty_turn", {"retry": usage["empty_turn_retries"]})
                continue
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
                    if call["name"] == "service_call":
                        budget = profile.spec.max_service_calls
                        if budget is not None and usage["service_calls"] >= budget:
                            raise ValueError(f"service_call budget of {budget} for this assignment is used up: "
                                             "write your result now with what you have; list the gaps under DOUBTS")
                        usage["service_calls"] += 1
                    if call["name"] not in profile.spec.tools:
                        allowed = ", ".join(sorted(profile.spec.tools)) or "none"
                        known = "is disabled by profile" if call["name"] in TOOLS else "is unknown"
                        raise ValueError(f"tool {call['name']!r} {known}; allowed tools: {allowed}")
                    # Bound now: a cancelled call may settle after the loop moved on.
                    def record_write(value, call=call):
                        if call["name"] in {"file_write", "file_patch", "file_write_bytes"}:
                            writes.append({"root": call["arguments"].get("root", "work"),
                                           "path": call["arguments"]["path"], **value})
                        elif call["name"] == "service_call" and isinstance(value, dict) \
                                and isinstance(value.get("saved_to"), dict):
                            writes.append(dict(value["saved_to"]))  # an answer saved with save_to is a write
                    def cancelled_result(value, call=call, record_write=record_write):
                        # A file tool or save that settled after the assignment stopped waiting: a
                        # cancellation, or (a save only) its own timeout, as the kit's landed_after says.
                        record_write(value)
                        after_timeout = isinstance(value, dict) and value.get("landed_after") == "timeout"
                        history.append("tool_result", {"id": call["id"], "ok": True, "value": value,
                                                       "settled_after_timeout" if after_timeout
                                                       else "settled_after_cancellation": True})
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
                    save = (next((code for code in SAVE_PROBLEMS if code in value), None)
                            if call["name"] == "service_call" and isinstance(value, dict) else None)
                    if save is not None:
                        # The service ran and answered, but save_to did not land (refused,
                        # failed or unknown): the model gets the answer and a failure, and the
                        # rest of this turn's calls, which may rely on the file, do not run
                        # (review finding F1, 2026-10-25).
                        detail = value[save] if isinstance(value[save], dict) else {}
                        message = (f"the service answered, but its answer was not saved ({save}"
                                   + (f": {detail.get('code') or detail.get('message')}" if detail else "") + ")")
                        outcome = {"ok": False, "error": message, "code": save, "value": value,
                                   "failure": {"status": "failed", "code": save,
                                               "effects": "unknown" if save == "save_to_failed" else "completed",
                                               "message": message}}
                        errors.append({"tool": "service_call", "error": message[:MAX_TOOL_ERROR_TEXT],
                                       "status": "failed"})

                except Refusal as error:
                    outcome = {"ok": False, "error": str(error), "status": "refused", **error.as_data()}
                    next_step = hint(call["name"], outcome, profile.spec.tools)
                    if next_step:
                        outcome["hint"] = next_step
                    fields = outcome.get("fields")  # argument names only, never values
                    text = str(error) + (f" (fields: {', '.join(map(str, fields))})"
                                         if isinstance(fields, list) and fields else "")
                    errors.append({"tool": call["name"][:64], "error": text[:MAX_TOOL_ERROR_TEXT]})
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
            reminder = profile.spec.write_reminder_after
            if (reminder is not None and not writes and not usage["write_reminders"]
                    and usage["tool_calls"] >= reminder):
                usage["write_reminders"] = 1
                history.append("write_reminder", {"tool_calls": usage["tool_calls"]})
                messages.append({"role": "user", "content": WRITE_REMINDER.format(calls=usage["tool_calls"])})
        return finish("incomplete", "turn_limit")

    try:
        try:
            return await asyncio.wait_for(steps(), timeout)
        except asyncio.TimeoutError:
            return finish("timed_out", "deadline")
    except BaseException as error:
        state = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
        reason = cancellation_reason() if state == "cancelled" and cancellation_reason else None
        with suppress(OSError, ValueError):  # the original error is raised regardless
            history.append("outcome", {"state": state, "reason": reason or str(error) or type(error).__name__,
                                       "write_actions_count": len(writes)})
        raise
    finally:
        await settled_thread(files.close)
        if own_runtime:
            await runtime.close()
