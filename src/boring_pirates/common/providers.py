"""The providers a Pirate can serve, one folder each.

A Pirate names its provider at the top of config.json. Its folder supplies:

    NAME                          "mistral"
    settings(raw, flavour) -> dict     the flavour's block named after the provider
                                  (its API's settings under its own names), checked;
                                  {} when the block is absent
    client(key, timeout) -> async context manager   one API client per Pirate
    Provider(model, client, timeout, tools, settings)   one flavour; .complete(messages) -> Turn,
                                  raising ProviderFailure when the API gives no usable answer
    inspect(client, model, tools) -> (problems, warnings, card)   asked once at start:
                                  problems keep a flavour from being offered, warnings
                                  are logged, card is shown in describe (or None)

common/ never reads a provider's block itself: it hands it over and shows what
settings() returned. The conversation is kept in the chat-message shape
(system, user, assistant with tool_calls, tool with tool_call_id); a provider
whose API differs translates in its folder.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from types import ModuleType
from typing import Any

KNOWN = ("mistral",)


@dataclass(frozen=True)
class Turn:
    """One model turn, as the assignment loop needs it."""
    message: dict[str, Any]  # the assistant message as it goes back into the conversation
    calls: list[dict[str, Any]]  # {"id", "name", "arguments"} or with "error" instead of arguments
    text: str
    finish_reason: str     # "stop", "tool_calls", or the provider's reason for stopping early
    usage: dict[str, Any]
    model: str | None = None


class ProviderFailure(Exception):
    """The provider gave no usable answer (after its own retries). The assignment
    ends "failed" with reason provider_<reason>; detail goes to the transcript."""

    REASONS = ("rate_limited", "unauthorized", "unavailable", "rejected", "timed_out", "invalid_response")

    def __init__(self, reason: str, detail: str):
        if reason not in self.REASONS:
            raise ValueError(f"unknown provider failure {reason!r}")
        super().__init__(f"provider_{reason}: {detail}")
        self.reason, self.detail = reason, detail


def provider(name: object) -> ModuleType:
    if name not in KNOWN:
        raise ValueError(f"provider must be one of {list(KNOWN)}")
    return importlib.import_module(f"boring_pirates.{name}")
