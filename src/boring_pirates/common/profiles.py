"""Server-owned profiles; every profile with "active": true is a flavour.

A flavour's settings are ours (enforced here, the same for every provider) plus
one block named after the Pirate's provider, which its folder checks and uses."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .limits import MAX_CONTEXT, MAX_DIGEST, DEFAULT_MAX_TURNS, positive_integer
from .providers import KNOWN, provider as provider_folder
from .tools import NAMES as TOOLS

from boring_worker_kit.file_policy import FilePolicy
log = logging.getLogger("boring_pirates")
FLAVOUR = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
KEYS = {"active", "model", "workdir", "handbook", "allow_overrides",
        "allowed_tools", "max_turns", "files", "allowed_services",
        "max_service_calls", "write_reminder_after", "digest_file"}
CREDENTIALS = Path("/run/credentials")  # systemd's LoadCredential= directories, every unit's
# The file tools run inside the Pirate process, which holds the key: its own memory and
# environment are readable to it under /proc/<pid>/, so these are denied like the key.
KERNEL = (Path("/proc"), Path("/sys"), Path("/dev"))
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
    context_files: tuple[Path, ...]  # the handbook: one file, or none
    tools: frozenset[str]
    max_turns: int
    files: FilePolicy  # parse_profiles always sets one: the flavour's, or `work` at its workdir
    allowed_services: frozenset[str] = frozenset()
    options: tuple[tuple[str, object], ...] = ()  # the provider block as its folder checked it
    # Budgets the assignment loop enforces; None: no budget.
    max_service_calls: int | None = None  # service_call refused after this many in one assignment
    write_reminder_after: int | None = None  # one reminder after this many tool calls without a write
    # Short rules appended to the end of every call's last message, not kept in history.
    digest_file: Path | None = None
    allow_overrides: bool = False  # per-call overrides accepted (common/overrides.py)

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
                "max_service_calls": self.max_service_calls,
                "write_reminder_after": self.write_reminder_after,
                "digest_file": None if self.digest_file is None else str(self.digest_file),
                **({"allow_overrides": True} if self.allow_overrides else {}),  # a fixed flavour's text unchanged
                self.provider: self.settings}

    def context_identity(self) -> list[dict]:
        """The context files as they are now: paths and sha256, not contents."""
        return [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                for path in self.context_files + ((self.digest_file,) if self.digest_file else ())]

    def resolve(self, system: str | None = None) -> ResolvedProfile:
        """Read the handbook now; edits take effect for the next run. `system` (an
        override) replaces the handbook for this run."""
        if system is None:
            combined, sources = load_context(self.context_files)
        else:
            combined, sources = system, []
        if len(combined.encode()) > MAX_CONTEXT:
            raise ValueError("profile context exceeds limit")
        # The digest repeats the handbook's key rules; a system override replaces the handbook.
        digest, digest_sources = load_context((self.digest_file,) if self.digest_file and system is None else ())
        if len(digest.encode()) > MAX_DIGEST:
            raise ValueError("digest file exceeds limit")
        sources = sources + digest_sources
        identity = {**self.summary(), "context": sources,
                    "system_override_sha256": None if system is None else hashlib.sha256(system.encode()).hexdigest()}
        revision = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return ResolvedProfile(self, revision, combined, tuple(sources), digest.strip())


@dataclass(frozen=True)
class ResolvedProfile:
    spec: ProfileSpec
    revision: str
    context: str
    context_sources: tuple[dict, ...]
    digest: str = ""

    def public(self) -> dict:
        return {"name": self.spec.name, "revision": self.revision, **self.spec.summary(),
                "context": list(self.context_sources)}

    def system_message(self) -> str:
        """What the model is told first: the handbook (or the override) and the
        effective tool policy, so it sees what it really has."""
        return self.context + "\n\nConfigured tool policy: " + json.dumps(self.spec.summary(), ensure_ascii=True)


def default_files(workdir: Path) -> FilePolicy:
    """A flavour's file policy when it names none: one root `work` at its workdir."""
    return FilePolicy.parse({"roots": {"work": {"path": str(workdir), "read": True, "write": True}}})


def protect_roots(name: str, policy: FilePolicy, protected: tuple[Path, ...],
                  checkout: Path | None = None) -> FilePolicy:
    """The policy with every protected directory out of the model's reach: the key's,
    the history, the state, the systemd credentials, /proc, /sys and /dev.

    A root equal to or inside one is refused. A root that contains one gets it as a
    deny entry, added here so a configuration cannot forget it (the file tools walk
    paths below a root without following symlinks, so the denied subtree is the only
    way in). Paths are compared resolved, so a symlinked root counts where it points.

    The root's own path is resolved once, here; the folders on it must therefore be
    ones the Pirate cannot replace. Its units can write only in the checkout (the
    installer's ReadWritePaths= is the checkout; keep them the same), so a root
    strictly inside the checkout is refused: exec could swap a folder on its path
    for a link to /proc."""
    guarded = [path.resolve() for path in (*protected, CREDENTIALS, *KERNEL)]
    roots = {}
    for root_name, root in policy.roots.items():
        path = root.path.resolve()
        if checkout is not None and checkout.resolve() in path.parents:
            raise ValueError(f"profile {name}: file root {root_name} ({root.path}) lies inside the checkout, "
                             "whose folders the Pirate can replace; use the checkout itself (workdir \".\"), "
                             "or a root outside it")
        deny = list(root.deny)
        for directory in guarded:
            if path == directory or directory in path.parents:
                raise ValueError(f"profile {name}: file root {root_name} ({root.path}) is inside {directory}, "
                                 "which holds the Pirate's key, credentials, history or state, or the kernel's "
                                 "view of the Pirate process")
            if path in directory.parents:
                inside = directory.relative_to(path).parts
                if inside not in deny:
                    deny.append(inside)
        roots[root_name] = dataclasses.replace(root, deny=tuple(deny))
    return dataclasses.replace(policy, roots=roots)


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
        handbook = value.get("handbook")
        if handbook is not None and not isinstance(handbook, str):
            raise ValueError(f"profile {name}: handbook must be a path or null")
        files = () if handbook is None else (Path(handbook).resolve(strict=True),)
        if any(p == checkout or checkout in p.parents or not p.is_file() for p in files):
            raise ValueError(f"profile {name}: the handbook must be a file outside the checkout")
        raw_digest = value.get("digest_file")
        if raw_digest is not None and not isinstance(raw_digest, str):
            raise ValueError(f"profile {name}: invalid digest_file")
        digest_file = None if raw_digest is None else Path(raw_digest).resolve(strict=True)
        if digest_file is not None and (digest_file == checkout or checkout in digest_file.parents or not digest_file.is_file()):
            raise ValueError(f"profile {name}: digest file must be a file outside the checkout")
        allow_overrides = value.get("allow_overrides", False)
        if type(allow_overrides) is not bool:
            raise ValueError(f"profile {name}: allow_overrides must be true or false")
        tools = value.get("allowed_tools", [])  # an omitted list grants no tools
        if (not isinstance(tools, list) or any(not isinstance(t, str) for t in tools)
                or len(tools) != len(set(tools)) or not set(tools) <= TOOLS):
            raise ValueError(f"profile {name}: invalid allowed_tools")
        services = value.get("allowed_services", [])
        if not isinstance(services, list) or any(not isinstance(x, str) or not FLAVOUR.fullmatch(x) for x in services) or len(set(services)) != len(services):
            raise ValueError('invalid flavour service allowances')
        options = folder.settings(value.get(provider), name)
        budgets = {key: None if value.get(key) is None else positive_integer(value[key], f"profile {name}: {key}")
                   for key in ("max_service_calls", "write_reminder_after")}
        files_policy = FilePolicy.parse(value["files"]) if "files" in value else default_files(workdir)
        files_policy = protect_roots(name, files_policy, protected, checkout)
        specs[name] = ProfileSpec(
            name, value["active"], str(provider), model, workdir, files, frozenset(tools),
            positive_integer(value.get("max_turns", DEFAULT_MAX_TURNS), "max_turns"),
            files_policy, frozenset(services), tuple(sorted(options.items())), **budgets,
            digest_file=digest_file, allow_overrides=allow_overrides)
    if not any(spec.active for spec in specs.values()):
        raise ValueError("no profile is active")
    return specs
