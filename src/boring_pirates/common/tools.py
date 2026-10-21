"""Our tools as the model sees them, in no provider's format: name, description and
a JSON Schema for the arguments. A provider folder translates them into its API's
tool format. A tool lives here when it does the same whatever model calls
it."""
from __future__ import annotations

from typing import Any

from boring_worker_kit.tools import definitions

from .images import DEFINITION as VIEW_IMAGE

SERVICES = [
    {"name": "service_list", "description": "List services granted to this Pirate and flavour",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "service_describe", "description": "Describe tools of an allowed service",
     "parameters": {"type": "object", "properties": {"service": {"type": "string"}}, "required": ["service"],
                    "additionalProperties": False}},
    {"name": "service_call", "description": "Call a service under this Pirate identity",
     "parameters": {"type": "object", "properties": {"service": {"type": "string"}, "tool": {"type": "string"},
                                                     "arguments": {"type": "object"}},
                    "required": ["service", "tool", "arguments"], "additionalProperties": False}},
]
# The worker kit's shared file and exec tools come in the chat "function" wrapping; unwrapped here.
SHARED: list[dict[str, Any]] = [entry["function"] for entry in definitions()]  # type: ignore[no-untyped-call]
TOOLS: list[dict[str, Any]] = SHARED + SERVICES + [VIEW_IMAGE]
NAMES = frozenset(tool["name"] for tool in TOOLS)
