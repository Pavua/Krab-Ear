"""Реальный Flask upload → executor; синтетический STT leaf, без ASR/сети."""
import sys
from pathlib import Path

SCRATCH = Path(sys.argv[1]).resolve()
sys.pycache_prefix = str(SCRATCH / 'pycache')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rest_stageb_boundary import Boundary  # noqa: E402
import _thread  # noqa: E402
import threading  # noqa: E402

REAL_START = threading.Thread.start
REAL_JOIN = threading.Thread.join
REAL_START_NEW = _thread.start_new_thread


class RestBoundary(Boundary):
    def __init__(self, scratch):
        super().__init__(scratch)
        self.workers = []

    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'backend.rest_server':
            return None
        return super().find_spec(fullname, path, target)

    def start(self, thread):
        import concurrent.futures.thread as executor
        if thread._target is not executor._worker or len(self.workers) >= 18:
            self.deny('only eighteen stdlib executor workers allowed')
        self.workers.append(thread)
        threading._start_new_thread = REAL_START_NEW
        try:
            REAL_START(thread)
        finally:
            threading._start_new_thread = lambda *a, **k: self.deny('direct thread start')

    def join(self, thread, timeout=None):
        if thread not in self.workers:
            self.deny('unknown thread join')
        REAL_JOIN(thread, timeout)


boundary = RestBoundary(SCRATCH)
boundary.install()
sys.path.insert(0, str(ROOT))
import atexit  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import wave  # noqa: E402


def run():
    assert sys.version_info[:2] == (3, 12)
    assert Path.cwd().resolve() == SCRATCH
    assert Path.home().resolve().is_relative_to(SCRATCH)
    from core import config
    from backend.state_store import StateStore
    data = Path.home() / 'Library/Application Support/KrabEar'
    store = StateStore(data)
    store.save_settings({'history_encryption_enabled': False, 'privacy_mode_enabled': False,
                         'stt_warmup_on_startup': False, 'rewriter_warmup_on_startup': False})
    from backend import rest_server as rs
    failures, cases, leaf_threads = [], [], []
    try:
        app = rs.create_app(config_mapping={'TESTING': True, 'RATELIMIT_ENABLED': False})
        client = app.test_client()
        config.settings.REST_API_AUTH_ENABLED = False
        config.settings.REST_API_KEY = ''
        # Эти два leaf не обращаются к hardware/provider; весь HTTP/executor real.
        rs.engine.normalize_audio = lambda path: None
        output = io.BytesIO()
        with wave.open(output, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b'\x10\0' * 80)
        results = [
            {'status': 'error', 'error': 'SYNTHETIC_DETAIL', 'text': 'SYNTHETIC_TEXT'},
            {'status': 'error', 'text': ''},
            {'error': 'SYNTHETIC_DETAIL', 'text': ''},
            {'text': '', 'reason': 'vad_skip', 'engine': 'vad_skip'},
        ]
        for call in (False, True):
            for number, result in enumerate(results):
                def transcribe(*args, **kwargs):
                    leaf_threads.append(threading.get_ident())
                    return result
                rs.transcriber.transcribe = transcribe
                data = {'file': (io.BytesIO(output.getvalue()), 'synthetic.wav', 'audio/wav'),
                        'persist_history': 'true'}
                if call:
                    data.update(request_profile='voice_gateway_call', language='auto')
                before_history = rs.store.get_history_stats()
                before_metrics = rs.metrics.total_requests
                response = client.post('/v1/stt/transcribe', data=data)
                expected = 503 if number < 3 else 200
                case = {'call': call, 'case': number, 'status': response.status_code}
                cases.append(case)
                if response.status_code != expected:
                    failures.append(case)
                if number < 3 and response.json != {'status': 'error', 'error': 'stt_failed', 'text': ''}:
                    failures.append({'payload_contract': case})
                if number < 3 and rs.store.get_history_stats() != before_history:
                    failures.append({'history_side_effect': case})
                if number < 3 and rs.metrics.total_requests != before_metrics:
                    failures.append({'metrics_side_effect': case})
                if number == 3:
                    assert response.json.get('reason') == 'vad_skip'
                assert response.headers.get('X-Request-ID')
                response.close()
                assert not list(rs.TEMP_DIR.iterdir())
        def extra_case(label, call, result, expected, *, privacy=None, expired=False, invalid=False):
            original_load = rs.store.load_settings
            original_call_privacy = rs.store.call_privacy_mode
            original_monotonic = rs.time.monotonic
            clock = [100.0]
            before_history = rs.store.get_history_stats()
            before_metrics = rs.metrics.total_requests
            before_workers = len(boundary.workers)

            def unavailable(*args, **kwargs):
                raise OSError('synthetic unreadable privacy')

            def transcribe(*args, **kwargs):
                leaf_threads.append(threading.get_ident())
                if privacy == 'enabled':
                    rs.store.save_settings({'privacy_mode_enabled': True})
                elif privacy == 'unreadable':
                    if call:
                        rs.store.call_privacy_mode = unavailable
                    else:
                        rs.store.load_settings = unavailable
                if expired:
                    clock[0] = 109.0
                return result

            rs.transcriber.transcribe = transcribe
            if expired:
                rs.time.monotonic = lambda: clock[0]
            data = {'file': (io.BytesIO(b'not audio' if invalid else output.getvalue()),
                             'synthetic.wav', 'audio/wav'), 'persist_history': 'true'}
            if call:
                data.update(request_profile='voice_gateway_call', language='auto', deadline_sec='8')
            try:
                response = client.post('/v1/stt/transcribe', data=data)
            finally:
                rs.store.load_settings = original_load
                rs.store.call_privacy_mode = original_call_privacy
                rs.time.monotonic = original_monotonic
                rs.store.save_settings({'privacy_mode_enabled': False})
            case = {'case': label, 'call': call, 'status': response.status_code}
            cases.append(case)
            assert response.status_code == expected, case
            if privacy:
                assert response.json.get('skipped') == 'privacy_mode', case
                assert not response.json.get('text'), case
            elif expired:
                assert not response.json.get('text'), case
            elif not invalid:
                assert response.json['text'] == result['text'], case
                if result.get('reason'):
                    assert response.json.get('reason') == result['reason'], case
            if expected != 200 or call:
                assert rs.store.get_history_stats() == before_history, case
            else:
                assert rs.store.get_history_stats()['active_count'] == before_history['active_count'] + bool(result.get('text')), case
            assert rs.metrics.total_requests == before_metrics + (expected == 200), case
            assert len(boundary.workers) - before_workers == (not invalid), case
            assert response.headers.get('X-Request-ID'), case
            assert 'SYNTHETIC_DETAIL' not in response.get_data(as_text=True), case
            response.close()
            assert not list(rs.TEMP_DIR.iterdir()), case

        # Privacy changed inside the actual worker: both ON and unreadable must
        # forbid retryable503/cloud fallback, without success metrics/history.
        for call in (False, True):
            for privacy in ('enabled', 'unreadable'):
                extra_case('privacy_' + privacy, call,
                           {'status': 'error', 'error': 'SYNTHETIC_DETAIL', 'text': 'SYNTHETIC_TEXT'},
                           403, privacy=privacy)
            extra_case('empty_audio', call, {'text': '', 'reason': 'empty_audio'}, 200)
            extra_case('null_error_success', call,
                       {'status': 'ok', 'error': None, 'text': 'synthetic retained'}, 200)
        # An already completed Future cannot bypass call deadline; privacy wins
        # even when error and expired deadline are observed together.
        extra_case('expired_engine_error', True,
                   {'status': 'error', 'error': 'SYNTHETIC_DETAIL', 'text': ''}, 504, expired=True)
        extra_case('expired_privacy_error', True,
                   {'status': 'error', 'error': 'SYNTHETIC_DETAIL', 'text': ''},
                   403, expired=True, privacy='enabled')
        for call in (False, True):
            extra_case('invalid_upload', call, {}, 400, invalid=True)
        expected_cases = ([(number, call) for call in (False, True) for number in range(4)]
                          + [(label, call) for call in (False, True) for label in
                             ('privacy_enabled', 'privacy_unreadable', 'empty_audio', 'null_error_success')]
                          + [('expired_engine_error', True), ('expired_privacy_error', True)]
                          + [('invalid_upload', False), ('invalid_upload', True)])
        assert len(cases) == 20
        assert [(case['case'], case['call']) for case in cases] == expected_cases
        assert len(leaf_threads) == 18
        assert all(t != threading.get_ident() for t in leaf_threads)
        assert all(not t.is_alive() for t in boundary.workers)
        assert not boundary.violations, boundary.violations
        receipt = {'full_rest_import': True, 'http_cases': len(cases), 'worker_starts': len(boundary.workers),
                   'violations': boundary.violations, 'cases': cases, 'failures': failures,
                   'scope': 'real Flask upload/executor/error mapping; synthetic transcriber leaf; no ASR/runtime proof'}
        print('REST_STAGEB_RECEIPT=' + json.dumps(receipt), flush=True)
        assert not failures, failures
    finally:
        rs._rest_engine_cleanup()
        atexit.unregister(rs._rest_engine_cleanup)
        assert rs.transcriber.close()


if __name__ == '__main__':
    run()
