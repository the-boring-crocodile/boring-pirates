"""PF1: one handbook per flavour, per-call overrides behind allow_overrides, caps."""
from __future__ import annotations

import hashlib

from boring_pirates.common.ipc import Pirate, settings
from boring_pirates.common.overrides import Caps
from boring_pirates.mistral.provider import settings as mistral_settings

import test_pirates as base
from test_pirates import ALL_TOOLS, answer, make_site, records, tool_call


class Recording(base.Script):
    """A scripted provider that keeps what it was sent."""

    def __init__(self, *turns, **kwargs):
        super().__init__(*turns, **kwargs)
        self.sent: list[list[dict]] = []

    async def complete(self, messages):
        self.sent.append(messages)
        return await super().complete(messages)


def lab_profiles(root, **lab):
    (root / "context").mkdir(exist_ok=True)
    handbook = root / "context" / "HANDBOOK.md"
    handbook.write_text("The handbook.")
    return {"fixed": {"active": True, "model": "m-fixed", "handbook": str(handbook), "allowed_tools": ALL_TOOLS},
            "lab": {"active": True, "model": "m-lab", "handbook": None, "allow_overrides": True,
                    "allowed_tools": ["file_read", "file_list", "exec"], "allowed_services": ["canvas", "knowledge"],
                    "max_turns": 4, "mistral": {"max_tokens": 1000, "temperature": 0.2}, **lab}}


class HandbookTest(base.TempCase):
    async def test_one_handbook_file_or_none(self):
        config = settings(make_site(self.root, lab_profiles(self.root)))
        fixed, lab = config.profiles["fixed"], config.profiles["lab"]
        self.assertEqual(fixed.resolve().context, "The handbook.")
        self.assertEqual(lab.resolve().context, "")
        self.assertEqual([c["path"] for c in fixed.context_identity()], [str(self.root / "context" / "HANDBOOK.md")])
        self.assertTrue(lab.allow_overrides)
        self.assertFalse(fixed.allow_overrides)

    async def test_keys_from_before_the_handbook_are_refused(self):
        """context_files and instructions (before 0.3.6rc9) are unknown settings now."""
        make_site(self.root)
        for key, value in (("instructions", "Rules."), ("context_files", [str(self.root / "context" / "INSTRUCTIONS.md")])):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, f"unknown settings \\['{key}'\\]"):
                settings(make_site(self.root, {"alpha": {"active": True, "model": "m", key: value}}))

    async def test_handbook_and_override_switch_are_checked(self):
        make_site(self.root)
        for bad in (3, ["a"], {"x": 1}):
            with self.subTest(handbook=bad), self.assertRaisesRegex(ValueError, "handbook must be a path or null"):
                settings(make_site(self.root, {"alpha": {"active": True, "model": "m", "handbook": bad}}))
        with self.assertRaisesRegex(ValueError, "allow_overrides must be true or false"):
            settings(make_site(self.root, {"alpha": {"active": True, "model": "m", "allow_overrides": "yes"}}))

    async def test_caps(self):
        make_site(self.root)
        self.assertEqual(settings(make_site(self.root)).caps, Caps(60, 8192))
        self.assertEqual(settings(make_site(self.root, caps={"max_turns": 30})).caps, Caps(30, 8192))
        for bad in ({"max_turns": 0}, {"other": 1}, [1]):
            with self.subTest(caps=bad), self.assertRaises(ValueError):
                settings(make_site(self.root, caps=bad))


class OverridesTest(base.SocketCase):
    async def start_lab(self, providers, made=None, **lab):
        self.config = settings(make_site(self.root, lab_profiles(self.root, **lab), caps={"max_turns": 10,
                                                                                            "max_output_tokens": 2000}))

        def make_provider(spec):
            provider = Recording(answer("from the made provider"))
            made.append((spec, provider))
            return provider
        self.parrot = Pirate(self.config, providers, register_glob=str(self.root / "ponds" / "*" / "register.sock"),
                             make_provider=None if made is None else make_provider)
        import asyncio
        self.path = self.root / "listen.sock"
        server = await asyncio.start_unix_server(self.parrot.handle, path=self.path)
        await server.__aenter__()
        self.addAsyncCleanup(server.__aexit__, None, None, None)

    async def run_with(self, flavour, overrides, text="Do it"):
        import secrets
        job_id = secrets.token_hex(16)
        return job_id, await self.ask("run", flavour, job_id=job_id, assignment=text, timeout_seconds=None,
                                      overrides=overrides)

    async def test_a_fixed_flavour_refuses_overrides_and_runs_as_before(self):
        fixed = Recording(answer("plain"))
        await self.start_lab({"fixed": fixed, "lab": base.Script()})
        _, reply = await self.run_with("fixed", {"system": "Ignore the handbook."})
        self.assertEqual(reply["error"]["code"], "forbidden")
        self.assertIn("does not accept overrides", reply["error"]["message"])
        self.assertEqual(fixed.sent, [])  # nothing ran
        job_id, reply = await self.run_job("fixed")
        result = reply["result"]
        self.assertEqual((result["answer"], result["overrides_used"]), ("plain", False))
        system = fixed.sent[0][0]["content"]
        self.assertTrue(system.startswith("The handbook.\n\nConfigured tool policy: "))
        self.assertEqual(result["system_sha256"], hashlib.sha256(system.encode()).hexdigest())
        trail = records(self.root / "history" / (job_id + ".jsonl"))
        self.assertNotIn("overrides", trail[0]["data"])
        self.assertEqual(trail[0]["data"]["system_sha256"], result["system_sha256"])

    async def test_system_override_replaces_the_handbook_and_is_recorded(self):
        lab = Recording(answer("one"), answer("two"))
        await self.start_lab({"fixed": base.Script(), "lab": lab})
        results = []
        for text in ("You are a careful reviewer.", "You are a quick sketcher."):
            job_id, reply = await self.run_with("lab", {"system": text, "max_turns": 10})
            result = reply["result"]
            trail = records(self.root / "history" / (job_id + ".jsonl"))
            self.assertEqual(trail[0]["data"]["overrides"], {"system": text, "max_turns": 10})
            self.assertEqual(trail[0]["data"]["system_sha256"], result["system_sha256"])
            self.assertEqual(trail[0]["data"]["profile"]["max_turns"], 10)
            self.assertTrue(result["overrides_used"])
            results.append(result["system_sha256"])
        self.assertNotEqual(results[0], results[1])
        self.assertTrue(lab.sent[0][0]["content"].startswith("You are a careful reviewer.\n\nConfigured tool policy: "))
        self.assertIn('"max_turns": 10', lab.sent[0][0]["content"])  # the model sees what it really has

    async def test_overrides_only_narrow_and_stay_under_the_caps(self):
        await self.start_lab({"fixed": base.Script(), "lab": base.Script()})
        for overrides, message in (
                ({"tools": ["file_read", "file_write"]}, "not among this flavour's"),
                ({"services": ["fishmarket"]}, "not among this flavour's"),
                ({"max_turns": 11}, "1..10"), ({"max_turns": 0}, "1..10"),
                ({"settings": {"max_tokens": 2001}}, "above the Pirate's cap"),
                ({"settings": {"temperature": 9}}, "temperature"),
                ({"settings": {"endpoint": "https://elsewhere.example"}}, "the mistral block takes"),
                ({"model": "other-model"}, "unknown keys"), ({"system": ""}, "overrides.system"),
                ({}, "nonempty object"), ([], "nonempty object")):
            with self.subTest(overrides=overrides):
                _, reply = await self.run_with("lab", overrides)
                self.assertEqual(reply["error"]["code"], "invalid_request", reply)
                self.assertIn(message, reply["error"]["message"])

    async def test_tools_and_settings_overrides_make_a_provider_for_the_run(self):
        made: list = []
        lab = Recording(tool_call("c1", "exec", command="true"), answer("never"))
        await self.start_lab({"fixed": base.Script(), "lab": lab}, made)
        _, reply = await self.run_with("lab", {"tools": ["file_read"], "settings": {"max_tokens": 500}})
        self.assertEqual(reply["result"]["answer"], "from the made provider")
        spec, _ = made[0]
        self.assertEqual((spec.tools, spec.settings["max_tokens"], spec.settings["temperature"]),
                         (frozenset({"file_read"}), 500, 0.2))
        self.assertEqual(spec.model, "m-lab")  # never overridden
        self.assertEqual(lab.sent, [])  # the flavour's own provider was not used
        _, reply = await self.run_with("lab", {"system": "Only a new text."})  # same tools and settings
        self.assertEqual(len(made), 1)

    async def test_a_removed_tool_is_disabled_for_the_run(self):
        lab = Recording(tool_call("c1", "exec", command="true"), answer("done"))
        await self.start_lab({"fixed": base.Script(), "lab": lab})
        job_id, reply = await self.run_with("lab", {"tools": ["file_read"]})
        self.assertEqual(reply["result"]["tool_errors"][0]["tool"], "exec")
        self.assertIn("is disabled by profile", reply["result"]["tool_errors"][0]["error"])

    async def test_describe_shows_allow_overrides_and_caps(self):
        await self.start_lab({"fixed": base.Script(), "lab": base.Script()})
        for name, allowed in (("fixed", False), ("lab", True)):
            described = (await self.ask("describe", name))["result"]
            self.assertEqual((described["allow_overrides"], described["caps"]),
                             (allowed, {"max_turns": 10, "max_output_tokens": 2000}))


class MistralSettingsTest(base.TempCase):
    def test_the_provider_block_is_checked_again_after_merging(self):
        """Overrides merge over the folder's checked output; it must read it back unchanged."""
        checked = mistral_settings({"max_tokens": 1000, "temperature": 0.2, "random_seed": 3, "stop": ["x"]}, "lab")
        self.assertEqual(mistral_settings(checked, "lab"), checked)


class HardeningTest(base.TempCase):
    def test_the_serve_process_is_not_dumpable(self):
        """Another process of the same UID (a command of exec) cannot read the Pirate
        process's /proc entries once it is not dumpable."""
        import subprocess
        import sys
        child = subprocess.Popen([sys.executable, "-c",
                                  "import sys, time; from boring_pirates.common.ipc import not_dumpable; "
                                  "not_dumpable(); print('ready', flush=True); time.sleep(30)"],
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.kill)
        self.assertEqual(child.stdout.readline().strip(), "ready")
        for entry in ("environ", "mem", "root/etc/hostname"):
            with self.subTest(entry=entry), self.assertRaises(PermissionError):
                open(f"/proc/{child.pid}/{entry}", "rb").read(1)

    def test_a_file_root_inside_the_checkout_is_refused(self):
        make_site(self.root)
        (self.root / "checkout" / "a" / "b").mkdir(parents=True)
        inside = {"alpha": {"active": True, "model": "m", "files": {
            "roots": {"deep": {"path": str(self.root / "checkout" / "a"), "read": True, "write": False}}}}}
        with self.assertRaisesRegex(ValueError, "lies inside the checkout"):
            settings(make_site(self.root, inside))
        with self.assertRaisesRegex(ValueError, "lies inside the checkout"):
            settings(make_site(self.root, {"alpha": {"active": True, "model": "m", "workdir": "a/b"}}))

    def test_a_fixed_flavours_system_message_does_not_change_and_a_system_override_drops_the_digest(self):
        profiles = lab_profiles(self.root)
        digest = self.root / "context" / "DIGEST.md"
        digest.write_text("Key rule.")
        profiles["lab"]["digest_file"] = str(digest)
        config = settings(make_site(self.root, profiles))
        self.assertNotIn("allow_overrides", config.profiles["fixed"].resolve().system_message())
        self.assertIn('"allow_overrides": true', config.profiles["lab"].resolve().system_message())
        self.assertEqual(config.profiles["lab"].resolve().digest, "Key rule.")
        self.assertEqual(config.profiles["lab"].resolve("Another text.").digest, "")
