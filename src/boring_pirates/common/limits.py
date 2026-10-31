"""Pirate-owned public budgets and internal envelope headroom."""
import math
from typing import Any

DEFAULT_MAX_TURNS = 12
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 90.0
# A streamed call fails after this long without data. Local servers are silent while
# they read a long prompt and while they generate a tool call (sent whole at its end).
DEFAULT_PROVIDER_IDLE_SECONDS = 120.0
MAX_PROMPT = 32 * 1024
MAX_ANSWER = 32 * 1024
MAX_CONTEXT = 128 * 1024
MAX_CONVERSATION = 2 * 1024 * 1024
MAX_DIGEST = 4 * 1024  # a flavour's digest_file, repeated on every call
MAX_TOOL_RESULT = 512 * 1024
MAX_FRAME = 1024 * 1024
MAX_HISTORY_RECORD = 4 * 1024 * 1024


def positive_integer(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(name + " must be a positive integer")
    return value


def positive_seconds(value: Any, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(name + " must be positive finite seconds")
    return float(value)
