"""Настоящие shell/Unix IPC gates не разрешают lifecycle над stop-tail."""
import json

import pytest

from test_restart_unknown_activity_2026_09_07 import reply, run_gate

SCRIPTS = ('safe_backend_restart.command', 'install_backend_launchagent.command')
IDLE = {'is_recording': False, 'owner': None, 'generation_token': None,
        'start_request_id': None, 'is_finalizing': False, 'finalizing_count': 0}


@pytest.mark.parametrize('script', SCRIPTS)
@pytest.mark.parametrize('patch', [
    {'is_finalizing': True, 'finalizing_count': 1},
    {'is_finalizing': False, 'finalizing_count': 1},
    {'finalizing_count': None}, {'finalizing_count': False},
    {'finalizing_count': -1}, {'finalizing_count': '0'},
    {'finalizing_count': 0.0}, {'is_finalizing': None},
    {'owner': 'dictation'}, {'generation_token': 'G1'},
    {'start_request_id': 'S1'},
])
def test_tail_or_unknown_refuses_lifecycle(tmp_path, script, patch):
    state = dict(IDLE, **patch)
    result, calls = run_gate(tmp_path, script, reply(**state), reply(active=False),
                             function_only=True)
    assert result.returncode == 1
    assert not calls


@pytest.mark.parametrize('script', SCRIPTS)
@pytest.mark.parametrize('missing', ['finalizing_count', 'is_finalizing', 'owner',
                                     'generation_token', 'start_request_id'])
def test_missing_lifecycle_proof_refuses(tmp_path, script, missing):
    state = dict(IDLE)
    del state[missing]
    result, calls = run_gate(tmp_path, script,
                             json.dumps({'id': '1', 'ok': True, 'result': state}),
                             reply(active=False), function_only=True)
    assert result.returncode == 1
    assert not calls


@pytest.mark.parametrize('script', SCRIPTS)
def test_explicit_empty_finalization_allows_gate(tmp_path, script):
    result, calls = run_gate(tmp_path, script, reply(**IDLE), reply(active=False),
                             function_only=True)
    assert result.returncode == 0
    assert 'bootout TEST_ONLY' in calls
