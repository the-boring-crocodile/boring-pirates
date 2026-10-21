"""Pirate-owned public budgets and internal envelope headroom."""
import math

DEFAULT_MAX_TURNS = 12
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 90.0
MAX_PROMPT = 32 * 1024
MAX_ANSWER = 32 * 1024
MAX_CONTEXT = 128 * 1024
MAX_CONVERSATION = 2 * 1024 * 1024
MAX_TOOL_RESULT = 512 * 1024
MAX_FRAME = 1024 * 1024
MAX_HISTORY_RECORD = 4 * 1024 * 1024


def positive_integer(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(name + " must be a positive integer")
    return value


def positive_seconds(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(name + " must be positive finite seconds")
    return float(value)
