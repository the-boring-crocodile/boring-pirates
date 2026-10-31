"""Mistral's API: its settings, its base URL, its content shape and its model card.
The transport (requests, retries, stream, errors) is common/chat.py."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from typing import Any
from urllib.parse import quote

import httpx

from ..common import chat
from ..common.limits import positive_integer

NAME = "mistral"
BASE = "https://api.mistral.ai/v1"
DEFAULT_MAX_TOKENS = 8192
REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
PROMPT_MODES = {"reasoning"}
SERVICE_TIERS = {"auto", "standard_only"}
# The chat settings a flavour may set, under the API's names; all but max_tokens are
# passed only when set. Mistral checks what is left open (the penalties' range).
SETTINGS = {"max_tokens", "temperature", "reasoning_effort", "top_p", "random_seed", "stop",
            "presence_penalty", "frequency_penalty", "safe_prompt", "service_tier", "prompt_mode", "stream"}


def _number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def settings(raw: object, flavour: str) -> dict[str, Any]:
    """A flavour's "mistral" block: Mistral's own chat settings, checked."""
    raw = {} if raw is None else raw
    if not isinstance(raw, dict) or set(raw) - SETTINGS:
        raise ValueError(f"profile {flavour}: the mistral block takes {sorted(SETTINGS)}")
    name = f"profile {flavour}: mistral."
    result: dict[str, Any] = {"max_tokens": positive_integer(raw.get("max_tokens", DEFAULT_MAX_TOKENS),
                                                             name + "max_tokens")}
    if raw.get("stream") is not None:  # not set: streamed; false: one answer at the end, no idle timeout
        if type(raw["stream"]) is not bool:
            raise ValueError(name + "stream must be true or false")
        result["stream"] = raw["stream"]
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


def validate_endpoint(value: object) -> None:
    if value is not None:
        raise ValueError("mistral uses its own API; provider_endpoint is not accepted")


def client(key: str, timeout: float, *, endpoint: object = None,
           transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    validate_endpoint(endpoint)
    return chat.client(BASE, key, timeout, transport=transport)


def text_of(content: Any) -> str:
    """Mistral's content as text: a string, or structured parts (reasoning models)
    whose text is kept and whose thinking and references are dropped."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValueError("expected text or a list of content parts")
    parts = []
    for chunk in content:
        if not isinstance(chunk, dict):
            raise ValueError("unsupported non-text completion")
        if chunk.get("type") == "text" and isinstance(chunk.get("text"), str):
            parts.append(chunk["text"])
        elif chunk.get("type") not in ("thinking", "reference"):
            raise ValueError(f"unsupported completion part {chunk.get('type')!r}")
    return "".join(parts)


class MistralProvider(chat.ChatProvider):
    settings = staticmethod(settings)
    text = staticmethod(text_of)

    def fields(self, messages: list[dict[str, Any]], streamed: bool) -> dict[str, Any]:
        selected = self.tools()
        body: dict[str, Any] = {"tools": selected, "tool_choice": "auto"} if selected else {"tool_choice": "none"}
        body.update((k, v) for k, v in self.options.items() if k != "stream")
        # One key per run (its system and first user message): every turn of a run
        # shares that prefix, so routing them together lets the provider's prompt
        # cache serve it (cached input is billed at a fraction).
        body["prompt_cache_key"] = cache_key(messages)
        # Several calls per turn; the assignment loop runs them in order and
        # stops a turn's remaining calls at the first that does not succeed.
        body.update(parallel_tool_calls=True, n=1)
        return body


async def inspect(client: httpx.AsyncClient, model: str, tools: frozenset[str]) -> tuple[list[str], list[str], dict[str, Any] | None]:
    """Mistral's card for the model: whether it can do what the flavour asks
    (tools need function calling, view_image needs vision), its retirement, its size.
    One try at start; no answer is no reason to refuse a flavour."""
    try:
        response = await asyncio.wait_for(client.get("models/" + quote(model, safe=""), timeout=chat.CARD_SECONDS),
                                          chat.CARD_SECONDS)
    except (httpx.HTTPError, asyncio.TimeoutError) as error:
        return [], [f"no model card for {model}: {chat.failure(error)}"], None
    if response.status_code in (400, 404, 422):
        return [f"Mistral does not know the model {model} ({response.status_code})"], [], None
    try:
        response.raise_for_status()
        card = response.json()
        if not isinstance(card, dict):
            raise ValueError("expected a model card object")
    except (httpx.HTTPError, ValueError) as error:
        return [], [f"no model card for {model}: {chat.failure(error) or error}"], None
    raw = card.get("capabilities")
    capabilities = raw if isinstance(raw, dict) else {}
    problems = []
    if tools and capabilities.get("function_calling") is not True:
        problems.append(f"{model} cannot call tools, and the flavour allows {len(tools)}")
    if "view_image" in tools and capabilities.get("vision") is not True:
        problems.append(f"{model} cannot see images, and the flavour allows view_image")
    deprecation = card.get("deprecation") if isinstance(card.get("deprecation"), str) else None
    replacement = card.get("deprecation_replacement_model")
    replacement = replacement if isinstance(replacement, str) else None
    warnings = ([f"Mistral retires {model} on {deprecation[:10]}"
                 + (f"; its replacement is {replacement}" if replacement else "")] if deprecation else [])
    size = card.get("max_context_length")
    info = {"id": card.get("id") if isinstance(card.get("id"), str) else None,
            "max_context_length": size if type(size) is int else None,
            "capabilities": sorted(k for k, v in capabilities.items() if v is True),
            "deprecation": deprecation, "replacement": replacement}
    return problems, warnings, info


def cache_key(messages: list[dict[str, Any]]) -> str:
    prefix = json.dumps(messages[:2], sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(prefix.encode()).hexdigest()[:32]
