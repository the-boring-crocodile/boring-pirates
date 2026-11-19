"""Image optimistic concurrency uses the shared stale outcome."""
import base64
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from boring_outcomes import Refused
from boring_pirates.common.images import read_image


class ImageReadTest(unittest.IsolatedAsyncioTestCase):
    async def test_changed_image_is_stale(self):
        files = SimpleNamespace(tools=SimpleNamespace(call=AsyncMock(return_value={
            "bytes": 1, "data": base64.b64encode(b"x").decode(), "length": 1,
            "eof": True, "sha256": "0" * 64})))
        with self.assertRaises(Refused) as caught:
            await read_image(files, "call", "work", "image.png")
        self.assertEqual(caught.exception.as_data(), {
            "status": "refused", "code": "stale",
            "message": "the image changed while it was read; try again"})
