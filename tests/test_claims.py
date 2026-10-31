from __future__ import annotations

import unittest

from boring_pirates.common.claims import check


class ClaimsTest(unittest.TestCase):
    def claims(self, answer, writes=()):
        result = check(answer, [{"root": "work", "path": p} for p in writes])
        return None if result is None else result["claimed"]

    def test_the_files_line_in_its_usual_shapes(self):
        self.assertEqual(self.claims("Patched.\nFILES: receiving-flow.dot"), ["receiving-flow.dot"])
        self.assertEqual(self.claims("**FILES:** `a.dot`, ./b/c.md."), ["a.dot", "b/c.md"])
        self.assertEqual(self.claims("FILES:\n- work:x.txt\n- y.txt (new)\n\nDOUBTS: none"), ["work:x.txt", "y.txt"])
        self.assertEqual(self.claims("FILES:\n1. a.md\n2) b.md"), ["a.md", "b.md"])
        self.assertEqual(self.claims("FILES: flow.dot (patched)"), ["flow.dot"])
        self.assertEqual(self.claims("FILES: a.md and b.md; c.md — new"), ["a.md", "b.md", "c.md"])
        self.assertEqual(self.claims("FILES: a.md\nmore\nFILES: b.md"), ["b.md"])  # the last label counts
        self.assertEqual(self.claims("FILES: a.md:12"), ["a.md"])
        self.assertIsNone(self.claims("No files line here."))
        self.assertIsNone(self.claims(None))

    def test_prose_and_quotes_are_not_claims(self):
        for answer in ("FILES: none", "FILES: none, the write was refused", "FILES: none; I could not write",
                       "FILES: *none*", "FILES: no files were written", "FILES: nothing (refused)"):
            with self.subTest(answer=answer):
                self.assertEqual(self.claims(answer), [])
        quoted = "FILES: a.md\n\nThe template says:\n```\nFILES: fake.md\n```\n"
        self.assertEqual(self.claims(quoted), ["a.md"])
        self.assertEqual(self.claims("FILES: C:\\work\\a.txt"), ["C:\\work\\a.txt"])  # not a root prefix

    def test_a_path_with_spaces_counts_when_it_was_written(self):
        result = check("FILES: docs/My Notes.md", [{"root": "work", "path": "docs/My Notes.md"}])
        self.assertEqual(result, {"claimed": ["docs/My Notes.md"], "claimed_not_written": [], "written_not_claimed": []})

    def test_claims_against_writes(self):
        writes = [{"root": "work", "path": "out/flow.dot"}, {"root": "work", "path": "notes.md"}]
        self.assertEqual(check("FILES: docs:notes.md", writes)["claimed_not_written"], ["docs:notes.md"])  # wrong root
        self.assertEqual(check("FILES: flow.dot, view.dot", writes),
                         {"claimed": ["flow.dot", "view.dot"], "claimed_not_written": ["view.dot"],
                          "written_not_claimed": ["notes.md"]})
        self.assertEqual(check("FILES: flow.dot", [])["claimed_not_written"], ["flow.dot"])
        self.assertIsNone(check("Done.", writes))
