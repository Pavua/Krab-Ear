# History key safety — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** запретить replacement key рядом с `ENC1` и сохранять proposed ON
только после свежего успешного provider preflight.

**Architecture:** общий Store guard под существующим EX flock; prepare
settings выполняет validation и fresh preflight, затем commit применяет
подготовленную сериализацию и публикует ключ после replace. Legacy restore использует тот же результат до/после
копирования без повторной проверки Keychain.

**Tech Stack:** существующие Python StateStore, SettingsService, HistoryService,
Unix JSON-RPC и pytest; новые зависимости не нужны.

**Spec:** [инварианты и ограничения](../specs/2026-10-14-history-key-safety.md).

**База:** `origin/codex/krab-ear-v2`, reviewed parent #2091 head `19f5deaf`
(merged `86a0b197`); owned `codex/ear-key-safety-20261014`, worktree
`/Users/pablito/.codex/worktrees/ear-passive-key-20261014/Krab Ear`.
Merge сохраняет ancestry; root проверяет свежую remote base перед интеграцией.

## Global Constraints

- Worker владеет только четырьмя backend-модулями ниже и regression tests.
  Root владеет docs, Git/PR/merge/release. Другие агенты работают параллельно:
  их WIP не откатывать. Shared checkout не менять; staging только явными путями.
- Только temp HOME/data/provider stub/Unix socket; никаких actual Keychain,
  history, settings, migrations, аудио, ML, сети или runtime lifecycle.
- `BackendService` всегда закрывать; тест-файлы запускать последовательно
  отдельными процессами на имеющихся Python 3.14 и 3.12. Общий venv не менять.
- Не запускать app, `launchctl`, `lms load`, brain enforcement, REST in-process;
  Main Krab, Voice Gateway, OrbStack/VPN и PR #1875 не трогать.
- Не добавлять зависимости, второй EventBridge, wake-word SSE, Swift visual,
  synthetic fine-tuning, token/config/account changes. Секреты не печатать.
- Existing SH getter не повышать до EX; новых lock-файлов не создавать.
  Key creation только EX; no compensating key deletion.

## Review Focus

Fresh preflight нельзя заменить старым cached getter. CAS/type/size failures
не должны трогать provider; old unknown policy не является exact True.
Prepare не публикует новый ключ: ошибка записи до replace должна сохранить cache.
Creating guard для restore видит live ∪ prospective, а ON→OFF guard сохраняет
prospective-only semantics. После первого copy2 не должно быть второго preflight.
Проверить реальные IPC callers, service.close и все десять journals; mocked
provider не считается real Keychain acceptance.

### Task 1: RED regression и границы Store

**Files:**
- Create: `KrabEar/tests/test_history_key_safety_20261014.py`.
- Modify: `KrabEar/backend/state_store.py`.

- [x] Зафиксировать RED на unchanged production: параметризованный
  `test_append_never_creates_replacement_beside_any_enc1` по registry десяти
  журналов. Вызвать реальный append; missing provider не добавляет ключ,
  journal bytes не меняются. Восстановление прежнего ключа разрешает retry.
- [x] Добавить `test_unproved_journal_readability_forbids_creation` и positive
  fresh creation/plaintext migration. RED должен показывать creating call,
  а не только произвольный exception.
- [x] Реализовать `_acquire_history_crypto_unlocked` и подключить creating
  getter/migration под existing EX. Passive lookup-only SH оставить рабочим.
- [x] Запустить тот же regression на обеих версиях; GREEN без ML/Keychain.

### Task 2: Activation preflight до settings commit

**Files:**
- Modify: `KrabEar/backend/state_store.py` — prepare/save/commit.
- Modify: `KrabEar/backend/settings_service.py` — internal force flag.
- Modify: `KrabEar/backend/service.py` — dedicated bool/enable/migration.
- Test: `KrabEar/tests/test_history_key_safety_20261014.py`.

- [x] RED `test_activation_failure_preserves_settings_revision_and_hooks`:
  direct/generic/isolated IPC/import/backup routes; provider read/add failures
  оставляют settings/revision/hooks неизменными.
- [x] RED warm-cache, repeated dedicated ON и corrupt/missing/nonbool old-policy
  repair. Controls: stale CAS/invalid payload не вызывают provider, unrelated
  already-ON commit не требует доступности Keychain.
- [x] Dedicated supplied non-bool отклонить **до** provider; missing enabled
  сохраняет прежний default False.
- [x] Validation/CAS/serialization-size выполнять до fresh preflight;
  `force_history_crypto_preflight` остаётся internal keyword, не settings key.
  Удалить dedicated creating-builder bypasses.
- [x] Proper RED missing/corrupt-policy repair + successful provider + failed
  atomic write до replace: следующий append не должен использовать candidate
  из отказавшего activation. Проверить warm old key → fresh different key при
  успешном activation и cache consistency при directory-fsync failure после replace.
- [x] GREEN всех activation cases на Python 3.12 и 3.14.

### Task 3: Legacy restore — один preflight до mutation

**Files:**
- Modify: `KrabEar/backend/history_service.py` — legacy restore.
- Modify: `KrabEar/backend/state_store.py` — prepared commit helper.
- Test: `KrabEar/tests/test_history_key_safety_20261014.py`.

- [x] RED `test_legacy_restore_refuses_before_copy_on_provider_failure` и
  `test_legacy_restore_uses_one_prepared_commit_before_copy`; журнал и policy
  неизменны при отказе, successful provider не вызывается снова после copy2.
- [x] RED live-only/prospective-only `ENC1` union cases при missing key.
- [x] Сохранить tuple `(settings, serialized, crypto_candidate)` из prepare и вызвать
  `_commit_prepared_settings_unlocked` после copy2 под тем же EX. Существующие
  atomic write/fsync/revision/repair semantics сохранить. Candidate публиковать
  после успешного `atomic_write_text`, перед отдельным directory fsync;
  `None` означает, что fresh preflight не требовался.
- [x] GREEN и прежние restore/ON→OFF tests на обеих версиях.

### Task 4: Verification, независимый gate и интеграция

**Files:** regression выше, существующие dependent tests и docs этой карточки.

- [x] Собрать affected tests по вызовам четырёх изменённых модулей; прогнать
  каждый файл отдельным процессом на обоих runtimes с CPU/import fences.
  Пример запуска внутри temp-profile guard: `PYTHONPATH="$PWD/KrabEar"
  <existing-python> -m pytest KrabEar/tests/test_history_key_safety_20261014.py -q`.
  Голый запуск без guard, изолирующего HOME/provider/ML imports, не применять.
- [x] CI-equivalent critical flake8, `git diff --check`, `make audit-all`:
  все 14 gates PASS. Не заменять failed/timeout test словом PASS.
- [x] Сохранить raw RED/GREEN logs и immutable production/test patch seal в
  ignored `.superpowers/sdd/2026-10-14-key-safety/`.
- [x] Root обновляет NOW/handoff по фактическим результатам и отправляет
  **весь** итоговый diff независимому Astra Ultra. Исправления review требуют
  meaningful regression и повторного scoped review.
- [ ] Root проверяет WT/root/branch/status/fresh remote, коммитит явные пути,
  публикует/attach PR и ждёт exact-head CI. Merge только после final gate;
  оба post-merge workflows проверяются на собственном merge SHA.
- [ ] Runtime/real-key acceptance остаётся отдельной owning процедурой;
  объединённый cutover #2090+#2091+crypto требует всех новых source/CI/release
  gates и fresh quiet ACK. Уже подготовленный bundle `86a0b197` не применять
  как доказательство или пакет для ещё не опубликованного crypto SHA.

Локальные результаты исходной фикстуры: по 1238 PASS + 142 subtests в 33 файлах на Python
3.12.7/3.14.8; отдельно по 185 PASS + 90 subtests в четырёх child-bearing
файлах под усиленным exact-target guard. На каждой версии 0 skips и 3 Swift
cases deselected. Подробные runner/hash/count границы — в
[handoff](../handoffs/2026-10-14-history-key-safety.md).

Найденный whole-card review P2 общего IPC socket закрыт proper RED двух живых
profiles (1/1 вместо 1/0) → unique temp root + owned LISTENING. Весь финальный
IPC-файл: 13 PASS на каждой версии; остальные 22 owned/32 test paths неизменны.
Требуется повторный scoped review; production Astra GO относится к прежнему
неизменённому `c3d4fc32` diff.
