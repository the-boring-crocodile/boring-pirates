"""Mistral's API behind one adapter, through Mistral's SDK: its settings, its client
(with the SDK's retries), its errors, its model cards, its message and tool shapes."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from ..common.limits import DEFAULT_PROVIDER_TIMEOUT_SECONDS, positive_integer, positive_seconds
from ..common.providers import ProviderFailure, Turn
from ..common.tools import TOOLS

NAME = "mistral"
DEFAULT_MAX_TOKENS = 8192
REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
PROMPT_MODES = {"reasoning"}
SERVICE_TIERS = {"auto", "standard_only"}
# The chat settings a flavour may set, under the SDK's names; all but max_tokens are
# passed only when set. Mistral checks what the SDK leaves open (the penalties' range).
SETTINGS = {"max_tokens", "temperature", "reasoning_effort", "top_p", "random_seed", "stop",
            "presence_penalty", "frequency_penalty", "safe_prompt", "service_tier", "prompt_mode"}
RETRY_SECONDS = 60  # at most this long retrying one call (429, 500, 502, 503, 504, no connection)
BACKOFF_MS = (1000, 16000)  # first wait, longest wait; doubling
CARD_TIMEOUT_MS = 10000  # the model card is asked once at start; no answer is no reason to wait


def _number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def settings(raw: object, flavour: str) -> dict:
    """A flavour's "mistral" block: Mistral's own chat settings, checked."""
    raw = {} if raw is None else raw
    if not isinstance(raw, dict) or set(raw) - SETTINGS:
        raise ValueError(f"profile {flavour}: the mistral block takes {sorted(SETTINGS)}")
    name = f"profile {flavour}: mistral."
    result: dict[str, Any] = {"max_tokens": positive_integer(raw.get("max_tokens", DEFAULT_MAX_TOKENS),
                                                             name + "max_tokens")}
    checks: dict[str, tuple[Any, str]] = {
        "temperature": (lambda v: _number(v) and 0 <= v <= 1.5, "a number from 0 to 1.5"),
        "top_p": (lambda v: _number(v) and 0 <= v <= 1, "a number from 0 to 1"),
        "random_seed": (lambda v: type(v) is int and v >= 0, "an integer from 0"),
        "presence_penalty": (_number, "a number"),
        "frequency_penalty": (_number, "a number"),
        "safe_prompt": (lambda v: type(v) is bool, "true or false"),
        "stop": (lambda v: (isinstance(v, str) and v != "") or (
            isinstance(v, list) and v and all(isinstance(x, str) and x for x in v)), "a text or a list of texts"),
        "reasoning_effort": (lambda v: v in REASONING_EFFORTS, f"one of {sorted(REASONING_EFFORTS)}"),
        "service_tier": (lambda v: v in SERVICE_TIERS, f"one of {sorted(SERVICE_TIERS)}"),
        "prompt_mode": (lambda v: v in PROMPT_MODES, f"one of {sorted(PROMPT_MODES)}"),
    }
    for key, (valid, what) in checks.items():
        if raw.get(key) is not None:
            if not valid(raw[key]):
                raise ValueError(f"{name}{key} must be {what}")
            result[key] = float(raw[key]) if key in ("temperature", "top_p") else raw[key]
    return result


def client(key: str, timeout: float, async_client: Any = None):
    """One SDK client per Pirate. The SDK retries 429 and 5xx answers and lost
    connections with exponential backoff, for at most RETRY_SECONDS per call
    (bounded by the request timeout); what still fails reaches complete() as an error."""
    from mistralai.client import Mistral
    from mistralai.client.utils.retries import BackoffStrategy, RetryConfig
    budget = int(min(RETRY_SECONDS, timeout) * 1000)
    retries = RetryConfig("backoff", BackoffStrategy(initial_interval=BACKOFF_MS[0], max_interval=BACKOFF_MS[1], exponent=2.0,
                                                     max_elapsed_time=budget), retry_connection_errors=True)
    return Mistral(api_key=key, timeout_ms=max(1, int(timeout * 1000)), retry_config=retries,
                   async_client=async_client)  # async_client: tests only


def failure(error: Exception) -> ProviderFailure | None:
    """The SDK's error as one of our provider failure reasons (None: not the API's)."""
    import httpx
    from mistralai.client import errors
    if isinstance(error, errors.MistralError):
        status = error.status_code
        reason = ("unauthorized" if status in (401, 403) else "rate_limited" if status == 429
                  else "invalid_response" if isinstance(error, errors.ResponseValidationError)
                  else "unavailable" if status >= 500 else "rejected")
        return ProviderFailure(reason, str(error))
    if isinstance(error, errors.NoResponseError):
        return ProviderFailure("unavailable", str(error) or "no response")
    if isinstance(error, httpx.TimeoutException):
        return ProviderFailure("timed_out", str(error) or type(error).__name__)
    if isinstance(error, httpx.TransportError):
        return ProviderFailure("unavailable", str(error) or type(error).__name__)
    return None


def _plain(value: Any) -> Any:
    """The SDK marks fields it did not get with a placeholder object: None here."""
    from mistralai.client.types.basemodel import Unset
    return None if value is None or isinstance(value, Unset) else value


async def inspect(client: Any, model: str, tools: frozenset[str]) -> tuple[list[str], list[str], dict | None]:
    """Mistral's card for the model: whether it can do what the flavour asks
    (tools need function calling, view_image needs vision), its retirement, its size."""
    from mistralai.client import errors
    from mistralai.client.utils.retries import BackoffStrategy, RetryConfig
    once = RetryConfig("none", BackoffStrategy(1, 1, 1.0, 1), False)  # at start: one try, briefly
    try:
        card = await client.models.retrieve_async(model_id=model, retries=once, timeout_ms=CARD_TIMEOUT_MS)
    except errors.MistralError as error:
        if error.status_code in (400, 404, 422):
            return [f"Mistral does not know the model {model} ({error.status_code})"], [], None
        return [], [f"no model card for {model}: {failure(error)}"], None
    except Exception as error:  # noqa: BLE001  (no card is no reason to refuse a flavour)
        return [], [f"no model card for {model}: {type(error).__name__}: {error}"], None
    capabilities = card.capabilities.model_dump() if _plain(getattr(card, "capabilities", None)) else {}
    problems = []
    if tools and not capabilities.get("function_calling"):
        problems.append(f"{model} cannot call tools, and the flavour allows {len(tools)}")
    if "view_image" in tools and not capabilities.get("vision"):
        problems.append(f"{model} cannot see images, and the flavour allows view_image")
    deprecation = _plain(getattr(card, "deprecation", None))
    replacement = _plain(getattr(card, "deprecation_replacement_model", None))
    warnings = ([f"Mistral retires {model} on {deprecation:%Y-%m-%d}"
                 + (f"; its replacement is {replacement}" if replacement else "")] if deprecation else [])
    info = {"id": _plain(card.id), "max_context_length": _plain(getattr(card, "max_context_length", None)),
            "capabilities": sorted(k for k, v in capabilities.items() if v is True),
            "deprecation": deprecation.isoformat() if deprecation else None, "replacement": replacement}
    return problems, warnings, info


def function(tool: dict) -> dict:
    """Our tool in Mistral's (chat "function") format."""
    return {"type": "function", "function": tool}


class MistralProvider:
    """One flavour's model and settings over the Pirate's shared client."""

    def __init__(self, model: str, client: Any, request_timeout: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
                 allowed_tools: frozenset[str] = frozenset(t["name"] for t in TOOLS),
                 options: dict | None = None):
        self.options = settings(options, model)
        self.request_timeout = positive_seconds(request_timeout, "provider_request_timeout_seconds")
        self.client = client
        self.model = model
        self.allowed_tools = allowed_tools

    async def complete(self, messages: list[dict]) -> Turn:
        selected = [function(tool) for tool in TOOLS if tool["name"] in self.allowed_tools]
        options: dict[str, Any] = {"tools": selected, "tool_choice": "auto"} if selected else {"tool_choice": "none"}
        for key, value in self.options.items():
            if key != "max_tokens":
                options[key] = value
        # One key per run (its system and first user message): every turn of a run
        # shares that prefix, so routing them together lets the provider's prompt
        # cache serve it (cached input is billed at a fraction).
        options["prompt_cache_key"] = cache_key(messages)
        try:
            response = await self.client.chat.complete_async(
                model=self.model, messages=list(messages), **options,
                # Several calls per turn; the assignment loop runs them in order and
                # stops a turn's remaining calls at the first that does not succeed.
                parallel_tool_calls=True, n=1, max_tokens=self.options["max_tokens"],
                timeout_ms=max(1, int(self.request_timeout * 1000)),
            )
        except Exception as error:
            mapped = failure(error)
            if mapped is None:
                raise
            raise mapped from error
        if len(response.choices) != 1 or response.choices[0].message is None:
            raise ValueError("expected exactly one completion message")
        choice = response.choices[0]
        reason = str(choice.finish_reason)
        message = choice.message.model_dump(mode="json", exclude_unset=True)
        usage = response.usage.model_dump(mode="json", exclude_unset=True)
        model = response.model if isinstance(response.model, str) else None
        content = message.get("content")
        if isinstance(content, list):
            # Structured content (reasoning models): keep the text, drop thinking.
            content = text_of(content)
            message["content"] = content
        if reason not in ("stop", "tool_calls"):
            # Truncated or otherwise unfinished: the caller reports it as an outcome.
            return Turn(message, [], content if isinstance(content, str) else "", reason, usage, model)
        calls = []
        for call in message.get("tool_calls") or []:
            if call.get("type") != "function" or not isinstance(call.get("id"), str):
                raise ValueError("unsupported tool call")
            # A call that is not a function call or has no ID cannot be answered: a
            # broken response (above). A bad name or arguments in one call is the
            # model's mistake: it goes back to the model as that call's tool error.
            called = call.get("function") if isinstance(call.get("function"), dict) else {}
            name, args, error = called.get("name"), called.get("arguments"), None
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError as problem:
                    error = f"tool arguments are not valid JSON: {problem}"
            if error is None and not isinstance(args, dict):
                error = "tool arguments must be a JSON object"
            if not isinstance(name, str) or not name:
                error = "tool call has no function name"
            if error is None:
                calls.append({"id": call["id"], "name": name, "arguments": args})
            else:
                calls.append({"id": call["id"], "name": name if isinstance(name, str) else None,
                              "arguments": None, "error": error})
        if (reason == "tool_calls") != bool(calls):
            raise ValueError("tool call finish reason disagrees with response")
        return Turn(message, calls, content or "", reason, usage, model)


def cache_key(messages: list[dict]) -> str:
    prefix = json.dumps(messages[:2], sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(prefix.encode()).hexdigest()[:32]


def text_of(chunks: list) -> str:
    """The text parts of structured message content; thinking and other parts are dropped."""
    parts = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            raise ValueError("unsupported non-text completion")
        if chunk.get("type") == "text" and isinstance(chunk.get("text"), str):
            parts.append(chunk["text"])
        elif chunk.get("type") not in ("thinking", "reference"):
            raise ValueError(f"unsupported completion part {chunk.get('type')!r}")
    return "".join(parts)
