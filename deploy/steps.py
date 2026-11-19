"""Deploy steps for Pirates (runtime venv; see boring_deploy).

One code HOME (/opt/boring-pirates) serves every Pirate on the host. The Pirates
are found on the host, never in a record: boring-pirate@NAME.service with
/etc/boring-pirates/NAME/config.json (deploy/install.py installs them).

Each runs HOME/current/bin/...; a deploy parses each configuration with the new
code as that Pirate, restarts the Pirates that run (the engine's switch decides
by `systemctl is-active`; a stopped Pirate stays stopped) and pings every active
flavour of each restarted Pirate as a live worker accepted by its runtime.

The units are rendered again from the new release's templates (pirate_units.py):
the check plans it and stops before any change when a unit is not what a deploy
wrote; the `units` hook, the engine's last step before the switch, writes the
changed units with a backup, so the switch's restart runs the Pirates on them.

`deploy --check` runs only the engine's step 1, without the new code's venv, so
it cannot parse the configuration as the release check does. It does report,
with the standard library, every shape an upgrade must change first
(preflight): all of them at once, the same list the deploy then starts
with, before anything is prepared.
"""
import json
from dataclasses import dataclass
from pathlib import Path

import pirate_units

UNITS_DIR = pirate_units.UNITS_DIR
CONFIG_DIR = pirate_units.CONFIG_DIR
CONFIG_NAME = "config.json"  # CONFIG_DIR/NAME/config.json: one Pirate's configuration
STATE_BASE = Path("/var/lib")  # where systemd puts a system unit's StateDirectory=
RUNTIME_BASE = Path("/run")  # and its RuntimeDirectory=
REGISTER_GLOB = "/run/boring-crocodile/guests/*/register.sock"
SOCKET_NAME = "pirate.sock"  # common/ipc.py's: the Pirate's socket in its RuntimeDirectory=
# Shapes earlier releases installed that this one refuses (CHANGELOG, Upgrade), and what replaces them.
OLD_CONFIG_KEYS = {"socket": "the unit's RuntimeDirectory=", "history_dir": "the unit's StateDirectory=",
                   "key_file": "the unit's LoadCredential=",
                   "client_uids": "Pirate/Pond admission in the Crocodile description"}
OLD_PROFILE_KEYS = {"context_files": "handbook (the one file it named)",
                    "instructions": "handbook"}  # gone since 0.3.5 (PF1)


@dataclass
class Pirate:
    name: str
    unit: str
    config: Path


def pirates():
    """Every Pirate on the host: its unit file and its configuration are both there."""
    found = []
    for config in sorted(CONFIG_DIR.glob(f"*/{CONFIG_NAME}")):
        unit = f"boring-pirate@{config.parent.name}.service"
        if (UNITS_DIR / unit).is_file():
            found.append(Pirate(config.parent.name, unit, config))
    return found


def _all(ctx):
    import boring_deploy
    found = pirates()
    if not found:
        raise boring_deploy.Failure("no Pirate on this host", "install one: deploy --install --name NAME")
    return found


def _show(ctx, pirate, name):
    return ctx.run("systemctl", "show", "-p", name, "--value", pirate.unit).stdout.strip()


def _user(ctx, pirate):
    import boring_deploy
    user = _show(ctx, pirate, "User")
    if not user:
        raise boring_deploy.Failure(f"{pirate.unit} has no User=")
    return user


def units(ctx):
    """Every Pirate's unit, running or not: the engine restarts those that run.

    Called by the engine right before the switch, it is also where the units the
    check planned are written (pirate_units.apply_all): after every check and the
    backup, before the restart that should run the Pirates on them. All plans
    are verified again before the first write; a failure after it stops the
    deploy before the switch and names what was written and the backups; with
    boring-deploy 0.7.8+ they also go to ctx.changed, which the engine reports
    instead of "nothing changed" (an older engine has no ctx.changed)."""
    found = _all(ctx)
    pirate_units.apply_all(getattr(ctx, "pirate_unit_plans", None) or [], ctx.run, ctx.log,
                           changed=getattr(ctx, "changed", None))
    return [pirate.unit for pirate in found]


def plan_units(ctx):
    """Each Pirate's units as the new release renders them (read-only); a unit a
    deploy did not write stops here, with its diff, before anything changes."""
    plans = [pirate_units.plan(pirate.name, ctx.release.directory, ctx.home, ctx.release.version)
             for pirate in _all(ctx)]
    lines, failure = pirate_units.refusal(plans)
    if any(plan.refused for plan in plans):
        for line in lines:
            ctx.log(line)
        raise failure
    for plan in plans:
        if plan.replace:
            ctx.log(f"{plan.name}: units to render again: " + ", ".join(u.file for u in plan.replace)
                    + " (written before the switch, old files kept)")
            for unit in plan.replace:
                for line in unit.diff(pirate_units.DIFF_LINES):
                    ctx.log("    " + line)
        elif plan.follow_ups:
            ctx.log(f"{plan.name}: units as the release renders them, not yet recorded so: reload, socket "
                    "restart and record before the switch")
    ctx.pirate_unit_plans = plans
    return plans


def _directory(ctx, pirate, setting, base):
    """The one directory the Pirate unit's StateDirectory= or RuntimeDirectory= makes."""
    import boring_deploy
    names = _show(ctx, pirate, setting).split()
    if len(names) != 1:
        raise boring_deploy.Failure(f"{pirate.unit}: {setting}= names {len(names)} directories, expected one")
    return base / names[0]


def socket(ctx, pirate):
    """Where the Pirate listens: SOCKET_NAME (common/ipc.py) in its runtime directory."""
    return _directory(ctx, pirate, "RuntimeDirectory", RUNTIME_BASE) / SOCKET_NAME


def _credentials(ctx, pirate):
    """The LoadCredential= the Pirate gets, from its unit file and drop-ins in
    systemd's order (systemctl does not show the values): lines accumulate, an
    empty one resets them. So the check loads exactly the key the Pirate gets."""
    import re
    import boring_deploy
    files = [UNITS_DIR / pirate.unit, *(Path(p) for p in _show(ctx, pirate, "DropInPaths").split())]
    found: list[str] = []
    for path in files:
        for line in path.read_text().splitlines():
            match = re.match(r"\s*LoadCredential\s*=\s*(.*?)\s*$", line)
            if match:
                found = found + [match.group(1)] if match.group(1) else []
    if not found:
        raise boring_deploy.Failure(f"{pirate.unit} has no LoadCredential=")
    return found


def _legacy_admission(ctx, pirate, uids):
    """Read-only replacement advice from the Crocodile's one identity inventory."""
    import boring_deploy
    from install_steps import _inventory
    prefix = f"{pirate.name}: {pirate.config}: client_uids refused; admission belongs in the Crocodile description"
    if not isinstance(uids, list) or any(type(uid) is not int or uid <= 0 for uid in uids):
        return [prefix, f"{pirate.name}: client_uids must be a list of positive integer UIDs"]
    if not uids:
        return [prefix, f"{pirate.name}: no pirate-add command is needed; remove the empty client_uids field"]
    try:
        inventory = _inventory(ctx)
    except boring_deploy.Failure as error:
        return [prefix, f"{pirate.name}: UID mapping unavailable: {error}; {error.hint}"]
    chosen, problems = set(), []
    for uid in sorted(set(uids)):
        names = sorted(name for name, worker_uid in inventory["pond_workers"].items() if worker_uid == uid)
        if len(names) != 1:
            problems.append(f"{pirate.name}: " + (f"unmapped UID {uid}" if not names
                            else f"ambiguous UID {uid}: " + ",".join(names)))
        else:
            chosen.add(names[0])
    if problems:
        return [prefix, *problems]
    command = f"boring-crocodile-admin pirate-add {pirate.name} --ponds " + ",".join(sorted(chosen))
    parked = chosen - set(inventory["ponds"])
    return [prefix, f"{pirate.name}: after Crocodile adoption, review and run: {command}",
            *([f"{pirate.name}: bring back parked Ponds first: " + ",".join(sorted(parked))] if parked else []),
            f"{pirate.name}: confirm the admission plan with --apply, then remove client_uids and retry; nothing applied"]


def preflight(ctx):
    """Every shape on the host this release refuses, as lines; read-only and standard
    library only (deploy --check has no venv of the new release). The release check
    remains the authority: it parses each configuration completely, as the Pirate."""
    import boring_deploy
    problems = []
    for pirate in pirates():
        try:
            raw = json.loads(pirate.config.read_text(encoding="utf-8"))
        except (OSError, ValueError) as problem:
            problems.append(f"{pirate.name}: {pirate.config} does not parse: {problem}")
            continue  # the units are rendered from it too
        if isinstance(raw, dict):
            if "client_uids" in raw:
                problems.extend(_legacy_admission(ctx, pirate, raw["client_uids"]))
            for key in sorted((set(raw) & set(OLD_CONFIG_KEYS)) - {"client_uids"}):
                problems.append(f"{pirate.name}: {pirate.config}: remove {key!r} (now {OLD_CONFIG_KEYS[key]})")
            profiles = raw.get("profiles")
            for flavour, value in sorted(profiles.items() if isinstance(profiles, dict) else []):
                for key in sorted(set(value) & set(OLD_PROFILE_KEYS) if isinstance(value, dict) else []):
                    problems.append(f"{pirate.name}: flavour {flavour}: replace {key!r} with {OLD_PROFILE_KEYS[key]}")
        try:
            plan = pirate_units.plan(pirate.name, ctx.release.directory, ctx.home, ctx.release.version)
        except boring_deploy.Failure as failure:
            problems.append(f"{pirate.name}: {failure}" + (f" ({failure.hint})" if failure.hint else ""))
            continue
        for unit in plan.refused:
            why = ("not named by the record" if unit.recorded is None else "changed since the last deploy")
            problems.append(f"{pirate.name}: {unit.file}: {why}: {pirate_units.refusal_hint([unit])}")
    return problems


def data_generation(ctx):
    """Pirates keep no versioned data; upgrade diagnostics belong to preflight."""
    return None


def _registration_clients():
    """Live socket owners are probe candidates, never an admission roster."""
    import glob
    import os
    import pwd
    import stat
    accounts = set()
    for path in glob.glob(REGISTER_GLOB):
        try:
            info = os.lstat(path)
            if stat.S_ISSOCK(info.st_mode) and info.st_uid > 0 and stat.S_IMODE(info.st_mode) == 0o660:
                accounts.add(pwd.getpwuid(info.st_uid).pw_name)
        except (OSError, KeyError):
            continue
    return sorted(accounts)


def check(ctx):
    """The new code checks each Pirate's configuration as that Pirate, in a unit of its
    own with the Pirate's user, state directory and key (`boring-pirate check-config`,
    what `serve` reads before it listens): context and digest files, descriptor,
    history and file roots are read, no file root may reach the key's directory, the
    history, the state or /run/credentials, and the key must be there and not empty.
    The running Pirate is not touched."""
    import boring_deploy
    found, problems = _all(ctx), []
    for pirate in found:
        try:
            raw = json.loads(pirate.config.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None  # The runtime parser below owns all other configuration errors.
        if isinstance(raw, dict) and "client_uids" in raw:
            problems.extend(_legacy_admission(ctx, pirate, raw["client_uids"]))
    if problems:
        raise boring_deploy.Failure("\n".join(problems))
    python = str(ctx.new / "app" / "bin" / "python3")
    for pirate in found:
        # The Pirate unit's identity, groups, working directory and key. Not its
        # StateDirectory= or RuntimeDirectory=: on a unit of its own systemd would
        # apply that unit's mode to the live state directory, and remove the live
        # runtime directory (the Pirate's socket) when the check ends. The check
        # needs only their paths, as systemd names them to the Pirate.
        properties = [f"User={_user(ctx, pirate)}", f"Group={_show(ctx, pirate, 'Group') or 'pirate-clients'}",
                      f"Environment=STATE_DIRECTORY={_directory(ctx, pirate, 'StateDirectory', STATE_BASE)}",
                      f"Environment=RUNTIME_DIRECTORY={_directory(ctx, pirate, 'RuntimeDirectory', RUNTIME_BASE)}"]
        for name in ("SupplementaryGroups", "WorkingDirectory"):
            value = _show(ctx, pirate, name)
            if value:
                properties.append(f"{name}={value}")
        properties += [f"LoadCredential={credential}" for credential in _credentials(ctx, pirate)]
        properties += ["NoNewPrivileges=yes", "PrivateTmp=yes", "ProtectSystem=strict", "ProtectHome=read-only"]
        command = ["systemd-run", "--wait", "--pipe", "--collect",
                   *(part for value in properties for part in ("-p", value)),
                   python, "-I", "-m", "boring_pirates.common.ipc", "check-config", "--config", str(pirate.config)]
        result = ctx.run(*command, check=False)
        try:
            report = json.loads(result.stdout)
        except ValueError:
            report = {"ok": False, "error": (result.stderr or result.stdout).strip()[-400:]}
        if result.returncode != 0 or report.get("ok") is not True:
            import boring_deploy
            reason = (str(report.get("error") or "").strip() or result.stderr.strip()
                      or f"systemd-run exited {result.returncode} without a configuration result")
            raise boring_deploy.Failure(f"{pirate.name}: configuration refused: {reason}")
        ctx.log(f"{pirate.name}: configuration parses; key {report['credential']}; "
                f"active flavours: {', '.join(sorted(report['flavours']))}")
    plan_units(ctx)


def smoke(ctx):
    """Ping active flavours as live worker identities; only runtime admission
    can make a ping succeed. Stopped Pirates remain stopped. Allow a minute
    for restart and registration, without creating or remembering a UID grant."""
    import time
    import boring_deploy
    pirate_command = str(ctx.home / "current" / "bin" / "boring-pirate")
    deadline = time.monotonic() + 60
    stopped = set(getattr(ctx, "stopped", ()))
    found = _all(ctx)
    running = [pirate for pirate in found if pirate.unit not in stopped]
    for pirate in found:
        if pirate.unit in stopped:
            ctx.log(f"{pirate.name}: stopped before the update, so not started and not pinged")
    if not running:
        ctx.log("every Pirate is stopped: not started, nothing to ping")
        return
    for pirate in running:
        raw = json.loads(pirate.config.read_text())
        flavours = sorted(n for n, p in raw["profiles"].items() if p.get("active"))
        listening = socket(ctx, pirate)
        clients = _registration_clients()
        if not clients:
            raise boring_deploy.Failure(f"{pirate.name}: no live Pond registration socket; cannot smoke-test",
                                       "start an admitted Pond, then deploy again")
        answered = set()
        for flavour in flavours:
            while True:
                errors = []
                for client in clients:
                    answer = ctx.run(pirate_command, "ping", "--socket", str(listening), "--flavour", flavour,
                                     user=client, check=False)
                    if answer.returncode == 0:
                        answered.add(client)
                        break
                    errors.append(f"{client}: {(answer.stderr or answer.stdout).strip()[-300:]}")
                else:
                    if time.monotonic() > deadline:
                        raise boring_deploy.Failure(f"{pirate.name}: flavour {flavour} has no accepted worker: "
                                                   + "; ".join(errors),
                                                   f"journalctl -u {pirate.unit} -n 50; check description admission")
                    time.sleep(2)
                    clients = _registration_clients()
                    continue
                break
        ctx.log(f"{pirate.name}: {len(flavours)} flavours answer {', '.join(sorted(answered))}: {', '.join(flavours)}")
