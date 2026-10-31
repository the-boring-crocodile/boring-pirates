"""An OpenAI-compatible server (a local model, for example): its base URL from the
configuration, its settings, its model list. The transport is common/chat.py."""
from __future__ import annotations

import asyncio
import math
from typing import Any

import httpx

from ..common import chat
from ..common.limits import positive_integer

NAME = "openai_compat"
SETTINGS = {"max_tokens", "temperature", "top_p", "seed", "stop", "reasoning_effort", "keep_reasoning", "stream"}


def validate_endpoint(value: object) -> str:
    """The operator owns routing/access policy; validate the API base URL."""
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("openai_compat requires provider_endpoint: an HTTP(S) base URL")
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL as error:
        raise ValueError("invalid provider_endpoint") from error
    if url.scheme not in ("http", "https") or not url.host or url.userinfo or url.query or url.fragment:
        raise ValueError("provider_endpoint needs HTTP(S), a host, no credentials, query or fragment")
    return str(url).rstrip("/")


def settings(raw: object, flavour: str) -> dict[str, Any]:
    raw = {} if raw is None else raw
    if not isinstance(raw, dict) or set(raw) - SETTINGS:
        raise ValueError(f"profile {flavour}: the openai_compat block takes {sorted(SETTINGS)}")
    prefix = f"profile {flavour}: openai_compat."
    result: dict[str, Any] = {
        "max_tokens": positive_integer(raw.get("max_tokens", 2048), prefix + "max_tokens"),
        "keep_reasoning": raw.get("keep_reasoning", False),
    }
    if type(result["keep_reasoning"]) is not bool:
        raise ValueError(prefix + "keep_reasoning must be true or false")
    if raw.get("stream") is not None:  # not set: streamed; false: a server that cannot stream (no idle timeout)
        if type(raw["stream"]) is not bool:
            raise ValueError(prefix + "stream must be true or false")
        result["stream"] = raw["stream"]
    for name, maximum in (("temperature", 2), ("top_p", 1)):
        value = raw.get(name)
        if value is not None:
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
                raise ValueError(f"{prefix}{name} must be a number from 0 to {maximum}")
            result[name] = float(value)
    if raw.get("seed") is not None:
        value = raw["seed"]
        if type(value) is not int or value < 0:
            raise ValueError(prefix + "seed must be an integer from 0")
        result["seed"] = value
    if raw.get("stop") is not None:
        value = raw["stop"]
        if not ((isinstance(value, str) and value)
                or (isinstance(value, list) and value and all(isinstance(x, str) and x for x in value))):
            raise ValueError(prefix + "stop must be text or a nonempty list of texts")
        result["stop"] = value
    if raw.get("reasoning_effort") is not None:
        value = raw["reasoning_effort"]
        if not isinstance(value, str) or value not in {"none", "low", "medium", "high"}:
            raise ValueError(prefix + "reasoning_effort must be none, low, medium or high")
        result["reasoning_effort"] = value
    return result


def client(key: str, timeout: float, *, endpoint: object = None,
           transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return chat.client(validate_endpoint(endpoint), key, timeout, transport=transport)


class OpenAICompatProvider(chat.ChatProvider):
    settings = staticmethod(settings)

    def fields(self, messages: list[dict[str, Any]], streamed: bool) -> dict[str, Any]:
        body = {k: v for k, v in self.options.items() if k not in ("keep_reasoning", "stream")}
        if streamed:
            body["stream_options"] = {"include_usage": True}
        selected = self.tools()
        if selected:
            body["tools"] = selected
        return body


async def inspect(client: httpx.AsyncClient, model: str, tools: frozenset[str]) -> tuple[list[str], list[str], dict[str, Any] | None]:
    """Discovery is not proof of tool/vision capability; acceptance tests are."""
    try:
        response = await asyncio.wait_for(client.get("models", timeout=chat.CARD_SECONDS), chat.CARD_SECONDS)
        response.raise_for_status()
        models = response.json()["data"]
        if not isinstance(models, list) or any(not isinstance(r, dict) or not isinstance(r.get("id"), str) for r in models):
            raise ValueError("expected model list")
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as error:
        return [], [f"no model list for {model}: {type(error).__name__}"], None
    if not any(row["id"] == model for row in models):
        return [f"openai_compat does not know the model {model}"], [], None
    return [], [], {"id": model, "server": NAME, "endpoint_host": client.base_url.host}
