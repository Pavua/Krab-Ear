# NOW — что делать сейчас (Krab Ear)

## 2026-10-26 — очередь интегрирована; релиз подготовлен, выкат удерживается

**Source / merge / exact post-merge CI: PASS. Runtime cutover: HOLD.**
На 26.10 21:50 UTC удалённая целевая ветка подтверждена на
`22a3c0e3193999bcb434d9fdb823e2b4eb967877`. Итоговое tree
`c98feed74a507394be33b25111a45e84ca95ac52` совпадает с независимо
проверенной композицией; Swift, scripts и core/config.py не менялись.

| PR | Merge SHA | Terminal post-merge CI / backend |
| --- | --- | --- |
| #2125: helper трёх хранилищ | `0fe59fecdd405d1445daaa2d8a6c873508f0a781` | 40338133375 / 40338133317 SUCCESS |
| #2126: отмена поздней доставки, удержание LiveSubs worker | `0b93461549df220f8c9a20f9682739f895f7702a` | 40348134838 / 40348134995 SUCCESS |
| #2127: владение принятой async batch-задачей | `22a3c0e3193999bcb434d9fdb823e2b4eb967877` | 40353515577 / 40353515682 SUCCESS |

Все три backend raw receipts: 16 chunks, aggregate rc0, без fallback и
isolated failed files; число файлов соответственно 1128 / 1129 / 1130.
Это числа файлов отдельных запусков, не сумма тестовых методов.
Повторные/отменённые запуски в этой интеграции не создавались;
все три наблюдателя завершены, дочерние группы пусты.

Владелец 26.10 прямо разрешил merge/deploy готового Ear. Подготовлен чистый
locked detached release `ear-release-22a3c0e3`; старый `ear-release-66e44c53`
сохранён для отката. `prepare_release_cutover.py` проверил private bundle
двух before/two after plist и manifest: меняются только Python entrypoint и
PYTHONPATH. Действующие LaunchAgents, Swift app и процессы не изменялись.
Приватные plist/env не публиковать и не включать в Git.

Независимое release review: **PREPARATION GO / CUTOVER HOLD до первой
остановки**. Старый `66e44` не учитывает все batch/background/nested/TTS
задачи; полного terminal drain доступные metadata не подтверждают.
Нулевые recording/meeting/native счётчики и health200 не закрывают это
UNKNOWN. Удерживаемое окно без новых запросов (включая VG periodic STT)
и passive/reconnect текущего Swift процесса тоже отдельно не подтверждены.
Новый код не исправляет уже работающую старую generation задним числом.

Следующий шаг: отдельное решение владельца о legacy-risk maintenance либо
сохранение текущего запуска. При maintenance сначала согласовать фактическое
окно и producer fence; не трактовать elapsed time/ответ соседнего чата как
разрешение stop. Использовать reviewed safe-release-cutover и stage-specific
откат обоих plist. Не делать автоматический kill/rollback занятого или
неизвестного нового процесса. Модели, клон и OrbStack остаются защищены.

Артефакты текущего чата: `ear-integration-release-20261026/` —
`ci_owner/INTEGRATION_FINAL.json`, `RELEASE_SOURCE_GATE_REVIEW_RU.md`,
`RELEASE_STAGED_RECEIPT.json`; приватный bundle хранится отдельно.
Итоговая память содержит абсолютные пути. Source/CI этого блока 100%;
выкат не начат, live E2E и сохранность фраз при реальном cutover не заявляются.

Предыдущие датированные разделы ниже — исторические checkpoints;
их «следующий шаг» не заменяет текущий статус.

## 2026-10-26 — первый срез конструктора: source/offline checkpoint

Ветка `codex/ear-startup-characterization-1025`, база `66e44c53`.
Production checkpoint `d4ec1f5b`: прежние три присваивания local text stores
вынесены в `_init_local_text_stores`, единственный вызов — сразу после plaintext
IPC. Порядок Vocabulary → Snippet → Phonetic, три отдельных чтения `data_dir`,
немедленные присваивания и распространение исключений сохранены.

Локальная характеристика конструктора: **40 PASS на Python 3.14 и 40 PASS на
Python 3.12**, семь исходных reader assertions исполнены в AST-обёртке;
это не полные исходные integration suites. Обратный inline даёт прежний module
AST; 16 статических audit-команд прошли. Отдельный ограниченный стенд:
**34 синтетические проверки PASS**, затем **92 исходных storage tests PASS**
(35 vocabulary / 27 snippets / 30 phonetic) на Python 3.14.8. В последнем
запуске загружены 11 канонических модулей, count и имена методов сверены,
ошибок, skips, запрещённых обращений и оставшихся потоков нет; дочерний процесс
завершён, его группа пуста, хеши исходников сохранены.

Стенд запуска и его квитанции остаются внешними проверочными артефактами;
они не добавляют приложению механизм runtime-защиты. Полный BackendService,
startup/IPC/E2E, полноценное Ubuntu parity-окружение и hosted CI этим checkpoint
не подтверждены. Следующий шаг — финальное whole-diff review и draft PR с
отдельным CI точного SHA. Runtime, модели, клон и OrbStack не менялись.

Владелец 26.10 разрешил лёгкие ограниченные offline-проверки параллельно между
сессиями с сообщениями START/FINISH. Общие тяжёлые CI/build/model/lifecycle
операции по-прежнему требуют согласования фактической нагрузки.

## 2026-10-23 — Task 2 partial/paste SSE: локальный source checkpoint

Ветка `codex/ear-partial-sse-1022`, stacked на Task 1 `f0b9f568` (#2109,
exact-head27 SUCCESS). Partial preview и streaming paste используют общий
transport: clean/error EOF восстанавливает подписку, event type сбрасывается,
stop/hide/recording stop отменяет retry, late callbacks прежней записи отклоняются.
Reconnect сохраняет recording owner/final barrier без дублирования final paste.
Приём partial и reservation синхронны до async input; порядок FIFO сохранён.
Render/text/revision/debounce и native paste leaf не менялись.

Оба actual controller EOF: localhost RED1connect вместо2 → GREEN. Ограниченный
harness **56 PASS**, включая прежние14 streaming regressions; actual extensions/
controller/transport/parser/InputAutomationQueue/result/reservation definitions,
UI и native PasteService leaf заменены stub (native calls fatalError, не используются).
Независимое повторное whole-diff source review + audit-all PASS; review-замечание
о маскирующем handleFinal тесте закрыто: прямой SSE→real reservation/FIFO→spy,
mutation dropped-final-dispatch RED → восстановленный whole56 GREEN. Полный AppKit/hosted Task2 и RealtimeOverlayTailTests впереди:
в оба явных Swift CI filters добавлены новые suites и tail regression.
Task3 status/translation остаётся впереди. Runtime recovery NOT_TESTED;
модели, процессы, микрофон, clipboard и чужие проекты не затронуты.
Новая ветка пока локальная; следующий push только в согласованном окне.

## 2026-10-22 — SSE recovery: Task 1, PR #2109 exact-head CI SUCCESS

Изолированная ветка `codex/ear-sse-recovery-1022`, база `3f08c9dc` + reviewed
plan `9a97009e`. ProbeSSEBox теперь использует общий Foundation transport owner:
clean/error EOF освобождает соединение и планирует один retry с backoff до 30s;
FIFO callbacks проверяют поколение/UUID, parser state сбрасывается при reconnect.
Отмена синхронно закрывает admission ещё до MainActor cleanup — независимое
ревью обнаружило этот race, новый actual-owner тест воспроизвёл RED → GREEN.

Локальный ограниченный harness: **31 PASS**, включая настоящий localhost
TCP/HTTP/URLSession EOF/reconnect; actual HealthMonitor/transport/parser,
StatusIndicatorView заменён leaf stub. Полный AppKit build и выбранный suite: 330 PASS; exact-head
`f0b9f568` hosted CI: 27 SUCCESS / 0 failures. Независимое повторное
source review PASS. Это только Task 1 плана `2026-10-21-sse-recovery.md`;
partial/paste/status/translation ещё не переведены. Runtime recovery не доказан,
production lifecycle, модели, clipboard и микрофон не затронуты. Main разбирает
новые Telegram ответы; полнота их исправления этим source gate не заявляется.

## 2026-10-21 — source: IPC stage timing, runtime-корреляция ожидает релиза

Карточка `superpowers/plans/2026-10-21-ipc-stage-timing.md`, база `0ff399bc`
(#2106). Opt-in UUID для ping/recording/meeting связывает metadata-only этапы
accept/worker/handler/privacy/audit/send с клиентским ID Main. Ограниченный
неблокирующий буфер и обычный auth/throttle query `get_ipc_timing`; при потере
приёма ID completeness всей эпохи UNKNOWN. Исходные RPC/тайм-ауты сохранены.
Локально 110 тестов в девяти отдельных процессах, новые 15 также на Python3.12,
Ruff/audit-all и независимый Astra whole-diff source review PASS. Exact-head CI
проверяется отдельно. Полный локальный parity harness не запускался: готового
venv нет, установка backend/ML dependencies отложена ради текущего memory HOLD.
Main/Gateway получили контракт и сохраняют паузу модельных окон. До релиза
runtime не знает query; причина прежнего 3s recv timeout остаётся UNKNOWN.
REST retry/deadline и decoder compatibility — отдельные карточки. ES после
стабилизации памяти; эта source-карточка не меняет lifecycle/config/модели.

## 2026-10-21 — source: advisory activity/lease diagnostics

Ветка `codex/ear-activity-diagnostics` от052b6c9, интегрирована на ab61bf45 (#2104/#2105): strict readonly lease observation,
неблокирующие metadata recording/finalizing/meeting/capture worker, REST native POST/WS
server-owned reservations внешнего transcriber callable (`completion_scope`).
Вложенные adapter futures и WS translation/emit явно остаются в untracked coverage. `get_memory_ledger` и `/v1/readiness` получают
additive поля; component idle не превращается в global owner-idle. Неполное coverage
явно оставляет owner generation:null/known:false; drain/admission/enforce не добавлены.
HTTPtimeout/disconnect не завершает native работу. Контракт:
`docs/superpowers/specs/2026-10-21-activity-diagnostics-design.md`.

Main/Gateway подтвердили semantics/coverage и owning границы. 21.10 05:49 владелец
разрешил Ear deploy/restart после проверок и согласованного окна; ES остаётся после
стабилизации памяти. Source validation/CI/review идут отдельно от release acceptance;
никакие runtime права не следуют из одного ledger/readiness/PID. Соседние lifecycle окна
серийные; Ear ждёт Main FINISH. Флаги и модельная политика не меняются этой карточкой.

## 2026-10-19 — Независимое ревью #2100/#2101, фиксы запушены; память сжата

Ревью вели бесплатные воркеры (DeepSeek/GLM/Nemotron через opencode, Gemini через agy); каждую находку я проверил по коду.
- **#2101** (STT residency): `/v1/readiness` падал бы 500 при сбое снимка → теперь `{"owner":"rest","state":"unavailable"}`; пары REST-файлов красные в одном порядке (`_FakeEngine` без метода) теперь зелёные в обоих (52 PASS, 219 по файлам PR). Коммит `63366fc6`.
- **#2100** (input-очередь вставки): подтверждены и исправлены (RED→GREEN, фильтр CI Swift локально **308 PASS**): (1) `revise(text:"")` затирал clipboard пустой строкой, слал лишний Cmd+V и стирал `lastPastedText`; (2) `SelectionTranslator` не возвращал saved-clipboard на выходах после Cmd+C. Коммит `29e7dda0`.
- **#2100, не исправлено (дизайн/унаследованное, решение владельца):** QuickEdit + streaming отбрасывает правку пользователя (было и до PR); `noEffects` терминален — fallback на обычную вставку запрещён (до PR разрешён); устаревшая streaming-сессия при выключении настройки посреди записи (редко); desync счётчика символов в AXSearchField; Undo отклоняется при concealed clipboard; повтор Cmd+Opt+V до записи `lastPastedAt`. Merge/deploy #2100 не выполнялись.
- **Память:** `CLAUDE.md` 173→109 КБ, подробности дословно в `docs/CLAUDE_ARCHIVE.md` (метка `§L<n>`); `NOW.md` 80→27 КБ, история в `docs/archive/NOW-history-2026-09-10.md`.
- Voice Gateway (19.10): `hint_terms` не нужен (звонки шли на OpenAI Realtime); обычные звонки пойдут каскадом через Ear — нагрузка на call-STT вырастет. Рестарт `ai.krab.ear.*` — только после согласования с Крабом и VG.

## 2026-10-18 — продолжение source: активация адресата вставки

Свежая удалённая база: `origin/codex/krab-ear-v2` = `19124f51` (#2099).
Оба post-merge workflow этого SHA перепроверены в GitHub: CI `38850476587`
и krab-ear-ci `38850476647` SUCCESS. #2097–2099 смержены; открытых PR при
входе в сессию не было. Ниже — исторические snapshots, их прежние pending/PID
не считать текущим состоянием. Runtime после перезагрузки здесь не квалифицирован.

В изолированной ветке `codex/ear-paste-activation-20261018` продолжается общая
async input/clipboard очередь. P3 исправлен отдельным `b4aead38`: clipboard
обещается только после успешной записи. Paste/replay/revision, streaming final
barrier и SelectionTranslator используют общую bounded lease; target фиксируется
до await, revision delete→insert атомарна относительно других собственных input.
Partial/unknown запрещают полный fallback и статус ok; подтверждённая CG пара
не доказывает физическую доставку. Explicit copy инвалидирует pending, restore CAS.
Активационный контракт и его13 тестов сохранены. [Текущая карточка](superpowers/plans/2026-10-18-paste-input-queue.md).

Review-фиксы: CG drift после down → unknown; streaming резервирует общую FIFO
при acceptance и не делает fresh fallback после terminal refusal; Undo использует
тот же PasteService/queue; translator не показывает success после drift и сохраняет
1s grace/CAS restore при cancellation. Public auto/snapshot/replay фиксируют request
до Task; translator валидирует исходный clipboard snapshot до и после AX read.
Source checkpoint: hosted306/306, existing affected56/56, dead Swift (0 мёртвых),
release `-j 1` и повторный audit-all после всех source changes PASS.
Python source/settings47/47 — прежний receipt, backend не менялся. Независимый
Astra Ultra whole-diff gate текущих review-фиксов pending. Это не release/runtime
acceptance и не доказательство устранения всех исторических AppHang. Живые клавиши,
микрофон, пользовательский clipboard, модели, настройки и lifecycle не менялись.

## 2026-10-18 — STT residency: source checkpoint, runtime не менялся

В изолированной ветке от `19124f51` реализованы truthful GigaAM unload bool и
nonwarming snapshot собственного STT owner: backend IPC `get_stt_residency`,
REST — metadata в существующем auth-gated `/v1/readiness`. Busy/неподтверждённый
cleanup не превращаются в успешную выгрузку; MLX manual unload отклоняется до
close без idle proof. Whisper наблюдает оба retained owner независимо от
transport-флага; произвольные raw model IDs/пути скрыты. Configured model отделена от
observed; live Whisper child без model metadata остаётся unknown. Снимок не
обещает call readiness/latency и не доказывает освобождение backend Metal.
Source tests используют fake sessions/OS leaves; модели, конфигурация,
процессы и call-ready policy не менялись. Linux3.12 CI и независимый review
ещё pending. [Owning card](superpowers/plans/2026-10-18-stt-residency.md).


## 2026-10-16 — Finalizing idle: локально318 PASS, audit/CI pending

В отдельной ветке на merged2098 `8dc5e48d` подготовлен фикс lifecycle-пробы:
`get_recording_state` теперь включает finalizing_count/is_finalizing под тем
же lock, что active owner/token. Оба shell-gate требуют явного доказательства
пустого tail; старый backend без полей получает UNKNOWN/HOLD.
Actual stop + barrier phase C: **RED2** по отсутствующим полям. Настоящие
private Unix IPC + оба shell/parser gate: **RED32 / 2 controls PASS**.
GREEN:318 PASS в12 последовательных файлах (174.31s), включая оба новых
регрессионных файла, прежние shell/IPC gates, recording lifecycle и privacy.
Own children reaped, timeout/group leaks нет. Preliminary independent review:
новых production P0–P2 не найдено; финальный frozen review/CI ещё pending.
`make audit-all` дошёл до последнего scanner, но bounded wrapper завершился
с PermissionError на post-wait process-group probe; итогового receipt/PASS нет.
Повтор полного аудита требует отдельного локального окна. Чужие процессы
не затрагивались; сейчас только source/RO во время VG CI.

Audio ducking [#2098](https://github.com/Pavua/Krab-Ear/pull/2098) MERGED в
`8dc5e48d88b9357e4782ae87ea5686c936618530`:27 exact-head checks SUCCESS,
оба Ubuntu1110files/16chunks/Aggregate0/noFallback/noIsofail. Source и artifact
Astra Ultra PASS; postmerge38845989548/38845989582 pending. **Не выкачено.**
Release-idle gate заблокирован отсутствием авторитетного finalizing signal
у уже работающего e996; новое source API не доказывает старый runtime.

Native acoustic one-shot05:23–05:25CEST: UI start/stop, mic RMS0.01579,
afplay exit0/reaped, история23279→23280. TextEdit остался пустым: Cua Raise/AX
не активировал его на уровне OS, captured target был Telegram; agent log
отметил paste-success туда. Send-команд не было. RO-проверка compose через
Cua не дала читаемого поля: draft state UNKNOWN, ничего не очищалось.
Приглушение восстановлено **true/15 в UI и IPC**, mic закрыт, own guard reaped.
Native TextEdit/autopaste acceptance **FAIL**, повтор не выполняется до
достоверной квалификации foreground. Причинность старых AppHang/504 UNKNOWN.
[Карточка](superpowers/plans/2026-10-16-finalizing-idle.md).

Отдельный свежий REST-инцидент05:34–05:35CEST: request
`3e674564-3b95-423c-957b-e7f257c0d109`, STT504 после29.290s при бюджете25s;
engine завершился29.31s, затем REST self-failfast exit70. Launchd поднял
REST20891 в05:35:02/runs2; backend1247 и Swift9366 прежние. Свежий RO health
IPC/REST PASS, REST20891 слушает5005, физическая запись/встреча неактивны,
история23280. Это не finalization-idle proof. Caller/profile UNKNOWN;
обработки аудио/текста и повторного запроса для диагностики не было.
Время старта запроса предшествует запуску pytest соседнего VG CI;
совпадение окон не доказывает источник. Operator lifecycle не выполнялся.


## 2026-10-16 — Audio ducking lifecycle: локальные проверки PASS, выпуск pending

На базе `be390073` реализован off-main сервис приглушения: одна serial queue,
UUID lease, общий monotonic deadline, bounded собственный child и сохранение
исходной громкости после partial mutation/failed restore. `start_recording`
ждёт prepare; stop/cancel до IPC не открывает recorder. Передача диктовки во
встречу сохраняет тот же lease и проверяет generation на terminal callback.

Proper actual-class RED3 (retry, partial mutation, MainActor201ms) → GREEN.
Independent review нашёл две дополнительные P2: provisional meeting finish
перед потерянным start и idle; поздний start заменял active lease. Обе получили
собственные RED → GREEN regressions. Финальный review выявил также
удержание старого lease после authoritative idle: two-toggle RED3 → GREEN;
задержанный idle до появления новой G1 не освобождает новый lease. Последний локальный Swift targeted-прогон:
**139 PASS**, включая29 новых actual service/owned-child/delegate tests.
Полный snapshot теперь отличает physical capture idle от terminal generation idle;
три stop/recovery regressions RED11 → GREEN и early-meeting negative control PASS.
Independent Astra Ultra whole-diff review: **PASS**, seal9/9 MATCH, новых P0–P2 нет.
Дополнительно216 hosted-filter Swift PASS,22 dependent Python PASS, audit14 PASS.
Release build `swift build -c release -j2` PASS (55s).
Exact-head/post-merge CI и выкладка ещё не завершены. [Карточка](superpowers/plans/2026-10-16-audio-ducking-lifecycle.md).

TTS admission [#2097](https://github.com/Pavua/Krab-Ear/pull/2097) MERGED в
`be3900730aaad64974ec3b70de4b9b90fc511d5c`:27 exact-head checks SUCCESS/SKIPPED.
Оба post-merge workflow точного `be390073` SUCCESS (38839317736/38839317743).
TTS admission отдельно в production не устанавливался.

Акустическая проба около04:00CEST: два Whisper HTTP200, физический микрофон8s,
ненулевой сигнал; similarity0 — **FAIL**. Настройки Ear не менялись. История,
parent generations и idle после пробы сохранены. Native microphone/autopaste
acceptance остаётся **INCOMPLETE**; причина несовпадения текста неизвестна.
Текущий установленный релиз остаётся `e996f30e`, без lifecycle изменений здесь.
Причинность исторических AppHang/504 не установлена.


## 2026-10-16 — TTS admission: source-проверка, выпуск ещё не выполнен

После установленного `e996f30e` обнаружены36 TTS HTTP200 за03:02–03:03CEST
с latency30.7–61.7s и timeout предупреждениями `say`. В том же окне был STT504;
кто создал TTS burst и связан ли он со STT/AppHang, пока UNKNOWN.

Узкий фикс в `codex/ear-tts-admission-20261016`: один активный синтез на
Python-процесс через общий IPC/REST handler; busy/unavailable → inner ошибка,
HTTP503; nonzero `say` не запускает converter, nonzero converter не отдаёт
частичный WAV. Оба отключённых TTS-флага сохраняют прежний success none.
Это ограничение процесса, а не общий lock Backend+REST на Mac.

Локально: RED6+2 → GREEN11 новых тестов, включая реальные HTTP и Unix IPC
listeners с синтетическим backend;52 прежних TTS +105 зависимых PASS,
audit14 PASS. Exact head CI, независимый final gate и выпуск — следующие этапы.
Local Python3.12 parity не подтверждён: shared harness содержит MLX, не менялся.

Физический mic/autopaste E2E **INCOMPLETE**: Cua не выводит TextEdit на передний
план (`windowNotFoundAtPosition`), предыдущая попытка захватила Chrome и дала
empty_audio. Синтетическая речь не проигрывалась; приглушение не менялось:
enabled=true, percent15; auto_paste=true, MacBook Pro Microphone. Три parent
PID/start/runs прежние. Требуется безопасно активировать тестовое поле перед
повтором; clipboard и synthetic REST smoke не заменяют эту проверку.

[Карточка](superpowers/plans/2026-10-16-tts-admission.md).

## 2026-10-16 — CURRENT: AppHang/Whisper установлен; default REST PASS

[#2094](https://github.com/Pavua/Krab-Ear/pull/2094) и
[#2095](https://github.com/Pavua/Krab-Ear/pull/2095) MERGED. Итоговый source
`e996f30efc3ef7ba9ad497d423a7095b38e05dd1`, tree `7b221acd`, соответствует
независимому whole-diff Astra Ultra PASS. У каждого reviewed head27 checks
SUCCESS/SKIPPED и три workflow SUCCESS; Whisper Ubuntu1109 файлов, Swift-ветка1107 файлов,
по два полных backend-прогона с Aggregate rc=0. Hosted Swift187 выбранных
тестов, включая шесть новых actual-callback WiringTests, PASS.

Whisper использует единый monotonic deadline на допуск, очередь сессии,
WAV и отправку; watchdog включается до stdin.write. Status button не создаёт
новый NSImage на одинаковом health-tick; переходы и восстановление временного
title сохранены. Историческая причинность AppHang/HTTP504 остаётся UNKNOWN.

Один offline synthetic RU worker E2E на новом Python-коде: PASS4.173s,
собственный child reaped, история23277 и production generations не изменились.
Это не default REST и не приёмка установленного Swift.

**Установлен `e996f30e`:** оба exact post-merge workflow SUCCESS,
[CI](https://github.com/Pavua/Krab-Ear/actions/runs/38820208308) и
[backend](https://github.com/Pavua/Krab-Ear/actions/runs/38820208434):21 SUCCESS,
1109files/16chunks/Aggregate0. После fresh Main/VG quiet и двух полных idle
окон выполнены Swift-first drain, coherent atomic app/plist replacement,
after verify и свежие GigaAM/engine/passive startup markers.
Backend **1247**, REST **1838**, Swift **9366**, `runs=1`; loaded Python roots
ведут в clean/detached/locked release `e996f30e`. UUID native
`8F0C7FE2-5700-3ECE-8708-DB8438082B4D`, прежняя подпись/AX trusted=true.

23:41:22UTC15.10 (01:41:22CEST16.10): один ephemeral **default REST** synthetic
ES запрос: **HTTP200, engine mlx-whisper; весь probe с проверками0.916s**, непустой текст boolean,
empty history_id, история23277 и все parent generations неизменны.
`request_profile` не задан; профиль пользователя не менялся. Native AX+скриншот
получены без записи/вставки. Quiet освобождён; 15min /16 снимков PASS:900.293s, без смены parent generations и потери IPC/REST.
Старые app/plists/release сохранены для rollback. Canonical non-app WIP и
native/runtime сохранены; OrbStack/VPN, соседние lifecycle и реальные
crypto/history activation не выполнялись.

[Receipt и границы](superpowers/handoffs/2026-10-16-apphang-whisper-release.md).
Sol6.1High — runtime/CI/docs; AstraUltra — узкий final/release gate.
Ниже — исторические checkpoints.

## Архив

Checkpoint'ы 2026-09-11 … 2026-10-14 (релизы, приёмки, cutover, A5.3/A5.4, RMW, key safety) вынесены дословно в
[`docs/archive/NOW-history-2026-09-10.md`](archive/NOW-history-2026-09-10.md). Ниже — всё ещё действующие разделы.

## База и runtime snapshot

- Репозиторий: [Pavua/Krab-Ear](https://github.com/Pavua/Krab-Ear)
- Прод-колея: **`origin/codex/krab-ear-v2`** @ `4bf1c4e9` (26.09: whisper #2054,
  polish+P3 #2047, глоссарий #2046, A5.2a #2052, CI-фикс #2053)
- **Прод-код (Python-бэкенд):** `bc09490f` — A5.2a и всё UI-мержи **source-only**,
  в прод не задеплоены; `history_encryption_enabled` остаётся **OFF**
- Агент (Swift) пересобран 26.09 (`make sign`) под голосовой ввод во время
  входящего скрининга; pid меняется, смотреть `pgrep -fl KrabEarAgent`
- Worktree: `git worktree add .worktrees/<slug> -b feat/<slug> origin/codex/krab-ear-v2`
- Main Krab Q2 (:8080 purpose slots, RIS/SergeyRG) — **не Ear**: [`ANTIGRAVITY_HANDOFF/2026-09-05-krab-8080-model-routing.md`](../ANTIGRAVITY_HANDOFF/2026-09-05-krab-8080-model-routing.md)

## CI-изоляция — cutover СДЕЛАН (read-back 2026-09-26)

- **API read-back:** в публичном `Pavua/Krab-Ear` — **0 self-hosted раннеров**;
  PR/push CI идёт на GitHub-hosted `macos-latest` (disposable) и не трогает личный Mac.
- Единственный self-hosted раннер — `krab-ear-m4max-private` в приватном
  `Pavua/Krab-CI-Control`: **только** MLX/Metal-гейт по exact SHA доверенной колеи
  и только когда владелец не под бенчмарками. Hosted macOS не заменяет MLX-проверку:
  VM не даёт эквивалентного Metal-пути; красный private MLX gate — отдельный
  сигнал расследования, не подмена PR CI.
- Прежний текст «раннер `krab-ear-m4max` ещё зарегистрирован в public repo» устарел.


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

### A5 — исторический checkpoint 30.09 (не текущая очередь)

- **Source `origin/codex/krab-ear-v2` @ `efecb801` сверён 30.09.** A5.2a DONE (#2052): fail-closed гейты plaintext legacy-sinks при `history_encryption_enabled=ON`, re-check под `history.lock`; adversarial-ревью A5.2a закрыло 2 CRITICAL (TOCTOU) и 1 MAJOR.
- **A5.2b DONE в source:** b1 #2056/4971c24a (encrypted snapshot), b2 #2058/96554b75 (restore/recovery + union tombstones/purged), b3 #2060/7afa21da (diskguard+retention); далее integrity #2066/#2068/#2069 accepted.
- **Runtime — исторический снапшот 1ebd12eb #2072 (17:47), encryption OFF:** активации и live encrypted E2E-доказательства нет; релиз уже отражён вверху NOW, blanket-GO не даётся.
- **Scoped inventory 30.09:** ограниченная локальная инвентаризация завершена / review PASS; полное покрытие INCOMPLETE, содержимое UNKNOWN, сырой отчёт приватен и не публикуется.
- **Долг код-видим:** `A5_2B_CALLER_SUCCESS_LOG_DEBT` активен (`service.py` логирует migration-complete при `MigrationResult.reason=history_encryption_operation_unavailable`); у PolicyReader callback `push_error` есть, но manager-вызовы его не пробрасывают — отдельным долгом, fixed не заявляется.
- **Спека:** [A5 history-at-rest](superpowers/specs/2026-09-24-a5-history-at-rest-design.md); [handoff 25.09](superpowers/handoffs/2026-09-25-a5-lifecycle-start.md) — исторический порядок работ, A5.2b1–b3 уже приняты.
- **На дату 30.09 дальше планировались** logging и A5.3 по spec §7. К 08.10 A5.3 source завершён (7/7 PR); актуальная очередь — A5.4 release/acceptance в начале NOW. Утверждения этого checkpoint о долгах и нереализованных контрактах не использовать как текущую проверку кода.

### Инцидент Glovo 2026-09-26 — закрыт (whisper)

- Ребёнок позвонил на DID, скрининг слушал, а подсказать агенту «на лету» было
  нечем. Закрыто двумя тонкими клиентами к одному бэкенд-эндпоинту VG
  `POST /v1/sessions/{id}/agent/whisper`:
  - **Вариант C (сделан, #2054):** поле «Шепнуть скринеру…» в нативном Call Observer,
    видно только на `meta.screening`-сессиях, one-in-flight, 404/503 → «Скринер
    недоступен» (текст сохраняется), успех → тихий чек + haptic. Агент собран и запущен.
  - **Вариант B (DRAFT, ждёт owner-review):** TG-бот «Krab Call Control» —
    спека `Krab Voice Gateway/docs/superpowers/specs/2026-09-26-call-control-tg-bot-design.md`.
    Правки — в VG-репо отдельной сессией (граница экосистемы).
- Известные нюансы для владельца: лимита длины подсказки нет ни в клиенте, ни в VG
  (длинный текст уходит в промпт дословно — может разогнать стоимость хода);
  `whisper_pending` в ответе 200 клиент не показывает.

C2/C3/C5 закрыты в колее 07–08.09 (интеграция #2000, схема #2003, сверено 14.09):
Telnyx вырезан из CD-секции (осталась payload-совместимость `Models.swift` + тесты),
Whisper ждёт по attempt-deadline, валидатор отвергает неверные типы brain/cloud-строк,
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
