"""A worker needs controller proof, an accepted compatible registration and its live socket."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import os
import socket
from unittest.mock import AsyncMock, patch

from boring_outcomes import Refused
from boring_pirates.common import ipc
import test_pirates as base
from test_pirates import Script, UID


class TrustTest(base.SocketCase):
    register_initial = False
    fake_pond = base.RegistrationTest.fake_pond

    async def prepare(self):
        await self.start({"alpha": Script(), "beta": Script()})
        self.allowed = True
        async def vouch(operation, service=None, *, pond=None):
            if not self.allowed:
                raise Refused("forbidden", "Pirate is not admitted for this Pond")
            return {"pond": pond, "pirate": "parrot", "uid": UID,
                    "socket_path": str(self.root / "ponds" / pond / "register.sock")}
        self.parrot.config = replace(self.config, service_authority=SimpleNamespace(request=vouch))
        self.reply = {"ok": True, "result": {
            "accepted": True, "pond": "garden", "pirate": "parrot",
            "ping_interval_seconds": 3}}
        path, received = await self.fake_pond("garden", self.reply)
        return path, received

    async def test_a_static_uid_is_not_permission_without_an_accepted_registration(self):
        await self.start({"alpha": Script(), "beta": Script()})
        self.assertEqual((await self.ask("ping", "alpha"))["outcome"]["code"], "forbidden")

    async def test_forged_worker_ack_does_not_replace_controller_admission(self):
        path, received = await self.prepare()
        self.allowed = False
        self.assertIsNone(await self.parrot.registrar(str(path), "alpha")())
        self.assertEqual(received, [])
        self.assertEqual((await self.ask("ping", "alpha"))["outcome"]["code"], "forbidden")

    async def test_identity_proof_and_acceptance_need_no_crocodile_version(self):
        path, received = await self.prepare()
        self.assertIsNotNone(await self.parrot.registrar(str(path), "alpha")())
        self.assertEqual(len(received), 1)
        self.assertTrue((await self.ask("ping", "alpha"))["ok"])

    async def test_refused_or_unaccepted_reply_revokes_previous_trust(self):
        path, _ = await self.prepare()
        self.assertIsNotNone(await self.parrot.registrar(str(path), "alpha")())
        self.reply["result"]["accepted"] = False
        self.assertIsNone(await self.parrot.registrar(str(path), "alpha")())
        self.assertEqual((await self.ask("ping", "alpha"))["outcome"]["code"], "forbidden")
        self.reply.clear()
        self.reply.update({"ok": False, "outcome": {
            "status": "refused", "code": "forbidden", "message": "withdrawn"}})
        self.assertIsNone(await self.parrot.registrar(str(path), "alpha")())

    async def test_live_socket_disappearance_revokes_before_the_next_scan(self):
        path, _ = await self.prepare()
        self.assertIsNotNone(await self.parrot.registrar(str(path), "alpha")())
        self.assertTrue((await self.ask("ping", "alpha"))["ok"])
        path.unlink()
        self.assertEqual((await self.ask("ping", "alpha"))["outcome"]["code"], "forbidden")

    async def test_socket_replacement_with_the_same_uid_revokes_before_scan(self):
        path, _ = await self.prepare()
        self.assertIsNotNone(await self.parrot.registrar(str(path), "alpha")())
        path.unlink()
        replacement = socket.socket(socket.AF_UNIX)
        self.addCleanup(replacement.close)
        replacement.bind(str(path))
        path.chmod(0o660)
        self.assertEqual((await self.ask("ping", "alpha"))["outcome"]["code"], "forbidden")

    async def test_controller_withdrawal_revokes_even_a_lingering_worker_socket(self):
        path, _ = await self.prepare()
        self.assertIsNotNone(await self.parrot.registrar(str(path), "alpha")())
        self.allowed = False
        self.assertEqual((await self.ask("ping", "alpha"))["outcome"]["code"], "forbidden")

    async def test_registration_peer_uid_is_verified_before_sending(self):
        path, received = await self.prepare()
        with patch.object(ipc, "peer_uid", return_value=UID + 1):
            self.assertIsNone(await self.parrot.registrar(str(path), "alpha")())
        self.assertEqual(received, [])
