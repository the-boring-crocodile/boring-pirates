"""Current Crocodile grants are the owner; flavours optionally narrow them."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from boring_pirates.common.ipc import settings
from boring_pirates.common.files import CheckoutFiles
from boring_pirates.common.runtime import ToolRuntime
from boring_pirates.common.overrides import apply, Caps
from boring_pirates.mistral.provider import settings as provider_settings
from boring_worker_kit.services import Services
from boring_worker_kit.outcomes import Refused
from test_pirates import make_site, unit_environment


def profile(root, **extra):
    config = settings(make_site(root, profiles={"alpha": {"active": True, "model": "fixture",
        "allowed_tools": ["service_list", "service_call"], "allow_overrides": True, **extra}}),
        environment=unit_environment(root))
    return config, config.profiles["alpha"]


def test_omitted_services_follow_new_grants_and_revocation_without_restart(tmp_path):
    config, spec = profile(tmp_path)
    assert spec.summary()["allowed_services"] is None  # no local restriction, not a grant snapshot
    grants = {}
    async def request(action, service=None):
        if action == "list":
            return {"caller": "pirate/parrot", "services": list(grants.values())}
        if service not in grants:
            raise Refused("forbidden", "not granted")
        return {"caller": "pirate/parrot", "token": "fixture-token", "grant": grants[service]}
    authority = AsyncMock()
    authority.request.side_effect = request
    runtime = ToolRuntime(authority=authority)
    files = CheckoutFiles(config.checkout, spec.files)
    async def scenario():
        deadline = asyncio.get_running_loop().time()+10
        listed = lambda: runtime.dispatch(files, spec, {"id":"1", "name":"service_list", "arguments":{}}, deadline)
        assert (await listed())["services"] == []
        grants["knowledge"] = {"name":"knowledge", "url":"https://knowledge.test/mcp"}
        assert (await listed())["services"] == [grants["knowledge"]]
        call = {"id":"2", "name":"service_call", "arguments":{"service":"knowledge","tool":"kb_search","arguments":{}}}
        with patch.object(Services, "call", AsyncMock(return_value={"read":"ok"})) as send:
            assert await runtime.dispatch(files, spec, call, deadline) == {"read":"ok"}
            del grants["knowledge"]
            with pytest.raises(Refused, match="not granted"):
                await runtime.dispatch(files, spec, call, deadline)
            assert send.await_count == 1
        assert (await listed())["services"] == []
        await runtime.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("narrowed", [[], ["knowledge"]])
def test_explicit_services_only_narrow_and_empty_means_none(tmp_path, narrowed):
    config, spec = profile(tmp_path, allowed_services=narrowed)
    authority = AsyncMock()
    authority.request.return_value = {"caller":"pirate/parrot",
        "services":[{"name":"knowledge"}, {"name":"canvas"}]}
    runtime = ToolRuntime(authority=authority)
    files = CheckoutFiles(config.checkout, spec.files)
    async def scenario():
        deadline = asyncio.get_running_loop().time()+10
        result = await runtime.dispatch(files, spec, {"id":"1","name":"service_list","arguments":{}}, deadline)
        assert [row["name"] for row in result["services"]] == narrowed
        with pytest.raises(Refused, match="not allowed"):
            await runtime.dispatch(files, spec, {"id":"2","name":"service_call",
                "arguments":{"service":"canvas","tool":"read","arguments":{}}}, deadline)
        assert authority.request.await_count == 1  # no token request for locally excluded service
        await runtime.close()
    asyncio.run(scenario())


def test_override_narrows_unrestricted_profile_and_cannot_widen_it_again(tmp_path):
    _, spec = profile(tmp_path)
    narrowed, _ = apply(spec, {"services":["knowledge"]}, Caps(), provider_settings)
    assert narrowed.allowed_services == frozenset({"knowledge"})
    with pytest.raises(ValueError, match="only narrows"):
        apply(narrowed, {"services":["canvas"]}, Caps(), provider_settings)
    empty, _ = apply(spec, {"services":[]}, Caps(), provider_settings)
    assert empty.allowed_services == frozenset()
    assert spec.allowed_services is None


def test_explicit_null_is_not_an_omitted_service_list(tmp_path):
    with pytest.raises(ValueError, match="service allowances"):
        profile(tmp_path, allowed_services=None)
