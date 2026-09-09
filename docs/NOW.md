# NOW — что делать сейчас (Krab Ear)

Обновлено: **2026-09-05**. Одна страница: база, политика brain/GPU, очередь. Журнал волн — [`ROADMAP-2026H2.md`](ROADMAP-2026H2.md), не очередь. Горизонт 2–4 нед: [`design-briefs/2026-09-05-horizon-plan.md`](design-briefs/2026-09-05-horizon-plan.md). Как работать: [`EXECUTOR_PLAYBOOK.md`](EXECUTOR_PLAYBOOK.md).

**Source-дополнение 2026-09-07:** PR [#2001](https://github.com/Pavua/Krab-Ear/pull/2001)
добавляет телефонный STT-профиль для Voice Gateway: explicit auto до общего STT,
RU через уже загруженный owner GigaAM, без второго worker и history. Контракт и незакрытая
runtime qualification — в [плане](superpowers/plans/2026-09-07-vg-gigaam-call-profile.md).
В этой работе runtime не перезапускался; флаги Gateway
`KRAB_STT_EAR_CALL_PROFILE_ENABLED` и `KRAB_SCREENING_AUTO_LANGUAGE_ENABLED` OFF.
Source-проверки не доказывают CI другого SHA или готовность живого звонка.
Текущие HEAD/CI — в primary `.remember/CODEX_CALL_STT_20260907.md` с повторной
проверкой Git/GitHub. SHA/PID ниже сохранены как snapshot 05.09, не текущая проверка.

**Source 2026-09-09 вечер (Cloud, `gh pr view` + `git fetch`; runtime не деплоили):**
`origin/codex/krab-ear-v2` = `033090ed` (#2007). MERGED: [#2005](https://github.com/Pavua/Krab-Ear/pull/2005) `5e24ee6`,
[#2006](https://github.com/Pavua/Krab-Ear/pull/2006) `5c88af5`, [#2007](https://github.com/Pavua/Krab-Ear/pull/2007) `033090ed`.
OPEN draft: [#2008](https://github.com/Pavua/Krab-Ear/pull/2008) siblings (collection/speaker live; text_processing wiring follow-up).
PID и прод-код 05.09 ниже — snapshot, не свежая проверка. Handoff: `.remember/CLOUD_2026-09-09_privacy_gates.md`.

## База и runtime snapshot 2026-09-05

- Репозиторий: [Pavua/Krab-Ear](https://github.com/Pavua/Krab-Ear)
- Прод-колея: **`origin/codex/krab-ear-v2`**
- **Прод-код:** `330bca9b` — #1999 C1 brain-holdoff + cloud при Studio down
- Backend pid **11891** (safe restart после #1999), агент pid **41353** (#1998, Krab Ear Dev Local)
- Worktree: `git worktree add .worktrees/<slug> -b feat/<slug> origin/codex/krab-ear-v2`
- Main Krab Q2 (:8080 purpose slots, RIS/SergeyRG) — **не Ear**: [`ANTIGRAVITY_HANDOFF/2026-09-05-krab-8080-model-routing.md`](../ANTIGRAVITY_HANDOFF/2026-09-05-krab-8080-model-routing.md)

## Задеплоено 2026-09-05

- **#1997** — сенсор памяти: `vm_pressure` + swap у потолка; SIGKILL воркера → `stt.worker_killed`, не `mlx.oom`. **`memory_conductor_enforce*` всё ещё OFF** (shadow только логирует).
- **#1998** — визуал Call Observer, Claude Design-секций, оверлея диктовки; parity-бинарь + relaunch агента.
- **#1999** — C1 brain-holdoff: на стопе записи / rewriter / summarize **не** `lms load` при пустом Studio; lease только если реально грузим; OOM-path не целится в brain; cloud-fallback при **Studio недоступен** (не пустой каталог). **`cloud_rewriter_enabled` всё ещё OFF** — путь есть, флаг не включён.

## Политика LM Studio / brain (владелец 2026-09-05)

**15+ ГБ local** — один слот экосистемы: у Краба обычно **`lm-studio-local/gemma-4-26b-a4b-it@4bit`** (`LOCAL_PREFERRED_MODEL`), не «второй» Ear-only 27B. Ear `llm_brain_model` = `qwen/qwen3.6-27b` — lease/unload/OOM-UI, **preload-on-stop уже False**.

| Режим | Поведение |
|---|---|
| Idle / away | Краб отвечает в группах из того же RAM-слота; саммари звонков — если модель уже загружена |
| Работа (Cursor, диктовка) | **Не autoload.** Ear не должен `lms load` после ручной выгрузки владельца |
| Любой LLM-путь Ear | **Сначала LM Studio** (каталог / chat), без преждевременного `lms load` |
| **Пустой каталог** Studio | ≠ «Studio недоступен». Пусто → extractive / сырой STT, **без autoload** (#1999) |
| **Studio недоступен** (сеть/процесс) | Cloud, **если** `cloud_rewriter_enabled=true` и не privacy; иначе extractive/сырой текст (#1999) |
| Кондуктор | **`enforce_brain` — никогда.** Не включать `memory_conductor_enforce*` «чтобы выгнать» 27B |

🔴 Автовозврат 15+ ГБ после ручной выгрузки сейчас чаще **Краб** `ensure_model_loaded`, не Ear. Ear holdoff в проде (#1999); Краб — бриф в handoff §5.

Живые флаги (не трогать без владельца): `llm_rewrite_enabled=False`, `cloud_rewriter_enabled=False`, `llm_brain_preload_on_stop=False`, `memory_conductor_enforce*=False`, `mlx_oom_auto_unload_enabled=True` (brain исключён из target, #1999).

## Контекст (коротко, ещё актуально)

- Телефония только через Voice Gateway (#1989/#1990); ключ VG синхронизирован 03.09.
- REST fail-fast с логом (#1991); корень зависания MLX — отдельно (`mlx_lock` без timeout на инференсе).
- GigaAM `confidence=0.9` (#1985) — ретрай по уверенности для RU мёртв; решение за владельцем.
- P0 turbo/REST worker, Memory Conductor shadow, Call Observer w1 — в проде; детали в `ROADMAP` / `CLAUDE.md`.
- Не включать: `REST_IN_PROCESS_ENABLED`, `semantic_search` / SenseVoice / Voxtral на этой машине, `history_encryption_enabled`.

## Следующая волна

### Cursor (корни) — порядок из horizon §3

1. **C2** — вырезать мёртвый Telnyx UI в CD (`call_provider=gateway` жив).
2. **C3** — интерактивный timeout на `mlx_lock` в `_transcribe_model` (не rebase `feat/stt-timeout-budgets`).
3. **C5** — схема: `llm_brain_*`, `cloud_rewriter_*`, мёртвые Swift-флаги в `DEFAULT_SETTINGS`.
4. Позже: HealthMonitor 2 с, GigaAM confidence consumers.

**Сиблинг (не этот чат):** включение `cloud_rewriter_enabled` — отдельное «да» владельца (путь #1999 уже в коде).

### agy / Gemini 3.1 Pro High (после Cursor-срезов)

1. Глоссарий «Все настройки» (259 ключей) — можно сразу.
2. «Автозвонки» VG-native — **после C2**.
3. Разговор + селекторы из `list_llm_models` — **после** политики brain (дорожка B).

## Не делать

- Не чекаутить `audit/*`, не мержить PR #1875 (`krab_ru` hard-negatives).
- Не строить заново C2/C3; не «чинить» HealthMonitor sticky-hang заново.
- Не `REST_IN_PROCESS_ENABLED`; не голый `launchctl kickstart -k` под запись — `scripts/safe_backend_restart.command`.
- Не запускать собранный `KrabEarAgent` из воркера. Не `git add -A`. Не коммитить `wake_word_models/hard_negatives_raw/tts_phrases.json`.
- Не трогать Main Krab runtime / VG `.env`. Не второй EventBridge. Не wake word на SSE.
- **Никогда** `memory_conductor_enforce*` / `enforce_brain`. Не дообучать `krab_ru` синтетикой.
