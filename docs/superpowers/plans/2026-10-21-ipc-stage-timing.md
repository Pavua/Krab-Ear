# IPC stage timing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** различить задержку очереди, handler, privacy/audit и send для двух activity RPC без ML-нагрузки и изменения тайм-аутов.

**Architecture:** opt-in `id=ear-diag-<32 lowercase hex>` для `ping`, `get_recording_state`, `get_meeting_live_state`. Ограниченный неблокирующий process-local буфер хранит только доверенные имена этапов и часы; обычный подписанный/throttled dispatch `get_ipc_timing` читает один ID. Ответ исходного RPC неизменен.

**Tech Stack:** stdlib, существующие IPCServer/BackendService, unittest, socketpair/локальный временный Unix socket.

**Spec:** текущий контракт Main/Ear 21.10: client per-method/requestID/UTC+monotonic, server queue/handler/privacy/audit/send; timeout не повышать; причина прежнего сбоя UNKNOWN.

**База:** origin/codex/krab-ear-v2 @ 0ff399bc. Изолированный worktree `ear-ipc-timing`, branch `codex/ear-ipc-timing`.

## Global Constraints

- Никаких ML/model load/build, runtime config, deploy/restart, clone/Main/Gateway/OrbStack lifecycle.
- Чужой WIP не трогать; git add только явные пути; без dependencies.
- Не менять privacy/auth/throttle/error/backstop и response schema существующих RPC.
- Не сохранять params/text/result/errors/signature, произвольные ID/имена методов.
- Полное покрытие памяти/admission/semantic STT не объявлять; timestamps не объясняют причину сами по себе.
- Писать диагностические данные только в ограниченную память, без I/O/ожидания lock; потеря/duplicate/eviction = UNKNOWN.

## Review Focus

- Небезопасный ID/метод/секрет в context: trace отсутствует.
- Lock занят, overflow, повтор ID: не ждать и не выдавать полную trace.
- Executor timeout/disconnect: поздняя worker completion не означает успешный send.
- Privacy/auth/throttle failures: бизнес-контракт сохранён, payload не раскрыт.
- Реальная очередь/медленный audit/send: отдельные этапы и часы, физический delivery не выводить из sendall.

### Task 1: bounded recorder и исполняемые тесты

**Files:** Create `KrabEar/backend/ipc_timing.py`, `KrabEar/tests/test_ipc_timing.py`.

- [x] RED: `python KrabEar/tests/test_ipc_timing.py` — fail из-за отсутствующей bounded trace API; тесты содержат настоящие assertions для opt-in, capacity, contended lock, duplicate, clocks и fork.
- [x] GREEN: `TimingStore.begin(payload)`, `record(trace, stage, stamp=None)`, `snapshot(params)`; 64 traces × 24 events максимум, nonblocking lock, ContextVar scope, PID/epoch reset after fork.
- [x] Проверить secret rejection, unknown/partial state, отсутствие filesystem/thread side effects.

### Task 2: реальная transport/service wiring

**Files:** Modify `KrabEar/backend/ipc_server.py`, `KrabEar/backend/service.py`; Test `KrabEar/tests/test_ipc_timing.py`.

- [x] RED: real socketpair + настоящий AST-extracted `BackendService.handle_request`, event-controlled audit/send; ожидается отсутствие нужных этапов до фикса.
- [x] GREEN: accept/connection/decode/submit/worker, handler/privacy/audit, serialize/send; ContextVar переносится в executor; mark в finally для исключений. Никакого logging внутри recorder.
- [x] Добавить `get_ipc_timing` в обычную dispatch table, с теми же auth/throttle checks; query не запускает trace и audit I/O ради чтения собственного буфера.
- [x] Test late worker после backstop, disconnect, rejected signing, реальный временный Unix listener; no payload/schema drift.

### Task 3: verification и review

**Files:** Modify `docs/IPC_API_REFERENCE.md`.

- [x] Целевые файлы отдельными процессами: new tests, ipc_server_wave1767, ipc_server_dedup_wave1768, ipc_broken_pipe, ipc_socket_errors, audit logger wiring, dispatch invariants и privacy/auth regression files по поиску зависимостей.
- [x] Новые15 тестов напрямую на Python3.12 без MLX; `make audit-all` PASS. Полный `scripts/pre_merge_py312_check.sh` отложен: готового parity venv нет; установка backend/ML dependencies не входит в memory HOLD. Ubuntu parity проверяется удалённым CI.
- [x] Независимое Astra adversarial whole-diff SOURCE PASS после трёх RED→GREEN исправлений.
- [ ] Точный head CI перед source acceptance.
- [ ] Явные git paths, commit/PR без merge/deploy. Runtime correlation недоступна до отдельного разрешённого релиза.

## Проверенный source checkpoint

110 тестов в9 отдельных процессах Python3.14; новые15 также напрямую Python3.12, Ruff и audit-all PASS.
Review: transport error сохраняет reply; потерянный begin монотонно инвалидирует known всей epoch; eviction очищает события и запрещает поздние append.
Статус complete означает только возврат sendall; production-корреляция и прежняя причина timeout не доказаны.
