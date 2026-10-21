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
                gives config.json the Pond workers' uids and the service authority
    config.json /etc/boring-pirates/NAME/config.json: "provider" at the top (--provider),
                one flavour, default (--model, the provider's default settings); the
                operator's from then on (more flavours and the provider's block go there)
    units       boring-pirate-task-NAME.socket, boring-pirate-task-NAME@.service,
                system-pirateNAME.slice, then boring-pirate@NAME.service last
                (with its config, what makes look say "installed")

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
import sys
import tomllib
from pathlib import Path

from boring_install import Failure, Look, Setting, seal

import steps

INSTANCE = Setting("name", "the Pirate's name: lowercase letters and digits, from a letter",
                   pattern=r"[a-z][a-z0-9]{0,15}")
PROVIDERS = ("mistral",)
SETTINGS = [
    Setting("ponds", "the Ponds it serves: all, or names with commas", pattern=r"[a-z0-9,-]+"),
    Setting("account", "its Unix account (empty: pirate-NAME)", default="", pattern=r"([a-z_][a-z0-9_-]{0,31})?"),
    Setting("display-name", "the name it introduces itself with (empty: NAME)", default="",
            pattern=r"[^\n@\"\\]{0,60}"),
    Setting("provider", "the model API: " + ", ".join(PROVIDERS), default="mistral", pattern="|".join(PROVIDERS)),
    Setting("model", "the model of its default flavour", default="mistral-small-2603", pattern=r"[A-Za-z0-9._:-]+"),
    Setting("key-file", "a file with the provider's API key (empty: typed at the terminal)", default=""),
    Setting("groups", "existing groups it also gets, with commas (empty: none)", default="",
            pattern=r"([a-z_][a-z0-9_-]*(,[a-z_][a-z0-9_-]*)*)?"),
]
CLIENTS = "pirate-clients"
CROCODILE = Path("/opt/boring-crocodile/current/bin/boring-crocodile-admin")
CROCODILE_CONFIG = Path("/etc/boring-crocodile/config.toml")
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


def _callers():
    """The Crocodile's Pirates (name -> uid), from its config as it is."""
    try:
        raw = tomllib.loads(CROCODILE_CONFIG.read_text())
    except (OSError, ValueError):
        return {}
    return dict(raw.get("pirate_services", {}).get("callers", {}))


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
    callers = _callers()
    if name in callers:
        notes.append(f"the Crocodile admits {name} already (uid {callers[name]}): kept")
    for kept in (paths["credentials"], paths["state"], paths["config"] / f"{steps.CONFIG_NAME}.uninstalled"):
        if kept.exists():
            notes.append(f"{kept} from an earlier install: " + ("left as it is" if kept.suffix else "reused"))
    return Look(False, irregular, notes)


def ready(ctx, settings):
    name = ctx.instance
    account = _account(ctx, settings)
    if not os.access(CROCODILE, os.X_OK):
        raise Failure(f"no Crocodile at {CROCODILE}", "a Pirate serves a running Pond: install the Crocodile first")
    if ctx.run(str(CROCODILE), "pirate-add", "--help", check=False).returncode != 0:
        raise Failure("this Crocodile cannot admit Pirates (pirate-add)", "deploy a newer Crocodile first")
    raw = tomllib.loads(CROCODILE_CONFIG.read_text())
    ponds = settings["ponds"].split(",")
    unknown = [pond for pond in ponds if pond != "all" and pond not in raw.get("ponds", {})]
    if unknown:
        raise Failure("not a Pond of this site: " + ", ".join(unknown))
    callers = _callers()
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
    if settings["key_file"] and not Path(settings["key_file"]).is_file():
        raise Failure(f"--key-file: {settings['key_file']} is not a file")
    if not settings["key_file"] and not key.exists() and not sys.stdin.isatty():
        raise Failure("the API key: --key-file, or a terminal to type it at")
    _refuse_links_in(_home(account))


def _unit_user(ctx, pirate):
    return ctx.run("systemctl", "show", "-p", "User", "--value", pirate.unit, check=False).stdout.strip()


# -- install -----------------------------------------------------------------

def plan(ctx, settings):
    name, account = ctx.instance, _account(ctx, settings)
    paths = _paths(name)
    lines = [] if _user(account) else [f"create the system account {account} (own group, nologin, "
                                       f"home {HOMES / account} with work/ and handbook/)"]
    lines += [f"{CLIENTS}: {account} joins (the group is created if missing)",
              f"{paths['state']} ({account}'s, 0700)",
              f"the {settings['provider']} key in {paths['credentials']} "
              + ("from " + settings["key_file"] if settings["key_file"] else "(kept if there, else typed now)"),
              f"the Pond: boring-crocodile-admin pirate-add {name} --account {account} --ponds {settings['ponds']}"
              " (restarts the controller and any worker that joins pirate-clients)",
              f"{paths['file']}: flavour default, {settings['provider']} {settings['model']}",
              f"units {paths['socket']}, {paths['task']}, {paths['slice']}, {paths['unit']}"
              + (f" (also in groups {settings['groups']})" if settings["groups"] else ""),
              "then the deploy: every Pirate here restarts and answers its pings"]
    return lines


def _write(path, text, mode, owner=None):
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


def render(directory, name, values):
    """A template from the release's install/ folder, @KEY@ filled in."""
    text = (Path(directory) / "install" / name).read_text()
    for key, value in values.items():
        text = text.replace(f"@{key}@", value)
    return text


def _template(ctx, name, values):
    return render(ctx.release.directory, name, values)


def site_document(directory, *, name, home, state, provider, model, client_uids, authority):
    """The Pirate's config.json: provider at the top, one flavour (default) from the
    release's profile.json with the provider's default settings."""
    home, paths = Path(home), _paths(name)
    profile = json.loads((Path(directory) / "install" / "profile.json").read_text())
    profile.update(model=model, context_files=[str(home / "handbook" / "INSTRUCTIONS.md")])
    return {
        "provider": provider,
        "socket": f"/run/boring-pirate-{name}/pirate.sock",
        "checkout": str(home / "work"),
        "history_dir": str(state),
        "key_file": f"/run/credentials/{paths['unit']}/{provider}-api-key",
        "client_uids": client_uids,
        "descriptor_file": str(home / "handbook" / "descriptor.json"),
        "default_timeout_seconds": 120, "max_timeout_seconds": 600, "retention_seconds": 604800,
        "provider_request_timeout_seconds": 300,
        "execution": {"name": name, "default_cwd": str(home / "work"), "allowed_roots": [str(home / "work")],
                      "task_socket_path": f"/run/boring-pirate-tasks/{name}.sock",
                      "task_unit_prefix": f"boring-pirate-task-{name}",
                      "task_cgroup_parent": f"/system.slice/{paths['slice']}", "output_limit_bytes": 65536},
        "service_authority": authority,
        "profiles": {"default": profile},
    }


def _admit(ctx, name, account, ponds):
    """The Crocodile's own plan, then its apply of exactly that plan."""
    command = [str(CROCODILE), "pirate-add", name, "--account", account, "--ponds", ponds]
    planned = json.loads(ctx.run(*command).stdout)
    outcome = json.loads(ctx.run(*command, "--apply", "--approve-digest", planned["plan_digest"]).stdout)
    if outcome.get("status") != "admitted":
        raise Failure(f"the Crocodile admitted {name} but reports {outcome.get('status')}",
                      "boring-crocodile-admin site-check; status --check")
    ctx.log(f"the Pond admits {name}: serves {', '.join(planned['ponds'])}")
    return outcome["for_site_json"]  # the key of the Crocodile's answer (an interface: kept)


def install(ctx, settings):
    name, account = ctx.instance, _account(ctx, settings)
    paths = _paths(name)
    _refuse_links_in(_home(account))  # again, just before the first change
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
    for directory in (home / "work", home / "handbook"):
        _directory(directory, user, 0o750)
    for document in HANDBOOK:
        if not os.path.lexists(home / "handbook" / document):
            _write(home / "handbook" / document, _template(ctx, document, values), 0o640, user)
    _directory(paths["state"], user, 0o700)
    paths["credentials"].mkdir(mode=0o700, parents=True, exist_ok=True)
    key = paths["credentials"] / f"{settings['provider']}-api-key"
    if not key.exists():
        secret = (Path(settings["key_file"]).read_text() if settings["key_file"]
                  else getpass.getpass(f"  {settings['provider']} API key for {name} (not shown): "))
        if not secret.strip():
            raise Failure("no API key given")
        _write(key, secret.strip() + "\n", 0o600)
        ctx.log(f"key stored in {key}")
    ctx.log(f"{home}: work/ and handbook/ ready; {paths['state']} ready")
    callers = _callers()
    if name in callers:
        if not paths["file"].exists():
            raise Failure(f"the Crocodile admits {name} already, but its {paths['file']} is missing",
                          f"boring-crocodile-admin pirate-remove {name}, then install again")
        ctx.log(f"the Pond admits {name} already")
    else:
        pond = _admit(ctx, name, account, settings["ponds"])
        site = site_document(ctx.release.directory, name=name, home=home, state=paths["state"],
                             provider=settings["provider"], model=settings["model"],
                             client_uids=pond["client_uids"], authority=pond["service_authority"])
        _write(paths["file"], json.dumps(site, indent=2) + "\n", 0o644)
        ctx.log(f"wrote {paths['file']} (yours from now on: more flavours go there)")
    for template, unit in (("boring-pirate-task.socket", paths["socket"]),
                           ("boring-pirate-task@.service", paths["task"]),
                           ("system-pirate.slice", paths["slice"]),
                           ("boring-pirate@.service", paths["unit"])):  # the Pirate's own unit last
        _write(steps.UNITS_DIR / unit, _template(ctx, template, values), 0o644)
    ctx.run("systemctl", "daemon-reload")
    ctx.run("systemctl", "enable", "--now", paths["socket"])
    ctx.run("systemctl", "enable", paths["unit"])
    ctx.start.append(paths["unit"])  # an update leaves a stopped Pirate stopped; a new one is started
    ctx.log(f"units in place; {paths['unit']} starts with the deploy")


# -- uninstall ---------------------------------------------------------------

NOT_RUNNING = ("inactive", "failed")  # what `systemctl is-active` says of a unit that runs nothing


def _stop(ctx, paths):
    """The Pirate, its socket and its task slice (every task it runs) stopped,
    and checked: a failed stop, or a unit that still runs, stops the uninstall
    before anything is removed."""
    units = [paths["unit"], paths["socket"]]
    ctx.run("systemctl", "disable", "--now", *units)
    ctx.run("systemctl", "stop", paths["slice"], check=False)  # verified below, like the others
    running = {}
    for unit in (*units, paths["slice"]):
        state = ctx.run("systemctl", "is-active", unit, check=False).stdout.strip()
        if state not in NOT_RUNNING:
            running[unit] = state or "unknown"
    if running:
        raise Failure("still running after the stop, so nothing is removed: "
                      + ", ".join(f"{unit} {state}" for unit, state in running.items()),
                      f"systemctl status {' '.join(running)}; once stopped, run uninstall again")
    ctx.log(f"stopped: {', '.join(units)} and {paths['slice']} are inactive")


def uninstall(ctx, delete_data):
    """Stop (verified), out of the Pond, with delete_data seal and remove the
    account, then the units and config.json: those two make look say installed,
    so they go last and a rerun after a failure before them finishes the job."""
    name = ctx.instance
    (pirate,) = [p for p in steps.pirates() if p.name == name]
    paths = _paths(name)
    account = _unit_user(ctx, pirate)
    _stop(ctx, paths)
    if name in _callers():
        command = [str(CROCODILE), "pirate-remove", name]
        planned = json.loads(ctx.run(*command).stdout)
        ctx.run(*command, "--apply", "--approve-digest", planned["plan_digest"])
        ctx.log(f"the Pond no longer admits {name}")
    user = _user(account) if account else None
    kept = []
    if delete_data and user is not None:
        home = Path(user.pw_dir)
        seal(ctx, [home, paths["state"]], user.pw_uid)  # raises: the account stays, for a rerun
        ctx.run("userdel", account)
        ctx.log(f"account {account} removed; what it owned is sealed (root's, 0700)")
        kept.append(str(home))
    elif user is not None:
        kept.append(user.pw_dir)
    for unit in (paths["task"], paths["slice"], paths["socket"], paths["unit"]):
        (steps.UNITS_DIR / unit).unlink(missing_ok=True)
    target = pirate.config.with_name(pirate.config.name + ".uninstalled")
    pirate.config.rename(target)
    ctx.run("systemctl", "daemon-reload")
    kept.insert(0, str(target))
    kept += [str(path) for path in (paths["state"], paths["credentials"]) if path.exists()]
    if not steps.pirates():
        kept.append(f"{ctx.home} (no Pirate left here: the code can go)")
    return kept
