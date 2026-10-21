"""Cooperating flavours serialize edits independent of root labels/path views."""
import concurrent.futures
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from boring_pirates.common.files import CheckoutFiles
from boring_worker_kit.file_policy import FilePolicy
from boring_worker_kit import files
from boring_worker_kit.outcomes import Refusal


class FileLockViewsTest(unittest.TestCase):
    def test_shared_target_compare_and_swap(self):
        for view in ('same', 'alias', 'nested'):
            with self.subTest(view=view), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                child = root / 'child'
                child.mkdir()
                target = child / 'note'
                target.write_text('original')
                expected = hashlib.sha256(b'original').hexdigest()
                alias = 'other' if view == 'alias' else 'work'
                second_root = child if view == 'nested' else root
                second_path = 'note' if second_root == child else 'child/note'
                def owner(name, directory):
                    return CheckoutFiles(root, policy=FilePolicy.parse({'roots': {
                        name: {'path': str(directory), 'read': True, 'write': True}}}))
                owners = [owner('work', root), owner(alias, second_root)]
                start = threading.Barrier(2)
                replacement = threading.Barrier(2)
                atomic = files.atomic_write
                def widened(*args, **kwargs):
                    # Old split locks let both writers reach replacement. Correct
                    # locking lets only one through; the other later sees stale.
                    try:
                        replacement.wait(timeout=.25)
                    except threading.BrokenBarrierError:
                        pass
                    return atomic(*args, **kwargs)
                def write(index):
                    start.wait(timeout=2)
                    try:
                        owners[index].tools._call('file_write', {
                            'operation': 'file_write', 'request_id': str(index),
                            'root': 'work' if index == 0 else alias,
                            'path': 'child/note' if index == 0 else second_path,
                            'content': str(index), 'expected_sha256': expected})
                        return 'written'
                    except Refusal as error:
                        return error.code
                    except ValueError as error:
                        if str(error).startswith('stale edit:'):
                            return 'stale'
                        raise
                try:
                    with patch.object(files, 'atomic_write', widened):
                        with concurrent.futures.ThreadPoolExecutor(2) as pool:
                            outcomes = list(pool.map(write, range(2)))
                    self.assertCountEqual(outcomes, ['written', 'stale'])
                    self.assertEqual(target.read_text(), str(outcomes.index('written')))
                finally:
                    for instance in owners:
                        instance.close()
