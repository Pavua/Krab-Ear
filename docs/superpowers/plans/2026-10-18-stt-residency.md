# STT residency — план реализации

> **Исполнитель:** native inline, executing-plans; root организует независимый Astra Ultra gate.

**Цель:** truthful GigaAM unload и nonwarming owner snapshots, без изменения memory policy.
**Архитектура:** существующие router/adapter/session locks и cached objects; никакой загрузки ради диагностики. Backend IPC и существующий auth-gated REST readiness показывают только своего владельца.
**Стек:** Python3.14 локально, pytest; локальный Python3.12 unavailable (без installs), Linux parity остаётся CI gate.
**Контракт:** прямой бриф root 2026-10-18: A только GigaAM; B configured/observed/unknown отдельно, child exact model unknown, без протокольного расширения.

## Границы

- База origin/codex/krab-ear-v2=19124f51; WT ear-stt-residency-20261018, отдельная codex ветка.
- Только source/tests/docs/explicit commits. Не push/merge/deploy/restart/config/models/inference/unload/микрофон/живой IPC.
- Чужой WIP и paste WT не менять. Никаких dependency installs.
- Не включать REST_IN_PROCESS_ENABLED, memory_conductor_enforce*, brain preload, не создавать второй owner/reaper.
- Call-ready policy не применяется. VG сейчас использует обычный balanced REST/Whisper; backend dictation и REST call owner различаются.
- A не освобождает доказанно backend Metal: engine unload касается GigaAM router, не Whisper/pyannote.
- B не импортирует MLX для наблюдения, не вызывает get_model/start/warmup/transcribe/get_gigaam_adapter.
- Model IDs ограничены именем или repo/id; абсолютные/локальные пути, история, аудио, env и секреты не возвращаются.

## A — truthful unload

- [x] Actual handler→AudioEngine.__new__→actual router→actual GigaAMAdapter с fake session: pin/busy/cleanupFalse сохраняют owner и дают unloadedFalse; release→True/cache cleared. Текущий handler выдаёт true: causal RED.
- [x] engine.unload_stt_models()->bool возвращает подтверждённый router result; handler требует explicit True, остальные исходы false/error. Документировать scope GigaAM.
- [x] GREEN того же теста + existing unload/lifecycle/call-admission files по одному.

## B — snapshots

Интерфейсы: AudioEngine.stt_residency(owner="backend")->dict; STTRouter.residency_snapshot()->dict; GigaAMAdapter.residency_snapshot()->dict; MLXWhisperSession.residency_snapshot()->dict. Небольшой core/stt_residency.py нормализует model ID и читает уже существующий sys.modules ModelHolder без import MLX. Lock недоступен→busy/unknown, не unloaded. Child alive→unknown модели, не inferred loaded. Поля owner/owner_pid/configured/whisper/gigaam; configured_model не выдаётся за observed_model.

- [x] Tests холодного backend/REST owner не создают adapter/session; configuredA/observedB различаются; worker alive model unknown; inflight/lock busy не подменяются unloaded; paths скрыты. Для нового API сначала assertion existence/semantic RED, не import error.
- [x] Actual handler get_stt_residency→engine; добавить точную запись dispatch service.py и API reference/parity. Никаких history getters.
- [x] Existing auth-gated /v1/readiness добавляет stt_residency owner=rest; HTTP auth и текущие overall_ready/components/status сохранены. Без нового endpoint/child protocol.
- [x] Actual Flask test_client с fake ML/OS leaves и actual engine snapshot доказывает auth/no-spawn/cross-owner distinction.

## Проверки и receipts

Каждый файл отдельно: `PYTHONPATH="$PWD/KrabEar" /Users/pablito/Antigravity_AGENTS/Krab\ Ear/.venv_krab_ear/bin/python -m pytest <file> -q`, output в ignored .superpowers/sdd/2026-10-18-stt-residency.
Ubuntu-parity: существующий Python3.12 harness только при готовом venv; никаких installs. `make audit-all` после всех source changes. Список затронутых контрактных тестов уточняется по действующим owning files, без запуска BackendService warmup fixtures. Root решает широкую CI проверку после immutable diff.

## Review focus

False/None/exception не превращаются в unloadedTrue; busy snapshot не требует I/O lock ожидания; metadata не создаёт cold owner; child alive не означает model loaded; owner-local snapshot не выдаётся за cross-process состояние; локальные model paths скрыты; REST auth сохраняется.

## Итог source-проверки 2026-10-18

- A: actual handler→engine→router→adapter, fake session: RED7 (ложный true), GREEN7. Existing unload4 PASS.
- B: semantic API RED8/controls7 PASS → GREEN15; после дополнительных controls23 PASS. Эти дополнительные controls не объявляются fail-before.
- HTTP: actual auth/route/engine RED1+control1 → GREEN2. Старые REST fixtures получили явный JSON snapshot вместо MagicMock; 200/503 semantics не менялись.
- Singleton contention: actual engine под чужим _session_lock RED1 (observer ждал release) → GREEN23. Новый module-level getter try-lock захватывает ссылку, отпускает global lock до session snapshot; existing peek_session/lifecycle не менялись.
- Снимок только наблюдательный. Worker alive означает unknown model; backend owner не знает REST child. Call latency/readiness policy не менялись.
- Готового локального Python3.12 нет; parity harness не запускался, так как он может устанавливать зависимости. Remote Linux CI обязателен после review/push.
- Логи/точный список команд: ignored `.superpowers/sdd/2026-10-18-stt-residency/`; финальный receipt создаётся перед freeze. `make audit-all` — отдельный обязательный шаг.

### Frozen validation

Локальный Python3.14: **203 unique tests PASS + 10 router subtests**, отдельно
повторены Whisper deadline5/idle8 после последнего source изменения. Новые
owner23 + HTTP2; existing unload4/lifecycle9/admission8/router51/deadline5/idle8/
management29/docs5/REST503control3/RESTreadiness4/versioning40/factory7/
GigaAMreview5. В GigaAM review 3 parametrized BackendService-init fixtures
осознанно deselected, чтобы не запускать warmup owners. `make audit-all` exit0,
All audit checks passed. `git diff --check` exit0.

Остаток: независимый frozen diff review, затем remote Linux3.12/полный CI.
Ни inference, ни настоящие unload/IPC, ни live call acceptance не запускались.
Для следующего whole-diff gate рекомендована Astra Ultra; для механики CI —
Sol6.1 Medium. Реализация A+B завершена, release/runtime acceptance вне scope.
