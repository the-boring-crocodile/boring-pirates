"""The deploy renders the Pirates' units again (deploy/pirate_units.py, DECISIONS
2026-10-25): what a deploy or install wrote is replaced before the switch with a
backup, an absent unit is written; anything else stops the deploy at its check
before any change. Drop-ins and config.json are never touched."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import boring_deploy
from test_install import UNITS, host, release  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(sys.version_info < (3, 11), reason="the deploy launcher needs Python 3.11 or newer")

CODE = "/opt/boring-pirates"


ACCOUNTS = {"pirate-mo": "/home/pirate-mo", "pirate-jo": "/home/pirate-jo"}


def add_pirate(host, name, groups="pond-engineering", provider="mistral"):
    """Pirate NAME as install leaves it: units rendered by release 1.0.0, the record,
    config.json and the key file."""
    (host.config / name / "credentials").mkdir(parents=True)
    (host.config / name / "credentials" / f"{provider}-api-key").write_text("synthetic\n")
    (host.config / name / "config.json").write_text(json.dumps({"provider": provider}))
    values = {"NAME": name, "ACCOUNT": f"pirate-{name}", "HOME": ACCOUNTS[f"pirate-{name}"], "CODE": CODE,
              "PROVIDER": provider, "GROUPS": groups}
    hashes = {}
    for template, unit in host.pirate_units.unit_files(name):
        text = host.pirate_units.render(host.release, template, values)
        (host.units / unit).write_text(text)
        hashes[unit] = host.pirate_units.digest(text)
    host.pirate_units.write_record(name, hashes, "1.0.0", f"pirate-{name}", groups.split())


@pytest.fixture
def mo(host, monkeypatch):
    """Pirate mo (accounts pirate-mo and pirate-jo are faked)."""
    def getpwnam(name):
        if name not in ACCOUNTS:
            raise KeyError(name)
        return SimpleNamespace(pw_dir=ACCOUNTS[name])
    monkeypatch.setattr(host.pirate_units, "pwd", SimpleNamespace(getpwnam=getpwnam))
    add_pirate(host, "mo")
    return host


def deploy_ctx(host, fails=lambda command: False):
    calls, logged = [], []

    def run(*command, check=True, **options):
        calls.append(command)
        if fails(command):
            raise boring_deploy.Failure(f"{' '.join(command)} exited 1")
        return SimpleNamespace(stdout="", stderr="", returncode=0)
    ctx = SimpleNamespace(release=SimpleNamespace(directory=host.release, version="1.1.0"), home=Path(CODE),
                          run=run, log=logged.append)
    return ctx, calls, logged


def change_template(host, template, line):
    path = host.release / "install" / template
    path.write_text(path.read_text() + line + "\n")


def snapshot(host):
    found = {}
    for root in (host.units, host.config):
        for path in sorted(root.rglob("*")):
            found[str(path)] = os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None
    return found


def test_units_as_rendered_need_nothing(mo):
    ctx, calls, logged = deploy_ctx(mo)
    before = snapshot(mo)
    (plan,) = mo.steps.plan_units(ctx)
    assert not plan.replace and not plan.refused and not plan.due
    assert mo.steps.units(ctx) == ["boring-pirate@mo.service"]
    assert snapshot(mo) == before and calls == [] and logged == []


def test_the_values_come_from_the_host(mo):
    filled = mo.pirate_units.values("mo", CODE, mo.pirate_units.read_record("mo"))
    assert filled == {"NAME": "mo", "ACCOUNT": "pirate-mo", "HOME": "/home/pirate-mo", "CODE": CODE,
                      "PROVIDER": "mistral", "GROUPS": "pond-engineering"}


def test_a_changed_template_replaces_what_a_deploy_wrote_with_a_backup(mo):
    change_template(mo, "boring-pirate-task@.service", "ProtectClock=yes")
    dropin = mo.units / "boring-pirate-task-mo@.service.d"
    dropin.mkdir()
    (dropin / "pf1-hide.conf").write_text("[Service]\nInaccessiblePaths=-/x\n")
    old = (mo.units / "boring-pirate-task-mo@.service").read_text()
    config = (mo.config / "mo" / "config.json").read_bytes()
    ctx, calls, logged = deploy_ctx(mo)
    mo.steps.plan_units(ctx)
    assert any("+ProtectClock=yes" in line for line in logged)
    assert (mo.units / "boring-pirate-task-mo@.service").read_text() == old  # the check writes nothing
    mo.steps.units(ctx)
    new = (mo.units / "boring-pirate-task-mo@.service").read_text()
    assert new == old + "ProtectClock=yes\n"
    (backup,) = (mo.config / "mo").glob("units-backup-*")
    assert [p.name for p in backup.iterdir()] == ["boring-pirate-task-mo@.service"]
    assert (backup / "boring-pirate-task-mo@.service").read_text() == old
    record = json.loads((mo.config / "mo" / "rendered.json").read_text())
    assert record["release"] == "1.1.0" and record["units"]["boring-pirate-task-mo@.service"] == \
        mo.pirate_units.digest(new)
    assert calls == [("systemctl", "daemon-reload")]  # a task@ template: the next command uses it
    assert (dropin / "pf1-hide.conf").read_text() == "[Service]\nInaccessiblePaths=-/x\n"
    assert (mo.config / "mo" / "config.json").read_bytes() == config


def test_a_changed_socket_is_restarted_if_it_runs(mo):
    change_template(mo, "boring-pirate-task.socket", "# changed")
    ctx, calls, _ = deploy_ctx(mo)
    mo.steps.plan_units(ctx)
    mo.steps.units(ctx)
    assert calls == [("systemctl", "daemon-reload"), ("systemctl", "try-restart", "boring-pirate-task-mo.socket")]


def test_a_hand_edit_stops_the_check_before_any_change(mo):
    change_template(mo, "boring-pirate@.service", "# new")
    unit = mo.units / "boring-pirate@mo.service"
    unit.write_text(unit.read_text().replace("MemoryMax=512M", "MemoryMax=1G"))
    before = snapshot(mo)
    ctx, calls, logged = deploy_ctx(mo)
    with pytest.raises(boring_deploy.Failure, match="units of mo differ from what a deploy wrote") as refused:
        mo.steps.plan_units(ctx)
    assert "drop-in" in refused.value.hint and "remove the unit file" in refused.value.hint
    assert any("changed since the last deploy" in line for line in logged)
    assert any("-MemoryMax=1G" in line for line in logged)
    assert snapshot(mo) == before and calls == []


def test_an_edit_moved_to_a_drop_in_and_the_unit_removed_is_written_again(mo):
    change_template(mo, "boring-pirate-task@.service", "ProtectClock=yes")
    unit = mo.units / "boring-pirate-task-mo@.service"
    unit.write_text(unit.read_text() + "TasksMax=7\n")
    ctx, calls, logged = deploy_ctx(mo)
    with pytest.raises(boring_deploy.Failure):
        mo.steps.plan_units(ctx)
    unit.unlink()  # the operator's remedy: the edit goes into a drop-in, the unit file goes
    ctx, calls, _ = deploy_ctx(mo)
    (plan,) = mo.steps.plan_units(ctx)
    mo.steps.units(ctx)
    assert unit.read_text().endswith("ProtectClock=yes\n") and "TasksMax=7" not in unit.read_text()
    assert not list((mo.config / "mo").glob("units-backup-*"))  # nothing to back up
    assert json.loads((mo.config / "mo" / "rendered.json").read_text())["units"][unit.name] == \
        mo.pirate_units.digest(unit.read_text())


def test_an_unrecorded_unit_that_differs_is_refused(mo):
    record = mo.config / "mo" / "rendered.json"
    document = json.loads(record.read_text())
    del document["units"]["boring-pirate-task-mo@.service"]
    record.write_text(json.dumps(document))
    change_template(mo, "boring-pirate-task@.service", "ProtectClock=yes")
    ctx, calls, logged = deploy_ctx(mo)
    with pytest.raises(boring_deploy.Failure):
        mo.steps.plan_units(ctx)
    assert any("the record does not name it" in line for line in logged) and calls == []


@pytest.mark.parametrize("record, message", [
    (None, "is missing"),
    ({"release": "0.3.8", "units": {}}, "names no account and groups"),
    ("{", "does not parse"),
])
def test_a_record_without_the_account_is_refused(mo, record, message):
    path = mo.config / "mo" / "rendered.json"
    if record is None:
        path.unlink()
    else:
        path.write_text(record if isinstance(record, str) else json.dumps(record))
    before = snapshot(mo)
    ctx, calls, _ = deploy_ctx(mo)
    with pytest.raises(boring_deploy.Failure, match=message) as refused:
        mo.steps.plan_units(ctx)
    assert "User=" in refused.value.hint and "SupplementaryGroups=" in refused.value.hint
    assert snapshot(mo) == before and calls == []


def test_an_install_stopped_after_the_units_is_finished_by_the_deploy(mo):
    """Install records the account before the units and their hashes last: units as
    rendered but not named get the follow-ups (reload, socket restart), then the record."""
    record = mo.config / "mo" / "rendered.json"
    record.write_text(json.dumps({**json.loads(record.read_text()), "units": {}}))
    ctx, calls, logged = deploy_ctx(mo)
    mo.steps.plan_units(ctx)
    mo.steps.units(ctx)
    assert json.loads(record.read_text())["release"] == "1.1.0"
    assert calls == [("systemctl", "daemon-reload"), ("systemctl", "try-restart", "boring-pirate-task-mo.socket")]
    assert not list((mo.config / "mo").glob("units-backup-*"))
    assert any("reloaded and recorded" in line for line in logged)


def test_a_symlinked_unit_is_refused_and_not_followed(mo, tmp_path):
    outside = tmp_path / "outside.service"
    outside.write_text("[Service]\n")
    unit = mo.units / "boring-pirate-task-mo@.service"
    unit.unlink()
    unit.symlink_to(outside)
    ctx, calls, _ = deploy_ctx(mo)
    with pytest.raises(boring_deploy.Failure, match="is a symlink"):
        mo.steps.plan_units(ctx)
    assert outside.read_text() == "[Service]\n" and calls == []


def test_a_unit_changed_after_the_check_stops_the_apply(mo):
    change_template(mo, "boring-pirate@.service", "# new")
    ctx, calls, _ = deploy_ctx(mo)
    mo.steps.plan_units(ctx)
    unit = mo.units / "boring-pirate@mo.service"
    unit.write_text(unit.read_text() + "# meanwhile\n")
    with pytest.raises(boring_deploy.Failure, match="changed since the check"):
        mo.steps.units(ctx)
    assert unit.read_text().endswith("# meanwhile\n") and calls == []


def test_a_failure_midway_names_what_was_written_and_a_rerun_finishes(mo, monkeypatch):
    add_pirate(mo, "jo")
    change_template(mo, "boring-pirate-task@.service", "ProtectClock=yes")
    ctx, calls, _ = deploy_ctx(mo)
    plans = mo.steps.plan_units(ctx)
    assert [p.name for p in plans] == ["jo", "mo"]
    real = mo.pirate_units.write

    def write(path, text, mode, owner=None):
        if path.name == "boring-pirate-task-mo@.service" and path.parent == mo.units:
            raise OSError(28, "No space left on device")
        real(path, text, mode, owner)
    monkeypatch.setattr(mo.pirate_units, "write", write)
    ctx.changed = []  # boring-deploy 0.7.8+: what the engine reports instead of "nothing changed"
    with pytest.raises(boring_deploy.Failure) as failed:
        mo.steps.units(ctx)
    assert f"wrote {mo.units / 'boring-pirate-task-jo@.service'}" in ctx.changed
    assert any(line.startswith("old units kept in ") for line in ctx.changed)
    message = str(failed.value)
    assert f"wrote {mo.units / 'boring-pirate-task-jo@.service'}" in message
    assert f"wrote {mo.units / 'boring-pirate-task-mo@.service'}" not in message
    assert "these units are not" in message and "Run the deploy again" in failed.value.hint
    assert "units-backup-" in failed.value.hint
    assert calls == []  # no reload, no restart, and no record yet
    assert json.loads((mo.config / "jo" / "rendered.json").read_text())["release"] == "1.0.0"
    monkeypatch.setattr(mo.pirate_units, "write", real)
    ctx, calls, _ = deploy_ctx(mo)
    jo, mo_plan = mo.steps.plan_units(ctx)
    assert [u.file for u in jo.follow_ups] == ["boring-pirate-task-jo@.service"] and not jo.replace
    assert [u.file for u in mo_plan.replace] == ["boring-pirate-task-mo@.service"]
    mo.steps.units(ctx)
    for name in ("jo", "mo"):
        assert (mo.units / f"boring-pirate-task-{name}@.service").read_text().endswith("ProtectClock=yes\n")
        assert json.loads((mo.config / name / "rendered.json").read_text())["release"] == "1.1.0"
    assert calls == [("systemctl", "daemon-reload")]


def test_every_plan_is_verified_before_the_first_write(mo):
    add_pirate(mo, "jo")
    change_template(mo, "boring-pirate@.service", "# new")
    ctx, calls, _ = deploy_ctx(mo)
    mo.steps.plan_units(ctx)
    late = mo.units / "boring-pirate@mo.service"
    late.write_text(late.read_text() + "# meanwhile\n")
    before = snapshot(mo)
    with pytest.raises(boring_deploy.Failure, match="changed since the check, so nothing is changed"):
        mo.steps.units(ctx)
    assert snapshot(mo) == before and calls == []  # jo, planned first, was not written either


def test_a_failed_socket_restart_is_finished_by_the_rerun(mo):
    change_template(mo, "boring-pirate-task.socket", "# changed")
    ctx, calls, _ = deploy_ctx(mo, fails=lambda c: c[:2] == ("systemctl", "try-restart"))
    mo.steps.plan_units(ctx)
    with pytest.raises(boring_deploy.Failure, match="failed at systemctl") as failed:
        mo.steps.units(ctx)
    assert "systemctl daemon-reload" in str(failed.value)
    assert json.loads((mo.config / "mo" / "rendered.json").read_text())["release"] == "1.0.0"  # record last
    ctx, calls, _ = deploy_ctx(mo)
    (plan,) = mo.steps.plan_units(ctx)
    assert not plan.replace and [u.file for u in plan.follow_ups] == ["boring-pirate-task-mo.socket"]
    mo.steps.units(ctx)
    assert calls == [("systemctl", "daemon-reload"), ("systemctl", "try-restart", "boring-pirate-task-mo.socket")]
    assert json.loads((mo.config / "mo" / "rendered.json").read_text())["release"] == "1.1.0"


@pytest.mark.parametrize("change, message", [
    (lambda h: (h.config / "mo" / "config.json").write_text('{"provider": "openai"}'), "provider"),
    (lambda h: change_record(h, account="pirate mo"), "account"),
    (lambda h: change_record(h, account=["pirate-mo"]), "account"),
    (lambda h: change_record(h, groups="pond-engineering"), "groups"),
    (lambda h: change_record(h, groups=["a,b"]), "groups"),
    (lambda h: change_record(h, account="pirate-xx"), "no account pirate-xx"),
    (lambda h: (h.config / "mo" / "credentials" / "mistral-api-key").unlink(), "which is not there"),
])
def test_values_install_would_not_accept_are_refused(mo, change, message):
    change(mo)
    ctx, calls, _ = deploy_ctx(mo)
    before = snapshot(mo)
    with pytest.raises(boring_deploy.Failure, match=message):
        mo.steps.plan_units(ctx)
    assert snapshot(mo) == before and calls == []


def test_a_home_with_a_specifier_is_refused(mo, monkeypatch):
    monkeypatch.setitem(ACCOUNTS, "pirate-mo", "/home/pirate%h")
    with pytest.raises(boring_deploy.Failure, match="home"):
        mo.steps.plan_units(deploy_ctx(mo)[0])


def change_record(host, **values):
    record = host.config / "mo" / "rendered.json"
    record.write_text(json.dumps({**json.loads(record.read_text()), **values}))


def test_backup_folders_never_collide(mo):
    import datetime
    now = datetime.datetime(2026, 10, 25, 22, 0, 0, 123456)
    first = mo.pirate_units._backup_dir("mo", now)
    second = mo.pirate_units._backup_dir("mo", now)
    assert first != second and first.name == "units-backup-20261025T220000.123456"
