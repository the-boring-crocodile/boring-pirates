"""Per-call overrides of a flavour (PF1): only for a flavour with allow_overrides.

An override chooses within the flavour, never beyond it: tools and services are
subsets of the flavour's, turns and output tokens stay under the Pirate's caps, and
the provider block is checked again by its folder. The model, the endpoint and the
credential are the Pirate's and cannot be overridden. The real boundary stays the
Pirate's grants at the Crocodile, its UID and its systemd sandbox."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Callable

from .limits import positive_integer
from .profiles import ProfileSpec

KEYS = {"system", "tools", "services", "max_turns", "settings"}
MAX_SYSTEM = 32 * 1024  # bytes of an overriding system text
DEFAULT_CAPS = {"max_turns": 60, "max_output_tokens": 8192}


@dataclass(frozen=True)
class Caps:
    """The Pirate's upper bounds for overrides (config.json "caps")."""
    max_turns: int = DEFAULT_CAPS["max_turns"]
    max_output_tokens: int = DEFAULT_CAPS["max_output_tokens"]

    @classmethod
    def parse(cls, raw: object) -> "Caps":
        if raw is None:
            return cls()
        if not isinstance(raw, dict) or set(raw) - set(DEFAULT_CAPS):
            raise ValueError(f"caps takes {sorted(DEFAULT_CAPS)}")
        return cls(**{key: positive_integer(raw.get(key, default), f"caps.{key}")
                      for key, default in DEFAULT_CAPS.items()})

    def public(self) -> dict[str, int]:
        return {"max_turns": self.max_turns, "max_output_tokens": self.max_output_tokens}


def _subset(raw: Any, allowed: frozenset[str], what: str) -> frozenset[str]:
    if (not isinstance(raw, list) or any(not isinstance(x, str) for x in raw)
            or len(set(raw)) != len(raw)):
        raise ValueError(f"overrides.{what} must be a list of distinct names")
    outside = sorted(set(raw) - allowed)
    if outside:
        raise ValueError(f"overrides.{what}: {outside} are not among this flavour's "
                         f"({sorted(allowed) or 'none'}); an override only narrows")
    return frozenset(raw)


def apply(spec: ProfileSpec, raw: object, caps: Caps,
          settings: Callable[[object, str], dict[str, Any]]) -> tuple[ProfileSpec, str | None]:
    """The effective flavour for one run and the system text replacing the handbook
    (None: the handbook). `settings`: the provider folder's settings(raw, flavour).
    Raises ValueError for an override this flavour cannot take (invalid_request)."""
    if not isinstance(raw, dict) or not raw:
        raise ValueError("overrides must be a nonempty object")
    unknown = sorted(set(raw) - KEYS)
    if unknown:
        raise ValueError(f"overrides: unknown keys {unknown}; known: {sorted(KEYS)}")
    changes: dict[str, Any] = {}
    system = raw.get("system")
    if "system" in raw and (not isinstance(system, str) or not system.strip()
                            or len(system.encode()) > MAX_SYSTEM):
        raise ValueError(f"overrides.system must be text of 1..{MAX_SYSTEM} bytes")
    if "tools" in raw:
        changes["tools"] = _subset(raw["tools"], spec.tools, "tools")
    if "services" in raw:
        changes["allowed_services"] = _subset(raw["services"], spec.allowed_services, "services")
    if "max_turns" in raw:
        turns = raw["max_turns"]
        if type(turns) is not int or not 1 <= turns <= caps.max_turns:
            raise ValueError(f"overrides.max_turns must be an integer of 1..{caps.max_turns} (the Pirate's cap)")
        changes["max_turns"] = turns
    if "settings" in raw:
        if not isinstance(raw["settings"], dict):
            raise ValueError("overrides.settings must be an object (the provider block's settings)")
        merged = settings({**spec.settings, **raw["settings"]}, spec.name)
        if merged.get("max_tokens", 0) > caps.max_output_tokens:
            raise ValueError(f"overrides.settings: max_tokens above the Pirate's cap of {caps.max_output_tokens}")
        changes["options"] = tuple(sorted(merged.items()))
    return dataclasses.replace(spec, **changes), system
