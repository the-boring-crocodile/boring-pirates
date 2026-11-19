"""Synthetic controller facts and a real accepted worker socket for runtime tests."""
import asyncio
from dataclasses import replace
import json
import os

from boring_outcomes import Refused

UID = os.getuid()


class FixtureAuthority:
    def __init__(self, root):
        self.root, self.allowed = root, set()

    async def request(self, operation, service=None, *, pond=None):
        if operation != "registration" or pond not in self.allowed:
            raise Refused("forbidden", "Pirate is not admitted for this Pond")
        return {"pond": pond, "pirate": "parrot", "uid": UID,
                "socket_path": str(self.root / "ponds" / pond / "register.sock")}


async def register_fixture(root, pirate, name="fixture"):
    authority = FixtureAuthority(root)
    authority.allowed.add(name)
    pirate.config = replace(pirate.config, service_authority=authority)
    directory = root / "ponds" / name
    directory.mkdir(parents=True, exist_ok=True)

    async def handle(reader, writer):
        try:
            await reader.readline()
            writer.write((json.dumps({"ok": True, "result": {
                "accepted": True, "pond": name, "pirate": "parrot",
                "ping_interval_seconds": 3}}) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    path = directory / "register.sock"
    server = await asyncio.start_unix_server(handle, path=path)
    path.chmod(0o660)
    for flavour in pirate.flavours:
        assert await pirate.registrar(str(path), flavour)() is not None
    return server
