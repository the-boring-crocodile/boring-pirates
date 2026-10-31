from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from boring_pirates.common.hints import field_hint, hint
from boring_pirates.common.runtime import ToolRuntime
from boring_worker_kit.outcomes import Refusal


class HintTest(unittest.TestCase):
    def test_stale_write_says_read_first_and_gives_the_hash(self):
        text = hint("file_write", {"code": "stale", "message": "stale edit: expected hash differs",
                                   "current_sha256": "ab" * 32})
        self.assertIn("already exists", text)
        self.assertIn("ab" * 32, text)
        self.assertIn("file_read", text)
        self.assertIn("Do not repeat this call", text)
        absent = hint("file_patch", {"code": "stale", "message": "stale edit: file is absent", "current_sha256": None})
        self.assertIn("expected_sha256 null", absent)

    def test_field_hints_come_from_the_schema(self):
        self.assertEqual(field_hint("file_write", "file_write.arguments.newline"),
                         "newline must be lf or crlf, or leave newline out.")
        self.assertEqual(field_hint("file_write", "file_write.arguments.encoding"),
                         "encoding must be utf-8 or utf-16-le or utf-16-be, or leave encoding out.")
        self.assertIn("or null to create", field_hint("file_write", "file_write.arguments.expected_sha256"))
        self.assertIn("file_patch needs it", field_hint("file_patch", "file_patch.arguments.expected_sha256"))
        self.assertEqual(field_hint("file_read", "file_read.arguments.path"),
                         "file_read needs path, as string: check that it is there and of that kind.")
        self.assertIn("edits, as array", field_hint("file_patch", "file_patch.arguments.edits"))  # sent but empty
        self.assertTrue(field_hint("file_list", "file_list.arguments.recursive").startswith(
            "file_list has no argument recursive; its arguments are "))
        self.assertIsNone(field_hint("file_patch", "file_patch.arguments.edits.0.old_text"))
        self.assertIsNone(field_hint("nope", "nope.arguments.x"))
        both = hint("file_write", {"code": "invalid_request", "message": "invalid arguments",
                                   "fields": ["file_write.arguments.newline", "file_write.arguments.bogus"]})
        self.assertIn("newline must be lf or crlf", both)
        self.assertIn("file_write has no argument bogus", both)

    def test_hints_name_only_the_flavours_tools(self):
        stale = {"code": "stale", "message": "stale edit: expected hash differs", "current_sha256": "ab" * 32}
        text = hint("file_write", stale, frozenset({"file_read", "file_write"}))
        self.assertNotIn("file_patch", text)
        self.assertIn("replace it whole with file_write", text)
        text = hint("file_write_bytes", stale, frozenset({"file_write_bytes"}))
        self.assertNotIn("file_read", text)
        self.assertIn("file_write_bytes and that sha256", text)
        absent = {"code": "stale", "message": "stale edit: file is absent"}
        self.assertEqual(hint("file_patch", absent, frozenset({"file_patch"})), "The file does not exist.")
        self.assertIn("use file_write_bytes", hint("file_write_bytes", absent, frozenset({"file_write_bytes"})))

    def test_no_hint_for_other_refusals(self):
        self.assertIsNone(hint("file_read", {"code": "not_found", "message": "file does not exist"}))
        self.assertIsNone(hint(None, {"code": "stale", "message": "stale edit: expected hash differs"}))


class ServiceArgumentsTest(unittest.IsolatedAsyncioTestCase):
    async def refusal(self, name, args):
        runtime = ToolRuntime(authority=AsyncMock())
        with self.assertRaises(Refusal) as caught:
            await runtime.dispatch(None, None, {"id": "1", "name": name, "arguments": args}, 0.0)
        return caught.exception

    async def test_service_arguments_name_expected_and_given_keys(self):
        error = await self.refusal("service_call", {"service": "knowledge", "tool": "kb_search", "query": "secret-value"})
        self.assertNotIn("secret-value", error.message)  # names only, never values
        self.assertEqual(error.code, "invalid_request")
        self.assertEqual(error.message, "invalid service arguments: service_call takes arguments, service, tool "
                                        "(missing arguments; not arguments of service_call: query). "
                                        "The service tool's own arguments go inside `arguments`.")
        error = await self.refusal("service_list", {"service": "knowledge"})
        self.assertEqual(error.message, "invalid service arguments: service_list takes no arguments "
                                        "(not arguments of service_list: service).")
