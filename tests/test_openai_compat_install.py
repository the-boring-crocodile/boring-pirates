"""Installer input is checked before account, admission or unit effects."""
import json
import os
import sys

import boring_deploy
import pytest

from boring_pirates.common.ipc import settings
from test_install import host, release, ctx, installed, recording_ctx, unit_environment

# install_steps.py runs inside the deploy launcher, which needs Python 3.11 or newer.
pytestmark = pytest.mark.skipif(sys.version_info < (3, 11), reason="the deploy launcher needs Python 3.11 or newer")


@pytest.mark.parametrize("provider,endpoint", [
    ("mistral", "http://host/v1"), ("openai_compat", ""), ("openai_compat", None),
    ("openai_compat", "file:///tmp/server"), ("openai_compat", "http://user:key@host/v1"),
    ("openai_compat", "http://host:bad/v1"), ("openai_compat", "http://host/v1?key=x"),
])
def test_bad_endpoint_has_no_install_effect(host, provider, endpoint):
    probe, calls, _ = recording_ctx(host, "mo")
    with pytest.raises(boring_deploy.Failure):
        host.install.install(probe, {"provider": provider, "endpoint": endpoint})
    assert calls == []
    assert not probe.home.exists()
    assert list(host.config.iterdir()) == []
    assert list(host.units.iterdir()) == []


def test_install_default_flavour_parses_with_compat_settings(host, tmp_path):
    path = installed(host, tmp_path, [os.getuid() + 1])
    old = json.loads(path.read_text())
    site = host.install.site_document(
        host.release, name="mo", home=tmp_path/"home",
        provider="openai_compat", model="qwen3.5:9b-q8_0",
        client_uids=old["client_uids"], authority=old["service_authority"],
        endpoint="http://model.example.test:11434/v1")
    for key in ("checkout", "descriptor_file"):
        site[key] = old[key]
    site["profiles"]["default"]["handbook"] = None
    path.write_text(json.dumps(site))
    (tmp_path / "credentials" / "openai_compat-api-key").write_text("local")
    parsed = settings(path, environment=unit_environment(tmp_path))
    assert parsed.provider_endpoint == "http://model.example.test:11434/v1"
    assert parsed.profiles["default"].settings == {
        "max_tokens": 2048, "keep_reasoning": False, "reasoning_effort": "none"}
