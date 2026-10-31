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
    {"name": "service_call",
     "description": ("Call a service under this Pirate identity. Content need not pass through you: any "
                     'argument value may be {"$file": {"root": ..., "path": ..., "sha256": optional}}, '
                     "replaced by that file's text (at most 512 KiB in all); save_to writes the answer to a "
                     "file and returns saved_to instead (expected_sha256 null for a new file). If the save "
                     "fails, the answer comes back with save_to_refused (nothing written) or save_to_failed "
                     "(the file may still be written: check its hash before writing again)."),
     "parameters": {"type": "object", "properties": {
         "service": {"type": "string"}, "tool": {"type": "string"}, "arguments": {"type": "object"},
         "save_to": {"type": "object", "properties": {"root": {"type": "string"}, "path": {"type": "string"},
                                                      "expected_sha256": {"type": ["string", "null"]}},
                     "required": ["root", "path", "expected_sha256"], "additionalProperties": False}},
                    "required": ["service", "tool", "arguments"], "additionalProperties": False}},
]
# The worker kit's shared file and exec tools come in the chat "function" wrapping; unwrapped here.
SHARED: list[dict[str, Any]] = [entry["function"] for entry in definitions()]  # type: ignore[no-untyped-call]
TOOLS: list[dict[str, Any]] = SHARED + SERVICES + [VIEW_IMAGE]
NAMES = frozenset(tool["name"] for tool in TOOLS)
