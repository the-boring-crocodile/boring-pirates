"""deploy/install.py and deploy/steps.py in a release assembled with the real
engine: the Pirates on a host are found as they are (boring-pirate@NAME with
its config), half a Pirate is irregular, the templates render
completely and the default flavour parses. The host steps themselves (accounts,
the Crocodile's pirate-add, units) are proven by the testbed scenario pirate."""
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import boring_deploy  # the check environment has it (scripts/check.py)

# install_steps.py runs inside the deploy launcher, which needs Python 3.11 or newer.
pytestmark = pytest.mark.skipif(sys.version_info < (3, 11), reason="the deploy launcher needs Python 3.11 or newer")
from boring_deploy import artifact
from boring_pirates.common.ipc import DESCRIPTOR, settings
from boring_pirates.common.profiles import TOOLS, parse_profiles

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "deploy" / "install"


@pytest.fixture
def release(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    files = {f"install/{p.name}": p for p in sorted(INSTALL.iterdir())}
    files.update({name: ROOT / name for name in ("LICENSE", "COPYING.md")})  # a release carries its licence
    archive = artifact.assemble(out, {"component": "boring-pirates", "version": "1.0.0", "runtime": "venv",
                                      "default_home": "/opt/boring-pirates", "python": "python.tar.gz"},
                                ROOT / "deploy/steps.py", files, install=ROOT / "deploy/install.py")
    return archive.parent / "boring-pirates-1.0.0"


@pytest.fixture
def host(release, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.syspath_prepend(str(release / "deploy.d"))
    for name in ("install_steps", "boring_install", "steps"):
        sys.modules.pop(name, None)
    steps = importlib.import_module("steps")
    install = importlib.import_module("install_steps")
    units, config = tmp_path / "units", tmp_path / "etc-boring-pirates"
    units.mkdir()
    config.mkdir()
    monkeypatch.setattr(steps, "UNITS_DIR", units)
    monkeypatch.setattr(steps, "CONFIG_DIR", config)
    monkeypatch.setattr(install, "CROCODILE_CONFIG", tmp_path / "crocodile.toml")
    monkeypatch.setattr(install, "STATE", tmp_path / "var-lib")
    return SimpleNamespace(steps=steps, install=install, units=units, config=config, release=release)


def ctx(host, instance):
    return SimpleNamespace(instance=instance, home=host.release.parent / "home",
                           run=lambda *a, **k: SimpleNamespace(stdout="", returncode=0))


def test_no_pirate_is_a_failure_with_the_install_hint(host):
    with pytest.raises(boring_deploy.Failure) as nothing:
        host.steps.units(None)
    assert str(nothing.value) == "no Pirate on this host" and "--install" in nothing.value.hint


def test_deploy_offers_name_and_the_first_install_settings(release):
    result = subprocess.run([sys.executable, str(release / "deploy"), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    for option in ("--name", "--ponds", "--provider", "--model", "--key-file", "--install"):
        assert option in result.stdout
    remove = subprocess.run([sys.executable, str(release / "uninstall"), "--help"], capture_output=True, text=True)
    assert remove.returncode == 0 and "--name" in remove.stdout


def test_pirates_are_found_as_they_are(host):
    assert host.steps.pirates() == []
    (host.units / "boring-pirate.service").write_text("[Service]\n")  # not a Pirate unit (no @NAME)
    (host.config / "site.json").write_text(json.dumps({"execution": {"name": "parrot"}}))
    (host.config / "mo").mkdir()
    (host.config / "mo" / "config.json").write_text("{}")
    assert host.steps.pirates() == []  # neither counts: a unit without config, a config without unit
    (host.units / "boring-pirate@mo.service").write_text("[Service]\n")
    assert [(p.name, p.unit) for p in host.steps.pirates()] == [("mo", "boring-pirate@mo.service")]
    assert host.install.look(ctx(host, "mo")).installed
    assert not host.install.look(ctx(host, "jo")).installed
    assert host.install.look(ctx(host, None)).installed


def test_half_a_pirate_is_irregular(host):
    (host.units / "boring-pirate-task-jo.socket").write_text("[Socket]\n")
    look = host.install.look(ctx(host, "jo"))
    assert not look.installed and "units without a Pirate" in look.irregular[0]
    (host.units / "boring-pirate-task-jo.socket").unlink()
    (host.config / "jo").mkdir()
    (host.config / "jo" / "config.json").write_text("{}")
    assert "exists without boring-pirate@jo.service" in host.install.look(ctx(host, "jo")).irregular[0]


def test_templates_render_completely_and_the_default_flavour_parses(host, tmp_path):
    values = {"NAME": "mo", "ACCOUNT": "pirate-mo", "DISPLAY": "Mo", "HOME": "/home/pirate-mo",
              "CODE": "/opt/boring-pirates", "PROVIDER": "mistral", "GROUPS": ""}
    probe = SimpleNamespace(release=SimpleNamespace(directory=host.release))
    for name in ("boring-pirate@.service", "boring-pirate-task.socket", "boring-pirate-task@.service",
                 "system-pirate.slice", "INSTRUCTIONS.md", "descriptor.json"):
        text = host.install._template(probe, name, values)
        assert "@" not in text.replace("boring-pirate@", "").replace("task-mo@", ""), name
    unit = host.install._template(probe, "boring-pirate@.service", values)
    assert "User=pirate-mo" in unit and "/etc/boring-pirates/mo/credentials/mistral-api-key" in unit
    assert json.loads(host.install._template(probe, "descriptor.json", values))["workspace_scope"] == \
        "/home/pirate-mo/work"
    profile = json.loads((INSTALL / "profile.json").read_text())
    profile.update(model="mistral-small-2603", context_files=[])
    (tmp_path / "work").mkdir()
    specs = parse_profiles({"default": profile}, tmp_path / "work", "mistral", protected=())
    assert specs["default"].active


def test_settings_are_checked(host):
    names = [s.name for s in host.install.SETTINGS]
    assert names[:1] == ["ponds"] and "model" in names
    with pytest.raises(boring_deploy.Failure):
        host.install.INSTANCE.check("Mo-1")
    with pytest.raises(boring_deploy.Failure):
        [s for s in host.install.SETTINGS if s.name == "provider"][0].check("openai")


def installed(host, tmp_path, client_uids):
    """What install writes for a Pirate mo, in a temp root: handbook, state, key, config.json."""
    home, state = tmp_path / "home", tmp_path / "state"
    (home / "work").mkdir(parents=True)
    (home / "handbook").mkdir()
    state.mkdir(mode=0o700)
    values = {"NAME": "mo", "ACCOUNT": "pirate-mo", "DISPLAY": "Mo", "HOME": str(home),
              "CODE": "/opt/boring-pirates", "PROVIDER": "mistral", "GROUPS": ""}
    for document in ("INSTRUCTIONS.md", "descriptor.json"):
        (home / "handbook" / document).write_text(host.install.render(host.release, document, values))
    site = host.install.site_document(host.release, name="mo", home=home, state=state, provider="mistral",
                                      model="mistral-small-2603", client_uids=client_uids,
                                      authority={"socket": "/run/boring-crocodile-authority/pirate-services.sock",
                                                 "uid": 997})
    (tmp_path / "credentials").mkdir()  # as /run/credentials/UNIT/: no file root may reach it
    (tmp_path / "credentials" / "key").write_text("synthetic")
    site.update(key_file=str(tmp_path / "credentials" / "key"), socket=str(tmp_path / "mo.sock"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps(site))
    return path


def test_what_install_writes_loads_with_one_flavour(host, tmp_path):
    config = settings(installed(host, tmp_path, [os.getuid() + 1]))
    assert config.provider == "mistral" and sorted(config.profiles) == ["default"]
    default = config.profiles["default"]
    # exec, services and view_image (vision models only) are opt-in per flavour.
    assert default.tools == TOOLS - {"exec", "service_list", "service_describe", "service_call", "view_image"}
    assert DESCRIPTOR <= set(config.descriptor) and config.descriptor["name"] == "Mo"
    assert "You are Mo" in default.resolve().context and default.settings == {"max_tokens": 8192}


def test_a_pirate_without_clients_refuses_to_start(host, tmp_path):
    with pytest.raises(ValueError, match="client_uids"):
        settings(installed(host, tmp_path, []))


def run_check(host, tmp_path, monkeypatch, extra_profiles=None):
    """steps.check on a host with Pirate mo as install writes it (its key file only in
    the unit's /run/credentials, absent here). systemctl answers as the unit would;
    the emitted probe runs unchanged in a subprocess on the current source (this
    interpreter, without -I, which would drop PYTHONPATH)."""
    site = json.loads(installed(host, tmp_path, [os.getuid() + 1]).read_text())
    site["key_file"] = "/run/credentials/boring-pirate@mo.service/mistral-api-key"
    site["profiles"].update(extra_profiles or {})
    (host.config / "mo").mkdir()
    (host.config / "mo" / "config.json").write_text(json.dumps(site))
    (host.units / "boring-pirate@mo.service").write_text("[Service]\n")
    monkeypatch.setattr(host.steps, "STATE_BASE", tmp_path)
    new = tmp_path / "new-release"
    probes, logged = [], []

    def run(*command, user=None, **options):
        if command[0] == "systemctl":
            answer = {"User": "pirate-mo", "StateDirectory": "state"}[command[3]]
            return subprocess.CompletedProcess(command, 0, answer + "\n", "")
        assert command[:3] == (str(new / "app" / "bin" / "python3"), "-I", "-c") and user == "pirate-mo"
        probes.append(command[3:])
        result = subprocess.run([sys.executable, "-c", *command[3:]], capture_output=True, text=True)
        if result.returncode != 0:
            raise boring_deploy.Failure(f"probe exited {result.returncode}: {result.stderr.strip()[-400:]}")
        return result
    host.steps.check(SimpleNamespace(new=new, run=run, log=logged.append))
    return probes, logged


def test_the_release_check_runs_the_services_settings_as_installed(host, tmp_path, monkeypatch):
    probes, logged = run_check(host, tmp_path, monkeypatch)
    assert probes == [(host.steps.PROBE, str(host.config / "mo" / "config.json"), str(tmp_path / "state"))]
    assert logged == ["mo: configuration parses; active flavours: default"]


@pytest.mark.parametrize("reach", ["state", "/run/credentials/boring-pirate@mo.service"])
def test_the_release_check_refuses_a_file_root_on_a_protected_directory(host, tmp_path, monkeypatch, reach):
    profile = json.loads((INSTALL / "profile.json").read_text())
    path = str(tmp_path / reach) if reach == "state" else reach
    profile.update(model="mistral-small-2603", files={"roots": {"peek": {"path": path, "read": True, "write": False}}})
    with pytest.raises(boring_deploy.Failure, match="profile peek: file root peek .* overlaps"):
        run_check(host, tmp_path, monkeypatch, {"peek": profile})


def switched(host, tmp_path, states):
    """Pirates jo and mo; the engine's own venv switch runs against a fake systemctl
    (is-active answers states[unit]), so ctx.stopped is what the engine reports."""
    for name in ("jo", "mo"):
        (host.config / name).mkdir()
        (host.config / name / "config.json").write_text(json.dumps({
            "client_uids": [os.getuid()], "socket": f"/run/{name}.sock",
            "profiles": {"default": {"active": True}, "spare": {"active": False}}}))
        (host.units / f"boring-pirate@{name}.service").write_text("[Service]\n")
    calls, logged = [], []

    def run(*command, user=None, check=True, **options):
        calls.append(command)
        stdout = states[command[2]] + "\n" if command[:2] == ("systemctl", "is-active") else ""
        return SimpleNamespace(stdout=stdout, stderr="", returncode=0)
    home = tmp_path / "pirates-home"
    new = home / "releases" / "1.1.0"
    new.mkdir(parents=True)
    release = boring_deploy.Release(host.release, "boring-pirates", "1.1.0", "venv", str(home))
    context = boring_deploy.Context(release, home, "1.0.0", log=logged.append, new=new)
    context.run = run
    boring_deploy.Venv().switch(context, host.steps.units(context))
    return context, calls, logged


def pings(calls):
    return [c[c.index("--socket") + 1] for c in calls if "ping" in c]


def test_the_smoke_pings_only_the_pirates_that_run(host, tmp_path):
    context, calls, logged = switched(host, tmp_path, {"boring-pirate@jo.service": "inactive",
                                                       "boring-pirate@mo.service": "active"})
    assert context.stopped == ["boring-pirate@jo.service"] and not context.not_started
    assert ("systemctl", "restart", "boring-pirate@mo.service") in calls
    host.steps.smoke(context)
    assert pings(calls) == ["/run/mo.sock"]
    assert "jo: stopped before the update, so not started and not pinged" in logged
    assert any(line.startswith("mo: 1 flavours answer") for line in logged)


def test_all_pirates_stopped_is_not_started_and_nothing_is_pinged(host, tmp_path):
    context, calls, logged = switched(host, tmp_path, {"boring-pirate@jo.service": "inactive",
                                                       "boring-pirate@mo.service": "inactive"})
    assert context.not_started and not [c for c in calls if c[:2] == ("systemctl", "restart")]
    host.steps.smoke(context)  # the engine skips it; called anyway, it pings nothing
    assert pings(calls) == [] and "every Pirate is stopped: not started, nothing to ping" in logged


UNITS = ("boring-pirate@mo.service", "boring-pirate-task-mo.socket", "boring-pirate-task-mo@.service",
         "system-piratemo.slice")


def recording_ctx(host, instance, answer=lambda command: "", fails=lambda command: False):
    """ctx whose run records each command and answers with answer(command) as stdout;
    a command that fails(command) exits 1 (and raises, as the engine's run does, with check)."""
    calls, logged = [], []

    def run(*command, check=True, **options):
        calls.append(command)
        if fails(command):
            if check:
                raise boring_deploy.Failure(f"{' '.join(command)} exited 1")
            return SimpleNamespace(stdout="", stderr="", returncode=1)
        return SimpleNamespace(stdout=answer(command), returncode=0)
    probe = SimpleNamespace(instance=instance, home=host.release.parent / "home", run=run, log=logged.append,
                            release=SimpleNamespace(directory=host.release), start=[])
    return probe, calls, logged


def stopped_host(state=lambda unit: "inactive"):
    """systemctl as a stopped Pirate's host answers: is-active by state(unit), User=pirate-mo."""
    def answer(command):
        if command[:2] == ("systemctl", "is-active"):
            return state(command[2]) + "\n"
        if command[:2] == ("systemctl", "show"):
            return "pirate-mo\n"
        return ""
    return answer


def test_uninstall_removes_the_units_and_keeps_the_data(host):
    for unit in UNITS:
        (host.units / unit).write_text("[Unit]\n")
    (host.config / "mo" / "credentials").mkdir(parents=True)
    (host.config / "mo" / "config.json").write_text('{"provider": "mistral"}')
    state = host.install.STATE / "boring-pirate-mo"
    state.mkdir(parents=True)
    (state / "a.jsonl").write_text("{}\n")
    probe, calls, _ = recording_ctx(host, "mo", lambda c: "inactive" if c[1] == "is-active" else "")
    kept = host.install.uninstall(probe, False)
    assert sorted(p.name for p in host.units.iterdir()) == []
    assert not (host.config / "mo" / "config.json").exists()
    assert (host.config / "mo" / "config.json.uninstalled").read_text() == '{"provider": "mistral"}'
    assert (state / "a.jsonl").read_text() == "{}\n"
    assert str(host.config / "mo" / "config.json.uninstalled") in kept
    assert str(state) in kept and str(host.config / "mo" / "credentials") in kept
    assert ("systemctl", "disable", "--now", UNITS[0], UNITS[1]) in calls
    assert ("systemctl", "stop", "system-piratemo.slice") in calls
    assert host.steps.pirates() == [] and not host.install.look(probe).installed


@pytest.fixture
def pirate(host, tmp_path, monkeypatch):
    """An installed Pirate mo with home and state; the account is the test user's.
    fchown is recorded, not done (the test cannot give a folder to root)."""
    import pwd
    for unit in UNITS:
        (host.units / unit).write_text("[Unit]\n")
    (host.config / "mo" / "credentials").mkdir(parents=True)
    (host.config / "mo" / "config.json").write_text('{"provider": "mistral"}')
    state = host.install.STATE / "boring-pirate-mo"
    state.mkdir(parents=True, mode=0o700)
    (state / "a.jsonl").write_text("{}\n")
    home = tmp_path / "home-pirate-mo"
    home.mkdir(mode=0o750)
    os.chmod(home, 0o750)
    (home / "work").mkdir()
    me = pwd.getpwuid(os.getuid())
    account = SimpleNamespace(pw_name="pirate-mo", pw_uid=me.pw_uid, pw_gid=me.pw_gid, pw_dir=str(home))
    monkeypatch.setattr(host.install, "_user", lambda name: account if name == "pirate-mo" else None)
    chowned = {}
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: chowned.__setitem__(os.readlink(f"/proc/self/fd/{fd}"),
                                                                                (uid, gid)))
    return SimpleNamespace(state=state, home=home, chowned=chowned)


def host_files(host, pirate):
    return {**snapshot(host.units), **snapshot(host.config), **snapshot(pirate.state), **snapshot(pirate.home)}


def test_a_failed_stop_removes_nothing_and_a_rerun_finishes(host, pirate):
    before = host_files(host, pirate)
    probe, calls, _ = recording_ctx(host, "mo", stopped_host(), fails=lambda c: c[:2] == ("systemctl", "disable"))
    with pytest.raises(boring_deploy.Failure, match="systemctl disable --now"):
        host.install.uninstall(probe, True)
    assert host_files(host, pirate) == before and host.install.look(probe).installed
    assert not [c for c in calls if c[0] == "userdel" or "pirate-remove" in c or c[1:2] == ("daemon-reload",)]
    # the stop "succeeds" but the Pirate (or a task in its slice) still runs
    for running in ("boring-pirate@mo.service", "system-piratemo.slice"):
        probe, calls, _ = recording_ctx(host, "mo", stopped_host(lambda u: "active" if u == running else "inactive"))
        with pytest.raises(boring_deploy.Failure, match=f"still running after the stop.*{running} active"):
            host.install.uninstall(probe, True)
        assert host_files(host, pirate) == before and not [c for c in calls if c[0] == "userdel"]
    probe, calls, _ = recording_ctx(host, "mo", stopped_host())
    kept = host.install.uninstall(probe, True)
    assert sorted(p.name for p in host.units.iterdir()) == [] and not host.install.look(probe).installed
    assert (host.config / "mo" / "config.json.uninstalled").exists() and str(pirate.home) in kept
    assert calls.index(("userdel", "pirate-mo")) > calls.index(("systemctl", "disable", "--now", UNITS[0], UNITS[1]))
    assert pirate.chowned == {str(pirate.home): (0, 0), str(pirate.state): (0, 0)}
    for folder in (pirate.home, pirate.state):
        assert folder.stat().st_mode & 0o777 == 0o700
    assert (pirate.state / "a.jsonl").read_text() == "{}\n" and (pirate.home / "work").is_dir()


def test_a_failed_userdel_keeps_the_pirate_installed_and_a_rerun_finishes(host, pirate):
    probe, calls, _ = recording_ctx(host, "mo", stopped_host(), fails=lambda c: c[0] == "userdel")
    with pytest.raises(boring_deploy.Failure, match="userdel pirate-mo exited 1"):
        host.install.uninstall(probe, True)
    assert host.install.look(probe).installed and sorted(p.name for p in host.units.iterdir()) == sorted(UNITS)
    assert (host.config / "mo" / "config.json").exists()
    assert pirate.home.stat().st_mode & 0o777 == 0o700  # sealed before userdel was tried
    probe, calls, _ = recording_ctx(host, "mo", stopped_host())
    host.install.uninstall(probe, True)
    assert ("userdel", "pirate-mo") in calls and not host.install.look(probe).installed
    assert sorted(p.name for p in host.units.iterdir()) == []


def test_a_seal_that_fails_keeps_the_account(host, pirate, tmp_path):
    """The state folder replaced by a symlink: not followed, not sealed, no userdel,
    nothing removed; once it is a folder again a rerun finishes."""
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    os.chmod(outside, 0o755)
    pirate.state.rename(tmp_path / "state-aside")
    pirate.state.symlink_to(outside)
    probe, calls, _ = recording_ctx(host, "mo", stopped_host())
    with pytest.raises(boring_deploy.Failure, match="is a symlink: not sealed, the account is kept"):
        host.install.uninstall(probe, True)
    assert not [c for c in calls if c[0] == "userdel"] and pirate.chowned == {}
    assert outside.stat().st_mode & 0o777 == 0o755 and pirate.home.stat().st_mode & 0o777 == 0o750
    assert host.install.look(probe).installed and sorted(p.name for p in host.units.iterdir()) == sorted(UNITS)
    pirate.state.unlink()
    (tmp_path / "state-aside").rename(pirate.state)
    probe, calls, _ = recording_ctx(host, "mo", stopped_host())
    host.install.uninstall(probe, True)
    assert ("userdel", "pirate-mo") in calls and set(pirate.chowned) == {str(pirate.home), str(pirate.state)}


def test_install_writes_site_key_and_units(host, tmp_path, monkeypatch):
    """install() itself, as the current user: the account and group lookups are faked
    (no root), the Crocodile answers its plan and apply."""
    import pwd
    me = pwd.getpwuid(os.getuid())
    account = SimpleNamespace(pw_name="pirate-mo", pw_uid=me.pw_uid, pw_gid=me.pw_gid,
                              pw_dir=str(tmp_path / "home-pirate-mo"))
    (tmp_path / "home-pirate-mo").mkdir()
    host.install.STATE.mkdir()
    monkeypatch.setattr(host.install, "_user", lambda name: account if name == "pirate-mo" else None)
    monkeypatch.setattr(host.install, "grp", SimpleNamespace(getgrnam=lambda name: SimpleNamespace(gr_gid=4242)))
    monkeypatch.setattr(os, "getgrouplist", lambda name, gid: [gid, 4242])
    authority = {"socket": "/run/boring-crocodile-authority/pirate-services.sock", "uid": 997}

    def crocodile(command):
        if "pirate-add" not in command:
            return ""
        if "--apply" in command:
            assert command[-1] == "d1g"
            return json.dumps({"status": "admitted",
                               "for_site_json": {"client_uids": [1001], "service_authority": authority}})
        return json.dumps({"plan_digest": "d1g", "ponds": ["pond"]})
    probe, calls, _ = recording_ctx(host, "mo", crocodile)
    key_file = tmp_path / "key-in"
    key_file.write_text("  synthetic-key\n")
    host.install.install(probe, {"ponds": "pond", "account": "", "display_name": "", "provider": "mistral",
                                 "model": "model-z", "key_file": str(key_file), "groups": ""})
    assert not [c for c in calls if c[0] in ("useradd", "groupadd", "gpasswd")]
    site_path = host.config / "mo" / "config.json"
    site = json.loads(site_path.read_text())
    assert (site["provider"], site["profiles"]["default"]["model"]) == ("mistral", "model-z")
    assert (site["client_uids"], site["service_authority"]) == ([1001], authority)
    assert site["checkout"] == str(tmp_path / "home-pirate-mo" / "work")
    assert site_path.stat().st_mode & 0o777 == 0o644
    key = host.config / "mo" / "credentials" / "mistral-api-key"
    assert key.read_text() == "synthetic-key\n"
    assert key.stat().st_mode & 0o777 == 0o600
    assert (host.install.STATE / "boring-pirate-mo").stat().st_mode & 0o777 == 0o700
    assert sorted(p.name for p in host.units.iterdir()) == sorted(UNITS)
    for unit in UNITS:
        assert (host.units / unit).stat().st_mode & 0o777 == 0o644
    assert "User=pirate-mo" in (host.units / "boring-pirate@mo.service").read_text()
    descriptor = json.loads((tmp_path / "home-pirate-mo" / "handbook" / "descriptor.json").read_text())
    assert descriptor["name"] == "mo"
    assert [p.name for p in host.steps.pirates()] == ["mo"]
    assert probe.start == ["boring-pirate@mo.service"]  # the deploy starts the new Pirate


def snapshot(directory):
    """Every entry under directory (itself included): mode, owner and bytes."""
    found = {}
    for path in [directory, *sorted(directory.rglob("*"))]:
        info = path.lstat()
        found[str(path)] = (info.st_mode, info.st_uid, info.st_gid,
                            path.read_bytes() if path.is_file() and not path.is_symlink() else None)
    return found


@pytest.mark.parametrize("planted", [".", "work", "handbook", "handbook/INSTRUCTIONS.md", "handbook/descriptor.json"])
def test_install_refuses_symlinks_in_the_pirates_home(host, tmp_path, monkeypatch, planted):
    """A symlink the Pirate planted in its home: install stops before any change,
    and what the link points to keeps its bytes, mode and owner."""
    import pwd
    me = pwd.getpwuid(os.getuid())
    home = tmp_path / "home-pirate-mo"
    account = SimpleNamespace(pw_name="pirate-mo", pw_uid=me.pw_uid, pw_gid=me.pw_gid, pw_dir=str(home))
    monkeypatch.setattr(host.install, "_user", lambda name: account if name == "pirate-mo" else None)
    host.install.STATE.mkdir()
    outside = tmp_path / "outside"
    (outside / "handbook").mkdir(parents=True)
    (outside / "secret").write_bytes(b"not the Pirate's\n")
    (outside / "secret").chmod(0o600)
    outside.chmod(0o700)
    if planted == ".":
        home.symlink_to(outside)
    else:
        home.mkdir()
        if planted.startswith("handbook/"):
            (home / "handbook").mkdir()
        (home / planted).symlink_to(outside / ("secret" if planted.startswith("handbook/") else ""))
    before = snapshot(outside)
    probe, calls, _ = recording_ctx(host, "mo")
    with pytest.raises(boring_deploy.Failure, match="symlinks in the Pirate's home"):
        host.install.install(probe, {"ponds": "pond", "account": "", "display_name": "", "provider": "mistral",
                                     "model": "model-z", "key_file": "", "groups": ""})
    assert snapshot(outside) == before
    assert calls == [] and list(host.install.STATE.iterdir()) == [] and list(host.config.iterdir()) == []


def test_root_writes_follow_no_symlink(host, tmp_path):
    """_write, should a link appear after the check: a linked directory fails, a
    link at the target is replaced (not written through)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"not the Pirate's\n")
    before = snapshot(outside)
    (tmp_path / "handbook").symlink_to(outside)
    with pytest.raises(OSError):
        host.install._write(tmp_path / "handbook" / "INSTRUCTIONS.md", "x", 0o640)
    with pytest.raises(OSError):
        host.install._directory(tmp_path / "handbook", SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()), 0o750)
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "INSTRUCTIONS.md").symlink_to(outside / "secret")
    host.install._write(tmp_path / "real" / "INSTRUCTIONS.md", "mine\n", 0o640)
    assert snapshot(outside) == before
    target = tmp_path / "real" / "INSTRUCTIONS.md"
    assert not target.is_symlink() and target.read_text() == "mine\n" and target.stat().st_mode & 0o777 == 0o640


def test_instructions_name_every_tool():
    text = (INSTALL / "INSTRUCTIONS.md").read_text()
    for tool in TOOLS:
        assert f"`{tool}(" in text, tool
    assert "read_file" not in text and "write_file(" not in text
