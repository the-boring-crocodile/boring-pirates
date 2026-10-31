"""The Pirates' systemd units: rendering, the record of what was written, and the
guarded re-render a deploy does (DECISIONS 2026-10-25, "The deploy re-renders").

One owner for the units' text: install (deploy.d/install_steps.py) and the deploy
(deploy.d/steps.py) both render with `render` and write with `write`, and both
leave /etc/boring-pirates/NAME/rendered.json, the record of what they wrote
(sha256 per unit file, and the release that wrote it).

A deploy renders every Pirate's four units with the NEW release's templates:

    unit equal to the new text, and      nothing to do
    recorded so
    unit equal to the record             replaced before the switch (backup kept)
    unit equal to the new text, not      the follow-ups are due: daemon-reload,
    recorded so (an earlier apply or     socket restart, then the record
    an install stopped part-way)
    unit absent                          written (there is nothing to lose)
    anything else (a hand edit, a        the deploy stops at its check, before any
    unit the record does not name, a     change, with the diff; the operator moves
    symlink)                             the edit into a drop-in and removes the
                                         unit file, which the deploy then writes

The values come from the host as it is: ACCOUNT and GROUPS from the record
(install writes them there first), HOME from the account, PROVIDER from
config.json, CODE from the deploy's home, each checked as install checks it.
Drop-ins (UNIT.d/) are the operator's and never touched; config.json neither.
The record is written last, after the reload and the socket restart, so a
rerun after any failure finishes them (apply_all).

Standard library only: it runs on the host's python3 from deploy.d/.
"""
import datetime
import difflib
import errno
import hashlib
import json
import os
import pwd
import re
from dataclasses import dataclass
from pathlib import Path

UNITS_DIR = Path("/etc/systemd/system")
CONFIG_DIR = Path("/etc/boring-pirates")
RECORD = "rendered.json"
TEMPLATES = (("boring-pirate-task.socket", "boring-pirate-task-{name}.socket"),
             ("boring-pirate-task@.service", "boring-pirate-task-{name}@.service"),
             ("system-pirate.slice", "system-pirate{name}.slice"),
             ("boring-pirate@.service", "boring-pirate@{name}.service"))  # the Pirate's own unit last
DIFF_LINES = 80  # of one unit's diff in a refusal or the check's log


def _failure(message, hint=None):
    import boring_deploy  # deploy.d/ has the engine; imported late, like steps.py does
    return boring_deploy.Failure(message, hint)


# -- text --------------------------------------------------------------------

def render(directory, name, values):
    """A template from the release's install/ folder, @KEY@ filled in."""
    text = (Path(directory) / "install" / name).read_text()
    for key, value in values.items():
        text = text.replace(f"@{key}@", value)
    return text


def unit_files(name):
    """(template, unit file name) of Pirate NAME, its own unit last."""
    return [(template, unit.format(name=name)) for template, unit in TEMPLATES]


def write(path, text, mode, owner=None):
    """Replace path with text, following no symlink: its directory and the new file
    are opened with O_NOFOLLOW, mode and owner are set on the open file, and the
    rename happens inside that directory (a link at path is replaced, not followed)."""
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        temporary = path.name + ".new"
        try:
            os.unlink(temporary, dir_fd=directory)  # a leftover of an interrupted write (a link: the link)
        except FileNotFoundError:
            pass
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                     dir_fd=directory)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            os.fchmod(fd, mode)
            if owner is not None:
                os.fchown(fd, owner.pw_uid, owner.pw_gid)
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        os.close(directory)


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def write_record(name, hashes, version, account, groups):
    """rendered.json: the Pirate's account and groups (install's choices), and which
    unit bytes the deploy (or install) may replace next time."""
    document = {"release": version, "account": account, "groups": list(groups), "units": dict(sorted(hashes.items()))}
    write(CONFIG_DIR / name / RECORD, json.dumps(document, indent=2) + "\n", 0o644)


def read_record(name):
    """The record ({"account", "groups", "units": {unit: sha256}}), checked. Missing,
    unparsable or without the account (a record of a release before 0.3.9) is a
    failure: without it the units cannot be rendered."""
    path = CONFIG_DIR / name / RECORD
    unit = UNITS_DIR / f"boring-pirate@{name}.service"
    fix = (f"write it: {{\"release\": \"hand\", \"account\": ..., \"groups\": [...], \"units\": {{...}}}}, "
           f"account and groups as in {unit}'s User= and SupplementaryGroups= (keep \"units\" if there; "
           "a unit it does not name is written only when absent)")
    try:
        text = read_nofollow(path)
        raw = None if text is None else json.loads(text)
    except (OSError, ValueError) as problem:
        raise _failure(f"{path} does not parse: {problem}", fix) from None
    if raw is None:
        raise _failure(f"{path} is missing", fix)
    if not isinstance(raw, dict) or "account" not in raw or "groups" not in raw:
        raise _failure(f"{path} names no account and groups (written before 0.3.9)", fix)
    units = raw.get("units")
    if not isinstance(units, dict) or any(not isinstance(v, str) for v in units.values()):
        raise _failure(f"{path}: no units object of names and hashes", fix)
    account, groups = raw["account"], raw["groups"]
    if not isinstance(account, str) or not re.fullmatch(ACCOUNT_PATTERN, account):
        _refuse(f"{path}: account", account, "an account name install accepts")
    if not isinstance(groups, list) or not all(isinstance(g, str) and re.fullmatch(GROUP_PATTERN, g) for g in groups):
        _refuse(f"{path}: groups", groups, "a list of group names")
    return raw


# -- the host's values ---------------------------------------------------------

# What install accepts (its settings use these), and so all a unit may say.
PROVIDERS = ("mistral", "openai_compat")
NAME_PATTERN = r"[a-z][a-z0-9]{0,15}"
ACCOUNT_PATTERN = r"[a-z_][a-z0-9_-]{0,31}"
GROUP_PATTERN = r"[a-z_][a-z0-9_-]*"
CONFIG_ROOT = "/etc/boring-pirates"  # the templates' literal path of CONFIG_DIR


def read_nofollow(path):
    """path's text, or None when absent; a symlink is refused, not followed."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as problem:
        if problem.errno == errno.ELOOP:
            raise _failure(f"{path} is a symlink", "the deploy writes unit files and follows no links: "
                           "replace it by the file it points to") from None
        raise
    with os.fdopen(fd) as stream:
        return stream.read()


def _refuse(what, value, rule):
    raise _failure(f"{what} {value!r} is not {rule}", "the deploy renders units only from values install accepts")


def values(name, code, record):
    """The template values of an installed Pirate, from its record, its account and
    its config.json (DISPLAY is not in any unit), each checked as install checks
    it: a value it would not accept is refused, never rendered."""
    if not re.fullmatch(NAME_PATTERN, name):
        _refuse("Pirate name", name, "lowercase letters and digits from a letter")
    account, groups = record["account"], " ".join(record["groups"])
    try:
        home = pwd.getpwnam(account).pw_dir
    except KeyError:
        raise _failure(f"{name}: no account {account}") from None
    if not home.startswith("/") or re.search(r"[\s@%]", home):
        _refuse(f"{account}'s home", home, "an absolute path without whitespace, @ or %")
    config = CONFIG_DIR / name / "config.json"
    try:
        provider = json.loads(read_nofollow(config) or "null")
        provider = provider.get("provider") if isinstance(provider, dict) else None
    except ValueError as problem:
        raise _failure(f"{config}: {problem}") from None
    if provider not in PROVIDERS:
        _refuse(f"{config}: provider", provider, "one of " + ", ".join(PROVIDERS))
    if not os.path.isabs(str(code)) or re.search(r"[\s@%]", str(code)):
        _refuse("the code home", str(code), "an absolute path without whitespace, @ or %")
    return {"NAME": name, "ACCOUNT": account, "HOME": home, "CODE": str(code), "PROVIDER": provider,
            "GROUPS": groups}


def _credential_present(name, rendered_unit):
    """The key file the new Pirate unit loads must be there, or the Pirate would not
    start after the switch. The template names it under /etc/boring-pirates
    (CONFIG_DIR's real place; mapped onto CONFIG_DIR for a test's tree)."""
    for match in re.finditer(r"^LoadCredential=[^:\n]*:(.*)$", rendered_unit, re.MULTILINE):
        path = Path(match.group(1).strip())
        if path.is_relative_to(CONFIG_ROOT):
            path = CONFIG_DIR / path.relative_to(CONFIG_ROOT)
        if not path.is_file():
            raise _failure(f"{name}: the rendered unit loads {match.group(1).strip()}, which is not there",
                           "put the provider's key there (root, 0600), or correct config.json's provider")


# -- the plan ------------------------------------------------------------------

@dataclass
class Unit:
    file: str
    current: str | None   # the text there now (None: absent)
    new: str              # what the release renders
    recorded: str | None  # the record's sha256 (None: no entry)

    @property
    def path(self):
        return UNITS_DIR / self.file

    @property
    def changed(self):
        return self.current != self.new

    @property
    def replaceable(self):
        """Absent (nothing to lose), or the bytes a deploy or install wrote."""
        return self.current is None or self.recorded == digest(self.current)

    @property
    def follow_up(self):
        """Already as rendered but not recorded so: a write whose daemon-reload,
        socket restart or record did not happen (or no record yet). Those are due."""
        return not self.changed and self.recorded != digest(self.new)

    def diff(self, limit=None):
        lines = list(difflib.unified_diff((self.current or "").splitlines(), self.new.splitlines(),
                                          f"{self.file} (now)", f"{self.file} (release)", lineterm="", n=1))
        if limit is not None and len(lines) > limit:
            lines = lines[:limit] + [f"... {len(lines) - limit} more lines"]
        return lines


@dataclass
class Plan:
    name: str
    version: str
    units: list[Unit]
    record: dict   # the record as the plan read it

    @property
    def replace(self):
        return [u for u in self.units if u.changed]

    @property
    def follow_ups(self):
        return [u for u in self.units if u.follow_up]

    @property
    def refused(self):
        """Units the deploy may not replace: changed, and not what was recorded."""
        return [u for u in self.units if u.changed and not u.replaceable]

    @property
    def due(self):
        return bool(self.replace or self.follow_ups)


def plan(name, directory, code, version):
    """Pirate NAME's units as the release at `directory` renders them, against the
    files and the record on the host. Read-only."""
    record = read_record(name)
    filled = values(name, code, record)
    units = []
    for template, file in unit_files(name):
        units.append(Unit(file, read_nofollow(UNITS_DIR / file), render(directory, template, filled),
                          record["units"].get(file)))
    _credential_present(name, units[-1].new)
    return Plan(name, version, units, record)


def refusal(plans):
    """The deploy's stop at its check: every refused unit's diff, then one Failure."""
    lines = []
    for p in plans:
        for unit in p.refused:
            why = ("not written by a deploy (the record does not name it)" if unit.recorded is None
                   else "changed since the last deploy")
            lines.append(f"{p.name}: {unit.file}: {why}")
            lines += ["    " + line for line in unit.diff(DIFF_LINES)]
    names = sorted({p.name for p in plans if p.refused})
    return lines, _failure(
        "the units of " + ", ".join(names) + " differ from what a deploy wrote, so nothing is changed",
        "hand edits belong in a drop-in (UNIT.d/*.conf; kept by every deploy): move the edit there, remove "
        "the unit file (the deploy writes a unit that is absent) and run the deploy again")


# -- apply -----------------------------------------------------------------------

def _backup_dir(name, now):
    """A new folder for the old files, unique even within one microsecond."""
    stamp = now.strftime("%Y%m%dT%H%M%S.%f")
    for suffix in ("", *(f"-{n}" for n in range(1, 100))):
        folder = CONFIG_DIR / name / f"units-backup-{stamp}{suffix}"
        try:
            folder.mkdir(mode=0o700)
            return folder
        except FileExistsError:
            continue
    raise _failure(f"no free backup folder name in {CONFIG_DIR / name}")


def verify(plans):
    """Every planned Pirate's units and record still as the check read them; a
    difference stops before anything is written."""
    for p in plans:
        for unit in p.units:
            if read_nofollow(unit.path) != unit.current:
                raise _failure(f"{unit.path} changed since the check, so nothing is changed",
                               "run the deploy again")
        if read_record(p.name) != p.record:
            raise _failure(f"{CONFIG_DIR / p.name / RECORD} changed since the check, so nothing is changed",
                           "run the deploy again")


def apply_all(plans, run, log, now=None, changed=None):
    """Bring every due Pirate's units to the release's text, in an order a rerun can
    finish: (1) all plans verified again, nothing written yet; (2) the old files
    backed up and the changed units written; (3) one daemon-reload; (4) each
    changed or unrecorded socket try-restarted (its listener keeps the old unit
    until then); (5) the records, last. A rerun after a failure finds the written
    units as rendered but not recorded, so it performs (3)-(5) for them.

    The Pirate's own unit is the switch's to restart (it restarts the Pirates that
    run); a task@ template applies to the next command; a slice's resource
    settings are applied by the daemon-reload. A failure after (1) names exactly
    what was done, and adds it to `changed` (boring-deploy 0.7.8+: ctx.changed),
    so the engine reports a host change instead of "nothing changed". With an
    older engine (`changed` None) its "nothing changed" means its release only."""
    due = [p for p in plans if p.due]
    if not due:
        return []
    for p in due:
        if p.refused:
            raise _failure(f"{p.name}: units not replaceable: " + ", ".join(u.file for u in p.refused))
    verify(due)
    now = now or datetime.datetime.now()
    done, backups = [], []

    def failed(problem, step):
        if changed is not None:
            changed.extend(done + [f"old units kept in {folder}" for folder in backups])
        raise _failure(
            f"rendering the units failed at {step}: {problem}. Done before it: "
            + ("; ".join(done) or "nothing") + ". The installed release is unchanged; these units are not",
            ("old files in " + ", ".join(map(str, backups)) + ". " if backups else "")
            + "Run the deploy again: it finds the written units as rendered and finishes the reload, the socket "
              "restart and the record. Or copy the old files back and systemctl daemon-reload")
    try:
        for p in due:
            kept = [unit for unit in p.replace if unit.current is not None]  # an absent unit has nothing to keep
            if kept:
                folder = _backup_dir(p.name, now)
                backups.append(folder)
                for unit in kept:
                    write(folder / unit.file, unit.current, 0o644)
            for unit in p.replace:
                write(unit.path, unit.new, 0o644)
                done.append(f"wrote {unit.path}")
                log(f"{p.name}: {unit.file} rendered by {p.version}")
    except Exception as problem:  # OSError, or a Failure from _backup_dir
        failed(problem, "writing")
    try:
        run("systemctl", "daemon-reload")
        done.append("systemctl daemon-reload")
        for p in due:
            for unit in (*p.replace, *p.follow_ups):
                if unit.file.endswith(".socket"):
                    run("systemctl", "try-restart", unit.file)
                    done.append(f"systemctl try-restart {unit.file}")
    except Exception as problem:
        failed(problem, "systemctl")
    try:
        for p in due:
            write_record(p.name, {u.file: digest(u.new) for u in p.units}, p.version, p.record["account"],
                         p.record["groups"])
            done.append(f"recorded {CONFIG_DIR / p.name / RECORD}")
    except OSError as problem:
        failed(problem, "the record")
    for p, folder in zip([p for p in due if any(u.current is not None for u in p.replace)], backups):
        log(f"{p.name}: old units kept in {folder}")
    for p in due:
        if not p.replace:
            log(f"{p.name}: units as the release renders them; reloaded and recorded")
    return backups
