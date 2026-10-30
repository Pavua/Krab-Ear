# NOW — что делать сейчас (Krab Ear)

Обновлено: **2026-10-30** — синхронизация с `.remember/` (страница отставала на 5–6 недель,
последняя правка была 18–26.09). Одна страница: база, деплой-блокеры, активные потоки, баны.
Журнал волн — [`ROADMAP-2026H2.md`](ROADMAP-2026H2.md), не очередь. Как работать —
[`EXECUTOR_PLAYBOOK.md`](EXECUTOR_PLAYBOOK.md).

## База и runtime snapshot (2026-10-30)

- Репозиторий: [Pavua/Krab-Ear](https://github.com/Pavua/Krab-Ear)
- **Source-колея:** `origin/codex/krab-ear-v2` @ **`f100ced4`** — PR [#2137](https://github.com/Pavua/Krab-Ear/pull/2137)
  слит 30.10 (10:58), дерево совпадает с draft-SHA `2861c288`, post-merge CI зелёный
  (1138 файлов / 16 групп). Внутри: нормализаторы дат/времени (ES-время `16:35 → 16:00:35`,
  границы дат, RU/ES порядковые даты `3-е`/`3-го`/`3.º`, «ч/мин», частичные порядковые даты —
  цепочка `80f86c26 → b7906637 → ab640f65 → 754c4b81 → 09383c76`, публичный кандидат `b05988d3`).
- **Prod/runtime:** **`5a09e630`** — установленная версия по фиксации журналов 30.10
  (история её выката вне октябрьских журналов). **Выкат `22a3c0e3` (мерж #2125–#2127 +
  docs #2128 от 29.10) ОТМЕНЁН как устаревший** — runtime уже на `5a09e630`; staged
  release/rollback `22a3` — историческое доказательство, требует свежей квалификации перед
  любым исполнением.
- Промежуточные точки колеи (29.10): `16552643` (#2136) — база всех октябрьских worktree-потоков.
- **Канонический чекаут:** ветка `chore/agent-parity-0928`, ДЕРЕВО ГРЯЗНОЕ (чужой WIP:
  бинарники `Krab Ear.app`, Sparkle, `native/runtime`, untracked). Чужой WIP не трогать;
  новые работы — только в owned worktree (`~/.codex/worktrees/<slug>`).
- Живое здоровье runtime/STT/звонков в октябрьских срезах НЕ перепроверялось — source-приёмки
  офлайн, runtime не перезапускался.
- Main Krab Q2 (:8080) — **не Ear**: [`ANTIGRAVITY_HANDOFF/2026-09-05-krab-8080-model-routing.md`](../ANTIGRAVITY_HANDOFF/2026-09-05-krab-8080-model-routing.md).

## Деплой — 0%. Блокеры (все три обязательны, ни один не закрыт)

1. **STT-only пауза Gateway:** draft PR VG **#551 не слит** (срок допуска исправлен,
   CI/ревью зелёные, но реальных допусков нет). Live ACK паузы ОТСУТСТВУЕТ — Gateway
   держит admission HOLD; старые disabled/404-статусы исторические, перепроб не проводился.
2. **FINISH/drain от Main:** подтверждённого завершения фоновых задач старого процесса нет;
   общий REST/server drain ACK не доказан (пробелы pre-admission ownership / nested Future
   tracking — их закрывает `ear-owned-subtree-1030`, см. ниже).
3. **Main сам на HOLD:** отсутствует обязательный `krab-runtime-qualification`. Не подменять
   синтетическими проверками и НЕ ОБХОДИТЬ.
- Условие владельца (29.10, не спрашивать повторно): принятый legacy Ear-stop риск — только
  после конца диктовки + реальной координированной STT-only паузы Gateway, с откатом.
  Clone, модели, OrbStack защищены; это не разрешение на рестарты/config VG.

## Активные потоки — параллельные Codex-сессии в worktrees (ЗАНЯТЫ, НЕ ДУБЛИРОВАТЬ)

- **ear-rest-owned-compose-1030** — root принял `fae4ec51` (11 unit + H1/H2); объединение
  `c77`+`fae` → `139d228f` (20 REST-сценариев, 57 методов, вкл. `503` с живой дочерней
  задачей). ASR/полный drain не подтверждены; выкат `22a3c0e3` здесь же помечен отменённым.
- **ear-owned-subtree-1030** — ветка от `f100ced`; причинный RED нашёл чужой Future →
  weakref-binding в `follow_future`; GREEN U1–U11 (11 PASS); H1/H2 Flask/engine готовятся.
- **ear-rest-dative-compose-1030** — REST HTTP200→503 + дательные даты (`c77aac34`,
  20 REST / 18 workers / 43 текстовых теста); `REST_NESTED_DESIGN` + `NEGATIVE_TEST_PLAN`
  согласованы с Gateway; публикация/выкат HOLD; новые запуски ждут Main FINISH.
- **ear-rest-contract-integration-1029** — Stage A реального Flask-импорта DONE
  (`6d02fe73`: 401/401/403, изоляция без рабочих потоков); Stage B (upload→executor→503)
  ждёт ревью и нового START Main.
- **ear-e2-e5-causal-1030** — JOIN-контракт переводчика (43 сценария) + изолированный
  Ear E2/E5 `0dd7eaba` на `139d`; локальный Whisper CPU без сети квалифицирован.
- **ear-date-boundaries-1030** — нормализаторы испечены в PR #2137 (слит, `f100ced4`);
  остаток: описание ограничения дат без пунктуации перед временем/другой датой.
- **ear-normalizers-public-1030** — публикация и слив #2137 выполнены 30.10; Main/Gateway/
  координатор уведомлены.
- **ear-full-lifecycle-1029** — `close()` при живом export-потоке починен (`9d9be174`,
  RED→2 PASS); ложный `healthy` отсутствующего лога → `UNKNOWN`/exit 2 (`173e86c9`);
  открыты: listener/replay ownership-aware detach, MemoryConductor stop-driver.
- **ear-aggregate-offline-1029** — offline-агрегат wiring (`91a9b714`, 84 component/cache
  cases + 2 lifecycle, ревью GO); полный backend import/конструктор НЕ квалифицирован.

## Ждёт внешнего ревью (Gateway)

- **JOIN translator** — контракт и тест-план на 43 сценария готовы.
- **Ear E2/E5** (`0dd7eaba`, база `139d228f`) — запуск ждёт ревью Gateway.

## Задачи владельца

- **Запись Golden Set для WER** до/после лексики W4 — инструмент `Record Golden Set.command`
  (открыто ещё с 09-18; без эталонов WER-замер не закрыт).
- **Решение по деплой-окну:** условия (конец диктовки + STT-only пауза Gateway + Main FINISH)
  не выполнены; пока решения нет — выкат 0%, staged `22a3` не кандидат без перепроверки.

## Docs-export — НЕ ОПУБЛИКОВАНО

- Ветка `codex/ear-instruction-docs-public-export-1030` @ `479c41d3` (11 файлов),
  независимое privacy review **CLEAR**. Публикация требует отдельной квалификации
  disclosure: legitimate author/committer с личным email остаётся в Git metadata;
  нужны fresh base и history. До квалификации — не публиковать.

## CI-изоляция — cutover СДЕЛАН (read-back 2026-09-26, без изменений в октябре)

- В публичном `Pavua/Krab-Ear` — **0 self-hosted раннеров**; PR/push CI идёт на
  GitHub-hosted `macos-latest` (disposable).
- Единственный self-hosted раннер `krab-ear-m4max-private` в приватном
  `Pavua/Krab-CI-Control`: только MLX/Metal-гейт по exact SHA доверенной колеи.
  Красный private MLX gate — сигнал расследования, не подмена PR CI.

## Политика LM Studio / brain (владелец 2026-09-05, в силе)

**15+ ГБ local** — один слот экосистемы (обычно `gemma-4-26b-a4b-it@4bit`), не «второй»
Ear-only 27B. Ear `llm_brain_model` = `qwen/qwen3.6-27b` — lease/unload/OOM-UI,
`llm_brain_preload_on_stop=False`. Пустой каталог Studio ≠ «Studio недоступен»
(пусто → extractive, без autoload, #1999). **Сначала LM Studio**, без преждевременного
`lms load`; **`enforce_brain` — никогда**. Живые флаги (не трогать без владельца):
`llm_rewrite_enabled=False`, `cloud_rewriter_enabled=False`,
`llm_brain_preload_on_stop=False`, `memory_conductor_enforce*=False`,
`mlx_oom_auto_unload_enabled=True`.

## Контекст (коротко)

- Телефония только через Voice Gateway; REST-контракт паузы/503 — в потоках `-1029/-1030`
  выше, не строить заново.
- W4/F1 STT-лексика в проде с 18.09 (`35b32bab`): hotwords 43, phonetic 11/31;
  live-добор выполнен. WER до/после — ждёт Golden Set (задача владельца выше).
- **A5.2a** — DONE source-only (26.09, #2052, в прод не задеплоено). **A5.2b** —
  DONE в source и ВЛИТО в колею: b1 #2056/`4971c24a` (encrypted snapshot), b2
  #2058/`96554b75` (restore/recovery), b3 #2060/`7afa21da` (diskguard+retention),
  integrity #2066/#2068/#2069 accepted. Runtime-активации НЕТ —
  `history_encryption_enabled` остаётся OFF. Активный код-долг
  `A5_2B_CALLER_SUCCESS_LOG_DEBT` (`service.py` логирует migration-complete при
  `MigrationResult.reason=history_encryption_operation_unavailable`; callback
  `push_error` у PolicyReader есть, но manager-вызовы его не пробрасывают). Спека
  `2026-09-24-a5-history-at-rest-design.md` и хендофф `2026-09-25-a5-lifecycle-start.md`
  в силе; очередь сместилась на нормализаторы/REST.
- Инцидент Glovo (26.09) закрыт: вариант C (whisper-поле в Call Observer, #2054) в колее;
  вариант B (TG-бот) — спека в VG-репо, правки отдельной сессией.
- GigaAM `confidence=0.9` (#1985) — ретрай по уверенности для RU мёртв; решение за владельцем.
- Не включать: `REST_IN_PROCESS_ENABLED`, `semantic_search` / SenseVoice / Voxtral на этой
  машине, `history_encryption_enabled`.
- История деплоев сентября (6561a030 → 5cab7988 → 35b32bab → e004ba3d, W4/F1, R1-табло/F5/F2
  с флагами OFF) — в `ROADMAP-2026H2.md`, здесь не актуальна.

## Не делать

- **Не дублировать и не перезапускать активные потоки `-1030`/`-1029` из worktrees**
  (девять перечисленных выше) — ими заняты параллельные Codex-сессии; перед любой работой
  сверять, не ведётся ли она уже.
- **Не обходить Main HOLD** (отсутствует `krab-runtime-qualification`) и не подменять его
  синтетическими прогонами.
- **Не публиковать Ear docs-export (`479c41d3`)** без отдельной disclosure-квалификации
  (личный email в Git metadata).
- **Не выкатывать `22a3c0e3`** как кандидата без свежей перепроверки — runtime уже
  `5a09e630`.
- Не чекаутить `audit/*`, не мержить PR #1875 (`krab_ru` hard-negatives).
- Не строить заново C2/C3; не «чинить» HealthMonitor sticky-hang заново.
- Не `REST_IN_PROCESS_ENABLED`; не голый `launchctl kickstart -k` под запись —
  `scripts/safe_backend_restart.command`.
- Не запускать собранный `KrabEarAgent` из воркера. Не `git add -A`.
- Не коммитить `wake_word_models/hard_negatives_raw/tts_phrases.json`.
- Не трогать Main Krab runtime / VG `.env`. Не второй EventBridge. Не wake word на SSE.
- **Никогда** `memory_conductor_enforce*` / `enforce_brain`. Не дообучать `krab_ru` синтетикой.
- Не трогать чужой WIP в каноническом чекауте (`chore/agent-parity-0928`).
