"""Unexpected exceptions keep their text in the Pirate log, never in replies."""
import json
import unittest

from boring_outcomes import Failed
from boring_pirates.common.outcomes import failure_data


class FailurePresentationTest(unittest.TestCase):
    def test_unexpected_exception_is_logged_but_not_returned(self):
        secret = "/private/provider?token=synthetic-secret"
        with self.assertLogs("boring_pirates", level="ERROR") as logged:
            try:
                raise RuntimeError(secret)
            except RuntimeError as error:
                reply = failure_data(error)
        self.assertEqual(reply["message"],
            "RuntimeError: The Pirate failed; see its log for details.")
        self.assertEqual((reply["code"], reply["effects"]), ("pirate_failed", "unknown"))
        self.assertNotIn(secret, json.dumps(reply))
        self.assertIn(secret, "\n".join(logged.output))

    def test_explicit_outcomes_keep_their_intended_message(self):
        error = Failed("unavailable", "service did not answer", effects="unknown")
        self.assertEqual(failure_data(error), error.as_data())
