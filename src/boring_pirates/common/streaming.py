"""A streamed chat completion, put back together as one completion body.

Every provider streams chunks in the chat shape: per chunk a choice with a
`delta` (text, reasoning, tool-call pieces) and at the end a finish reason; the
last chunk may carry the usage. The idle timeout is the point of streaming: a
call may run long while tokens arrive, and fails only after the stream has been
silent for the idle time. (Local servers can be silent for a while: Ollama
0.40.10 buffers a whole tool call and sends it as one chunk; 26 s for about
1500 tokens on an RTX 4070, measured 2026-10-25.)
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

MAX_STREAM_CHUNKS = 200_000  # far above any answer within max_tokens; a runaway stream stops here


class StreamError(ValueError):
    """The stream does not make one completion (the provider maps it to invalid_response)."""


def plain_text(value: Any) -> str:
    """Content as plain text only (the default; a provider with structured content passes its own)."""
    if not isinstance(value, str):
        raise StreamError("expected text content")
    return value


class Assembly:
    """Chunks in, one {"choices": [{"message", "finish_reason"}], "usage", "model"} out."""

    def __init__(self, text: Callable[[Any], str] | None = None) -> None:
        self.text = text or plain_text
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.calls: dict[Any, dict[str, Any]] = {}  # in arrival order
        self.finish_reason: str | None = None
        self.usage: dict[str, Any] | None = None
        self.model: str | None = None
        self.chunks = 0
        self.saw_content = False

    def add(self, chunk: dict[str, Any]) -> None:
        self.chunks += 1
        if self.chunks > MAX_STREAM_CHUNKS:
            raise StreamError("stream exceeds chunk limit")
        if not isinstance(chunk, dict):
            raise StreamError("stream chunk is not an object")
        if isinstance(chunk.get("model"), str):
            self.model = chunk["model"]
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not isinstance(choices, list) or len(choices) > 1:
            raise StreamError("expected at most one choice per chunk")
        if not choices:
            return  # a usage-only chunk
        choice = choices[0]
        if not isinstance(choice, dict):
            raise StreamError("stream choice is not an object")
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            raise StreamError("stream delta is not an object")
        if delta.get("content") is not None:
            self.saw_content = True
            self.content.append(self.text(delta["content"]))
        if isinstance(delta.get("reasoning"), str):
            self.reasoning.append(delta["reasoning"])
        for position, piece in enumerate(delta.get("tool_calls") or []):
            self._call(position, piece)
        reason = choice.get("finish_reason")
        if reason is not None:
            if not isinstance(reason, str) or not reason:
                raise StreamError("invalid finish_reason")
            self.finish_reason = reason

    def _call(self, position: int, piece: Any) -> None:
        if not isinstance(piece, dict):
            raise StreamError("tool call piece is not an object")
        index = piece.get("index")
        ident = piece.get("id") if isinstance(piece.get("id"), str) and piece["id"] else None
        key: Any = index if isinstance(index, int) else ident or (len(self.calls) + position)
        # A new id at an index already in use is a new call: parallel calls must not
        # be glued together when a server repeats an index (or leaves it at 0).
        while key in self.calls and ident is not None and self.calls[key]["id"] not in (None, ident):
            key = (key, ident) if not isinstance(key, tuple) else (*key, ident)
        call = self.calls.setdefault(key, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
        if isinstance(piece.get("id"), str) and piece["id"]:
            call["id"] = piece["id"]
        if isinstance(piece.get("type"), str):
            call["type"] = piece["type"]
        function = piece.get("function") or {}
        if not isinstance(function, dict):
            raise StreamError("tool call function is not an object")
        if isinstance(function.get("name"), str):
            call["function"]["name"] += function["name"]
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            if not isinstance(call["function"]["arguments"], str):
                raise StreamError("tool call arguments mix object and text")
            call["function"]["arguments"] += arguments
        elif isinstance(arguments, dict):  # some servers send the object whole
            if call["function"]["arguments"]:
                raise StreamError("tool call arguments mix text and object")
            call["function"]["arguments"] = arguments

    def body(self) -> dict[str, Any]:
        if self.finish_reason is None:
            raise StreamError("stream ended without a finish_reason")
        message: dict[str, Any] = {"role": "assistant",
                                   "content": "".join(self.content) if self.saw_content else None}
        if self.reasoning:
            message["reasoning"] = "".join(self.reasoning)
        if self.calls:
            message["tool_calls"] = list(self.calls.values())
        body: dict[str, Any] = {"choices": [{"message": message, "finish_reason": self.finish_reason}]}
        if self.usage is not None:
            body["usage"] = self.usage
        if self.model is not None:
            body["model"] = self.model
        return body
