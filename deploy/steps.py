"""Deploy steps for Pirates (runtime venv; see boring_deploy).

One code HOME (/opt/boring-pirates) serves every Pirate on the host. The Pirates
are found on the host, never in a record: boring-pirate@NAME.service with
/etc/boring-pirates/NAME/config.json (deploy/install.py installs them).

Each runs HOME/current/bin/...; a deploy parses each configuration with the new
code as that Pirate, restarts the Pirates that run (the engine's switch decides
by `systemctl is-active`; a stopped Pirate stays stopped) and pings every active
flavour of each restarted Pirate as one of its clients.
"""
import json
from dataclasses import dataclass
from pathlib import Path

UNITS_DIR = Path("/etc/systemd/system")
CONFIG_DIR = Path("/etc/boring-pirates")
CONFIG_NAME = "config.json"  # CONFIG_DIR/NAME/config.json: one Pirate's configuration
STATE_BASE = Path("/var/lib")  # where systemd puts a system unit's StateDirectory=
# The release check: the new code's own settings(), the one `serve` starts from, run
# as the Pirate with the unit's state directories (argv[2], as $STATE_DIRECTORY).
PROBE = ("import os, sys; from pathlib import Path; from boring_pirates.common.ipc import settings; "
         "os.environ['STATE_DIRECTORY'] = sys.argv[2]; "
         "config = settings(Path(sys.argv[1]), credential_loaded=False); "
         "print(' '.join(sorted(n for n, s in config.profiles.items() if s.active)))")


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


def _user(ctx, pirate):
    import boring_deploy
    user = ctx.run("systemctl", "show", "-p", "User", "--value", pirate.unit).stdout.strip()
    if not user:
        raise boring_deploy.Failure(f"{pirate.unit} has no User=")
    return user


def _state_directories(ctx, pirate):
    """$STATE_DIRECTORY as systemd sets it for the unit: its StateDirectory= under /var/lib."""
    names = ctx.run("systemctl", "show", "-p", "StateDirectory", "--value", pirate.unit).stdout.split()
    return ":".join(str(STATE_BASE / name) for name in names)


def units(ctx):
    """Every Pirate's unit, running or not: the engine restarts those that run."""
    return [pirate.unit for pirate in _all(ctx)]


def check(ctx):
    """The new code validates each Pirate's configuration as that Pirate, with the
    settings() its service starts from (PROBE): context files, descriptor, history
    and file roots are read, and no file root may reach the key's directory, the
    history, the state or /run/credentials. The provider credential exists only
    inside the unit, so the key itself is proven by step 5."""
    python = str(ctx.new / "app" / "bin" / "python3")
    for pirate in _all(ctx):
        flavours = ctx.run(python, "-I", "-c", PROBE, str(pirate.config), _state_directories(ctx, pirate),
                           user=_user(ctx, pirate)).stdout.split()
        ctx.log(f"{pirate.name}: configuration parses; active flavours: {', '.join(flavours)}")


def smoke(ctx):
    """Every active flavour of every running Pirate answers its ping through that
    Pirate's socket, asked as one of its clients (the Pond workers; root is not a
    client). A Pirate the switch left stopped (ctx.stopped) is not pinged; with
    all stopped the engine skips the smoke, and so does this.
    A unit is active before its socket exists: wait for it, up to a minute."""
    import pwd
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
        client = pwd.getpwuid(raw["client_uids"][0]).pw_name
        flavours = sorted(n for n, p in raw["profiles"].items() if p.get("active"))
        for flavour in flavours:
            while True:
                answer = ctx.run(pirate_command, "ping", "--socket", raw["socket"], "--flavour", flavour,
                                 user=client, check=False)
                if answer.returncode == 0:
                    break
                if time.monotonic() > deadline:
                    raise boring_deploy.Failure(f"{pirate.name}: flavour {flavour} does not answer: "
                                                f"{answer.stderr.strip()[-300:]}",
                                                f"journalctl -u {pirate.unit} -n 50")
                time.sleep(2)
        ctx.log(f"{pirate.name}: {len(flavours)} flavours answer {client}: {', '.join(flavours)}")
