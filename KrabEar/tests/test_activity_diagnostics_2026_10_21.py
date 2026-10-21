"""Реальные metadata helpers без BackendService init, моделей и аудиоустройств."""
import threading
import time
import unittest
from unittest.mock import Mock, patch

from backend.recorder import AudioRecorder
from backend.recording_core_service import RecordingCoreService
from backend.meeting_session_service import MeetingSessionService


class ComponentActivityTests(unittest.TestCase):
    def recorder(self):
        recorder = AudioRecorder.__new__(AudioRecorder)
        recorder._lock = threading.Lock()
        recorder._lifecycle_lock = threading.Lock()
        recorder._is_recording = False
        recorder._thread = None
        return recorder

    def recording(self):
        service = RecordingCoreService.__new__(RecordingCoreService)
        service._recording_lifecycle_lock = threading.RLock()
        service._active_generation = None
        service._finalizing_generations = {}
        service.recorder = self.recorder()
        return service

    def meeting(self):
        service = MeetingSessionService.__new__(MeetingSessionService)
        for name in ['_transition_lock', '_lock', '_worker_lock']:
            setattr(service, name, threading.RLock())
        service._setup_done_event = threading.Event()
        service._setup_done_event.set()
        service._stopping = service._recovery_pending = False
        service._session = service._worker = None
        return service

    def assert_contended(self, service, lock):
        acquired, release = threading.Event(), threading.Event()

        def hold():
            with lock:
                acquired.set()
                release.wait(3)

        thread = threading.Thread(target=hold)
        thread.start()
        try:
            self.assertTrue(acquired.wait(1))
            start = time.monotonic()
            result = service.activity_snapshot()
            self.assertLess(time.monotonic() - start, 0.2)
            self.assertFalse(result['known'])
            self.assertEqual(result['state'], 'unknown')
        finally:
            release.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_finalizing_without_capture_is_busy(self):
        service = self.recording()
        service._finalizing_generations = {'g1': {'private': 'do not expose'}}
        result = service.activity_snapshot()
        self.assertEqual(result['state'], 'busy')
        self.assertEqual(result['finalizing_count'], 1)
        self.assertNotIn('private', str(result))
        service._finalizing_generations.clear()
        self.assertEqual(service.activity_snapshot()['state'], 'idle')

    def test_capture_active_generation_and_retained_recorder_worker(self):
        service = self.recording()
        for field, value in [('capture', True), ('generation', {}), ('worker', Mock(is_alive=lambda: True))]:
            with self.subTest(field=field):
                service.recorder._is_recording = value if field == 'capture' else False
                service._active_generation = value if field == 'generation' else None
                service.recorder._thread = value if field == 'worker' else None
                self.assertEqual(service.activity_snapshot()['state'], 'busy')

    def test_no_legacy_blocking_recorder_property_or_preview_reads(self):
        service = self.recording()
        with patch.object(AudioRecorder, 'is_recording', property(lambda _: (_ for _ in ()).throw(AssertionError('blocking property')))):
            self.assertTrue(service.activity_snapshot()['known'])
        self.assertFalse(hasattr(service, '_preview_lock'))

    def test_recorder_missing_or_provider_error_is_unknown(self):
        service = self.recording()
        for recorder in [object(), Mock(activity_snapshot=Mock(side_effect=RuntimeError))]:
            service.recorder = recorder
            self.assertEqual(service.activity_snapshot()['state'], 'unknown')
            service._finalizing_generations['g'] = {}
            self.assertEqual(service.activity_snapshot()['state'], 'busy')
            service._finalizing_generations.clear()

    def test_core_and_recorder_lock_contention_are_nonblocking(self):
        service = self.recording()
        for lock in [service._recording_lifecycle_lock, service.recorder._lifecycle_lock, service.recorder._lock]:
            self.assert_contended(service, lock)

    def test_meeting_setup_stopping_recovery_session_and_worker_are_busy(self):
        service = self.meeting()
        self.assertEqual(service.activity_snapshot()['state'], 'idle')
        for field, value in [('_stopping', True), ('_recovery_pending', True), ('_session', object()),
                             ('_worker', Mock(is_alive=lambda: True))]:
            with self.subTest(field=field):
                old = getattr(service, field)
                setattr(service, field, value)
                self.assertEqual(service.activity_snapshot()['state'], 'busy')
                setattr(service, field, old)
        service._setup_done_event.clear()
        self.assertEqual(service.activity_snapshot()['state'], 'busy')

    def test_meeting_all_locks_contended_are_unknown(self):
        service = self.meeting()
        for field in ['_transition_lock', '_lock', '_worker_lock']:
            self.assert_contended(service, getattr(service, field))

    def test_aggregate_does_not_claim_owner_idle_or_cas_generation(self):
        from backend.activity_diagnostics import ipc_activity_snapshot
        result = ipc_activity_snapshot(self.recording(), self.meeting())
        self.assertEqual(result['state'], 'unknown')
        self.assertFalse(result['coverage']['complete'])
        self.assertIsNone(result['generation'])
        self.assertFalse(result['generation_known'])
        self.assertTrue(result['advisory'])
        self.assertTrue(result['owner_epoch'])
        self.assertEqual(result['owner_epoch'], ipc_activity_snapshot(None, None)['owner_epoch'])
        recording = self.recording()
        recording._finalizing_generations['g'] = {}
        self.assertEqual(ipc_activity_snapshot(recording, None)['state'], 'busy')

    def test_existing_memory_ledger_handler_adds_observations_without_writes(self):
        from backend.service import BackendService
        shell = Mock()
        shell._recording_core_svc, shell._meeting_svc = self.recording(), self.meeting()
        shell._memory_ledger.read_all.return_value = {'legacy': True}
        shell._memory_conductor.get_diagnostics.return_value = {'legacy': True}
        with patch('backend.brain_lease.observe_brain_lease', return_value={'known': False, 'state': 'contention'}) as lease:
            result = BackendService._handle_get_memory_ledger(shell, {})
        self.assertEqual(result['ledger'], {'legacy': True})
        self.assertEqual(result['activity']['owner'], 'backend')
        self.assertEqual(result['lease_observation']['state'], 'contention')
        shell._memory_conductor.get_diagnostics.assert_called_once_with(read_only=True)
        lease.assert_called_once_with()

    def test_actual_dispatch_uses_additive_diagnostic_handler(self):
        from backend.service import BackendService
        shell = Mock()
        shell._recording_core_svc, shell._meeting_svc = self.recording(), self.meeting()
        shell._memory_ledger.read_all.return_value = {}
        shell._memory_conductor.get_diagnostics.return_value = {}
        shell._request_signer = shell._ipc_throttle = None
        shell._get_runtime_setting = lambda *_: False
        shell._handle_get_memory_ledger = lambda params: BackendService._handle_get_memory_ledger(shell, params)
        shell._dispatch_table = BackendService._build_dispatch_table(shell)
        with patch('backend.brain_lease.observe_brain_lease', return_value={'state': 'absent', 'known': True}):
            result = BackendService.handle_request(shell, {'id': 'activity', 'method': 'get_memory_ledger', 'params': {}})
        self.assertTrue(result['ok'])
        self.assertEqual(result['result']['activity']['owner'], 'backend')

    def test_read_only_conductor_does_not_sync_shadow_marker(self):
        from backend.memory_conductor import MemoryConductor
        conductor = MemoryConductor.__new__(MemoryConductor)
        conductor._sync_shadow_marker = Mock(side_effect=AssertionError('diagnostic write'))
        conductor._get = lambda key, default: default
        conductor._thread = None
        conductor._last_tick_ts = conductor._shadow_since = None
        conductor._pressure_streak = conductor._would_skip_brain_reload = 0
        conductor._counters, conductor._decisions = {}, []
        self.assertEqual(conductor.get_diagnostics(read_only=True)['residents'], {})
        conductor._sync_shadow_marker.assert_not_called()


if __name__ == '__main__':
    unittest.main()
