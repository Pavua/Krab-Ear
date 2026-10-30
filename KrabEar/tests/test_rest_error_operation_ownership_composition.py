"""Combined c77 error gate + fae subtree ownership; synthetic leaf, no ASR."""
from concurrent.futures import ThreadPoolExecutor
import io
from pathlib import Path
import threading
import unittest
import wave


ROOT = Path(__file__).resolve().parents[1]


class RestErrorOperationOwnershipTests(unittest.TestCase):
    profile = None  # Только проверенный disposable runner передаёт actual Flask objects.
    observed = []

    def test_error_503_retains_running_child_without_history_or_success_metrics(self):
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
        before_metrics = rs.metrics.total_requests
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
            return {'status': 'error', 'error': 'SYNTHETIC_DETAIL', 'text': 'SYNTHETIC_TEXT'}
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
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json, {'status': 'error', 'error': 'stt_failed', 'text': ''})
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
            self.assertEqual(rs.metrics.total_requests, before_metrics)
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
