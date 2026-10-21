# Диагностика занятости и памяти Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** честная additive диагностика lease и component activity без ложного owner idle.
**Architecture:** read-only lease observation, nonblocking existing-owner metadata helpers, completion tokens внешнего transcriber callable вокруг существующих REST native STT путей; nested adapter futures/WS translation+emit явно untracked. IPC/REST процессы имеют разные epoch, неполная owner generation явно неизвестна; drain API отсутствует.
**Tech Stack:** Python3.12+, stdlib, существующий Flask/IPC, без новых dependencies.
**Spec:** docs/superpowers/specs/2026-10-21-activity-diagnostics-design.md
**База:** origin/codex/krab-ear-v2 @052b6c9; isolated codex/ear-activity-diagnostics.

## Global Constraints

- Чужой shared WIP неприкосновенен; git add только явных путей.
- Не менять legacy lease API/write protocol, admission, enforce, Main/VG/clone/OrbStack, не создавать второй coordinator/queue.
- Diagnostic GET не вызывает model/STT/audio/subprocess и не возвращает текст/PII.
- До VG FINISH и Main FINISH Ear lifecycle/ML HOLD; ES запрещена до memory stability. Релиз после source gates и fresh idle/resource checks.
- BackendService в тесте закрывать; test fixtures предпочтительно narrow services/Futures/tempfiles без ML.

## Review Focus

- Rename/flock и in-place mutation lease должны давать replaced/unknown, не noholder.
- Finalizing-only и retained meeting worker не idle; contention не блокирует запрос.
- HTTP504/WSdisconnect не снимают actual native reservation.
- Future cancellation до start, submit failure, callback/worker race не дают false idle и не вызывают двойного release.
- Epoch/version/coverage не изображают атомарное право на выгрузку; warmup/batch coverage остаётся unknown.

### Task 1: Strict lease observation

**Files:** Modify KrabEar/backend/brain_lease.py; Create KrabEar/tests/test_brain_lease_observation_2026_10_21.py.
**Interfaces:** observe_brain_lease(lock_path: Path|None=None)->dict; schema1/state/known/advisory + validated holder metadata.
- [x] RED tests: `assert observe_brain_lease(missing)['state']=='absent'`; contention→knownFalse; malformed/NaN/duplicateJSON/oversize/nonregular→knownFalse; rename during read→replaced; validheld/expired/empty read-only; legacy behavior unchanged.
- [x] Run `PYTHONPATH=KrabEar python3.12 -m unittest discover -s KrabEar/tests -p test_brain_lease_observation_2026_10_21.py -v`, expect missing API failure.
- [x] Implement bounded readonly observer; no _read_payload reuse or production writes.
- [x] Same command GREEN + existing brain lease tests file-by-file.
- [x] Commit explicit source/test/docs paths.

### Task 2: Nonblocking IPC component metadata

**Files:** Create KrabEar/backend/activity_diagnostics.py; Modify recorder.py, recording_core_service.py, meeting_session_service.py, memory_conductor.py, service.py; Create KrabEar/tests/test_activity_diagnostics_2026_10_21.py. AudioRecorder.is_recording берёт внутренний data lock, поэтому нужен собственный metadata helper; conductor read_only=True не синхронизирует shadow marker из нового GET.
**Interfaces:** activity_snapshot()->dict on both services; ipc_activity_snapshot(recording,meeting)->dict; process_identity()->dict. get_memory_ledger adds activity and lease_observation.
- [x] RED real service metadata tests: finalizing1→busy; provider missing/exception→unknown; locks held by another thread→unknown promptly; meeting reservation/stopping/recovery/retainedworker→busy; no recorder preview/settings/model calls. Aggregate no-busy→unknown with incomplete coverage, generationNone; per-process epoch stable and separate across incarnation.
- [x] Run new unittest file; expect missing methods.
- [x] Implement narrow locks and aggregate; no old state-handler calls and no lifecycle mutations.
- [x] GREEN tests + existing recording/meeting/memory ledger tests; real dispatch wrapper proves additive wiring.
- [x] Commit explicit paths.

### Task 3: REST server-owned completion observation

**Files:** Modify activity_diagnostics.py, rest_server.py; Create KrabEar/tests/test_rest_activity_completion_2026_10_21.py; Modify docs/IPC_API_REFERENCE.md and NOW.md for source receipt.
**Interfaces:** ActivityReservations.reserve(channel)->token; run(token,fn,*args,**kwargs); follow_future(token,future); complete(token); mark_unknown(token); snapshot()->dict. REST readiness adds activity(native_stt subset + lease observation); POST worker and native WS callbacks share observer.
- [x] RED controlled Future tests: reserved1/running0 busy; timeout while running retains token until actual completion; cancelled queued token clears; submit/worker error clears; unknown Future conservatively retained; nonblocking snapshot contention; generation changes on observer events. POST and WS wiring invokes actual observer helpers with mocked native leaves, no model imports for GET.
- [x] Run new unittest/pytest selected tests; fail because API/wiring absent.
- [x] Add bounded metadata tracker, exact completion/cancel wiring, leave legacy singleflight policy unchanged.
- [ ] GREEN new tests + REST singleflight/timeout/native WS/readiness auth regression files, Python3.12 parity/hostedCI, flake8, make audit-all.
- [ ] Commit explicit paths, open/attach PR; independent cumulative Astra whole-diff; exact CI green; merge/release only within owner-approved Ear scope and coordinated window. Postmerge CI + safe nonwarming IPC/GET smoke; ES deferred.

Review clarification: внешний callable может завершиться при ещё работающем nested adapter Future; RED→GREEN тест фиксирует completion_scope и явное untracked coverage. Это advisory source, не допуск к cutover/drain по component idle.
