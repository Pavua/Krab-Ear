# NOW — что делать сейчас (Krab Ear)

Обновлено: **2026-09-18**. Одна страница: база, политика brain/GPU, очередь. Журнал волн — [`ROADMAP-2026H2.md`](ROADMAP-2026H2.md), не очередь. Горизонт 2–4 нед: [`design-briefs/2026-09-05-horizon-plan.md`](design-briefs/2026-09-05-horizon-plan.md). Как работать: [`EXECUTOR_PLAYBOOK.md`](EXECUTOR_PLAYBOOK.md).

## Деплой 2026-09-18 №2 (актуальный runtime) — ночная волна

- **Прод-код:** `e004ba3d` — R1 табло, F5 (ленивая выгрузка семантики),
  F2/F2b (спенд-кап харденинг), журналы. Поведение прода не меняется:
  `cloud_rewriter_enabled=False`, `semantic_search_enabled=False` (весь код
  спит до флагов). Процедура §деплой 09-11: busy 60 с idle + финальный,
  worktree --detach, swap SHA (бэкап `/tmp/ear-plist-backup-20260919/`),
  bootout (poll до «gone», ~5 с) → bootstrap без EIO/ретраев.
- Backend pid **89956**, REST **90743** (GigaAM worker 90534); агент **7602**
  не тронут. Прежний релиз `35b32bab` оставлен для отката.
- Постдеплой: ping ok (v2.0.5, ~10 с), diagnostics 13/13, REST `/health` 200
  (~6 с), e2e 44/44 + 21/21 green, privacy-gates hold, Sentry — 0 инцидентов Ear.
  F1-лексика цела (phonetic 11/31, hotwords 43); semantic off/model не грузился.

## Деплой 2026-09-18 №1 — F1 (35b32bab)

- **Прод-код:** `35b32bab` — F1 (лексика W4) в проде. Процедура §деплой
  09-11 без отклонений: busy-check 60 с idle + финальный, worktree --detach,
  swap SHA в обоих plist (бэкап `/tmp/ear-plist-backup-20260918/`), bootout
  (poll до «gone», ~6 с) → bootstrap (ретраи не потребовались; EIO не возник).
- Backend pid **41951**, REST **42836**; агент **7602** не тронут. Прежний
  релиз `5cab7988` оставлен для отката.
- Постдеплой: ping ok (v2.0.5, ~8 с), diagnostics 13/13, REST `/health` 200
  (~4 с), e2e-смоки 44/44 + 21/21 green, privacy-gates hold, Sentry — ноль
  инцидентов Ear за окно.
- 🔴 **Live-добор W4** (авто-seed в коде — только на пустой файл; у владельца
  файлы непустые): hotwords `оверлей`,`openclaw` через IPC (42→43);
  phonetic +10 кураторских записей / 28 вариантов через `add_phonetic_entry`
  (было 1/3 → стало 11/31). Следующие запекания лексики — тоже IPC-добором.
- WER до/после — ждёт R2-записи владельца (инструмент: `Record Golden Set.command`).

## Деплой 2026-09-16

- **Прод-код:** `5cab7988` (R1 encryption fail-closed + R2/R4 тесты, CИ зелёный:
  CI + krab-ear-ci + mlx-nightly). Процедура §09-11 без изменений; EIO на
  первом bootstrap REST сработал как задокументировано (ретрай +5с — ок).
- Backend pid **21156**, REST **22059** (оба свежие); агент **1020** не тронут
  (Swift не менялся). Прежний релиз `6561a030` оставлен для отката.
- Проверка «нет записи/встречи»: 60 с idle + финальный чек перед bootout.
  Постдеплой: ping ok, diagnostics 13/13, агент 1, Sentry — один
  `GigaAM worker shutdown` warn-batch (штатный артефакт рестарта, класс
  self-heal). Fable retro-gate R1 — пост-квотой (см. BACKLOG).
- R1 в проде нулевого эффекта (фича banned-off) — деплой гигиенический
  (колея == прод), не релиз фич.

## Деплой 2026-09-11

- **Прод-код:** `6561a030` (#2016). Backend и REST запускаются из неизменяемого
  release-worktree `~/.local/share/krab-ear/releases/<sha>` (locked, detached);
  путь прописан в `PYTHONPATH` и `ProgramArguments` обоих plist
  (`ai.krab.ear.backend`, `ai.krab.ear.rest`). 🔴 Деплой = `git worktree add --detach`
  нового SHA + замена SHA в plist + `bootout`/`bootstrap` (kickstart plist не перечитывает;
  bootstrap сразу после bootout даёт EIO, пока старый процесс гасится — повторить).
  Прежний релиз `375d4bed` оставлен для отката. Проверка «нет записи/встречи» — перед bootout.
- Вошло: privacy fail-closed #2005–#2016 (корень — `service._get_runtime_setting`
  для `privacy_mode_enabled`, #2016), brain-lease конечный TTL #2004. Swift не менялся —
  агент не пересобирался. Живой e2e (`scripts/run_e2e_smokes.command`): 65 PASS / 0 FAIL.
- Открыто 11.09 → закрыто: encryption fail-open — волна R1 (fail-closed +
  громко), задеплоено 16.09; soak-таймаут — волна R2 (per-cycle unload
  шторм убран фикстурой, soak ~22 с, гард 30 с цел).

**Source-дополнение 2026-09-07:** PR [#2001](https://github.com/Pavua/Krab-Ear/pull/2001)
добавляет телефонный STT-профиль для Voice Gateway: explicit auto до общего STT,
RU через уже загруженный owner GigaAM, без второго worker и history. Контракт и незакрытая
runtime qualification — в [плане](superpowers/plans/2026-09-07-vg-gigaam-call-profile.md).
В этой работе runtime не перезапускался; флаги Gateway
`KRAB_STT_EAR_CALL_PROFILE_ENABLED` и `KRAB_SCREENING_AUTO_LANGUAGE_ENABLED` OFF.
Source-проверки не доказывают CI другого SHA или готовность живого звонка.
Текущие HEAD/CI — в primary `.remember/CODEX_CALL_STT_20260907.md` с повторной
проверкой Git/GitHub. SHA/PID ниже сохранены как snapshot 05.09, не текущая проверка.

## База и runtime snapshot 2026-09-18

- Репозиторий: [Pavua/Krab-Ear](https://github.com/Pavua/Krab-Ear)
- Прод-колея: **`origin/codex/krab-ear-v2`** @ `e004ba3d`
- **Прод-код:** `e004ba3d` (релиз 18.09 №2, §деплой выше; колея == прод)
- Backend pid **89956**, REST **90743** (деплой 18.09 №2), агент pid **7602** (не тронут)
- Worktree: `git worktree add .worktrees/<slug> -b feat/<slug> origin/codex/krab-ear-v2`
- Main Krab Q2 (:8080 purpose slots, RIS/SergeyRG) — **не Ear**: [`ANTIGRAVITY_HANDOFF/2026-09-05-krab-8080-model-routing.md`](../ANTIGRAVITY_HANDOFF/2026-09-05-krab-8080-model-routing.md)

## Подготовленная CI-изоляция (ещё не operational cutover)

- Public PR/push Swift CI переводится на standard `macos-latest` и остаётся disposable.
- MLX/Metal gate переезжает в private `Pavua/Krab-CI-Control` и принимает только exact SHA доверенной колеи.
- До отдельного quiet-window cutover runner `krab-ear-m4max` ещё зарегистрирован в public repo; не объявлять `total_count=0` раньше API read-back.
- Hosted macOS не заменяет MLX проверку: VM не даёт эквивалентного Metal-пути. Красный private MLX gate — отдельный сигнал расследования, не подмена PR CI.

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
- REST fail-fast с логом (#1991); C3 в колее (#2000: attempt-deadline на Whisper; таймаут самого ожидания замка по-прежнему отвергнут — честная очередь за GPU).
- GigaAM `confidence=0.9` (#1985) — ретрай по уверенности для RU мёртв; решение за владельцем.
- P0 turbo/REST worker, Memory Conductor shadow, Call Observer w1 — в проде; детали в `ROADMAP` / `CLAUDE.md`.
- Не включать: `REST_IN_PROCESS_ENABLED`, `semantic_search` / SenseVoice / Voxtral на этой машине, `history_encryption_enabled`.

## Следующая волна

C2/C3/C5 закрыты в колее 07–08.09 (интеграция #2000, схема #2003, сверено 14.09):
Telnyx вырезан из CD-секции (осталась payload-совместимость `Models.swift` + тесты),
Whisper ждёт по attempt-deadline, валидатор отклоняет неверные типы brain/cloud-строк,
paste-флаги типизированы. Не строить заново.

### Корни (порядок)

1. **R1** — DONE + задеплоено 16.09 (fail-closed, Fable retro-gate пост-квотой).
2. **R2** — DONE (unload-нейтер, soak ~22 с; CI nightly подтверждает скипы).
3. **PR #2001** — синтетика 10/10 (p50 0.4 с); ЖИВОЙ ЗВОНОК СОСТОЯЛСЯ
   16.09 (VG-сессия, отель SH Valencia Palace, 92 с, IVR, ~$0.03–0.04,
   запись+саммари в TG): тракт чист, НО Ear-профиль не задействован
   (0 обращений к :5005, es→groq). Живой RU-замер открыт: форсированный
   Ear-STT (карточка VG) или RU-сценарий. Busy-probe оппортунистически.
4. **Smoke-раннер Ear** — DONE (launchd, штатные прогоны OK 15–16.09).
5. **W1/W2** — DONE+задеплоено (mic-hold гейты; callassist ownership-gate).
   GigaAM-финал: вердикт без кода (роутинг уже GigaAM-first + покрыт).
6. **W4/F1 STT-лексика** — ЗАПЕЧЕНО 18.09 (#2029) и **В ПРОДЕ** (деплой
   `35b32bab` 18.09): 10 phonetic-записей (openclow, RU/ES-препараты,
   висперед→whisper, лрд/lrd→p0lrd), seed-hotwords оверлей/openclaw, tail-фильтр
   голого `dimatorzok`, `phonetic_vocab_enabled=True`, REST-движок wired.
   Live-добор выполнен (seed не доехал на непустые файлы): hotwords 42→43,
   phonetic 1/3→11/31 через IPC. WER до/после — ждёт R2-эталоны.
   `maby` НЕ запечён (ждёт примеров; EN "maybe" — контроль в R2-сценарии).
7. **Волна 0 (гигиена, до 30.09)** — 0.1 (изоляция e2e-моста), 0.2 (контракт),
   0.3 (quality_profile из настроек), 0.4 (паритет IPC-документации: 50 записей),
   0.5 (меню Update Channel удалено) — DONE+смержены 17.09. Остаток волны: ответы
   соседей (0.6, ждём Krab Main / VG).
8. **Ночная волна 18/19.09 (в колее, ждёт деплой-окна; флаги OFF)** — R1 табло
   (#2033/#2034: сканер+панель+launchd 06:00; первый снимок — fail из-за
   исторических bridge_401=24, вымылись к 10:00), F5 ленивая выгрузка семантики
   (#2037/#2038: `_semantic_step`, always-on, 1800с/0=off, бeз enforce и IPC),
   F2/F2b spend-cap (#2035/#2036 + #2039/#2040: атомарный резерв через
   `core/atomic_io`, inf-кламп, fail-closed; adversarial-ревью нашло 5 дыр →
   закрыты, SECURITY-PASS). Поведение прода не меняется (флаги OFF).
   D10 исполнен (−126 локально/−1260 origin, auto-delete on).

Позже: HealthMonitor 2 с (C6, не чинить sticky-hang заново), GigaAM confidence consumers (#1985 — решение за владельцем).

**Сиблинг (не этот чат):** включение `cloud_rewriter_enabled` — отдельное «да» владельца (путь #1999 уже в коде).

### agy / визуал

1. Глоссарий «Все настройки» (245 ключей) — **DONE** (`SettingsGlossary.swift` + `docs/settings-glossary-ru.md`, двухстрочный UI, полнотекстовый поиск, паритет 245/245).
2. «Автозвонки» VG-native — **разблокирован** (C2 в колее #2000).
3. Разговор + селекторы из `list_llm_models` — **после** политики brain (дорожка B).
4. Пилот дешёвого визуала (GPT 5.4 Mini) — мелкий фикс с гейтом диффа здесь; жирные брифы остаются за Gemini 3.1 Pro High.

## Не делать

- Не чекаутить `audit/*`, не мержить PR #1875 (`krab_ru` hard-negatives).
- Не строить заново C2/C3; не «чинить» HealthMonitor sticky-hang заново.
- Не `REST_IN_PROCESS_ENABLED`; не голый `launchctl kickstart -k` под запись — `scripts/safe_backend_restart.command`.
- Не запускать собранный `KrabEarAgent` из воркера. Не `git add -A`. Не коммитить `wake_word_models/hard_negatives_raw/tts_phrases.json`.
- Не трогать Main Krab runtime / VG `.env`. Не второй EventBridge. Не wake word на SSE.
- **Никогда** `memory_conductor_enforce*` / `enforce_brain`. Не дообучать `krab_ru` синтетикой.
