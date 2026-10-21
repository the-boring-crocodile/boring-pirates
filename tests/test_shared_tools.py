"""New tool journeys and isolation boundaries, without paid model calls."""
import asyncio
import json
import base64
from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, AsyncMock

from boring_pirates.common import assignment
from boring_pirates.common.history import FileHistory
from boring_pirates.common.ipc import settings
from boring_pirates.common.runtime import ToolRuntime
from boring_worker_kit.services import Services
from boring_worker_kit.file_policy import FilePolicy
from boring_worker_kit.execution import EnvironmentExecutor
from boring_worker_kit.host_limits import HostLimits
from boring_worker_kit.settings import EnvironmentSettings
from boring_worker_kit.task_client import ContainmentError
from boring_worker_kit.services import ServiceFailure
from boring_worker_kit.files import FileTools
from test_pirates import make_site, Script, tool_call, answer, records, UID

class ToolCase(unittest.IsolatedAsyncioTestCase):
    """Set-up and helpers for the tool tests; no tests of its own."""
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = settings(make_site(self.root, profiles={'alpha': {'active': True, 'model': 'synthetic',
            'allowed_tools': ['file_read', 'file_write', 'file_patch', 'file_read_bytes', 'file_write_bytes',
                              'file_list', 'file_search', 'exec', 'service_list', 'service_describe', 'service_call'],
            'allowed_services': ['canvas']}}))
        self.spec = self.config.profiles['alpha']
        self.job = 'a'*32


    async def run_script(self, script, runtime=None, timeout=5, spec=None):
        history = FileHistory(self.config.history_dir, self.job, 'alpha', UID, 'mistral', 'synthetic')
        try:
            return await assignment.run(script, self.config.checkout, (spec or self.spec).resolve(),
                                        history, 'Goal/input/output/acceptance, or ordinary text.', timeout, runtime)
        finally:
            history.close()


    def trail(self):
        return records(self.config.history_dir/(self.job+'.jsonl'))


    def executor(self):
        # Explicit injected development fixture only: production has no local fallback.
        config = EnvironmentSettings('parrot', self.config.checkout, (self.config.checkout,), None,
                                     'boring-pirate-task-parrot', Path('/system.slice/system-pirateparrot.slice'), 1024)
        return EnvironmentExecutor(config, HostLimits(task_slots=2, job_timeout_seconds=30))


class SharedToolTest(ToolCase):
    async def test_file_format_parity_strict_admission_and_bytes(self):
        original = b'\xff\xfe' + 'first\r\nsecond\r\n'.encode('utf-16-le')
        (self.config.checkout/'note').write_bytes(original)
        result = await self.run_script(Script(
            tool_call('1', 'file_read', root='work', path='note'),
            tool_call('2', 'file_patch', root='work', path='note', expected_sha256=hashlib.sha256(original).hexdigest(),
                      edits=[{'old_text': 'first', 'new_text': 'new'}]),
            tool_call('3', 'file_write_bytes', root='work', path='raw', data=base64.b64encode(b'\x00\xff').decode(), expected_sha256=None),
            tool_call('4', 'file_read_bytes', root='work', path='raw'),
            tool_call('5', 'file_write', root='work', path='bad', content='no', expected_sha256=None, operation='file_read'),
            answer('done')))
        self.assertEqual((result['state'], result['write_actions_count'], result['tool_errors_count']), ('completed', 2, 1))
        self.assertEqual((self.config.checkout/'note').read_bytes(), b'\xff\xfe'+'new\r\nsecond\r\n'.encode('utf-16-le'))
        self.assertEqual((self.config.checkout/'raw').read_bytes(), b'\x00\xff')
        self.assertFalse((self.config.checkout/'bad').exists())
        self.assertEqual(result['tool_errors'][0]['tool'], 'file_write')

    async def test_disallowed_dispatch_and_readonly_root(self):
        readonly = self.root/'facts'; readonly.mkdir(); (readonly/'doc').write_text('fact')
        policy = FilePolicy.parse({'roots': {'facts': {'path': str(readonly), 'read': True, 'write': False}}})
        spec = replace(self.spec, tools=frozenset({'file_read','file_write'}), files=policy)
        result = await self.run_script(Script(
            tool_call('1', 'exec', command='touch wrong'),
            tool_call('2', 'file_write', root='facts', path='bad', content='x', expected_sha256=None),
            tool_call('3', 'file_read', root='facts', path='doc'), answer('done')), spec=spec)
        self.assertEqual(result['tool_errors_count'], 2)
        self.assertFalse((readonly/'bad').exists())
        self.assertEqual(self.trail()[-3]['data']['value']['content'], 'fact')
        self.assertFalse((self.config.checkout/'wrong').exists())  # the refused exec ran nothing
        self.assertEqual(sorted(p.name for p in readonly.iterdir()), ['doc'])
        self.assertEqual((readonly/'doc').read_text(), 'fact')

    async def test_exec_real_uid_nonzero_and_bounded_output(self):
        runtime = ToolRuntime(executor=self.executor())
        result = await self.run_script(Script(
            tool_call('1', 'exec', command="touch made; id -u; python3 -c 'print(\"x\"*10000)'; exit 7", timeout_seconds=10),  # room for a loaded host
            answer('nonzero reported')), runtime)
        outcome = next(x['data'] for x in self.trail() if x['kind']=='tool_result')
        self.assertTrue(outcome['ok'])
        self.assertEqual(outcome['value']['exit_code'], 7)
        self.assertTrue(outcome['value']['stdout_truncated'])
        self.assertTrue(outcome['value']['stdout'].startswith(str(UID)))
        self.assertEqual(result['tool_errors_count'], 0)
        self.assertTrue((self.config.checkout/'made').is_file())  # it really ran, in the checkout

    async def test_file_write_settles_and_receipt_survives_disconnect(self):
        entered, release = threading.Event(), threading.Event()
        original = FileTools._call
        def slow(owner, name, request):
            entered.set(); release.wait(2)
            return original(owner, name, request)
        with patch.object(FileTools, '_call', slow):
            work = asyncio.create_task(self.run_script(Script(tool_call('1', 'file_write', root='work',
                path='settled', content='yes', expected_sha256=None))))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            work.cancel(); await asyncio.sleep(.02); self.assertFalse(work.done()); release.set()
            with self.assertRaises(asyncio.CancelledError): await work
        self.assertEqual((self.config.checkout/'settled').read_text(), 'yes')
        receipt = next(x['data'] for x in self.trail() if x['kind']=='tool_result')
        self.assertTrue(receipt['settled_after_cancellation'])
        self.assertEqual(self.trail()[-1]['data']['write_actions_count'], 1)

    async def test_exec_cancellation_keeps_sibling_alive(self):
        executor = self.executor()
        # Its own timeout is far off, so only the cancellation can end it within the poll below.
        slow = asyncio.create_task(executor.execute('echo $$ > pid; exec sleep 10', None, 20))
        sibling = asyncio.create_task(executor.execute('sleep .1; printf sibling', None, 10))
        pid_file = self.config.checkout/'pid'
        for _ in range(500):  # up to 5 s on a loaded host
            if pid_file.exists() and pid_file.read_text().strip():
                break
            await asyncio.sleep(.01)
        pid = int(pid_file.read_text())
        self.assertTrue(Path(f'/proc/{pid}').exists())
        slow.cancel()
        with self.assertRaises(asyncio.CancelledError): await slow
        self.assertEqual((await sibling)['stdout'], 'sibling')
        for _ in range(500):  # the cancelled command is gone (killed and reaped), not left running
            if not Path(f'/proc/{pid}').exists():
                break
            await asyncio.sleep(.01)
        self.assertFalse(Path(f'/proc/{pid}').exists())

    async def test_cleanup_uncertainty_is_fatal(self):
        executor = AsyncMock()
        executor.execute.side_effect = ContainmentError('cleanup not confirmed')
        with self.assertRaises(ContainmentError):
            await self.run_script(Script(tool_call('1', 'exec', command='anything')), ToolRuntime(executor=executor))
        self.assertEqual(self.trail()[-1]['data']['state'], 'failed')

    async def test_service_narrowing_identity_and_failure_evidence(self):
        authority = AsyncMock()
        authority.request.side_effect = [
            {'caller':'pirate/parrot', 'services':[{'name':'canvas'}, {'name':'private'}]},
            {'caller':'pirate/parrot','token':'parrot-token','grant':{'name':'canvas','url':'https://canvas.test/mcp'}},
            {'caller':'pirate/other','token':'other-token','grant':{'name':'canvas','url':'https://canvas.test/mcp'}}]
        runtime = ToolRuntime(authority=authority)
        self.assertIsNone(runtime.services)  # the caller is known with the first token
        self.addAsyncCleanup(runtime.close)
        call = AsyncMock(side_effect=ServiceFailure({'status':'failed','effects':'unknown','message':'lost'}))
        with patch.object(Services, 'call', call):
            result = await self.run_script(Script(
                tool_call('1','service_list'),
                tool_call('2','service_call',service='private',tool='write',arguments={}),
                tool_call('3','service_call',service='canvas',tool='write',arguments={'text':'x'}),
                tool_call('4','service_call',service='canvas',tool='write',arguments={'text':'y'}),
                answer('unknown effects')), runtime)
        self.assertEqual(authority.request.await_count, 3)
        call.assert_awaited_once_with('canvas', 'write', {'text':'x'}, 'parrot-token', grant={'name':'canvas','url':'https://canvas.test/mcp'})
        self.assertEqual(runtime.services.pond, 'pirate/parrot')  # set once; another caller is a failure
        outcomes=[x['data'] for x in self.trail() if x['kind']=='tool_result']
        self.assertEqual([x['name'] for x in outcomes[0]['value']['services']], ['canvas'])
        self.assertEqual(outcomes[1]['status'], 'refused')
        self.assertEqual(outcomes[2]['failure']['effects'], 'unknown')
        self.assertIn("caller 'pirate/other'", outcomes[3]['error'])
        self.assertEqual(result['tool_errors_count'], 3)


def png(width, height):
    import struct, zlib
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    raw = b''.join(b'\x00' + b'\x00\x00\x00' * width for _ in range(height))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


class ViewImageTest(ToolCase):
    async def test_the_model_sees_the_latest_image_only(self):
        (self.config.checkout / 'one.png').write_bytes(png(3, 2))
        (self.config.checkout / 'two.png').write_bytes(png(5, 4))
        (self.config.checkout / 'pic.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        seen = []

        class Watching(Script):
            async def complete(self, messages):
                seen.append([dict(m) for m in messages])
                return await super().complete(messages)
        spec = replace(self.spec, tools=self.spec.tools | {'view_image'})
        result = await self.run_script(Watching(
            tool_call('1', 'view_image', root='work', path='one.png'),
            tool_call('2', 'view_image', root='work', path='pic.svg'),
            tool_call('3', 'view_image', root='work', path='two.png'),
            answer('looked')), spec=spec)
        self.assertEqual(result['state'], 'completed')
        after_first = seen[1][-1]
        self.assertEqual(after_first['role'], 'user')
        self.assertTrue(after_first['content'][1]['image_url'].startswith('data:image/png;base64,'))
        tool_result = next(m for m in seen[1] if m['role'] == 'tool')
        self.assertIn('"width": 3', tool_result['content'])  # metadata only, no image bytes in the text
        self.assertNotIn('base64', tool_result['content'])
        svg = [m for m in seen[2] if m['role'] == 'tool'][-1]
        self.assertIn('PNG or JPEG', svg['content'])
        final = seen[3]
        images = [p for m in final if isinstance(m.get('content'), list) for p in m['content'] if p['type'] == 'image_url']
        self.assertEqual(len(images), 1)  # the first image became a note
        self.assertIn('only the latest image', json.dumps(final))
        tool_records = [r['data'] for r in self.trail() if r['kind'] == 'tool_result']
        self.assertNotIn('base64,', json.dumps(tool_records))  # the transcript keeps no image bytes

    async def test_not_offered_unless_configured_and_limits_hold(self):
        from boring_pirates.common import images
        (self.config.checkout / 'big.png').write_bytes(png(5000, 1))
        spec = replace(self.spec, tools=self.spec.tools | {'view_image'})
        result = await self.run_script(Script(
            tool_call('1', 'view_image', root='work', path='big.png'), answer('ok')), spec=spec)
        self.assertIn('4096', result['tool_errors'][0]['error'])
        self.job = 'b' * 32
        result = await self.run_script(Script(
            tool_call('1', 'view_image', root='work', path='big.png'), answer('ok')))
        self.assertIn('disabled by profile', result['tool_errors'][0]['error'])
        with self.assertRaises(Exception):
            images.dimensions(b'\xff\xd8\xff\xd9')
