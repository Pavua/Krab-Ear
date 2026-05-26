<!--
Расширенная дорожная карта Krab Ear Native.
Единый долгосрочный план для человека и любых LLM-моделей.
-->

# Roadmap: Krab Ear Native (Expanded)

Дата версии: 2026-02-12  
Статус: Active

## 0. Progress Snapshot (2026-02-12)

Выполнено в текущих автономных итерациях:

1. S03 (частично): настраиваемое ducking системного звука `0..100%`.
2. S04 (частично): усилены recovery-инструменты через быстрые действия панели.
3. S09 (частично): UI перевода расширен, включая стиль/глоссарий.
4. S14 (частично): фильтры истории по `translation_status` и диапазону дат.
5. S15 (частично): импорт/экспорт NDJSON с защитой от дублей.
6. S27/S28/S29 (частично): кэш/глоссарий/стили перевода в рабочем контуре.
7. S61 (частично): preset `Live Translation` в panel + menu bar.
8. S62 (частично): адаптивный realtime-перевод с backoff и guard от лишних запросов.
9. S65 (частично): drag-and-drop очередь импорта с отменой и отчётом.
10. S22 (частично): переразметка панели для узких экранов + регулировка прозрачности оверлея.
11. S11 (частично): усилена очистка хвостовых артефактов (tripled-tail + known hallucination tail).
12. S14 (частично): статусы перевода расширены (`model_unavailable_*`, `translate_error`).
13. S15 (частично): добавлены статистика истории и детальная компактация (`reclaimed_bytes`).
14. S63 (частично): добавлен режим перевода `bilingual_ru_es` в backend, menu и panel.
15. S65 (частично): предпросмотр импорта расширен (`by_ext`, `total_bytes`) + отчёт по форматам.
16. S22 (частично): добавлены `history_page_size` (25/50/100/200) и кнопка `Загрузить всё`.
17. S09/S22 (частично): быстрые пресеты фильтров истории (`Сегодня`, `7 дней`, `Ошибки перевода`, `Сброс дат`).
18. S63 (частично): `Call Assist` получил `call_auto_summary` и автосохранение summary звонка в историю.
19. S02/S04 (частично): `update_agent.command` переведён в неблокирующий режим с проверкой фактического старта агента.
20. S12 (частично): добавлено действие `Вставить plain (1 строка)` для последнего результата.
21. S14 (частично): ускорен поиск по истории через индекс последних N записей с корректным fallback.
22. S19 (частично): в `update_agent.command` добавлены preflight-проверки перед обновлением.
23. S21 (частично): добавлен автоматический `Run Release Checklist.command` с fail-fast отчётом.
24. S37 (частично): очередь batch-импорта получила `pause/resume` без потери состояния.
25. S23 (частично): onboarding получил диагностический шаг с тест-кнопками проверки прав.
26. S24 (частично): добавлен `Run Daily Driver Validation.command` с residual-risks отчётом.
27. S35 (частично): добавлены профили поведения hotkey `Default/Meeting/Translation`.
28. S49/S50 (частично): hour-runner получил checkpoints и safe stop-condition по fail-streak.
29. S51 (частично): добавлен автоматический sprint prioritizer report.
30. S52 (частично): добавлен self-update report для roadmap snapshot.
31. S53 (частично): добавлен regression radar по agent/backend логам.
32. S17 (частично): добавлен локальный IPC `summarize_text` и summary выбранной записи истории.
33. S18 (частично): quick-action шаблоны RU/ES follow-up для последнего результата.
34. S20 (частично): добавлены backup validation и restore preview скрипты.
35. S42 (частично): update-channel `stable/beta` вынесен в settings/UI/update flow.
36. S43 (частично): добавлен GitHub CI workflow (`.github/workflows/krabear-ci.yml`).
37. S44 (частично): добавлены публичные документы `docs/API.md` и `docs/RUNBOOK.md` (оба архивированы в `docs/archive/2026-05-26-pre-marathon/`).
38. S54 (частично): добавлен локальный UX telemetry report.
39. S55 (частично): добавлена проверка performance budget по telemetry.
40. S56 (частично): улучшены keyboard shortcuts для ключевых menu-действий.
41. S22/S56 (частично): добавлен persistent режим `Фокус истории` для разгрузки экрана и увеличения таблицы.
42. S22 (частично): добавлен режим плотности строк истории `Normal/Compact` + быстрая навигация к последней записи.
43. S15/S22 (частично): добавлены обзорные метрики истории (`get_history_overview`) и вывод среза качества в панели.
44. S04/S54 (частично): добавлен one-click `Run History Health.command` и markdown health-отчёт истории.
45. S04/S44 (частично): внедрено разделение зон Codex/Antigravity + boundary-check с отчётами.
46. S22 (частично): во вкладке `Диктовка` добавлен блок `Последние транскрибации` с быстрым переходом в `Историю`.
47. S45/S46 (частично): добавлена сквозная оценка стоимости `telephony + AI` (Gateway API, Krab Ear UI/IPC, OpenClaw `!callcost`).
48. S58/S64 (частично): расширен iPhone контур (Gateway mobile API + iOS WS-субтитры + PushKit/CallKit skeleton + device/session snapshot).

## 1. Product Strategy

Krab Ear развивается как локальный voice-first ассистент для macOS с фокусом на три цели:

1. Безотказный core-сценарий диктовки.
2. Перевод в один шаг (в первую очередь RU <-> ES, затем EN -> RU).
3. Удобный ежедневный UX без терминала и без ручного восстановления после сбоев.

Нельзя ломать инвариант:
`hotkey start -> hotkey stop -> stt -> paste -> history fallback`

## 2. Planning Principles

1. Любой спринт обязан заканчиваться рабочим состоянием core-сценария.
2. Любая фича проходит через release-gate: `build + unit + smoke + soak`.
3. Любая новая настройка должна быть доступна из GUI.
4. Любая новая IPC-команда документируется в `ARCHITECTURE.md`.
5. Любая рискованная фича вводится под feature-flag.

## 3. Program Tracks

### Track A: Reliability Core

Цель: ноль критических регрессий и предсказуемые recover-сценарии.

### Track B: Translation Core (RU <-> ES first)

Цель: режимы перевода и вставки без потери скорости диктовки.

### Track C: Realtime and UX

Цель: визуальный комфорт, прозрачный статус, минимум ручных действий.

### Track D: Automation and Reporting

Цель: длинные автономные циклы с понятными отчётами и чёткими next steps.

### Track E: Distribution and Ops

Цель: стабильный запуск, обновления без повторной выдачи прав, backup/release дисциплина.

## 4. Long Sprint Queue

Ниже очередь спринтов минимум на несколько десятков автономных сессий.
Каждый спринт рассчитан на 1-3 автономных запуска.

### Phase 1. Core Hardening

#### S01. Paste Determinism

Цель:

- исключить дубли вставки и дрейф выделения.

Deliverables:

- единый канал вставки;
- пост-верификация состояния курсора;
- лог причин `paste_failed`.

Acceptance:

- нет повторной вставки одного текста в 300+ циклах;
- нет залипающего выделения после вставки.

#### S02. Permissions Stability

Цель:

- минимизировать повторные запросы Accessibility/Input Monitoring.

Deliverables:

- стабильный runtime-binary lifecycle;
- документация по repair-flow;
- диагностика “почему прав нет”.

Acceptance:

- обычный `Start` не требует перевыдачи прав;
- ошибки прав диагностируются из логов без догадок.

#### S03. Audio Capture Hygiene

Цель:

- убрать системные звуки из диктовки и шум в начале/конце записи.

Deliverables:

- ранний ducking перед стартом;
- аккуратный restore после stop;
- дополнительный pre-roll filter.

Acceptance:

- в начале записи нет слышимого хвоста системного аудио;
- restore не ломает пользовательский volume.

#### S04. Recovery Paths

Цель:

- усилить fallback при нестандартных сбоях backend/IPC.

Deliverables:

- fallback цепочки с приоритетами;
- понятные пользовательские уведомления;
- отчётливые причины ошибок в `agent.log`.

Acceptance:

- при любой ошибке вставки текст остаётся в истории и буфере;
- нет silent-failure.

### Phase 2. Translation v1 (Priority: RU <-> ES)

#### S05. Translation Architecture

Цель:

- утвердить контракт режима перевода в settings и IPC.

Deliverables:

- `translation_mode`: `off | ru_to_es | es_to_ru | en_to_ru | auto | bilingual_ru_es`;
- `translate_and_paste`: `true/false`;
- новый IPC-метод перевода финального текста.

Acceptance:

- режимы читаются/сохраняются из UI и backend согласованно.

#### S06. Translation Engine Adapter

Цель:

- абстракция движка перевода с offline-first политикой.

Deliverables:

- `Translator` интерфейс;
- локальный движок по умолчанию;
- online-адаптер только по opt-in.

Acceptance:

- перевод не уходит в сеть в offline-default.

#### S07. RU <-> ES Pipeline

Цель:

- рабочий быстрый перевод RU -> ES и ES -> RU в одном потоке диктовки.

Deliverables:

- постпроцессор языка исходника;
- профиль пунктуации/регистра после перевода;
- отдельная запись истории “оригинал + перевод”.

Acceptance:

- пользователь получает корректный перевод и вставку за один цикл.

#### S08. EN -> RU Support

Цель:

- добавить третий приоритетный маршрут перевода.

Deliverables:

- режим `en_to_ru`;
- быстрый переключатель из панели.

Acceptance:

- EN -> RU стабильно работает без деградации RU <-> ES.

#### S09. Translation UI/UX

Цель:

- сделать перевод понятным без чтения документации.

Deliverables:

- селектор режима перевода;
- бейдж активного режима в статус-меню;
- подсказки по горячим сценариям.

Acceptance:

- пользователь с нуля включает перевод за <30 секунд.

### Phase 3. Realtime and Editing Quality

#### S10. Realtime Quality Gate

Цель:

- убрать подвисания и ложные “обрывы” realtime текста.

Deliverables:

- adaptive polling;
- контроль длины preview;
- деградация до lightweight режима при нагрузке.

Acceptance:

- realtime не зависает в 15-минутной диктовке.

#### S11. Tail Hallucination Guard

Цель:

- снизить галлюцинации/повторы в конце транскрибации.

Deliverables:

- multi-pass cleanup `soft/strict/auto`;
- эвристика финальной фразы;
- отчёт “сколько удалено”.

Acceptance:

- снижение жалоб на “дописал лишнее” в реальных сценариях.

#### S12. Quick Actions on Last Result

Цель:

- ускорить рутинные исправления после вставки.

Deliverables:

- `вставить как plain text`;
- `скопировать последний результат`;
- `вставить перевод вместо оригинала`.

Acceptance:

- операции доступны за 1-2 клика.

### Phase 4. History Power Mode

#### S13. History Data Model v2

Цель:

- добавить поля для перевода и метаданных качества.

Deliverables:

- расширенный NDJSON формат;
- миграция без потери старых записей.

Acceptance:

- старые записи читаются, новые пишутся в расширенном формате.

#### S14. Advanced Search

Цель:

- поиск по оригиналу, переводу, статусу, диапазону дат.

Deliverables:

- фильтры в UI;
- ускоренный индекс последних N записей.

Acceptance:

- поиск по 10k+ записей без заметных фризов.

#### S15. Export/Import

Цель:

- удобный вынос истории в файл и обратный импорт.

Deliverables:

- экспорт в `.txt` и `.ndjson`;
- выбор диапазона/фильтров;
- безопасный импорт без дублей.

Acceptance:

- пользователь может восстановить/перенести историю без скриптов.

### Phase 5. Workflow Expansion

#### S16. File Drop Zone

Цель:

- drag-and-drop аудио в UI для пакетной обработки.

Deliverables:

- drop zone в панели;
- очередь задач и прогресс;
- отчёт по ошибкам по файлам.

Acceptance:

- папка/набор файлов обрабатываются без CLI.

#### S17. Summarization Mode

Цель:

- краткие summary для длинных аудио/диктовок.

Deliverables:

- `summary_short` и `summary_detailed`;
- сохранение summary в истории рядом с исходником.

Acceptance:

- summary генерируется без блокировки основного потока.

#### S18. Templates and Macros

Цель:

- шаблоны сообщений поверх диктовки/перевода.

Deliverables:

- пользовательские шаблоны;
- переменные времени/имени/языка;
- команда “применить и вставить”.

Acceptance:

- шаблоны ускоряют рутину и не ломают авто-вставку.

### Phase 6. Ops and Release Discipline

#### S19. Stable Update Channel

Цель:

- управляемый update-процесс без ручного cleanup прав.

Deliverables:

- отдельный update flow;
- pre-flight checks до перезапуска.

Acceptance:

- обновление не требует повторных ручных repair-действий в типовом случае.

#### S20. Backup and Restore UX

Цель:

- backup в один клик с проверкой целостности.

Deliverables:

- валидация backup;
- restore preview;
- журнал стабильных снимков.

Acceptance:

- rollback до стабильной версии выполняется без терминала.

#### S21. Release Checklist Automation

Цель:

- стандартизировать релиз перед backup/github.

Deliverables:

- автоматический checklist report;
- fail-fast при красных тестах.

Acceptance:

- каждый релиз имеет формализованный отчёт.

### Phase 7. Product Polish

#### S22. Visual Polish

Цель:

- сделать UI более чистым и предсказуемым.

Deliverables:

- типографика и spacing;
- адаптивные размеры;
- понятные статусы режима.

Acceptance:

- длинные тексты и малые экраны читаются без наложений.

#### S23. Onboarding 2.0

Цель:

- сократить трение первого запуска.

Deliverables:

- мастер объяснения прав;
- тест-кнопки “проверить вставку/проверить микрофон”.

Acceptance:

- первый рабочий диктовочный цикл без ручного дебага.

#### S24. “Daily Driver” Validation

Цель:

- провести финальный прогон на повседневные сценарии.

Deliverables:

- чек-лист реального использования;
- финальный список открытых рисков.

Acceptance:

- продукт готов к длительному ежедневному использованию.

## 5. Backlog Extension (Post-S24)

Если очередь до S24 закрыта, агент не останавливается сразу.
Действует правило расширения backlog:

1. Сначала берутся технические долги и стабилизация.
2. Затем берутся UX-полировки с низким риском.
3. Затем формируется новый блок `S25+` в конце этого файла.

Ограничение:
- high-risk изменения (архитектурные миграции, смена движка STT/translation)
  добавляются в roadmap как `PROPOSAL`, не внедряются молча.

## 6. 60-Minute Autonomous Session Contract

Один час автономной работы делится на 4 этапа:

1. `10-15 мин`: чтение контекста и выбор ближайшего незавершённого спринта.
2. `25-30 мин`: реализация и локальные тесты.
3. `10-15 мин`: build + smoke + минимальный soak.
4. `5-10 мин`: отчёт и обновление roadmap-статуса.

Минимальный отчёт по завершению часа:

1. Что сделано (по файлам).
2. Что проверено (команды и результаты).
3. Что осталось в спринте.
4. Какие решения требуют подтверждения пользователя.

## 7. Exit Criteria for “Stable Milestone”

1. `paste_success_rate` стабилен в долгих прогонах.
2. `crash_count_per_1000_sessions = 0`.
3. RU <-> ES перевод работает в штатном потоке диктовки.
4. Нет обязательных ручных repair-действий при обычном запуске.
5. Есть валидный backup и release-отчёт в `docs/reports`.

## 8. Long-Horizon Backlog (S25-S60)

Ниже расширенный backlog для длинных автономных сессий (1-3 часа и более).
Каждый пункт рассчитан как минимум на один полноценный инженерный цикл.

### Phase 8. Translation v2 and Realtime

#### S25. Realtime Translation Overlay

Цель:

- показать перевод прямо в экранном realtime-оверлее.
Deliverables:

- второй блок текста в оверлее (оригинал + перевод);
- throttle, чтобы не перегружать CPU.
Acceptance:

- во время записи пользователь видит актуальный перевод без фризов.

#### S26. Translation Confidence & Status UX

Цель:

- сделать статус перевода прозрачным.
Deliverables:

- бейдж `ok/unavailable/error` в истории;
- понятные причины fallback на оригинал.
Acceptance:

- пользователь всегда понимает, почему вставился оригинал.

#### S27. Translation Cache

Цель:

- ускорить повторные переводы похожих фраз.
Deliverables:

- LRU-cache по `(mode, text_hash)`;
- инвалидация при смене движка/профиля.
Acceptance:

- повторные переводы заметно быстрее без потери корректности.

#### S28. Glossary / Custom Dictionary

Цель:

- пользовательские замены терминов и имён.
Deliverables:

- словарь RU/ES/EN;
- постпроцессор в STT и translation.
Acceptance:

- фиксированные термины стабильно переводятся одинаково.

#### S29. Style Modes for Translation

Цель:

- стиль перевода под контекст.
Deliverables:

- `neutral`, `chat`, `formal`;
- сохранение выбора в settings.
Acceptance:

- пользователь может менять стиль без ручной правки текста.

### Phase 9. Speaker and Meeting Features

#### S30. Diarization Architecture (Design-First)

Цель:

- утвердить техдизайн разделения говорящих.
Deliverables:

- ADR по offline/online вариантам;
- формат хранения speaker-turns.
Acceptance:

- решение принято и документировано, есть чёткий план реализации.

#### S31. Speaker Segmentation for Imported Audio (Beta)

Цель:

- первая рабочая версия разделения голосов в файлах.
Deliverables:

- offline beta-пайплайн segmentation;
- разметка `SPEAKER_1/2/...` в результате.
Acceptance:

- длинный диалог в файле разбивается на говорящих с приемлемой точностью.

#### S32. Meeting Transcript View

Цель:

- удобный просмотр диалогов по спикерам.
Deliverables:

- grouped-view по говорящим;
- быстрый copy по отдельному speaker turn.
Acceptance:

- пользователь может копировать реплики по спикерам без ручного парсинга.

#### S33. Call/Meeting Summary

Цель:

- автосводка ключевых решений и задач.
Deliverables:

- шаблон summary для встреч;
- секции `решения`, `задачи`, `follow-up`.
Acceptance:

- после импорта звонка доступен готовый конспект.

### Phase 10. Productivity and Automation

#### S34. Clipboard Safety Modes

Цель:

- гибкое управление буфером обмена.
Deliverables:

- режимы `always_copy`, `copy_on_fail`, `never_copy`;
- предупреждение о риске потери текста.
Acceptance:

- поведение буфера полностью предсказуемо и настраиваемо.

#### S35. Hotkey Profiles

Цель:

- несколько hotkey-профилей под разные сценарии.
Deliverables:

- `default`, `meeting`, `translation`;
- безопасная проверка конфликтов.
Acceptance:

- пользователь переключает профиль за 1 клик.

#### S36. Macro Actions

Цель:

- post-actions после транскрибации.
Deliverables:

- действия: copy, paste, translate, summarize, notify;
- pipeline-конфиг в settings.
Acceptance:

- рутинные задачи выполняются автоматически по сценарию.

#### S37. Queue for Batch Jobs

Цель:

- устойчивость массовых задач.
Deliverables:

- очередь импорта/транскрибации;
- pause/resume/cancel.
Acceptance:

- большой пакет файлов обрабатывается контролируемо.

### Phase 11. Quality Engineering

#### S38. Golden Dataset for STT

Цель:

- регрессионный контроль качества транскрибации.
Deliverables:

- набор эталонных аудио и ожидаемых текстов;
- автоматический quality-report.
Acceptance:

- изменения в STT не проходят без сравнения метрик.

#### S39. Golden Dataset for Translation RU<->ES

Цель:

- стабильность ключевого переводческого маршрута.
Deliverables:

- эталонные пары фраз;
- smoke-мониторинг качества переводов.
Acceptance:

- регрессии перевода ловятся автоматически.

#### S40. Long Soak 10k Sessions

Цель:

- доказать стабильность длительных прогонов.
Deliverables:

- soak-runner 10k циклов;
- отчёт по crash/timeout/paste_rate.
Acceptance:

- критические метрики в зелёной зоне.

### Phase 12. Distribution and Ecosystem

#### S41. Signed App Bundle (Optional)

Цель:

- подготовка к более нативной дистрибуции.
Deliverables:

- app bundle pipeline;
- notes по signing/notarization.
Acceptance:

- сборка может запускаться как полноценное приложение.

#### S42. Update Channel UX

Цель:

- безопасный update без ручного копания.
Deliverables:

- канал `stable/beta`;
- pre-update backup и rollback.
Acceptance:

- обновление в один клик с возможностью отката.

#### S43. GitHub CI Profile

Цель:

- базовая CI-автоматизация проверок.
Deliverables:

- workflow: tests + smoke + report artifact;
- ветка релизной готовности.
Acceptance:

- pull request не проходит без зелёных проверок.

#### S44. Public Docs Pack

Цель:

- документация для других моделей и разработчиков.
Deliverables:

- `PRD`, `ARCHITECTURE`, `API` (archived), `RUNBOOK` (archived);
- единые термины и changelog.
Acceptance:

- любой инженер может продолжить проект без устных пояснений.

### Phase 13. Experimental Track (Feature Flags)

#### S45. Realtime System Audio Translation (Research)

Цель:

- исследовать законный и стабильный захват системного аудио.
Deliverables:

- техдок по ограничениям macOS;
- прототип под feature-flag.
Acceptance:

- принято решение: в прод или в archive.

#### S46. Phone/Call Bridge Integrations (Research)

Цель:

- изучить варианты интеграции с звонками/мессенджерами.
Deliverables:

- матрица интеграций и ограничений;
- PoC для 1-2 источников.
Acceptance:

- есть реалистичный план или формальный отказ по рискам.

#### S47. Speaker Embeddings and Profiles

Цель:

- персональные профили говорящих.
Deliverables:

- хранение эмбеддингов (локально);
- привязка speaker label к имени.
Acceptance:

- повторяющиеся собеседники определяются стабильнее.

#### S48. Real-Time Bilingual Mode

Цель:

- live режим “слушай на RU, показывай ES” и обратно.
Deliverables:

- duplex-интерфейс;
- режим минимальной задержки.
Acceptance:

- двуязычный realtime сценарий работает устойчиво.

### Phase 14. Autonomy Protocol

#### S49. Autonomous Hour Runner

Цель:

- стандартизировать 60-минутные автономные сессии.
Deliverables:

- шаблон “часового цикла”;
- auto-report в `docs/reports/autonomous_*.md`.
Acceptance:

- каждая длинная сессия имеет формальный отчёт.

#### S50. Autonomous Multi-Hour Mode

Цель:

- поддержка 2-4 часовых пакетов без потери контроля.
Deliverables:

- контроль checkpoints каждые N спринтов;
- безопасные stop conditions.
Acceptance:

- можно запускать длинный пакет улучшений управляемо.

#### S51. Sprint Scoring and Prioritizer

Цель:

- автоматически выбирать наиболее полезные спринты.
Deliverables:

- score по impact/risk/effort;
- полуавтоматическое формирование очереди.
Acceptance:

- автономные циклы дают измеримый прогресс.

#### S52. Roadmap Self-Update

Цель:

- roadmap обновляется по фактическим результатам.
Deliverables:

- автообновление статусов `planned/in_progress/done`;
- журнал причин переноса.
Acceptance:

- roadmap остаётся актуальным без ручной синхронизации.

#### S53. Regression Radar

Цель:

- быстрый сигнал о повторяющихся багах.
Deliverables:

- сбор повторяющихся ошибок из логов;
- weekly regression report.
Acceptance:

- повторные баги быстрее находят приоритетное исправление.

#### S54. UX Telemetry (Local-only)

Цель:

- локальная аналитика UX без отправки данных.
Deliverables:

- метрики latency, paste_success, fallback_rate;
- локальный dashboard.
Acceptance:

- решения о приоритетах основаны на реальных метриках.

#### S55. Performance Budget

Цель:

- ограничить рост задержек и нагрузки.
Deliverables:

- budgets для CPU/RAM/latency;
- алерты при выходе за пределы.
Acceptance:

- новые фичи не деградируют core-поток.

#### S56. Accessibility and Keyboard UX

Цель:

- максимально keyboard-first и доступный интерфейс.
Deliverables:

- горячие клавиши панели;
- улучшения для VoiceOver/large text.
Acceptance:

- основные действия выполняются без мыши.

#### S57. Security Review Pass

Цель:

- проверить локальную безопасность и права доступа.
Deliverables:

- threat model;
- hardening рекомендаций и фиксы.
Acceptance:

- высокий уровень рисков закрыт или документирован.

#### S58. Stable Milestone Candidate

Цель:

- собрать “почти релизный” срез.
Deliverables:

- freeze-ветка;
- полный regression pack.
Acceptance:

- кандидат готов к ежедневной эксплуатации без заметных проблем.

#### S59. Release Candidate

Цель:

- финальная предрелизная стабилизация.
Deliverables:

- RC build;
- known-issues list.
Acceptance:

- критических блокеров нет.

#### S60. Stable Release

Цель:

- выпуск стабильной версии и backup.
Deliverables:

- tagged stable snapshot;
- backup + release notes + migration notes.
Acceptance:

- пользователь переходит на стабильный релиз без ручного дебага.

### Phase 15. Live Translation RU/ES Program (S61-S75)

#### S61. Live Translation UX Baseline

Цель:

- закрепить единый UX-поток для разговорного перевода в реальном времени.
Deliverables:

- отдельный preset `Live Translation`;
- быстрые переключатели режима в панели и menu bar;
- понятные статусы fallback на оригинал.
Acceptance:

- пользователь включает режим за 1 клик и понимает, что происходит.

#### S62. Realtime Translation Stability

Цель:

- устранить зависания и "обрывы" realtime при длительной диктовке.
Deliverables:

- адаптивный троттлинг переводов;
- защита от повторной отправки одинаковых фрагментов;
- метрики свежести realtime-вывода.
Acceptance:

- 20+ минут непрерывной диктовки без freeze.

#### S63. Bilingual Conversation Mode (RU<->ES)

Цель:

- удобный двуязычный режим "сказал -> увидел перевод".
Deliverables:

- режим двустороннего отображения RU/ES;
- быстрый swap направления;
- пресеты пунктуации под разговорный стиль.
Acceptance:

- сценарий "живого диалога" работает без ручных переключений.

#### S64. Inline Correction Loop

Цель:

- быстрые правки последней фразы перед вставкой.
Deliverables:

- команда "повторить перевод";
- локальные правки глоссария "на лету";
- повторный paste без нового цикла записи.
Acceptance:

- исправление последней фразы выполняется за 2-3 действия.

#### S65. Import Queue v2

Цель:

- устойчивый batch-импорт больших наборов аудио.
Deliverables:

- drag-and-drop очередь;
- прогресс, отмена, сводка ошибок;
- безопасная обработка больших папок.
Acceptance:

- пакет 100+ файлов обрабатывается без зависаний UI.
