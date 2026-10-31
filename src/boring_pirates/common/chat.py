"""Chat completions over plain HTTP: the one transport every provider folder uses.

Every provider we serve speaks the chat-completions wire format: POST
BASE/chat/completions (server-sent events when streamed), GET BASE/models.
This module owns the request, the retries, the stream, the failure reasons and
the parse. A provider folder supplies its base URL, its settings, the body
fields its API takes, how its content becomes text, and its model card
(common/providers.py).
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx

from .limits import DEFAULT_PROVIDER_IDLE_SECONDS, DEFAULT_PROVIDER_TIMEOUT_SECONDS, positive_seconds
from .providers import ProviderFailure, Turn
from .streaming import Assembly, StreamError, plain_text
from .tools import TOOLS

RETRY_SECONDS = 60.0  # retries (no connection, 429/5xx) start only within this time of the call's start
BACKOFF_SECONDS = (1.0, 16.0)  # first wait, longest wait; doubling
RETRY_STATUS = {429, 500, 502, 503, 504}
CARD_SECONDS = 10.0  # the model card is asked once at start; no answer is no reason to wait
ALL_TOOLS: frozenset[str] = frozenset(tool["name"] for tool in TOOLS)

Text = Callable[[Any], str]


def client(base: str, key: str, timeout: float, *,
           transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """One HTTP client per Pirate, for its provider's API base."""
    if not isinstance(key, str) or not key.strip():
        raise ValueError("empty provider credential")
    return httpx.AsyncClient(base_url=base.rstrip("/") + "/",
                             timeout=positive_seconds(timeout, "provider_request_timeout_seconds"),
                             headers={"Authorization": f"Bearer {key}"}, transport=transport)


def failure(error: Exception) -> ProviderFailure | None:
    """An HTTP error as one of our provider failure reasons (None: not the API's)."""
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        reason = ("unauthorized" if status in (401, 403) else "rate_limited" if status == 429
                  else "unavailable" if status >= 500 else "rejected")
        words = error.response.text[:500].strip()  # the provider's own words, for the transcript
        return ProviderFailure(reason, f"HTTP {status}" + (f": {words}" if words else ""))
    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)):
        return ProviderFailure("unavailable", type(error).__name__)
    if isinstance(error, (httpx.TimeoutException, asyncio.TimeoutError)):
        return ProviderFailure("timed_out", type(error).__name__)
    if isinstance(error, httpx.TransportError):
        return ProviderFailure("unavailable", type(error).__name__)
    return None


async def attempt(client: httpx.AsyncClient, body: dict[str, Any], remaining: float, idle: float,
                  text: Text = plain_text) -> dict[str, Any]:
    """One request. Streamed: no data for `idle` seconds fails it (waiting for the
    headers included: a local server sends them only once it starts answering).
    A server that answers a streamed request with plain JSON is read as JSON."""
    streamed = body.get("stream") is True
    gap = min(idle, remaining) if streamed else remaining
    request = client.build_request("POST", "chat/completions", json=body,
                                   headers={"Accept": "text/event-stream" if streamed else "application/json"},
                                   timeout=httpx.Timeout(remaining, connect=min(5.0, remaining)))
    try:
        response = await asyncio.wait_for(client.send(request, stream=True), gap)
    except asyncio.TimeoutError:
        if not streamed:
            raise
        raise ProviderFailure("timed_out", f"no answer for {gap:g} s (provider_idle_timeout_seconds)") from None
    try:
        if response.status_code >= 400:
            await response.aread()
            response.raise_for_status()
        if not streamed or not response.headers.get("content-type", "").startswith("text/event-stream"):
            await response.aread()
            try:
                value = response.json()
            except ValueError as error:
                raise ProviderFailure("invalid_response", f"not JSON: {error}") from error
            if not isinstance(value, dict):
                raise ProviderFailure("invalid_response", "expected a completion object")
            return value
        assembly = Assembly(text=text)
        lines = response.aiter_lines()
        while True:
            try:
                line = await asyncio.wait_for(lines.__anext__(), gap)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                raise ProviderFailure("timed_out", f"stream silent for {gap:g} s "
                                      "(provider_idle_timeout_seconds)") from None
            line = line.strip()
            if not line.startswith("data:"):
                continue  # blank separators, comments (":"), event names
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                assembly.add(json.loads(data))
            except (ValueError, StreamError) as error:
                raise ProviderFailure("invalid_response", f"stream: {error}") from error
        try:
            return assembly.body()
        except StreamError as error:
            raise ProviderFailure("invalid_response", f"stream: {error}") from error
    finally:
        await response.aclose()


async def completion(client: httpx.AsyncClient, body: dict[str, Any], seconds: float,
                     idle: float = DEFAULT_PROVIDER_IDLE_SECONDS, text: Text = plain_text) -> dict[str, Any]:
    """The completion body, within one absolute call budget of `seconds` (attempts and
    backoff included; cancellation propagates); a streamed call also fails after
    `idle` seconds without data.

    A local model can generate for minutes, so the budget is the configured
    provider_request_timeout_seconds and the idle time catches a server that stopped.
    Retrying is a separate, shorter window: a new attempt starts only within
    RETRY_SECONDS of the call's start, and only before an answer began.
    """
    loop = asyncio.get_running_loop()
    start = loop.time()
    until = start + seconds
    retry_until = start + min(seconds, RETRY_SECONDS)
    delay = BACKOFF_SECONDS[0]
    last: ProviderFailure | None = None
    while True:
        now = loop.time()
        if last is not None and now >= retry_until:
            raise last
        remaining = until - now
        if remaining <= 0:
            raise ProviderFailure("timed_out", f"no complete answer within {seconds:g} s "
                                  "(provider_request_timeout_seconds)")
        try:
            return await asyncio.wait_for(attempt(client, body, remaining, idle, text), remaining)
        except asyncio.TimeoutError:
            # The whole budget ran out (idle gaps fail inside attempt): a timeout, even
            # when an earlier attempt failed for another reason.
            raise ProviderFailure("timed_out", f"no complete answer within {seconds:g} s "
                                  "(provider_request_timeout_seconds)") from None
        except httpx.HTTPError as error:
            mapped = failure(error)
            if mapped is None:
                raise
            retry = (isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout))
                     or isinstance(error, httpx.HTTPStatusError) and error.response.status_code in RETRY_STATUS)
            if not retry:
                raise mapped from error
            last = mapped
            remaining = retry_until - loop.time()
            if remaining <= 0:
                raise mapped from error
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2, BACKOFF_SECONDS[1])


def turn_of(body: Any, *, text: Text = plain_text, keep_reasoning: bool = False) -> Turn:
    """A completion body (whole or assembled from the stream) as the assignment
    loop's turn. `text` turns the answer's content into text (null stays null)."""
    try:
        if not isinstance(body, dict):
            raise ValueError("expected a completion object")
        choices = body["choices"]
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ValueError("expected exactly one choice")
        choice = choices[0]
        original, reason = choice["message"], choice["finish_reason"]
        if not isinstance(original, dict) or original.get("role") != "assistant" or not isinstance(reason, str) or not reason:
            raise ValueError("expected assistant message and finish_reason")
        content = original.get("content")
        content = None if content is None else text(content)
        reasoning = original.get("reasoning")
        if reasoning is not None and not isinstance(reasoning, str):
            raise ValueError("expected text reasoning")
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if keep_reasoning and reasoning is not None:
            message["reasoning"] = reasoning
        raw_calls = original.get("tool_calls")
        raw_calls = [] if raw_calls is None else raw_calls
        if not isinstance(raw_calls, list):
            raise ValueError("expected tool_calls list")
        if raw_calls:
            message["tool_calls"] = raw_calls
        usage = body.get("usage")
        usage = {} if usage is None else usage
        if not isinstance(usage, dict):
            raise ValueError("expected usage object")
        usage = dict(usage)
        for name in ("prompt_tokens", "completion_tokens"):
            value = usage.setdefault(name, 0)
            if type(value) is not int or value < 0:
                raise ValueError("invalid token count")
        usage["reasoning_chars"] = len(reasoning or "")
        calls: list[dict[str, Any]] = []
        if reason in ("stop", "tool_calls"):
            ids: set[str] = set()
            for call in raw_calls:
                if (not isinstance(call, dict) or call.get("type") != "function"
                        or not isinstance(call.get("id"), str) or not call["id"] or call["id"] in ids):
                    raise ValueError("unsupported or duplicate tool call")
                ids.add(call["id"])
                # A call that is not a function call or has no ID cannot be answered: a
                # broken response (above). A bad name or arguments in one call is the
                # model's mistake: it goes back to the model as that call's tool error.
                called = call.get("function")
                function = called if isinstance(called, dict) else {}
                function_name, arguments, problem = function.get("name"), function.get("arguments"), None
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError as error:
                        problem = f"tool arguments are not valid JSON: {error}"
                if problem is None and not isinstance(arguments, dict):
                    problem = "tool arguments must be a JSON object"
                if not isinstance(function_name, str) or not function_name:
                    problem = "tool call has no function name"
                if problem is None:
                    calls.append({"id": call["id"], "name": function_name, "arguments": arguments})
                else:
                    calls.append({"id": call["id"], "name": function_name if isinstance(function_name, str) else None,
                                  "error": problem})
            if (reason == "tool_calls") != bool(calls):
                raise ValueError("tool call finish reason disagrees with response")
        return Turn(message, calls, content or "", reason, usage,
                    body.get("model") if isinstance(body.get("model"), str) else None)
    except (ValueError, KeyError, TypeError) as error:
        raise ProviderFailure("invalid_response", str(error)) from error


class ChatProvider:
    """One flavour's model and settings over the Pirate's shared client. A provider
    folder subclasses it with its `settings` check, its `text` and its `fields`."""

    settings: Callable[[object, str], dict[str, Any]]  # the folder's check of its block
    text: Text = staticmethod(plain_text)  # the answer's content as text

    def __init__(self, model: str, client: httpx.AsyncClient,
                 request_timeout: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
                 allowed_tools: frozenset[str] = ALL_TOOLS,
                 options: dict[str, Any] | None = None, idle_timeout: float = DEFAULT_PROVIDER_IDLE_SECONDS):
        self.model, self.client = model, client
        self.request_timeout = positive_seconds(request_timeout, "provider_request_timeout_seconds")
        self.idle_timeout = min(positive_seconds(idle_timeout, "provider_idle_timeout_seconds"), self.request_timeout)
        self.allowed_tools = allowed_tools
        self.options = type(self).settings(options, model)

    def tools(self) -> list[dict[str, Any]]:
        return [{"type": "function", "function": tool} for tool in TOOLS if tool["name"] in self.allowed_tools]

    def fields(self, messages: list[dict[str, Any]], streamed: bool) -> dict[str, Any]:
        """The request body's fields beyond model, messages and stream."""
        raise NotImplementedError

    async def complete(self, messages: list[dict[str, Any]]) -> Turn:
        streamed = self.options.get("stream", True)
        body = {**self.fields(messages, streamed), "model": self.model, "messages": list(messages), "stream": streamed}
        result = await completion(self.client, body, self.request_timeout, self.idle_timeout, type(self).text)
        return turn_of(result, text=type(self).text, keep_reasoning=self.options.get("keep_reasoning", False))
