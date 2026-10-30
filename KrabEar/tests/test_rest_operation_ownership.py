"""Flask subtree profile + статическая точка wiring; без ASR-доказательства."""
import ast
from concurrent.futures import ThreadPoolExecutor
import io
from pathlib import Path
import threading
import unittest
import wave


ROOT = Path(__file__).resolve().parents[1]


class RestOperationOwnershipTests(unittest.TestCase):
    profile = None  # Только проверенный disposable runner передаёт actual Flask objects.
    observed = []

    def test_post_root_tracks_real_adapter_child_after_outer_returns(self):
        if self.profile is None:
            self.skipTest('requires reviewed isolated Flask ownership profile')
        rs, submit_owned = self.profile['rest_server'], self.profile['submit_owned']
        original_transcribe = rs.transcriber.transcribe
        original_normalize = rs.engine.normalize_audio
        entered, release = threading.Event(), threading.Event()
        futures = []
        pool = ThreadPoolExecutor(max_workers=1)
        response = None
        before_history = rs.store.get_history_stats()
        self.assertEqual(rs._REST_OPERATIONS.snapshot()['operations'], [])
        def leaf():
            entered.set()
            if not release.wait(5):
                raise AssertionError('nested ownership leaf was not released')
            return 42
        def transcribe(*args, **kwargs):
            futures.append(submit_owned(pool, leaf))
            if not entered.wait(1):
                raise AssertionError('nested executor never entered leaf')
            return {'text': 'synthetic ownership response', 'engine': 'synthetic', 'confidence': 0.9}
        try:
            rs.transcriber.transcribe = transcribe
            rs.engine.normalize_audio = lambda path: None
            output = io.BytesIO()
            with wave.open(output, 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(8000)
                wav.writeframes(b'\x10\0' * 80)
            app = self.profile['app']
            response = app.test_client().post('/v1/stt/transcribe', data={
                'file': (io.BytesIO(output.getvalue()), 'synthetic.wav', 'audio/wav'),
                'persist_history': 'false', 'language': 'auto', 'quality_profile': 'balanced',
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['text'], 'synthetic ownership response')
            self.assertTrue(response.headers.get('X-Request-ID'))
            self.assertEqual(len(futures), 1)
            self.assertFalse(futures[0].done())
            self.assertFalse(futures[0].cancel())
            snapshot = rs._REST_OPERATIONS.snapshot()
            self.assertEqual(len(snapshot['operations']), 2)
            root = next(n for n in snapshot['operations'] if n['parent_operation_id'] is None)
            child = next(n for n in snapshot['operations'] if n['parent_operation_id'] == root['operation_id'])
            self.assertEqual(root['callable_state'], 'completed')
            self.assertTrue(root['registration_sealed'])
            self.assertEqual(root['state'], 'running')
            self.assertEqual(child['state'], 'running')
            self.assertTrue(child['execution_started'])
            self.assertFalse(child['registration_sealed'])
            self.assertFalse(snapshot['scope_complete'])
            self.assertEqual(snapshot['admission'], 'open')
            self.assertIsNone(snapshot['fence_result'])
            self.assertIn('gigaam_internal_stuck_future', snapshot['exclusions'])
            # Existing observer can be idle while real owned child still runs.
            advisory = rs._REST_ACTIVITY.snapshot()
            self.assertEqual(advisory['completion_scope'], 'transcriber_callable')
            self.assertEqual(advisory['state'], 'idle')
            release.set()
            self.assertEqual(futures[0].result(1), 42)
            settled = rs._REST_OPERATIONS.snapshot()
            self.assertTrue(all(n['state'] == 'completed' for n in settled['operations']))
            self.assertFalse(settled['scope_complete'])
            self.assertIsNone(settled['fence_result'])
            self.assertEqual(rs.store.get_history_stats(), before_history)
            self.observed.append({'http_status': response.status_code, 'owned_nodes': 2,
                                  'outer_completed_child_running': True,
                                  'after_release_subtree_completed': True,
                                  'scope_complete': False, 'fence_result': None})
        finally:
            release.set()
            pool.shutdown(wait=True, cancel_futures=True)
            rs.transcriber.transcribe = original_transcribe
            rs.engine.normalize_audio = original_normalize
            if response is not None:
                response.close()
        self.assertFalse(list(rs.TEMP_DIR.iterdir()))

    def test_engine_dispatch_binding_and_exclusions(self):
        tree = ast.parse((ROOT / 'core/engine.py').read_bytes())
        owner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'AudioEngine')
        method = next(n for n in owner.body if isinstance(n, ast.FunctionDef)
                      and n.name == '_transcribe_with_fallback_impl')
        calls = [n for n in ast.walk(method) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == 'submit_owned']
        self.assertEqual(len(calls), 1)
        self.assertEqual([n.id for n in calls[0].args], ['_pool', 'adapter_fn'])
        self.assertEqual(len(calls[0].keywords), 1)
        self.assertIsNone(calls[0].keywords[0].arg)
        self.assertEqual(calls[0].keywords[0].value.id, '_adapter_kwargs')
        self.assertTrue(any(isinstance(n, ast.ImportFrom) and n.module == 'core.operation_ownership'
                            and any(alias.name == 'submit_owned' for alias in n.names) for n in tree.body))
        registry = ast.parse((ROOT / 'core/operation_ownership.py').read_bytes())
        exclusions = next(n.value for n in registry.body if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == '_EXCLUSIONS' for t in n.targets))
        self.assertIn('gigaam_internal_stuck_future', ast.literal_eval(exclusions))
        self.assertIn('unregistered_adapter_pools', ast.literal_eval(exclusions))
