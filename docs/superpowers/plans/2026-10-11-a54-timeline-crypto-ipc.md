# A5.4 Timeline — synthetic Swift/crypto IPC acceptance

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** проверить существующий Timeline consent из PR #2084 через production Swift coordinator, Unix IPC и настоящий AES-GCM на временном профиле.

**Architecture:** расширить существующий `PlaintextExportIntegrationHarness` backend-режимом; использовать существующий `IsolatedBackend(crypto_key=...)`. Production authorizer, IPCClient, coordinator, StateStore и Timeline exporters не заменять. Источник ключа — случайный memory-only fixture; системный Keychain запрещён существующим isolation guard.

**Tech Stack:** Python 3.12, pytest, Swift compiler для трёх исходников harness; без нового dependency и полной сборки приложения.

**Spec:** `docs/superpowers/specs/2026-09-24-a5-history-at-rest-design.md`, A5.3 consent contract и `docs/superpowers/plans/2026-10-08-a54-release-acceptance.md` §3.

**База:** review-кандидат PR #2084 `5e30937619d91aaf6bf4afa0a01385caf877f352`, поверх `origin/codex/krab-ear-v2` = `16d2b22678474c871cfad9eb4c09c75f4b7d0cd5`.
**Worktree:** `/Users/pablito/.codex/worktrees/ear-failclosed-followup-20261011/Krab Ear`, branch `codex/ear-failclosed-followup-20261011`.

**Баны:** shared/foreign WIP не менять; staging только явные пути. Нет runtime restart/build-install, live UI, Keychain, real history, activation/migration/purge/key rotation, Main/VG mutations, внешних уведомлений. Не менять production sources ради теста. BackendService fixture обязан закрываться; native ML/network/live persistence блокируются существующим guard. Тесты по одному файлу без xdist.

## Review Focus

- Отказ от consent: файл не появляется; повтор билета отвергается.
- Privacy ON: файл не появляется и consent не запрашивается.
- Grant: все три формата содержат synthetic marker; история остаётся ENC1 без marker в сыром журнале.
- Revoked/stale context после restart: ни один новый экспорт не появляется, старые файлы и история сохраняются.
- Outputs/stdout/logs/audit не содержат случайного ключа, capability, receipt или session ID; positive controls сохранены.

### Task 1: настоящий Timeline export через Swift и crypto IPC

**Files:**
- Modify: `native/KrabEarAgent/Tests/Integration/PlaintextExportIntegrationHarness.swift`
- Create: `KrabEar/tests/test_timeline_crypto_ipc_integration.py`
- Modify только при необходимом control hook: `KrabEar/tests/_plaintext_export_integration_backend.py`

- [x] Проверить baseline существующего harness и изоляцию; определить конкретные непокрытые случаи без дублирования A5.3.
- [x] Добавить backend-mode harness: `prepareBackend(method:consent:)` → `performBackend(_:params:)`, summary только counts/boolean; все 3 production метода; повтор ticket = отказ; shutdown cleanup awaited.
- [x] На memory-key ENC1-профиле добавить synthetic запись через настоящий IPC. Для SVG/JSON/iCal проверить grant/decline/privacy; сырой history содержит ENC1 и не содержит текста; allowed output содержит только fixture text в ожидаемом contained пути.
- [x] Через настоящий Python IPC проверить revoke и restart stale контекста для всех 3 методов, byte-exact journals/settings и отсутствие новых outputs при отказе.
- [x] Отдельно воспроизвести wrong-key/tag tamper для всех 3 Timeline siblings: отказ должен происходить до записи, без ложного успешного пустого файла. Если текущий source возвращает success, зафиксировать RED; production fix и его regression — отдельная Task 2.
- [x] Выполнить `scripts/pre_merge_py312_check.sh KrabEar/tests/test_timeline_crypto_ipc_integration.py KrabEar/tests/test_plaintext_export_integration.py KrabEar/tests/test_history_encryption_integration.py`; разрешено только переиспользование уже готового CPU-only venv без установки/пересоздания. Также проверен зависимый Timeline/scheduler sinks файл; всего83PASS+27subtests, exit0.
- [x] Независимый Astra review новой дельты; docs/NOW/handoff с exact source/CI/local границами. Commit только явные пути; PR допустим, merge/deploy требуют отдельного scope. Source/test `f1b86d10`, draft #2085; новый CI является отдельным gate.

Добавление coverage для уже написанного source не объявляется RED→GREEN production fix. Если проверка выявляет дефект, сначала отдельный воспроизводящий тест с правильным RED, затем минимальный source fix и новый независимый gate.

### Task 2: только подтверждённый fail-open Timeline read

**Files:** Modify `KrabEar/backend/service.py`; regression принадлежит Task 1.

- [x] Получить правильный RED Task 1: wrong-key/tamper export сейчас сообщает успешный пустой файл.
- [x] Убрать exception→`raw_items=[]` из всех 3 `_handle_export_timeline_*`; настоящий пустой журнал сохраняет штатный пустой export, ошибка чтения поднимается к существующему IPC error boundary до file-write.
- [x] Повторить Task 1 GREEN и зависимые Timeline/source-contract тесты, audit-all и Python3.12 parity. Independent Astra review всей source/test дельты. Python3.12:83tests+27subtests, audit-all14gates и final Astra PASS.
