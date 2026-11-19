"""Install steps for Pirates (see boring_deploy.install): add a Pirate to a running Pond.

A Pirate is an instance (--name NAME): one host can have several, all running
the one code HOME (/opt/boring-pirates). What install makes for NAME:

    account     ACCOUNT (--account, default pirate-NAME): system, own group, nologin,
                home /home/ACCOUNT (0750) with work/ and handbook/ (INSTRUCTIONS.md,
                descriptor.json from the release; never overwritten)
    groups      pirate-clients (its socket's group; created when missing), ACCOUNT in
                it; --groups adds existing groups to its units
    state       /var/lib/boring-pirate-NAME (0700, ACCOUNT's)
    key         /etc/boring-pirates/NAME/credentials/PROVIDER-api-key (root, 0600):
                --key-file, or typed at the terminal (hidden); one already there is kept
    the Pond    `boring-crocodile-admin pirate-add NAME --account ACCOUNT --ponds ...`:
                the Crocodile's own plan and apply (site-check first); its answer
                gives config.json the controller service authority; the description owns admission
    config.json /etc/boring-pirates/NAME/config.json: "provider" at the top (--provider),
                one flavour, default (--model, the provider's default settings); the
                operator's from then on (more flavours and the provider's block go there)
    units       boring-pirate-task-NAME.socket, boring-pirate-task-NAME@.service,
                system-pirateNAME.slice, then boring-pirate@NAME.service last
                (with its config, what makes look say "installed"); rendered.json
                beside config.json records the account, the groups and the units
                (a later deploy renders them again, deploy.d/pirate_units.py)

Uninstall NAME: its units and its task slice stop, and nothing is removed
unless they are then inactive; the Crocodile takes it out (pirate-remove); with
--delete-data its home and state are sealed (root's, 0700) and then the
account goes (a seal that fails keeps the account); last its units go and
config.json becomes config.json.uninstalled. Its home, state and key stay and are
listed. A rerun after a failure on the way finishes the job.

Standard library only: it runs on the host's python3 from deploy.d/.
"""
import getpass
import grp
import json
import os
import pwd
import re
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from boring_install import Failure, Look, Setting, seal
from boring_deploy import root_input

import pirate_units
import steps
from pirate_units import ACCOUNT_PATTERN, GROUP_PATTERN, NAME_PATTERN, PROVIDERS, render, write as _write

INSTANCE = Setting("name", "the Pirate's name: lowercase letters and digits, from a letter",
                   pattern=NAME_PATTERN)
SETTINGS = [
    Setting("ponds", "the Ponds it serves: all, or names with commas", pattern=r"[a-z0-9,-]+"),
    Setting("account", "its Unix account (empty: pirate-NAME)", default="", pattern=f"({ACCOUNT_PATTERN})?"),
    Setting("display-name", "the name it introduces itself with (empty: NAME)", default="",
            pattern=r"[^\n@\"\\]{0,60}"),
    Setting("provider", "the model API: " + ", ".join(PROVIDERS), default="mistral", pattern="|".join(PROVIDERS)),
    Setting("model", "the model of its default flavour", default="mistral-small-2603", pattern=r"[A-Za-z0-9._:-]+"),
    Setting("endpoint", "OpenAI-compatible API base URL (required for openai_compat; empty for mistral)", default=""),
    Setting("key-file", "a file with the provider's API key (empty: typed at the terminal)", default=""),
    Setting("groups", "existing groups it also gets, with commas (empty: none)", default="",
            pattern=f"({GROUP_PATTERN}(,{GROUP_PATTERN})*)?"),
    Setting("lab", "also a flavour lab that takes per-call overrides: yes or no", default="no", pattern="yes|no"),
]
CAPS = {"max_turns": 60, "max_output_tokens": 8192}  # upper bounds for overrides (the code's defaults too)
CLIENTS = "pirate-clients"
CROCODILE = Path("/opt/boring-crocodile/current/bin/boring-crocodile-admin")
HOMES = Path("/home")
STATE = Path("/var/lib")


# -- names -------------------------------------------------------------------

def _account(ctx, settings=None):
    return (settings or {}).get("account") or f"pirate-{ctx.instance}"


def _paths(name):
    config = steps.CONFIG_DIR / name
    return {"config": config, "file": config / steps.CONFIG_NAME, "credentials": config / "credentials",
            "unit": f"boring-pirate@{name}.service", "socket": f"boring-pirate-task-{name}.socket",
            "task": f"boring-pirate-task-{name}@.service", "slice": f"system-pirate{name}.slice",
            "state": STATE / f"boring-pirate-{name}"}


def _user(name):
    try:
        return pwd.getpwnam(name)
    except KeyError:
        return None


def _home(account):
    user = _user(account)
    return Path(user.pw_dir) if user is not None else HOMES / account


HANDBOOK = ("INSTRUCTIONS.md", "descriptor.json")


def _refuse_links_in(home):
    """Root writes into the Pirate's home, which the Pirate controls: a symlink
    there (the home, work/, handbook/ or a handbook document) would carry those
    writes elsewhere. Found before anything changes, it stops the install."""
    links = [path for path in (home, home / "work", home / "handbook",
                               *(home / "handbook" / name for name in HANDBOOK)) if path.is_symlink()]
    if links:
        raise Failure("irregular, so nothing is changed: symlinks in the Pirate's home: "
                      + ", ".join(map(str, links)),
                      "install writes there as root and follows no links: replace them by what they point to")


def _refuse_retained_paths(name, account):
    """Never turn a removed account's sealed data into a new Pirate's data."""
    paths, user = _paths(name), _user(account)
    homes, saved = {_home(account)}, None
    record_path = paths["config"] / pirate_units.RECORD
    if os.path.lexists(record_path):
        saved = pirate_units.read_record(name).get("retained_account")
        if saved:
            homes.add(Path(saved["home"]))
    retained = [path for path in (*sorted(homes), paths["state"])
                if os.path.lexists(path) and (user is None or path.lstat().st_uid != user.pw_uid)]
    if user is None and (saved or os.path.lexists(paths["config"] / f"{steps.CONFIG_NAME}.uninstalled")):
        retained.append(paths["config"])
    if retained:
        if os.path.lexists(paths["config"]) and paths["config"] not in retained:
            retained.append(paths["config"])
        raise Failure("retained paths from an earlier Pirate; nothing is changed: "
                      + ", ".join(map(str, retained)),
                      "the operator must decide whether to move or delete these paths before installing again")


def _inventory(ctx):
    """Read the Crocodile-owned admin contract; never interpret its description."""
    hint = "deploy Crocodile from the matching release set; its admin list --json must include pond_workers UIDs"
    reply = ctx.run(str(CROCODILE), "list", "--json", check=False)
    if reply.returncode != 0:
        raise Failure("Crocodile admin inventory failed", hint)
    try:
        data = json.loads(reply.stdout)
    except (TypeError, ValueError):
        raise Failure("Crocodile admin inventory is not JSON", hint) from None
    if (not isinstance(data, dict) or set(data) != {"public_base_url", "ponds", "pirates", "pond_workers"}
            or not isinstance(data["public_base_url"], str) or not data["public_base_url"]
            or not isinstance(data["ponds"], list)
            or any(not isinstance(name, str) or not name for name in data["ponds"])
            or len(set(data["ponds"])) != len(data["ponds"])
            or not isinstance(data["pirates"], dict)
            or any(not isinstance(name, str) or not name or type(uid) is not int or uid <= 0
                   for name, uid in data["pirates"].items())
            or not isinstance(data["pond_workers"], dict)
            or any(not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", name)
                   or type(uid) is not int or uid <= 0 for name, uid in data["pond_workers"].items())
            or set(data["ponds"]) - set(data["pond_workers"])):
        raise Failure("Crocodile admin inventory has an invalid shape", hint)
    return data


def _callers(ctx):
    """The admitted Pirates (name -> uid), owned by the Crocodile."""
    return _inventory(ctx)["pirates"]


# -- reality -----------------------------------------------------------------

def look(ctx):
    found = {pirate.name: pirate for pirate in steps.pirates()}
    if ctx.instance is None:  # the component: a Pirate, or at least its code
        return Look(bool(found) or (ctx.home / "current").exists(),
                    notes=[f"Pirates here: {', '.join(found) or 'none'}"])
    name, irregular, notes = ctx.instance, [], []
    paths = _paths(name)
    if name in found:
        return Look(True, notes=[f"{name}: {found[name].unit} with {found[name].config}"])
    leftovers = [unit for unit in (paths["unit"], paths["socket"], paths["task"], paths["slice"])
                 if (steps.UNITS_DIR / unit).exists()]
    if leftovers:
        irregular.append(f"{name}: units without a Pirate (no {paths['file']}): {', '.join(leftovers)}")
    if paths["file"].exists():
        irregular.append(f"{paths['file']} exists without {paths['unit']}")
    callers = _callers(ctx)
    if name in callers:
        notes.append(f"the Crocodile admits {name} already (uid {callers[name]}): kept")
    for kept in (paths["credentials"], paths["state"], paths["config"] / f"{steps.CONFIG_NAME}.uninstalled"):
        if kept.exists():
            notes.append(f"{kept}: retained from an earlier install")
    return Look(False, irregular, notes,
                retained=(paths["config"] / f"{steps.CONFIG_NAME}.uninstalled").is_file())


def ready(ctx, settings):
    provider_endpoint(settings["provider"], settings.get("endpoint"))
    name = ctx.instance
    account = _account(ctx, settings)
    if not os.access(CROCODILE, os.X_OK):
        raise Failure(f"no Crocodile at {CROCODILE}", "a Pirate serves a running Pond: install the Crocodile first")
    if ctx.run(str(CROCODILE), "pirate-add", "--help", check=False).returncode != 0:
        raise Failure("this Crocodile cannot admit Pirates (pirate-add)", "deploy a newer Crocodile first")
    inventory = _inventory(ctx)
    ponds = settings["ponds"].split(",")
    unknown = [pond for pond in ponds if pond != "all" and pond not in inventory["ponds"]]
    if unknown:
        raise Failure("not a Pond of this site: " + ", ".join(unknown))
    callers = inventory["pirates"]
    user = _user(account)
    if name in callers and (user is None or callers[name] != user.pw_uid):
        raise Failure(f"the Crocodile admits another account as {name}", f"boring-crocodile-admin pirate-remove {name}")
    others = [pirate.name for pirate in steps.pirates() if _unit_user(ctx, pirate) == account]
    if user is not None and others:
        raise Failure(f"{account} is the account of the Pirate {others[0]} here")
    for group in filter(None, settings["groups"].split(",")):
        try:
            grp.getgrnam(group)
        except KeyError:
            raise Failure(f"no group {group}") from None
    key = _paths(name)["credentials"] / f"{settings['provider']}-api-key"
    if settings["key_file"]:
        with root_input(Path(settings["key_file"]).expanduser().absolute()):
            pass
    if settings["provider"] == "mistral" and not settings["key_file"] and not key.exists() and not sys.stdin.isatty():
        raise Failure("the API key: --key-file, or a terminal to type it at")
    _refuse_links_in(_home(account))
    _refuse_retained_paths(name, account)


def _unit_user(ctx, pirate):
    return ctx.run("systemctl", "show", "-p", "User", "--value", pirate.unit, check=False).stdout.strip()


# -- install -----------------------------------------------------------------

def plan(ctx, settings):
    endpoint = provider_endpoint(settings["provider"], settings.get("endpoint"))
    name, account = ctx.instance, _account(ctx, settings)
    paths = _paths(name)
    lines = [] if _user(account) else [f"create the system account {account} (own group, nologin, "
                                       f"home {HOMES / account} with work/ and handbook/)"]
    lines += [f"{CLIENTS}: {account} joins (the group is created if missing)",
              f"{paths['state']} ({account}'s, 0700)",
              f"the {settings['provider']} key in {paths['credentials']} "
              + ("from " + settings["key_file"] if settings["key_file"] else
                 "(kept if there, else local placeholder)" if settings["provider"] == "openai_compat" else
                 "(kept if there, else typed now)"),
              f"the Pond: boring-crocodile-admin pirate-add {name} --account {account} --ponds {settings['ponds']}"
              " (restarts the controller and every enabled worker)",
              f"{paths['file']}: flavour default, {settings['provider']} {settings['model']}",
              f"units {paths['socket']}, {paths['task']}, {paths['slice']}, {paths['unit']}"
              + (f" (also in groups {settings['groups']})" if settings["groups"] else ""),
              "then the deploy: every Pirate here restarts and answers its pings"]
    if endpoint is not None:
        lines.append(f"provider endpoint: {endpoint} (network access control is deployment-owned)")
    if settings["key_file"]:
        with root_input(Path(settings["key_file"]).expanduser().absolute()) as source:
            lines.extend(source.describe())
    return lines


def _directory(path, owner, mode):
    """path as a directory of owner's (made with mode when missing), changed through
    an O_NOFOLLOW descriptor: a symlink at path fails instead of being followed."""
    try:
        path.mkdir(mode=0o700)
        made = True
    except FileExistsError:
        made = False
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if made:
            os.fchmod(fd, mode)
        os.fchown(fd, owner.pw_uid, owner.pw_gid)
    finally:
        os.close(fd)


def _template(ctx, name, values):
    return render(ctx.release.directory, name, values)


def provider_endpoint(provider, value):
    """Validate installer input before host effects; runtime is not installed yet."""
    if provider == "mistral":
        if value not in (None, ""):
            raise Failure("mistral uses its own API; --endpoint is not accepted")
        return None
    if provider != "openai_compat":
        raise Failure("unknown provider")
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value):
        raise Failure("--endpoint: an HTTP(S) base URL is required for openai_compat")
    try:
        parsed = urlsplit(value)
        _ = parsed.port  # Parsing the hostname alone does not validate the port.
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment):
            raise ValueError("invalid base URL")
    except ValueError:
        raise Failure("--endpoint: HTTP(S) base URL, no credentials, query or fragment") from None
    return value.rstrip("/")


def site_document(directory, *, name, home, provider, model, authority, endpoint=None,
                  lab=False):
    """The Pirate's config.json: provider at the top, one fixed flavour (default) from
    the release's profile.json with the provider's default settings and its handbook;
    with `lab`, a flavour lab as well: the same model and tools, no handbook, and
    per-call overrides allowed (within the Pirate's caps)."""
    endpoint = provider_endpoint(provider, endpoint)
    home, paths = Path(home), _paths(name)
    profile = json.loads((Path(directory) / "install" / "profile.json").read_text())
    profile.update(model=model, handbook=str(home / "handbook" / "INSTRUCTIONS.md"))
    if provider == "openai_compat":
        profile[provider] = {"max_tokens": 2048, "reasoning_effort": "none"}
    profiles = {"default": profile}
    if lab:
        profiles["lab"] = {**profile, "handbook": None, "allow_overrides": True}
    document = {
        "provider": provider,
        "checkout": str(home / "work"),
        "descriptor_file": str(home / "handbook" / "descriptor.json"),
        "default_timeout_seconds": 120, "max_timeout_seconds": 600, "retention_seconds": 604800,
        "provider_request_timeout_seconds": 300,
        "execution": {"name": name, "default_cwd": str(home / "work"), "allowed_roots": [str(home / "work")],
                      "task_socket_path": f"/run/boring-pirate-tasks/{name}.sock",
                      "task_unit_prefix": f"boring-pirate-task-{name}",
                      "task_cgroup_parent": f"/system.slice/{paths['slice']}", "output_limit_bytes": 65536},
        "service_authority": authority,
        "caps": dict(CAPS),
        "profiles": profiles,
    }
    if endpoint is not None:
        document["provider_endpoint"] = endpoint
    return document


def _admit(ctx, name, account, ponds):
    """Let the Crocodile show and confirm its plan, then read the admitted settings."""
    command = [str(CROCODILE), "pirate-add", name, "--account", account, "--ponds", ponds]
    ctx.run(*command, "--apply", capture=False, interactive=True)
    # The interactive command owns stdout/stderr. A read-only rerun verifies the
    # admission and returns the controller authority without hiding the prompt.
    outcome = json.loads(ctx.run(*command).stdout)
    if outcome.get("status") != "already_serves":
        raise Failure(f"the Crocodile admission of {name} reports {outcome.get('status')}",
                      "boring-crocodile-admin site-check; status --check")
    ctx.log(f"the Pond admits {name}: serves {', '.join(outcome['ponds'])}")
    return outcome["service_authority"]


def install(ctx, settings):
    endpoint = provider_endpoint(settings["provider"], settings.get("endpoint"))
    name, account = ctx.instance, _account(ctx, settings)
    paths = _paths(name)
    _refuse_links_in(_home(account))  # again, just before the first change
    _refuse_retained_paths(name, account)
    supplied_key = None
    if settings["key_file"]:
        # Validate and read before changing accounts, directories or admission.
        with root_input(Path(settings["key_file"]).expanduser().absolute()) as source:
            supplied_key = source.read_text()
        if not supplied_key.strip() and not (paths["credentials"] / f"{settings['provider']}-api-key").exists():
            raise Failure("no API key given")
    ctx.home.mkdir(mode=0o755, parents=True, exist_ok=True)
    if _user(account) is None:
        ctx.run("useradd", "--system", "--user-group", "--create-home", "--home-dir", str(HOMES / account),
                "--shell", "/usr/sbin/nologin", "--comment", f"Boring Pirate {name}", account)
        ctx.log(f"created the account {account}")
    user = _user(account)
    home = Path(user.pw_dir)
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fchmod(fd, 0o750)
    finally:
        os.close(fd)
    try:
        grp.getgrnam(CLIENTS)
    except KeyError:
        ctx.run("groupadd", "--system", CLIENTS)
        ctx.log(f"created the group {CLIENTS}")
    if grp.getgrnam(CLIENTS).gr_gid not in os.getgrouplist(account, user.pw_gid):
        ctx.run("gpasswd", "-a", account, CLIENTS)
    display = settings["display_name"] or name
    values = {"NAME": name, "ACCOUNT": account, "DISPLAY": display, "HOME": str(home), "CODE": str(ctx.home),
              "PROVIDER": settings["provider"], "GROUPS": settings["groups"].replace(",", " ")}
    _directory(home / "work", user, 0o750)
    # A new handbook is root's and world-readable (the home, 0750, keeps others out;
    # the units run with Group=pirate-clients, not the account's own group): a fixed
    # flavour's rules cannot be changed by the Pirate's UID, whose units' sandbox
    # also keeps all but work/ read-only. An existing handbook folder is left as it is.
    keeper = SimpleNamespace(pw_uid=0, pw_gid=0)
    if not os.path.lexists(home / "handbook"):
        _directory(home / "handbook", keeper, 0o755)
    for document in HANDBOOK:
        if not os.path.lexists(home / "handbook" / document):
            _write(home / "handbook" / document, _template(ctx, document, values), 0o644, keeper)
    _directory(paths["state"], user, 0o700)
    paths["credentials"].mkdir(mode=0o700, parents=True, exist_ok=True)
    key = paths["credentials"] / f"{settings['provider']}-api-key"
    if not key.exists():
        secret = (supplied_key if supplied_key is not None
                  else "local" if settings["provider"] == "openai_compat"
                  else getpass.getpass(f"  {settings['provider']} API key for {name} (not shown): "))
        if not secret.strip():
            raise Failure("no API key given")
        _write(key, secret.strip() + "\n", 0o600)
        ctx.log(f"key stored in {key}")
    ctx.log(f"{home}: work/ and handbook/ ready; {paths['state']} ready")
    callers = _callers(ctx)
    if name in callers:
        if not paths["file"].exists():
            raise Failure(f"the Crocodile admits {name} already, but its {paths['file']} is missing",
                          f"boring-crocodile-admin pirate-remove {name}, then install again")
        ctx.log(f"the Pond admits {name} already")
    else:
        pond = _admit(ctx, name, account, settings["ponds"])
        site = site_document(ctx.release.directory, name=name, home=home,
                             provider=settings["provider"], model=settings["model"],
                             authority=pond, endpoint=endpoint,
                             lab=settings.get("lab") == "yes")
        _write(paths["file"], json.dumps(site, indent=2) + "\n", 0o644)
        ctx.log(f"wrote {paths['file']} (yours from now on: more flavours go there)")
    # The account and groups first (the deploy renders the units from them), with no
    # unit named yet; the units' hashes last, so a failure in between leaves them
    # unrecorded and the deploy finishes the reload, the socket restart and the record.
    groups = [g for g in settings["groups"].split(",") if g]
    pirate_units.write_record(name, {}, ctx.release.version, account, groups)
    written = {}
    for template, unit in pirate_units.unit_files(name):  # the Pirate's own unit last
        written[unit] = _template(ctx, template, values)
        _write(steps.UNITS_DIR / unit, written[unit], 0o644)
    ctx.run("systemctl", "daemon-reload")
    ctx.run("systemctl", "enable", "--now", paths["socket"])
    ctx.run("systemctl", "enable", paths["unit"])
    # What a later deploy may replace (it renders the units again with its templates).
    pirate_units.write_record(name, {unit: pirate_units.digest(text) for unit, text in written.items()},
                              ctx.release.version, account, groups)
    ctx.start.append(paths["unit"])  # an update leaves a stopped Pirate stopped; a new one is started
    ctx.log(f"units in place; {paths['unit']} starts with the deploy")


# -- uninstall ---------------------------------------------------------------

NOT_RUNNING = ("inactive", "failed")  # what `systemctl is-active` says of a unit that runs nothing


def _inactive(ctx, paths):
    running = {}
    for unit in (paths["unit"], paths["socket"], paths["slice"]):
        state = ctx.run("systemctl", "is-active", unit, check=False).stdout.strip()
        if state not in NOT_RUNNING:
            running[unit] = state or "unknown"
    if running:
        raise Failure("still running after the stop, so nothing is removed: "
                      + ", ".join(f"{unit} {state}" for unit, state in running.items()),
                      f"systemctl status {' '.join(running)}; once stopped, run uninstall again")


def _stop(ctx, paths):
    """Stop and verify the Pirate, its socket and all tasks before removal."""
    units = [paths["unit"], paths["socket"]]
    ctx.run("systemctl", "disable", "--now", *units)
    ctx.run("systemctl", "stop", paths["slice"], check=False)
    _inactive(ctx, paths)
    ctx.log(f"stopped: {', '.join(units)} and {paths['slice']} are inactive")


def _retained_user(ctx, paths):
    """Use retained identity; older removals can be identified by their owned home."""
    if os.path.lexists(paths["config"] / pirate_units.RECORD):
        record = pirate_units.read_record(ctx.instance)
        user = _user(record["account"])
        saved = record.get("retained_account")
        if user is not None and saved is not None and (
                user.pw_uid != saved["uid"] or user.pw_dir != saved["home"]):
            raise Failure("the retained Pirate account identity changed; nothing is removed")
        return user
    saved = paths["config"] / f"{steps.CONFIG_NAME}.uninstalled"
    try:
        raw = json.loads(pirate_units.read_nofollow(saved))
        checkout = raw["checkout"]
        if not isinstance(checkout, str) or not Path(checkout).is_absolute():
            raise ValueError("no absolute checkout")
        home = Path(checkout).parent
        info = home.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid == 0:
            raise ValueError("the retained home is not an account-owned directory")
        try:
            user = pwd.getpwuid(info.st_uid)
        except KeyError:
            return None
        if Path(user.pw_dir) != home:
            raise ValueError("the account's home differs from the retained configuration")
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise Failure(f"cannot identify the retained Pirate account: {error}",
                      "inspect the retained config, home and account; restore its rendered.json account record") from None
    return user


def uninstall(ctx, delete_data):
    """Keep data and account identity; --delete-data also finishes an earlier removal."""
    name = ctx.instance
    pirate = next((p for p in steps.pirates() if p.name == name), None)
    paths = _paths(name)
    target = paths["config"] / f"{steps.CONFIG_NAME}.uninstalled"
    if pirate is not None:
        account = _unit_user(ctx, pirate)
        _stop(ctx, paths)
        user = _user(account) if account else None
    else:
        if not delete_data or not target.is_file():
            raise Failure(f"no installed or retained Pirate {name}")
        user = _retained_user(ctx, paths)
        account = user.pw_name if user is not None else None
        if account and any(_unit_user(ctx, other) == account for other in steps.pirates()):
            raise Failure(f"account {account} belongs to another installed Pirate; nothing is removed")
        _inactive(ctx, paths)
    if delete_data or name in _callers(ctx):
        command = [str(CROCODILE), "pirate-remove", name]
        if delete_data:
            command.append("--retire")
        ctx.run(*command, "--apply", capture=False, interactive=True)
        ctx.log(f"the Pond no longer admits {name}"
                + ("; its service caller grants are revoked" if delete_data else ""))
    kept = []
    if user is not None:
        if user.pw_uid == 0:
            raise Failure("the Pirate account is root; it is not removed")
        home = Path(user.pw_dir)
        record_path = paths["config"] / pirate_units.RECORD
        record = pirate_units.read_record(name) if os.path.lexists(record_path) else {"units": {}, "groups": []}
        pirate_units.write_record(name, record["units"], ctx.release.version, account, record["groups"],
                                  retained_account={"uid": user.pw_uid, "home": user.pw_dir})
        if delete_data:
            seal(ctx, [home, paths["state"]], user.pw_uid)
            ctx.run("userdel", account)
            ctx.log(f"account {account} removed; what it owned is sealed (root's, 0700)")
        kept.append(str(home))
    for unit in (paths["task"], paths["slice"], paths["socket"], paths["unit"]):
        (steps.UNITS_DIR / unit).unlink(missing_ok=True)
    record = paths["config"] / pirate_units.RECORD
    if record.exists():
        kept.append(str(record))
    if pirate is not None:
        pirate.config.rename(target)
        ctx.run("systemctl", "daemon-reload")
    kept.insert(0, str(target))
    kept += [str(path) for path in (paths["state"], paths["credentials"]) if path.exists()]
    if not steps.pirates():
        kept.append(f"{ctx.home} (no Pirate left here: the code can go)")
    return kept
