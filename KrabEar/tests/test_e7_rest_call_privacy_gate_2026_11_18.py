"""E7 gate: desired invariant tests against ea9931122; REDs are expected.
All collaborators are synthetic, no network, devices, real models or accounts.
AST extraction is explicitly limited to functions with process/import side effects.
"""

import ast

import functools

import hashlib

import hmac

import json

import logging

import os

from pathlib import Path

import subprocess

import sys

import threading

from types import SimpleNamespace

from unittest.mock import MagicMock, patch

import numpy as np

import pytest

BASE = Path(__file__).resolve().parents[2]

from backend.rest_auth import RestAuth

def actual_function(path, name, namespace):
    """Compile an unchanged function AST from the archived commit, no reimplementation."""
    p = BASE / path
    tree = ast.parse(p.read_text())
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(p), 'exec'), namespace)
    return namespace[name]

def join_threads(*threads):
    for t in threads: t.start()
    for t in threads:
        t.join(5)
        assert not t.is_alive(), 'fixture thread did not finish'

def test_rest_revocation_same_mtime_is_not_resurrected(tmp_path):
    a = RestAuth(str(tmp_path)); raw, meta = a.create_token('gate')
    b = RestAuth(str(tmp_path)); timestamp = a._path.stat()
    assert b.revoke_token(meta['id'])
    os.utime(a._path, ns=(timestamp.st_atime_ns, timestamp.st_mtime_ns))
    assert a.verify_token(raw) is None, 'revoked token accepted with same mtime'

def test_rest_concurrent_create_preserves_both_tokens(tmp_path, monkeypatch):
    auths = [RestAuth(str(tmp_path)), RestAuth(str(tmp_path))]
    barrier = threading.Barrier(2); errors=[]
    for a in auths:
        save = a._save
        def synchronized(tokens, save=save):
            try: barrier.wait(0.2)
            except threading.BrokenBarrierError: pass  # Correct transaction locking may serialize entry.
            save(tokens)
        monkeypatch.setattr(a, '_save', synchronized)
    def create(a):
        try: a.create_token('synthetic')
        except Exception as e: errors.append(e)
    join_threads(*(threading.Thread(target=create, args=(a,)) for a in auths))
    assert not errors
    assert len(json.loads(auths[0]._path.read_text())) == 2, 'successful token creation was lost'

def test_rest_verify_does_not_undo_concurrent_revoke(tmp_path, monkeypatch):
    a = RestAuth(str(tmp_path)); raw, meta = a.create_token('gate')
    b = RestAuth(str(tmp_path)); save=a._save
    revoked=threading.Event(); errors=[]; threads=[]
    def revoke():
        try: assert b.revoke_token(meta['id'])
        except Exception as e: errors.append(e)
        finally: revoked.set()
    def revoke_then_save(tokens):
        t=threading.Thread(target=revoke);threads.append(t);t.start()
        # Existing code permits revoke before this save. A fixed transaction
        # may block revoke until verify exits; do not demand an unsafe schedule.
        revoked.wait(0.2)
        save(tokens)
    monkeypatch.setattr(a, '_save', revoke_then_save)
    a.verify_token(raw)
    for t in threads:
        t.join(5); assert not t.is_alive()
    assert not errors
    assert RestAuth(str(tmp_path)).verify_token(raw) is None, 'last_used flush resurrected revoked token'

def test_rest_corrupt_store_is_not_overwritten(tmp_path):
    p=tmp_path/'api_tokens.json'; p.write_text('{corrupt')
    try: RestAuth(str(tmp_path)).create_token('new')
    except (ValueError, RuntimeError, OSError): pass
    assert p.read_text() == '{corrupt', 'corrupt store overwritten by token creation'

def test_rest_empty_scopes_do_not_expand(tmp_path):
    _, meta=RestAuth(str(tmp_path)).create_token('none', scopes=[])
    assert meta['scopes'] == [], 'explicit empty scopes expanded to wildcard'

@pytest.mark.parametrize('initial', ['invalid', 'absent'])
def test_rest_key_concurrent_obtain_converges(tmp_path, monkeypatch, initial):
    from backend import rest_api_key as m
    p=tmp_path/m.KEY_FILENAME
    if initial == 'invalid': p.write_text('bad')
    original_read=m._read_valid; original_create=m._create_atomically
    both_read=threading.Barrier(2); created=threading.Event(); values=[]; errors=[]
    local=threading.local()
    def read(path):
        value=original_read(path)
        if not getattr(local,'read',False):
            local.read=True
            try: both_read.wait(0.2)
            except threading.BrokenBarrierError: pass  # A correct per-store transaction may serialize reads.
            # Корректная транзакция вправе упорядочить потоки любым образом: если 'late' первым
            # взял блокировку, создавать будет он сам, и ждать чужого создания нельзя.
            if threading.current_thread().name=='late': created.wait(0.3)
        return value
    def create(path,key):
        result=original_create(path,key); created.set(); return result
    original_replace=m._replace_invalid
    def replace(path,key):
        original_replace(path,key); created.set()
    monkeypatch.setattr(m,'_read_valid',read)
    monkeypatch.setattr(m,'_create_atomically',create)
    monkeypatch.setattr(m,'_replace_invalid',replace)
    def worker():
        try: values.append(m._obtain_key(tmp_path))
        except Exception as e: errors.append(e)
    join_threads(threading.Thread(target=worker,name='first'),threading.Thread(target=worker,name='late'))
    assert not errors
    assert len(set(values)) == 1, 'two process-equivalent starts returned different keys'

def test_rest_existing_key_mode_is_private(tmp_path):
    from backend.rest_api_key import _obtain_key, KEY_FILENAME
    p=tmp_path/KEY_FILENAME; p.write_text('synthetic-gate-key-'*3); p.chmod(0o644)
    _obtain_key(tmp_path)
    assert p.stat().st_mode & 0o777 == 0o600, 'accepted key remains world readable'

def make_call(privacy=True):
    from backend.call_assist_service import CallAssistService
    cfg={'privacy_mode_enabled':privacy,'voice_gateway_url':'http://synthetic.invalid','call_auto_summary':True}
    store=MagicMock(); store.load_settings.return_value=cfg
    store.add_history_item.return_value=SimpleNamespace(id='synthetic-history')
    recorder=MagicMock(); recorder.is_recording=False
    gateway=MagicMock(); gateway.post.return_value={'ok':True,'payload':{'summary':'PRIVATE-CALL'}}
    gateway.stop_session.return_value={'ok':True}
    gateway.get.return_value={'ok':True,'payload':{'content':'PRIVATE-CALL','summary':'PRIVATE-CALL','stats':{'count':1}}}
    svc=CallAssistService(store,recorder,MagicMock(),gateway=gateway,settings_get=lambda k,d:cfg.get(k,d))
    svc._state.update(active=True,status='active',session_id='s',gateway_session_id='gw')
    return svc,cfg,store,gateway

def test_call_stop_does_not_save_private_summary():
    svc,_,store,gw=make_call()
    result=svc.handle_stop({})
    assert not store.add_history_item.called and not gw.post.called and 'summary' not in result, 'private call summary fetched and persisted'

def test_call_state_hides_cached_summary():
    svc,_,_,_=make_call(); svc._state['summary']={'summary':'PRIVATE-CALL'}
    assert 'summary' not in svc.handle_get_state({}), 'cached call summary exposed in privacy'

def test_call_quick_phrase_blocked_in_privacy():
    svc,_,_,gw=make_call(); svc.handle_quick_phrase({'text':'PRIVATE-PHRASE'})
    assert not gw.post.called, 'quick phrase sent during privacy'

def test_call_stream_rechecks_privacy_after_stt():
    svc,cfg,_,gw=make_call(False); cfg['privacy_mode_enabled']=False
    svc.recorder.is_recording=True; svc.recorder.snapshot_audio.return_value=(np.ones(16000),1)
    def transcribe(*a,**k): cfg['privacy_mode_enabled']=True; return {'text':'PRIVATE-LATE-STT'}
    svc.transcriber.transcribe_preview.side_effect=transcribe
    def post(*a,**k): svc._state['active']=False; return {'ok':True}
    gw.post.side_effect=post
    gen=object(); svc._gen=gen
    with patch('backend.call_assist_service.time.sleep', side_effect=lambda *_:svc._state.update(active=False)):
        svc._assist_loop(gen,'s','http://synthetic.invalid','')
    assert not gw.post.called, 'STT text sent after privacy changed'

def make_meeting():
    from backend.meeting_session_service import MeetingSessionService,_MeetingSession
    cfg={'privacy_mode_enabled':False,'llm_brain_lease_enabled':False}
    recorder=MagicMock(); recorder.is_recording=True; recorder.get_duration_sec.return_value=1
    recorder.snapshot_range.return_value=np.ones(16000)
    bus=MagicMock(); transcriber=MagicMock()
    svc=MeetingSessionService(recorder,transcriber,MagicMock(),MagicMock(),lambda k,d:cfg.get(k,d),bus)
    svc._session=_MeetingSession(generation_token='synthetic-gen')
    return svc,cfg,bus

def test_meeting_stt_completion_rechecks_privacy():
    svc,cfg,bus=make_meeting()
    def late(*a,**k): cfg['privacy_mode_enabled']=True; return {'text':'PRIVATE-MEETING'}
    svc._transcriber.transcribe_preview.side_effect=late
    with patch('backend.recording_core_service.bump_stt_activity'):
        svc._job_chunk_stt(svc._session)
    assert not bus.emit.called and not svc._session.chunks, 'late STT published after privacy toggle'

def test_meeting_privacy_wipes_buffers():
    svc,cfg,_=make_meeting(); session=svc._session
    session.chunks=['PRIVATE']; session.items=[{'text':'PRIVATE'}]; session.decisions=['PRIVATE']
    cfg['privacy_mode_enabled']=True; svc._run_due_job_once(1)
    assert not session.chunks and not session.items and not session.decisions, 'privacy stopped processing but retained meeting text'

def test_live_subs_direct_ingest_does_not_retain_private_audio():
    from backend.live_subs_service import LiveSubsService
    svc=LiveSubsService(MagicMock(),MagicMock(),settings_get=lambda k,d:True if k=='privacy_mode_enabled' else d)
    try:
        svc.ingest(np.ones(1600,dtype=np.int16).tobytes(),16000,'es',False)
        assert not svc._buffer_samples and not svc._buffer, 'direct WS ingest retained private audio'
    finally: svc.close()

def migration_fixture(tmp_path):
    from backend.data_migrator import DataMigrator
    data=tmp_path/'data'; data.mkdir()
    (data/'settings.json').write_text(json.dumps({'history_encryption_enabled':False,'privacy_mode_enabled':False}))
    backup=data/'backups'/'migration_backup_synthetic'; backup.mkdir(parents=True)
    (backup/'history.ndjson').write_text('{"id":"backup","text":"OLD"}\n')
    return DataMigrator(data_dir=data),data,backup

def singleton_function():
    return actual_function('KrabEar/core/workers/gigaam_worker.py','_acquire_singleton_lock',dict(os=os,sys=sys))

def plugin_fixture(tmp_path):
    from backend.plugin_system import PluginManager
    root=tmp_path/'data'; directory=root/'plugins'/'gate'; directory.mkdir(parents=True)
    (directory/'plugin.json').write_text(json.dumps({'name':'gate','version':'1','entry_point':'plugin.py'}))
    marker=tmp_path/'EXECUTED'
    (directory/'plugin.py').write_text('from pathlib import Path\nPath('+repr(str(marker))+').write_text("synthetic")\nclass Plugin:\n name="gate"\n version="1"\n def initialize(self,s): pass\n def get_ipc_methods(self): return {}\n')
    return PluginManager(data_dir=root),directory,marker

