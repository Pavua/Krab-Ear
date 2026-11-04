# NOW — что делать сейчас (Krab Ear)

## 04.11: REST error-result — локальный source-only gate

Owned `codex/ear-rest-errors-1104`, база `6e2043fb` (#2159). Восстановлен
существующий механизм из `50cbcdc3`/`c9e62600`: engine error-result возвращает
`503/stt_failed` до history/успешных metrics; privacy `403` сохраняет приоритет,
нормальный пустой результат остаётся `200`. Actual Flask upload/executor:
RED шесть ложных `200` → GREEN шесть `503` + два silence `200`, восемь workers
завершены; синтетический STT leaf, без моделей/сети/live runtime.
Проверочный parent не сигналит после reap; финальный receipt только после cleanup.
Независимое Astra/Ultra whole-diff review: GO после причинного закрытия teardown-P2.
Карточка: [REST error-result](superpowers/plans/2026-11-04-rest-error-status.md).
Публикация этой ветки ещё не разрешена; merge/deploy/restart не выполнялись.
Ни `503`, ни этот sandbox-прогон не доказывают общий server/nested-native drain.

Ниже — исторический snapshot 03.11; PID/старые worktree-статусы не являются
текущим runtime read-back. Отдельные draft #2160/#2161 не входят в эту ветку.

Обновлено: **2026-11-03** — база догнана до HEAD колеи `#2158`/`f6afd456`;
волна lifecycle/agent/backend 02–03.11 (#2153–#2158). Python runtime не перезапускался.
Одна страница: база, деплой-блокеры, активные потоки, баны.
Журнал волн — [`ROADMAP-2026H2.md`](ROADMAP-2026H2.md), не очередь. Как работать —
[`EXECUTOR_PLAYBOOK.md`](EXECUTOR_PLAYBOOK.md).

## База и runtime snapshot (source 2026-11-03, runtime read-back 03.11)

- Репозиторий: [Pavua/Krab-Ear](https://github.com/Pavua/Krab-Ear)
- **Source-колея:** `origin/codex/krab-ear-v2` @ **`f6afd456`** — PR [#2158](https://github.com/Pavua/Krab-Ear/pull/2158)
  (AST-тесты после EventBus detach). Хвост 02–03.11 после [#2152](https://github.com/Pavua/Krab-Ear/pull/2152)/`6d881004`:
  [#2153](https://github.com/Pavua/Krab-Ear/pull/2153) NOW → `d9471793`,
  [#2154](https://github.com/Pavua/Krab-Ear/pull/2154) agent recovery log → `6bb76e0c`,
  [#2155](https://github.com/Pavua/Krab-Ear/pull/2155) flock → `14e40251`,
  [#2156](https://github.com/Pavua/Krab-Ear/pull/2156) MemoryConductor stop-driver → `250b3c56`,
  [#2157](https://github.com/Pavua/Krab-Ear/pull/2157) EventBus detach on close → `b225aeaf`,
  [#2158](https://github.com/Pavua/Krab-Ear/pull/2158) → `f6afd456`.
  Golden/WER 02.11 (не дублировать): [#2142](https://github.com/Pavua/Krab-Ear/pull/2142)–[#2151](https://github.com/Pavua/Krab-Ear/pull/2151)
  — см. [#2153](https://github.com/Pavua/Krab-Ear/pull/2153). Предки 30.10: [#2141](https://github.com/Pavua/Krab-Ear/pull/2141)/`839e75f4`.
- **Split runtime (prod ≠ колея):** **Python** **`5a09e630`**, PID **1390**
  (`ear-release-5a09e630`, data-dir prod) — без рестарта с 02.11; **03.11** IPC `ping` ok,
  `is_recording=false`. **Swift-агент** PID **65661** (`.app` в каноне); бэкенд PID не менялся.
  Фиксы агента #2154/#2155 и backend #2156–#2158 в source-колее — **Python deploy 0%**,
  пересборка `.app` только по отдельному idle-окну владельца.
- Промежуточные точки колеи (29.10): `16552643` (#2136) — база всех октябрьских worktree-потоков.
- **Канонический чекаут:** ветка `chore/agent-parity-0928`, ДЕРЕВО ГРЯЗНОЕ (чужой WIP:
  бинарники `Krab Ear.app`, Sparkle, `native/runtime`, untracked). Чужой WIP не трогать;
  новые работы — только в owned worktree (`~/.codex/worktrees/<slug>`).
- Полный STT/звонковый smoke 03.11 не делался; сверка: git + `ps` + IPC ping (см. выше).
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
- **ear-date-boundaries-1030** — нормализаторы в PR #2137 (`f100ced4`); ограничение
  дат без пунктуации перед временем/другой датой уже описано в
  [`2026-10-30-normalizer-contracts.md`](superpowers/plans/2026-10-30-normalizer-contracts.md)
  — не открытый хвост.
- **ear-normalizers-public-1030** — публикация и слив #2137 выполнены 30.10; Main/Gateway/
  координатор уведомлены.
- **ear-full-lifecycle-1029** — **DONE в колее 03.11** (хвосты закрыты, поток не дублировать):
  export `close()` (`9d9be174`), log health (`173e86c9`), MemoryConductor stop-driver
  ([#2156](https://github.com/Pavua/Krab-Ear/pull/2156)), EventBus listener detach on
  `BackendService.close()` ([#2157](https://github.com/Pavua/Krab-Ear/pull/2157)/[#2158](https://github.com/Pavua/Krab-Ear/pull/2158)).
- **ear-aggregate-offline-1029** — offline-агрегат wiring (`91a9b714`, 84 component/cache
  cases + 2 lifecycle, ревью GO); полный backend import/конструктор НЕ квалифицирован.

## Ждёт внешнего ревью (Gateway)

- **JOIN translator** — контракт и тест-план на 43 сценария готовы.
- **Ear E2/E5** (`0dd7eaba`, база `139d228f`) — запуск ждёт ревью Gateway.

## Задачи владельца

- **Golden Set** — **записан** владельцем 02.11: `record_golden.py --dry-run` → **50/50**
  WAV в `~/Library/Application Support/KrabEar/golden/` (речь, не тишина). WER-прогон
  `scripts/wer_eval.py` — по решению владельца (не блокер кода; харнесс #2152 в колее).
  Чек-лист: [`2026-10-30-golden-set-wer-checklist.md`](superpowers/plans/2026-10-30-golden-set-wer-checklist.md).
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
  live-добор выполнен. Golden Set записан 02.11; формальный WER до/после — по решению
  владельца (`wer_eval.py`, харнесс #2152).
- **A5.2a** — DONE source-only (26.09, #2052, в прод не задеплоено). **A5.2b** —
  DONE в source и ВЛИТО в колею: b1 #2056/`4971c24a`, b2 #2058/`96554b75`,
  b3 #2060/`7afa21da`, integrity #2066/#2068/#2069. Лог-долг
  `A5_2B_CALLER_SUCCESS_LOG_DEBT` закрыт `3a253bc52` (честный startup-лог
  refused vs complete). Runtime-активации НЕТ — `history_encryption_enabled` OFF.
  Спека `2026-09-24-a5-history-at-rest-design.md` и хендофф
  `2026-09-25-a5-lifecycle-start.md` в силе; очередь сместилась на нормализаторы/REST.
- Инцидент Glovo (26.09) закрыт: вариант C (whisper-поле в Call Observer, #2054) в колее;
  вариант B (TG-бот) — спека в VG-репо, правки отдельной сессией.
- GigaAM `confidence=0.9` (#1985) — ретрай по уверенности для RU мёртв; решение за владельцем.
- Не включать: `REST_IN_PROCESS_ENABLED`, `semantic_search` / SenseVoice / Voxtral на этой
  машине, `history_encryption_enabled`.
- История деплоев сентября (6561a030 → 5cab7988 → 35b32bab → e004ba3d, W4/F1, R1-табло/F5/F2
  с флагами OFF) — в `ROADMAP-2026H2.md`, здесь не актуальна.
- **Брошенные WIP разобраны 30.10, все три SUPERSEDED (НЕ восстанавливать):**
  `codex/ear-paste-async-20261016` (async paste CI2099 — колея реализовала шире:
  весь `PasteService` `@MainActor`-async + seam `modifierKeysDown` + оба теста,
  `598087333`), `feat/a53-card-a-authorizer` (A5.3 slice 3 — все поверхности
  покрыты CAS-коммитом; «уникальный» auto-repair settings.json колея осознанно
  отвергла no-write-дизайном, тест `test_integrity_repair_does_not_reset_unknown_to_defaults`
  ожидает `fixed==0`) и `codex/ear-a5-lifecycle` (ahead-коммит `9db5162e` — два docs-файла,
  байт-в-байт уже в колее через A5.2a #2052; весь план пройден дальше: A5.2 задеплоена
  `84deb513`, A5.3 закрыт, A5.4 в проде 10.10 — уникального кода ноль). Worktrees и ветки
  удалены; a53-diff заархивирован в `docs/audit/2026-10-30-superseded-wip-a53-card-a-authorizer.patch`.

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
