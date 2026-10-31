"""What to do next, added to a refusal the model sees.

A refusal names what is wrong; a small local model often cannot tell from that
what to do instead and repeats the call (observed with Gemma 4 12B: ten minutes
of identical stale writes). A hint says the next step in one or two sentences.
Hints are built from the refusal's code, its message and the tool's own schema,
never from the rejected values.
"""
from __future__ import annotations

from typing import Any

from .tools import TOOLS

SCHEMAS: dict[str, dict[str, Any]] = {tool["name"]: tool["parameters"] for tool in TOOLS}
MAX_HINT = 400  # characters


def _types(schema: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for part in [schema, *schema.get("anyOf", []), *schema.get("oneOf", [])]:
        kind = part.get("type")
        for name in kind if isinstance(kind, list) else [kind]:
            if isinstance(name, str) and name not in found:
                found.append(name)
    return found


def _values(schema: dict[str, Any]) -> tuple[list[str], bool]:
    """The enumerated values a property allows, and whether it may be left out as null."""
    values: list[str] = []
    nullable = False
    for part in [schema, *schema.get("anyOf", []), *schema.get("oneOf", [])]:
        values += [str(v) for v in part.get("enum", []) if str(v) not in values]
        nullable = nullable or part.get("type") == "null"
    return values, nullable


def field_hint(tool: str, field: str) -> str | None:
    """One sentence for a rejected argument, from the tool's schema."""
    schema = SCHEMAS.get(tool)
    parts = field.split(".")
    if schema is None or len(parts) < 3 or parts[0] != tool or parts[1] != "arguments":
        return None
    name = parts[2]
    properties = schema.get("properties", {})
    if name not in properties:
        return f"{tool} has no argument {name}; its arguments are {', '.join(properties)}."
    if len(parts) > 3:
        return None  # nested (an edit inside file_patch): the field name says enough
    values, nullable = _values(properties[name])
    required = name in schema.get("required", [])
    if name == "expected_sha256":
        return ("expected_sha256 is the sha256 that file_read returned for the file"
                + (", or null to create a new file." if nullable else "; file_patch needs it."))
    if values:
        choice = " or ".join(values)
        if nullable and not required:
            return f"{name} must be {choice}, or leave {name} out."
        return f"{name} must be {choice}."
    kinds = " or ".join(_types(properties[name])) or "the documented kind"
    if required:  # rejected: missing, or there but not of its kind
        return f"{tool} needs {name}, as {kinds}: check that it is there and of that kind."
    return f"{name} must be {kinds}, or leave {name} out."


def hint(tool: object, data: dict[str, Any], allowed: frozenset[str] | None = None) -> str | None:
    """The next step for a refusal's data ({code, message, ...}), or None. `allowed`:
    the flavour's tools; a hint names only those (None: every tool)."""
    if not isinstance(tool, str):
        return None
    def has(name: str) -> bool:
        return allowed is None or name in allowed
    code, message = data.get("code"), str(data.get("message", ""))
    if code == "stale" and "differs" in message:
        current = data.get("current_sha256")
        known = f" (its sha256 now is {current})" if isinstance(current, str) else ""
        steps = [f"The file already exists{known}. Do not repeat this call."]
        if has("file_read"):
            steps.append("Read it with file_read first.")
        whole = tool if tool in ("file_write", "file_write_bytes") else "file_write"
        ways = (["change it with file_patch"] if has("file_patch") and tool != "file_write_bytes" else []) + (
            [f"replace it whole with {whole} and that sha256 as expected_sha256"] if has(whole) else [])
        if ways:
            steps.append("Then " + ", or ".join(ways) + ".")
        return " ".join(steps)
    if code == "stale" and "absent" in message:
        creator = next((name for name in (tool, "file_write", "file_write_bytes")
                        if name in ("file_write", "file_write_bytes") and has(name)), None)
        return "The file does not exist." + (f" To create it, use {creator} with expected_sha256 null."
                                              if creator else "")
    if code == "invalid_request" and "patch requires expected hash" in message:
        return "file_patch needs expected_sha256: the sha256 file_read returned for this file."
    fields = data.get("fields")
    if isinstance(fields, list) and fields:
        sentences = [s for s in (field_hint(tool, str(f)) for f in fields) if s]
        if sentences:
            return " ".join(sentences)[:MAX_HINT]
    return None
