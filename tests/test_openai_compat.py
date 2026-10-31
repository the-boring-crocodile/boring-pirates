"""Real adapter/loop with synthetic HTTP: protocol, failure and cancellation."""
import asyncio
import json
import time
from unittest.mock import patch

import httpx
import pytest

from boring_pirates.common.providers import ProviderFailure
from boring_pirates.common import chat, ipc
from boring_pirates.openai_compat import provider as compat
from boring_pirates.mistral import provider as mistral
import test_pirates as base

ENDPOINT = "http://model.example.test:11434/v1"


def answer(content="ok", calls=None, reason=None, **extra):
    message = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": reason or ("tool_calls" if calls else "stop")}],
            "model": "model-a", "usage": {"prompt_tokens": 5, "completion_tokens": 2}, **extra}


def call(cid="one", name="file_read", arguments='{"root":"work","path":"input.md"}'):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": arguments}}


def mocked(handler):
    return compat.client("local", 1, endpoint=ENDPOINT, transport=httpx.MockTransport(handler))


class CompletionClock:
    """Control only the provider's clock/backoff; HTTP and asyncio deadlines stay real."""
    wait_for = staticmethod(asyncio.wait_for)
    TimeoutError = asyncio.TimeoutError

    def __init__(self, overshoot=0):
        self.now, self.overshoot, self.sleeps = 0.0, overshoot, []

    def get_running_loop(self):
        return self

    def time(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds + self.overshoot


class AdapterTests(base.TempCase):
    run_with = base.AssignmentTest.run_with

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = ipc.settings(base.make_site(self.root, provider="openai_compat", provider_endpoint=ENDPOINT))

    async def test_plain_answer_and_exact_request(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=answer())
        async with mocked(handler) as client:
            turn = await compat.OpenAICompatProvider("model-a", client,
                allowed_tools=frozenset({"file_read"})).complete([{"role": "user", "content": "hello"}])
        self.assertEqual((turn.text, turn.finish_reason, turn.model), ("ok", "stop", "model-a"))
        self.assertEqual((turn.usage["prompt_tokens"], turn.usage["completion_tokens"]), (5, 2))
        self.assertEqual(seen[0].url.path, "/v1/chat/completions")
        body = json.loads(seen[0].content)
        self.assertEqual([t["function"]["name"] for t in body["tools"]], ["file_read"])
        self.assertEqual(body["max_tokens"], 2048)
        self.assertNotIn("keep_reasoning", body)
        self.assertTrue(body["stream"])  # streamed; this server answered with plain JSON, which is read as such
        self.assertEqual(body["stream_options"], {"include_usage": True})

    async def test_multiple_calls_bad_json_and_missing_usage(self):
        raw = answer(None, [call(), call("two", arguments="{")])
        del raw["usage"]
        async with mocked(lambda r: httpx.Response(200, json=raw)) as client:
            turn = await compat.OpenAICompatProvider("model-a", client).complete([])
        self.assertEqual(turn.calls[0]["arguments"], {"root": "work", "path": "input.md"})
        self.assertIn("error", turn.calls[1])
        self.assertEqual(turn.message["tool_calls"], raw["choices"][0]["message"]["tool_calls"])
        self.assertEqual((turn.usage["prompt_tokens"], turn.usage["completion_tokens"]), (0, 0))

    async def test_malformed_arguments_are_answered_as_tool_error_then_loop_continues(self):
        bodies = [answer(None, [call(arguments="{")]), answer("corrected")]
        seen = []
        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=bodies.pop(0))
        async with mocked(handler) as client:
            result = await self.run_with(compat.OpenAICompatProvider("model-a", client))
        self.assertEqual((result["state"], result["answer"], result["tool_errors_count"]), ("completed", "corrected", 1))
        self.assertEqual(seen[1]["messages"][-1]["role"], "tool")
        self.assertIn("not valid JSON", seen[1]["messages"][-1]["content"])

    async def test_reasoning_is_not_replayed_by_default_and_opt_in_is_explicit(self):
        for keep in (False, True):
            raw = answer()
            raw["choices"][0]["message"]["reasoning"] = "private thought"
            seen = []
            def handler(request):
                seen.append(json.loads(request.content))
                return httpx.Response(200, json=raw)
            async with mocked(handler) as client:
                provider = compat.OpenAICompatProvider("model-a", client, options={"keep_reasoning": keep})
                turn = await provider.complete([])
                await provider.complete([turn.message])
            self.assertEqual("reasoning" in seen[1]["messages"][0], keep)
            self.assertEqual("reasoning" in turn.message, keep)
            self.assertEqual(turn.usage["reasoning_chars"], len("private thought"))

    async def test_retry_and_terminal_http_failures(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(503 if len(seen) < 3 else 200, json=answer())
        with patch.object(chat, "BACKOFF_SECONDS", (.001, .002)):
            async with mocked(handler) as client:
                turn = await compat.OpenAICompatProvider("model-a", client).complete([])
        self.assertEqual((turn.text, len(seen)), ("ok", 3))
        for status, reason in ((400, "rejected"), (401, "unauthorized"), (403, "unauthorized"),
                               (404, "rejected"), (422, "rejected"), (429, "rate_limited"), (503, "unavailable")):
            with patch.object(chat, "RETRY_SECONDS", .01), patch.object(chat, "BACKOFF_SECONDS", (.001, .002)):
                async with mocked(lambda r: httpx.Response(status)) as client:
                    with self.assertRaises(ProviderFailure) as caught:
                        await compat.OpenAICompatProvider("model-a", client).complete([])
            self.assertEqual(caught.exception.reason, reason)

    async def test_unavailable_budget_read_timeout_and_cancellation(self):
        def refused(request):
            raise httpx.ConnectError("refused", request=request)
        began = time.monotonic()
        with patch.object(chat, "RETRY_SECONDS", .03), patch.object(chat, "BACKOFF_SECONDS", (.001, .002)):
            async with mocked(refused) as client:
                with self.assertRaises(ProviderFailure) as caught:
                    await compat.OpenAICompatProvider("model-a", client).complete([])
        self.assertEqual(caught.exception.reason, "unavailable")
        self.assertLess(time.monotonic() - began, 1)
        def timeout(request):
            raise httpx.ReadTimeout("read", request=request)
        async with mocked(timeout) as client:
            with self.assertRaises(ProviderFailure) as caught:
                await compat.OpenAICompatProvider("model-a", client).complete([])
        self.assertEqual(caught.exception.reason, "timed_out")
        entered = asyncio.Event()
        async def slow(request):
            entered.set()
            await asyncio.Event().wait()
        async with mocked(slow) as client:
            pending = asyncio.create_task(compat.OpenAICompatProvider("model-a", client).complete([]))
            await entered.wait()
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending

    async def test_absolute_budget_bounds_an_unresponsive_transport(self):
        async def slow(request):
            await asyncio.sleep(10)
            return httpx.Response(200, json=answer())
        began = time.monotonic()
        async with mocked(slow) as client:
            with self.assertRaises(ProviderFailure) as caught:
                await compat.OpenAICompatProvider("model-a", client, request_timeout=.02).complete([])
        self.assertEqual(caught.exception.reason, "timed_out")
        self.assertLess(time.monotonic() - began, 1)

    async def test_a_long_generation_may_outlast_the_retry_window(self):
        # A local model answering for longer than RETRY_SECONDS is not cut off;
        # provider_request_timeout_seconds bounds the call.
        async def generating(request):
            await asyncio.sleep(.1)
            return httpx.Response(200, json=answer())
        with patch.object(chat, "RETRY_SECONDS", .01):
            async with mocked(generating) as client:
                turn = await compat.OpenAICompatProvider("model-a", client, request_timeout=2).complete([])
        self.assertEqual(turn.text, "ok")

    async def test_no_retry_starts_after_the_retry_window(self):
        clock, seen = CompletionClock(), []

        def late_failure(request):
            seen.append(clock.time())
            clock.now = 11
            return httpx.Response(503)

        with patch.object(chat, "asyncio", clock), patch.object(chat, "RETRY_SECONDS", 10):
            async with mocked(late_failure) as client:
                with self.assertRaises(ProviderFailure) as caught:
                    await compat.OpenAICompatProvider("model-a", client, request_timeout=600).complete([])
        self.assertEqual((caught.exception.reason, seen, clock.sleeps), ("unavailable", [0], []))

    async def test_backoff_cannot_admit_a_retry_at_or_after_the_window(self):
        for overshoot in (0, 1):
            for status, reason in ((429, "rate_limited"), (503, "unavailable"), (None, "unavailable")):
                with self.subTest(overshoot=overshoot, status=status):
                    clock, seen = CompletionClock(overshoot), []

                    def handler(request):
                        seen.append(clock.time())
                        if len(seen) > 1:
                            return httpx.Response(200, json=answer())
                        if status is None:
                            raise httpx.ConnectError("refused", request=request)
                        return httpx.Response(status)

                    with patch.object(chat, "asyncio", clock), patch.object(chat, "RETRY_SECONDS", 10), \
                            patch.object(chat, "BACKOFF_SECONDS", (20, 20)):
                        async with mocked(handler) as client:
                            with self.assertRaises(ProviderFailure) as caught:
                                await compat.OpenAICompatProvider("model-a", client, request_timeout=600).complete([])
                    self.assertEqual((caught.exception.reason, seen, clock.sleeps), (reason, [0], [10]))
                    self.assertEqual(caught.exception.detail, "ConnectError" if status is None else f"HTTP {status}")

    async def test_the_call_budget_is_a_timeout_even_after_a_retried_failure(self):
        seen = []

        async def handler(request):
            seen.append(request)
            if len(seen) == 1:
                return httpx.Response(503)
            await asyncio.sleep(1)  # generation outruns the call budget
            return httpx.Response(200, json=answer())

        with patch.object(chat, "BACKOFF_SECONDS", (.001, .002)):
            async with mocked(handler) as client:
                with self.assertRaises(ProviderFailure) as caught:
                    await compat.OpenAICompatProvider("model-a", client, request_timeout=0.3).complete([])
        self.assertEqual((caught.exception.reason, len(seen)), ("timed_out", 2))
        self.assertIn("provider_request_timeout_seconds", caught.exception.detail)

    async def test_retry_admitted_inside_the_window_can_finish_after_it(self):
        clock, seen = CompletionClock(), []

        def handler(request):
            seen.append(clock.time())
            if len(seen) == 1:
                return httpx.Response(503)
            clock.now = 20  # Generation crosses the retry boundary, within the independent call budget.
            return httpx.Response(200, json=answer())

        with patch.object(chat, "asyncio", clock), patch.object(chat, "RETRY_SECONDS", 10), \
                patch.object(chat, "BACKOFF_SECONDS", (1, 1)):
            async with mocked(handler) as client:
                turn = await compat.OpenAICompatProvider("model-a", client, request_timeout=600).complete([])
        self.assertEqual((turn.text, seen, clock.sleeps, clock.now), ("ok", [0, 1], [1], 20))

    async def test_invalid_wire_responses_and_truncation(self):
        bad = [httpx.Response(200, text="<html>no</html>"), httpx.Response(200, json={}),
               httpx.Response(200, json=answer(usage=[])), httpx.Response(200, json=answer(usage={"prompt_tokens": True}))]
        raw = answer()
        raw["choices"][0]["message"]["tool_calls"] = {}
        bad.append(httpx.Response(200, json=raw))
        for response in bad:
            async with mocked(lambda r: response) as client:
                with self.assertRaises(ProviderFailure) as caught:
                    await compat.OpenAICompatProvider("model-a", client).complete([])
            self.assertEqual(caught.exception.reason, "invalid_response")
        async with mocked(lambda r: httpx.Response(200, json=answer(None, [call(arguments="{")], reason="length"))) as client:
            turn = await compat.OpenAICompatProvider("model-a", client).complete([])
        self.assertEqual((turn.finish_reason, turn.calls), ("length", []))

    async def test_discovery_missing_model_outage_and_card(self):
        async with mocked(lambda r: httpx.Response(200, json={"data": [{"id": "model-a"}]})) as client:
            problems, warnings, card = await compat.inspect(client, "model-a", frozenset({"file_read"}))
            self.assertEqual((problems, warnings), ([], []))
            self.assertEqual(card, {"id": "model-a", "server": "openai_compat", "endpoint_host": "model.example.test"})
            self.assertTrue((await compat.inspect(client, "missing", frozenset()))[0])
        def refused(request):
            raise httpx.ConnectError("refused", request=request)
        async with mocked(refused) as client:
            problems, warnings, card = await compat.inspect(client, "model-a", frozenset())
        self.assertEqual((problems, card), ([], None))
        self.assertTrue(warnings)

    async def test_configuration_routes_endpoint_without_changing_mistral_defaults(self):
        path = base.make_site(self.root)
        self.assertIsNone(ipc.settings(path).provider_endpoint)
        value = json.loads(path.read_text())
        value["provider_endpoint"] = ENDPOINT
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "mistral uses its own API"):
            ipc.settings(path)
        value["provider"] = "openai_compat"
        path.write_text(json.dumps(value))
        self.assertEqual(ipc.settings(path).provider_endpoint, ENDPOINT)
        del value["provider_endpoint"]
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "requires provider_endpoint"):
            ipc.settings(path)


@pytest.mark.parametrize("endpoint", [None, "", "file:///tmp/model", "http://", "http://user:secret@host/v1",
                                      "http://host/v1?key=x", "http://host/#x", "http://host/ a"])
def test_invalid_endpoints(endpoint):
    with pytest.raises(ValueError):
        compat.validate_endpoint(endpoint)


@pytest.mark.parametrize("options", [{"wat": 1}, {"max_tokens": True}, {"temperature": True},
    {"temperature": 3}, {"temperature": float("nan")}, {"top_p": -1}, {"seed": True},
    {"seed": -1}, {"stop": []}, {"stop": [1]}, {"reasoning_effort": []}, {"keep_reasoning": "no"}])
def test_invalid_settings(options):
    with pytest.raises(ValueError):
        compat.settings(options, "test")


def test_lan_url_and_mistral_endpoint_contract():
    assert compat.validate_endpoint(ENDPOINT + "/") == ENDPOINT
    with pytest.raises(ValueError, match="mistral uses its own API"):
        mistral.client("synthetic", 1, endpoint=ENDPOINT)


def sse(*chunks, done=True):
    text = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + ("data: [DONE]\n\n" if done else "")
    return text.encode()


def chunk(delta=None, reason=None, **extra):
    return {"model": "model-a", "choices": [{"index": 0, "delta": delta or {}, "finish_reason": reason}], **extra}


def streaming(body: bytes | None = None, *, pieces=None, pause=0.0, header_pause=0.0, seen=None):
    """A server that streams `body` (or `pieces` with `pause` between them) after `header_pause`."""
    async def content():
        for piece in pieces:
            await asyncio.sleep(pause)
            yield piece

    async def handler(request):
        if seen is not None:
            seen.append(json.loads(request.content))
        await asyncio.sleep(header_pause)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=body if pieces is None else content())
    return mocked(handler)


class StreamTests(base.TempCase):
    async def test_pieces_reasoning_and_tool_call_arguments_are_joined(self):
        body = sse(chunk({"role": "assistant", "reasoning": "Let me "}), chunk({"reasoning": "look."}),
                   chunk({"content": "Reading "}), chunk({"content": "it."}),
                   chunk({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                          "function": {"name": "file_read", "arguments": '{"root":"wo'}}]}),
                   chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'rk","path":"a.md"}'}}]}),
                   chunk(reason="tool_calls"),
                   {"model": "model-a", "choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 4}})
        seen = []
        async with streaming(body, seen=seen) as client:
            turn = await compat.OpenAICompatProvider("model-a", client, options={"keep_reasoning": True}).complete([])
        self.assertEqual((turn.text, turn.finish_reason), ("Reading it.", "tool_calls"))
        self.assertEqual(turn.calls, [{"id": "c1", "name": "file_read", "arguments": {"root": "work", "path": "a.md"}}])
        self.assertEqual(turn.message["reasoning"], "Let me look.")
        self.assertEqual((turn.usage["prompt_tokens"], turn.usage["completion_tokens"], turn.usage["reasoning_chars"]),
                         (9, 4, 12))
        self.assertEqual((seen[0]["stream"], seen[0]["stream_options"]), (True, {"include_usage": True}))

    async def test_ollama_shape_as_captured(self):
        # Ollama 0.40.10, Gemma 4 12B, 2026-10-25: the whole tool call in one chunk, usage after the finish.
        raw = (b'data: {"id":"chatcmpl-283","object":"chat.completion.chunk","created":1792881592,"model":"gemma4:12b-it-qat",'
               b'"system_fingerprint":"fp_ollama","choices":[{"index":0,"delta":{"role":"assistant","content":"","tool_calls":'
               b'[{"id":"call_17soe0a3","index":0,"type":"function","function":{"name":"file_list","arguments":'
               b'"{\\"path\\":\\".\\",\\"root\\":\\"work\\"}"}}]},"finish_reason":null}]}\n\n'
               b'data: {"id":"chatcmpl-283","object":"chat.completion.chunk","created":1792881592,"model":"gemma4:12b-it-qat",'
               b'"system_fingerprint":"fp_ollama","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n'
               b'data: {"id":"chatcmpl-283","object":"chat.completion.chunk","created":1792881592,"model":"gemma4:12b-it-qat",'
               b'"system_fingerprint":"fp_ollama","choices":[],"usage":{"prompt_tokens":83,"prompt_tokens_details":'
               b'{"cached_tokens":0},"completion_tokens":25,"total_tokens":108}}\n\ndata: [DONE]\n\n')
        async with streaming(raw) as client:
            turn = await compat.OpenAICompatProvider("gemma4:12b-it-qat", client).complete([])
        self.assertEqual(turn.calls, [{"id": "call_17soe0a3", "name": "file_list", "arguments": {"path": ".", "root": "work"}}])
        self.assertEqual((turn.model, turn.usage["prompt_tokens"], turn.text), ("gemma4:12b-it-qat", 83, ""))

    async def test_a_silent_stream_fails_after_the_idle_time(self):
        pieces = [sse(chunk({"content": "start"}), done=False), sse(chunk(reason="stop"))]
        async with streaming(pieces=pieces, pause=0.3) as client:
            started = time.monotonic()
            with self.assertRaises(ProviderFailure) as caught:
                await compat.OpenAICompatProvider("model-a", client, request_timeout=5, idle_timeout=0.1).complete([])
        self.assertEqual(caught.exception.reason, "timed_out")
        self.assertIn("provider_idle_timeout_seconds", caught.exception.detail)
        self.assertLess(time.monotonic() - started, 2)
        async with streaming(sse(chunk(reason="stop")), header_pause=0.3) as client:
            with self.assertRaises(ProviderFailure) as caught:
                await compat.OpenAICompatProvider("model-a", client, request_timeout=5, idle_timeout=0.1).complete([])
        self.assertIn("no answer for 0.1 s", caught.exception.detail)

    async def test_a_stream_that_keeps_talking_may_outlast_the_idle_time(self):
        pieces = [sse(chunk({"content": str(n)}), done=False) for n in range(6)] + [sse(chunk(reason="stop"))]
        async with streaming(pieces=pieces, pause=0.05) as client:
            turn = await compat.OpenAICompatProvider("model-a", client, request_timeout=5, idle_timeout=0.15).complete([])
        self.assertEqual(turn.text, "012345")  # 0.35 s in all, never 0.15 s silent
        async with streaming(pieces=pieces, pause=0.05) as client:  # but not the call budget
            with self.assertRaises(ProviderFailure) as caught:
                await compat.OpenAICompatProvider("model-a", client, request_timeout=0.2, idle_timeout=0.15).complete([])
        self.assertEqual(caught.exception.reason, "timed_out")

    async def test_broken_streams_are_invalid_responses(self):
        for body, message in ((sse(chunk({"content": "x"})), "without a finish_reason"),
                              (b"data: {not json\n\n", "stream:"),
                              (sse({"choices": [{"delta": {}}, {"delta": {}}]}), "at most one choice")):
            with self.subTest(message=message):
                async with streaming(body) as client:
                    with self.assertRaises(ProviderFailure) as caught:
                        await compat.OpenAICompatProvider("model-a", client).complete([])
                self.assertEqual(caught.exception.reason, "invalid_response")
                self.assertIn(message, caught.exception.detail)

    async def test_stream_false_asks_for_one_answer(self):
        seen = []
        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=answer())
        async with mocked(handler) as client:
            provider = compat.OpenAICompatProvider("model-a", client, options=compat.settings({"stream": False}, "a"))
            self.assertEqual((await provider.complete([])).text, "ok")
        self.assertFalse(seen[0]["stream"])
        self.assertNotIn("stream_options", seen[0])
        with self.assertRaisesRegex(ValueError, "stream must be true or false"):
            compat.settings({"stream": 1}, "a")

    async def test_idle_timeout_configuration(self):
        site = base.make_site(self.root, provider="openai_compat", provider_endpoint=ENDPOINT,
                              provider_request_timeout_seconds=60)
        self.assertEqual(ipc.settings(site).provider_idle_timeout, 60)  # the default, capped by the call budget
        site = base.make_site(self.root, provider="openai_compat", provider_endpoint=ENDPOINT,
                              provider_request_timeout_seconds=900, provider_idle_timeout_seconds=300)
        config = ipc.settings(site)
        self.assertEqual((config.provider_request_timeout, config.provider_idle_timeout), (900, 300))
        site = base.make_site(self.root, provider="openai_compat", provider_endpoint=ENDPOINT,
                              provider_request_timeout_seconds=60, provider_idle_timeout_seconds=120)
        with self.assertRaisesRegex(ValueError, "provider_idle_timeout_seconds exceeds"):
            ipc.settings(site)

    async def test_mistral_stream_idle_and_close(self):
        client = base.FakeMistral([base.mistral_answer("stop", "slow")], pause=0.3)
        with self.assertRaises(ProviderFailure) as caught:
            await mistral.MistralProvider("model-a", client, request_timeout=5, idle_timeout=0.1).complete([])
        self.assertEqual(caught.exception.reason, "timed_out")
        self.assertIn("silent", caught.exception.detail)
        self.assertTrue(client.streams[0].closed)
        client = base.FakeMistral([base.mistral_answer("stop", "fine")], pause=0.05)
        turn = await mistral.MistralProvider("model-a", client, request_timeout=5, idle_timeout=0.2).complete([])
        self.assertEqual((turn.text, client.streams[0].closed), ("fine", True))


class AssemblyTests(base.TempCase):
    def test_parallel_calls_with_one_index_stay_apart(self):
        from boring_pirates.common.streaming import Assembly
        assembly = Assembly()
        assembly.add(chunk({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "file_read", "arguments": '{"p":1}'}}]}))
        assembly.add(chunk({"tool_calls": [{"index": 0, "id": "b", "function": {"name": "file_list", "arguments": '{"p":2}'}}]},
                           reason="tool_calls"))
        calls = assembly.body()["choices"][0]["message"]["tool_calls"]
        self.assertEqual([(c["id"], c["function"]["name"], c["function"]["arguments"]) for c in calls],
                         [("a", "file_read", '{"p":1}'), ("b", "file_list", '{"p":2}')])
        # pieces of one call without an id on later pieces still join
        assembly = Assembly()
        assembly.add(chunk({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "file_read", "arguments": '{"p"'}}]}))
        assembly.add(chunk({"tool_calls": [{"index": 0, "function": {"arguments": ':1}'}}]}, reason="tool_calls"))
        self.assertEqual(assembly.body()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"], '{"p":1}')

    def test_mixed_arguments_and_non_text_content_are_refused(self):
        from boring_pirates.common.streaming import Assembly, StreamError
        assembly = Assembly()
        assembly.add(chunk({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "x", "arguments": {"p": 1}}}]}))
        with self.assertRaisesRegex(StreamError, "mix object and text"):
            assembly.add(chunk({"tool_calls": [{"index": 0, "function": {"arguments": "more"}}]}))
        with self.assertRaisesRegex(StreamError, "expected text content"):
            Assembly().add(chunk({"content": [{"type": "image_url"}]}))

    async def test_mistral_idle_before_the_stream_names_the_idle_time(self):
        async def late(request):
            await asyncio.sleep(0.5)
            return httpx.Response(200, json=base.mistral_answer("stop", "late"))
        async with mistral.client("synthetic", 5, transport=httpx.MockTransport(late)) as client:
            with self.assertRaises(ProviderFailure) as caught:
                await mistral.MistralProvider("model-a", client, request_timeout=5, idle_timeout=0.1).complete([])
        self.assertIn("provider_idle_timeout_seconds", caught.exception.detail)
