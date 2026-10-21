"""Server-owned profiles; every profile with "active": true is a flavour.

A flavour's settings are ours (enforced here, the same for every provider) plus
one block named after the Pirate's provider, which its folder checks and uses."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .limits import MAX_CONTEXT, DEFAULT_MAX_TURNS, positive_integer
from .providers import KNOWN, provider as provider_folder
from .tools import NAMES as TOOLS

from boring_worker_kit.file_policy import FilePolicy
FLAVOUR = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
KEYS = {"active", "model", "workdir", "context_files", "instructions",
        "allowed_tools", "max_turns", "files", "allowed_services"}
CREDENTIALS = Path("/run/credentials")  # systemd's LoadCredential= directories, every unit's
LAYOUT = ('config.json names its provider at the top ("provider": "mistral"); a flavour carries the '
          'provider\'s settings in a block named after it ("mistral": {"max_tokens": ..., "temperature": ..., '
          '"reasoning_effort": ...}), not provider, max_output_tokens, temperature or reasoning_effort')


def load_context(paths: tuple[Path, ...]) -> tuple[str, list[dict]]:
    chunks = []
    sources = []
    for path in paths:
        # Site configuration, not model-supplied paths, outside the checkout.
        data = path.read_bytes()
        if len(data) > MAX_CONTEXT:
            raise ValueError("context file exceeds limit")
        chunks.append(data.decode("utf-8"))
        sources.append({"path": str(path), "sha256": hashlib.sha256(data).hexdigest()})
    return "\n\n".join(chunks), sources


@dataclass(frozen=True)
class ProfileSpec:
    name: str
    active: bool
    provider: str
    model: str
    workdir: Path
    context_files: tuple[Path, ...]
    instructions: str
    tools: frozenset[str]
    max_turns: int
    files: FilePolicy  # parse_profiles always sets one: the flavour's, or `work` at its workdir
    allowed_services: frozenset[str] = frozenset()
    options: tuple[tuple[str, object], ...] = ()  # the provider block as its folder checked it

    @property
    def settings(self) -> dict:
        return dict(self.options)

    def file_policy_identity(self) -> str:
        policy = {"roots": {name: {"path": str(root.path), "read": root.read, "write": root.write,
                                      "deny": root.deny} for name, root in self.files.roots.items()},
                  "limits": self.files.limits}
        return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()

    def summary(self) -> dict:
        return {"provider": self.provider, "model": self.model, "workdir": str(self.workdir),
                "allowed_tools": sorted(self.tools), "max_turns": self.max_turns,
                "files": self.files.public(),
                "file_policy_sha256": self.file_policy_identity(),
                "allowed_services": sorted(self.allowed_services),
                self.provider: self.settings}

    def context_identity(self) -> list[dict]:
        """The context files as they are now: paths and sha256, not contents."""
        return [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                for path in self.context_files]

    def resolve(self) -> ResolvedProfile:
        """Read the context files now; edits take effect for the next run."""
        context, sources = load_context(self.context_files)
        combined = "\n\n".join(part for part in (self.instructions, context) if part)
        if len(combined.encode()) > MAX_CONTEXT:
            raise ValueError("profile context exceeds limit")
        identity = {**self.summary(), "context": sources,
                    "instructions_sha256": hashlib.sha256(self.instructions.encode()).hexdigest()}
        revision = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return ResolvedProfile(self, revision, combined, tuple(sources))


@dataclass(frozen=True)
class ResolvedProfile:
    spec: ProfileSpec
    revision: str
    context: str
    context_sources: tuple[dict, ...]

    def public(self) -> dict:
        return {"name": self.spec.name, "revision": self.revision, **self.spec.summary(),
                "context": list(self.context_sources)}


def default_files(workdir: Path) -> FilePolicy:
    """A flavour's file policy when it names none: one root `work` at its workdir."""
    return FilePolicy.parse({"roots": {"work": {"path": str(workdir), "read": True, "write": True}}})


def refuse_protected_roots(name: str, policy: FilePolicy, protected: tuple[Path, ...]) -> None:
    """No file root may equal, contain or sit inside a protected directory (the key's,
    the history, the state, the systemd credentials): the model must not reach them.
    Paths are compared resolved, so a symlinked root counts where it points."""
    guarded = [path.resolve() for path in (*protected, CREDENTIALS)]
    for root_name, root in policy.roots.items():
        path = root.path.resolve()
        for directory in guarded:
            if path == directory or directory in path.parents or path in directory.parents:
                raise ValueError(f"profile {name}: file root {root_name} ({root.path}) overlaps {directory}, "
                                 "which holds the Pirate's key, credentials, history or state")


def parse_profiles(raw: object, checkout: Path, provider: object, *,
                   protected: tuple[Path, ...]) -> dict[str, ProfileSpec]:
    """The flavours of a Pirate serving `provider` (config.json's top-level name).

    `protected`: directories no flavour's file root may overlap (with CREDENTIALS)."""
    if provider is None:
        raise ValueError(LAYOUT)
    folder = provider_folder(provider)
    if not isinstance(raw, dict) or not raw:
        raise ValueError("profiles must be a nonempty object")
    specs = {}
    for name, value in raw.items():
        if not FLAVOUR.fullmatch(name):
            raise ValueError(f"profile name {name!r} must match [a-z0-9][a-z0-9-]{{0,62}}")
        if not isinstance(value, dict) or type(value.get("active")) is not bool:
            raise ValueError(f"profile {name}: \"active\" must be true or false")
        others = set(value) & (set(KNOWN) - {provider})
        if others:
            raise ValueError(f"profile {name}: this Pirate serves {provider}, not {sorted(others)}")
        if set(value) - KEYS - {provider}:
            raise ValueError(f"profile {name}: unknown settings {sorted(set(value) - KEYS - {provider})}")
        model = value.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ValueError(f"profile {name}: needs a model name")
        relative = value.get("workdir", ".")
        if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError(f"profile {name}: workdir must be relative to checkout")
        workdir = (checkout / relative).resolve(strict=True)
        if (workdir != checkout and checkout not in workdir.parents) or not workdir.is_dir():
            raise ValueError(f"profile {name}: workdir must be a directory inside checkout")
        raw_files = value.get("context_files", [])
        if not isinstance(raw_files, list) or any(not isinstance(p, str) for p in raw_files):
            raise ValueError(f"profile {name}: invalid context_files")
        files = tuple(Path(p).resolve(strict=True) for p in raw_files)
        if any(p == checkout or checkout in p.parents or not p.is_file() for p in files):
            raise ValueError(f"profile {name}: context files must be files outside the checkout")
        instructions = value.get("instructions", "")
        if not isinstance(instructions, str):
            raise ValueError(f"profile {name}: instructions must be text")
        tools = value.get("allowed_tools", [])  # an omitted list grants no tools
        if (not isinstance(tools, list) or any(not isinstance(t, str) for t in tools)
                or len(tools) != len(set(tools)) or not set(tools) <= TOOLS):
            raise ValueError(f"profile {name}: invalid allowed_tools")
        services = value.get("allowed_services", [])
        if not isinstance(services, list) or any(not isinstance(x, str) or not FLAVOUR.fullmatch(x) for x in services) or len(set(services)) != len(services):
            raise ValueError('invalid flavour service allowances')
        options = folder.settings(value.get(provider), name)
        files_policy = FilePolicy.parse(value["files"]) if "files" in value else default_files(workdir)
        refuse_protected_roots(name, files_policy, protected)
        specs[name] = ProfileSpec(
            name, value["active"], str(provider), model, workdir, files, instructions, frozenset(tools),
            positive_integer(value.get("max_turns", DEFAULT_MAX_TURNS), "max_turns"),
            files_policy, frozenset(services), tuple(sorted(options.items())))
    if not any(spec.active for spec in specs.values()):
        raise ValueError("no profile is active")
    return specs
