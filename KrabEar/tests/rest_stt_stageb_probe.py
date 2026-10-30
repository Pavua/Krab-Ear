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
        if thread._target is not executor._worker or len(self.workers) >= 8:
            self.deny('only eight stdlib executor workers allowed')
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
        assert len(leaf_threads) == 8
        assert all(t != threading.get_ident() for t in leaf_threads)
        assert all(not t.is_alive() for t in boundary.workers)
        assert not boundary.violations, boundary.violations
        receipt = {'full_rest_import': True, 'http_cases': 8, 'worker_starts': len(boundary.workers),
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
