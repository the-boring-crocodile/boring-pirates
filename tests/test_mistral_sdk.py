"""What Pirates use of Mistral's SDK, against a mocked Mistral over HTTP: the
SDK's retries (429 and 5xx), its errors as our failure reasons, the model card
at start (a flavour its model cannot serve is not offered), and the extra
settings passed through."""
from __future__ import annotations

import datetime
import json

import httpx

from boring_pirates.common.providers import ProviderFailure
from boring_pirates.mistral import provider as mistral
import test_pirates as base
from test_pirates import sdk_response

CARD = {"id": "model-a", "object": "model", "created": 1, "owned_by": "mistralai", "type": "base",
        "max_context_length": 131072, "aliases": [], "deprecation": None,
        "capabilities": {"completion_chat": True, "function_calling": True, "vision": False}}


def mocked(*answers, seen=None):
    """A Mistral client whose HTTP answers are `answers` in turn (status, json)."""
    answers = list(answers)

    def respond(request):
        if seen is not None:
            seen.append((request.url.path, json.loads(request.content) if request.content else None))
        status, body = answers.pop(0) if len(answers) > 1 else answers[0]
        return httpx.Response(status, json=body)
    return mistral.client("synthetic", 1.5, httpx.AsyncClient(transport=httpx.MockTransport(respond)))


OK = (200, sdk_response("stop", "ok").model_dump(mode="json"))


class SDKTest(base.TempCase):
    run_with = base.AssignmentTest.run_with

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = base.settings(base.make_site(self.root))
        self.backoff = mistral.BACKOFF_MS
        mistral.BACKOFF_MS = (10, 40)  # the SDK's waits, short for the test
        self.addCleanup(setattr, mistral, "BACKOFF_MS", self.backoff)

    async def test_the_sdk_retries_429_and_5xx_and_then_answers(self):
        seen = []
        provider = mistral.MistralProvider("model-a", mocked((429, {"message": "slow down"}), (503, {}), OK, seen=seen))
        turn = await provider.complete([{"role": "user", "content": "hi"}])
        self.assertEqual((turn.text, len(seen)), ("ok", 3))

    async def test_errors_become_failure_reasons(self):
        for status, reason in ((401, "unauthorized"), (403, "unauthorized"), (400, "rejected"),
                               (429, "rate_limited"), (503, "unavailable")):
            with self.subTest(status=status):
                seen = []
                provider = mistral.MistralProvider("model-a", mocked((status, {"message": "no"}), seen=seen))
                with self.assertRaises(ProviderFailure) as caught:
                    await provider.complete([{"role": "user", "content": "hi"}])
                self.assertEqual(caught.exception.reason, reason)
                if status < 429:
                    self.assertEqual(len(seen), 1)  # never retried

    async def test_an_assignment_ends_failed_with_the_reason(self):
        provider = mistral.MistralProvider("model-a", mocked((401, {"message": "bad key"})))
        result = await self.run_with(provider)
        self.assertEqual((result["state"], result["reason"]), ("failed", "provider_unauthorized"))
        self.assertIsNone(result["usage"]["input_tokens"])  # the unanswered call's tokens are unknown
        last = base.records(self.root / "history" / (self.job_id + ".jsonl"))[-1]
        self.assertEqual((last["kind"], last["data"]["state"], last["data"]["reason"]),
                         ("outcome", "failed", "provider_unauthorized"))

    async def test_extra_settings_are_passed_as_set(self):
        seen = []
        options = mistral.settings({"top_p": 0.9, "random_seed": 7, "stop": ["END"], "service_tier": "auto",
                                    "safe_prompt": True, "presence_penalty": 0.2}, "alpha")
        provider = mistral.MistralProvider("model-a", mocked(OK, seen=seen), options=options)
        await provider.complete([{"role": "user", "content": "hi"}])
        body = seen[0][1]
        for key, value in (("top_p", 0.9), ("random_seed", 7), ("stop", ["END"]), ("service_tier", "auto"),
                           ("safe_prompt", True), ("presence_penalty", 0.2), ("max_tokens", 8192)):
            self.assertEqual(body[key], value, key)
        self.assertNotIn("frequency_penalty", body)


class CardTest(base.SocketCase):
    async def inspect(self, tools, *answers):
        return await mistral.inspect(mocked(*answers), "model-a", frozenset(tools))

    async def test_what_the_card_says(self):
        problems, warnings, card = await self.inspect(["file_read"], (200, CARD))
        self.assertEqual((problems, warnings), ([], []))
        self.assertEqual((card["max_context_length"], card["capabilities"]), (131072, ["completion_chat", "function_calling"]))
        problems, _, _ = await self.inspect(["file_read", "view_image"], (200, CARD))
        self.assertEqual(problems, ["model-a cannot see images, and the flavour allows view_image"])
        blind = {**CARD, "capabilities": {"completion_chat": True}}
        problems, _, _ = await self.inspect(["file_read"], (200, blind))
        self.assertIn("cannot call tools", problems[0])
        self.assertEqual((await self.inspect([], (200, blind)))[0], [])  # no tools, no function calling needed
        retiring = {**CARD, "deprecation": "2026-12-31T00:00:00Z", "deprecation_replacement_model": "model-b"}
        _, warnings, card = await self.inspect(["file_read"], (200, retiring))
        self.assertEqual(warnings, ["Mistral retires model-a on 2026-12-31; its replacement is model-b"])
        problems, _, card = await self.inspect([], (404, {"message": "no such model"}))
        self.assertIn("does not know the model", problems[0])
        problems, warnings, card = await self.inspect([], (500, {}))
        self.assertEqual((problems, card), ([], None))  # no card is no reason to refuse
        self.assertIn("no model card", warnings[0])

    async def test_a_flavour_its_model_cannot_serve_is_not_offered(self):
        from boring_pirates.common.ipc import inspect_flavours
        await self.start({})
        cards = {"model-a": (200, {**CARD, "capabilities": {"completion_chat": True}}), "model-b": (200, CARD)}

        class Folder:
            @staticmethod
            async def inspect(client, model, tools):
                return await mistral.inspect(mocked(cards[model]), model, tools)
        with self.assertLogs("boring_pirates", "ERROR") as logs:
            await inspect_flavours(self.parrot, Folder, None)
        self.assertEqual(sorted(self.parrot.flavours), ["beta"])
        self.assertIn("flavour alpha is not offered", logs.output[0])
        self.assertEqual((await self.ask("ping", "alpha"))["error"]["code"], "not_found")
        described = (await self.ask("describe", "beta"))["result"]["profile"]["mistral"]
        self.assertEqual((described["max_tokens"], described["model_card"]["max_context_length"]), (8192, 131072))


assert datetime  # (dates arrive as text and are parsed by the SDK)
