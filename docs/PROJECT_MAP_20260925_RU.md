# Krab Ear — карта проекта и доказательств, 25.09.2026

Снимок собран 25.09.2026, 03:22–03:32 CEST. Это проверенная точка состояния,
не автоматически обновляемый монитор и не обещание готовности всей системы.
Основной scope — Krab Ear; Krab Main и Voice Gateway показаны как соседи,
их приёмка остаётся в собственных сессиях.

## 1. Где находится проект

- Проверенная удалённая ветка `origin/codex/krab-ear-v2`:
  `a0d101638e4b91d54f430df5e59bb7cf28214b92`.
- Два post-merge workflow этого SHA завершены success: [CI 36046230276](https://github.com/Pavua/Krab-Ear/actions/runs/36046230276),
  [krab-ear-ci 36046230264](https://github.com/Pavua/Krab-Ear/actions/runs/36046230264).
- Backend и REST реально запущены из release
  `bc09490f20b06e84dcd19ed9238defc844e64e20`: сверены argv процессов,
  launchd plist и HEAD release worktree. История A5.1 пока только в source.
- Swift agent запущен из `Krab Ear.app`; принадлежность его бинарника к
  конкретному source SHA этим проходом не установлена.
- Shared checkout `feat/visual-polish-pack` содержит чужие untracked-файлы.
  Этот документ готовится отдельно в `codex/ear-a5-lifecycle` от `a0d10163`.

## 2. Устройство

Основные компоненты: Swift-приложение, Python backend, отдельный REST-процесс
и inference workers. Формулировка «ровно два процесса» была бы неверной.

```text
Хоткей → Swift: панели, глобальные сочетания, Accessibility
  → Unix JSON-RPC команды start/stop
  → BackendService: 360 методов → специализированные сервисы
      → Python AudioRecorder ← микрофон (sounddevice.InputStream)
      → AudioEngine: нормализация, VAD, GigaAM / Whisper, post-processing
      → StateStore: история, журналы, настройки
      → перевод / встречи / TTS / поиск / интеграции
  → Swift: превью, итоговый текст, вставка, UI

Системное аудио → Swift ScreenCaptureKit → IPC audio ingest → STT субтитры

Backend events → EventBridge → отдельный REST → SSE consumers
Wake-word и ErrorBus → IPC polling (не изображать только через SSE)
Voice Gateway ↔ REST/STT/TTS/stream contracts
Krab Main ↔ отдельные интеграционные контракты
LM Studio ↔ локальный LLM и общий ресурсный слот экосистемы
```

## 3. Продуктовые направления

«Есть в source» означает найденный рабочий путь в коде. Это не свежая живая
проверка каждой функции и не оценка пользовательского качества.

| Направление | Что уже есть в source | Что ограничивает утверждение «готово» |
|---|---|---|
| Диктовка и вставка | Hotkey, recorder, partials, finalize, Accessibility/clipboard | Реальная запись/вставка после reboot не проверялась этим проходом |
| STT и аудио | GigaAM RU, Whisper, VAD, маршрутизация, fallback | WER/p95 baseline не принят; SIGKILL/wake causal diagnosis открыт |
| Лексика | Hotwords, фонетические замены, QuickEdit, suggestions | F1 source+deployment записаны в handoff; эффект WER ещё не измерен |
| История | NDJSON, CRUD, коллекции, архив, экспорт, версии | A5.1 в source; защищённый lifecycle и export session ещё не завершены |
| Поиск и аналитика | Обычный/нечёткий поиск, аналитика, semantic implementation | Semantic flag OFF; код не равен активированной функции |
| Перевод | RU/ES/EN, selection translation, glossary/cache | Свежий языковой live benchmark не выполнен |
| Встречи | Live transcript, summary, action items, report, speaker options | Сквозной сценарий с голосом владельца отдельно |
| Quick Capture | Заметки, история, коллекция, Notes/Obsidian | Сессионная политика plaintext для Obsidian — A5.3 |
| Субтитры | ScreenCaptureKit, IPC ingest, STT, overlay | Нет свежего сквозного испытания после reboot |
| Wake / разговор / TTS | IPC polling, conversation UI, TTS engines | Wake running, но текущая health-проверка не доказывает точность срабатываний |
| Звонки | Call Assist, Observer, session lifecycle, phone STT profile | Нужен реальный RU-звонок с доказанным Ear STT path; соседний VG scope |
| LLM / GPU | LM Studio, rewrite/summary, leases, cloud branch, locks | Rewrite/cloud/preload OFF; общий слот, enforce запрещён |
| Приватность | Privacy gates, purge, encryption codec, spill/rescue | Не весь PII scope; флаг encryption OFF до полной A5 и решения владельца |
| Наблюдение | Diagnostics, ErrorBus, supervisor, reliability dashboard | Есть свежие Sentry issues; health не доказывает отсутствие потерь |
| Дистрибуция | Wizard, bundled Python, Sparkle, CI, release paths | Закрытая бета 3–5 тестеров и onboarding ≤15 минут ещё не доказаны |

Опорные модули: `backend/service.py`, `recording_core_service.py`,
`history_service.py`, `state_store.py`, `meeting_session_service.py`,
`translation_service.py`, `live_subs_service.py`, `call_stt_service.py`,
`core/engine.py`, `core/stt_router.py`, `core/pipeline/`,
`native/KrabEarAgent/Sources/KrabEarAgent/`.

## 4. Приёмка A1–A5: разный уровень доказательств

| Карточка | Подтверждено | Открыто |
|---|---|---|
| A1 — безопасный CI cleanup | Исправление, review, merge/CI; в release | Health текущего приложения не является новым тестом cleanup |
| A2 — изоляция тестовых мостов | Исправление, review, merge/CI; в release | Долговременный live эффект и физическая диктовка отдельно |
| A3 — аудио/SIGKILL/wake | PII-free диагностика в #2048, зелёный CI, release bc09490f | Источник SIGKILL и причина stale-after-reinit не доказаны; сегодняшний REST timeout/AppHang — отдельные наблюдения |
| A4 — качество STT | Сценарий 50 эталонов: 30 RU / 12 ES / 8 EN; инструменты существуют | Голос владельца, корпус и сравнение WER/CER/p95 не представлены как принятый результат |
| A5 — история at rest | #2049 fail-closed и #2050 A5.1 journal codec; exact CI зелёный | A5.2 lifecycle, A5.3 session exports, A5.4 release/live activation |

A5.1 source/CI — 100% текущего блока. В A5 завершён 1 из 4 этапов поставки:
это 25% **по числу этапов**, а не по времени, объёму работ или готовности продукта.
Единого честного «Krab Ear готов на X%» сейчас нет: backlog открыт, веса не
согласованы, live-качество и потери диктовок не имеют принятой базы измерений.

## 5. Измеримые числа

Статический инвентарь exact `a0d10163`, без импортов, запуска тестов и ML:

| Величина | Значение | Метод |
|---|---:|---|
| Tracked paths | 2617 | git tree |
| Python файлы в KrabEar | 1344 | git tree, расширение .py |
| Backend Python | 153 | git tree |
| Core Python | 93 | git tree, включая 24 pipeline-файла |
| Python test-файлы | 1068 | KrabEar/tests/**/test*.py |
| Swift production files | 159 | Sources, расширение .swift |
| Swift test files | 127 | Tests, расширение .swift |
| IPC handlers | 360 | AST return-table `_build_dispatch_table` |
| DEFAULT_SETTINGS keys | 246 | AST `core/config.py` |
| Docs paths / scripts paths / workflows | 647 / 131 / 3 | git tree |

1195 test-файлов — сумма 1068+127, не число исполняемых тестов, не покрытие
и не pass-rate. Source `service.py` — 6356 строк, но размер не мера качества.
143 targeted Python 3.12 tests для A5.1 прошли в предыдущем блоке; это
историческое локальное доказательство A5.1, а не новый полный suite после reboot.

| Продуктовая метрика | Принятый текущий замер | Цель стратегии Q4 |
|---|---|---|
| Потери/обрезание диктовок | Недостаточно доказательств за окно | 0 четыре недели подряд |
| p95 конец речи → текст, 30-секундная речь | Не установлен в этом проходе | Не хуже baseline; затем −20% |
| WER RU / ES / EN | Нет принятого Golden Set результата | RU −15% относительно baseline |
| Нечистые смерти backend / месяц | Не пересчитано | ≤1 |
| Двусторонние контракты соседей | Не проверены полностью | 100% внешних вызовов |
| Первый запуск тестером | Не измерен | ≤15 минут, без Terminal |

Цели не являются уже достигнутыми значениями. 3 мс IPC и 9 мс REST health
ниже — сетевой/service smoke, не задержка распознавания речи.

## 6. После reboot: живой снимок

- macOS 27.2, build 26B5091g; uptime около 9 минут при первом сэмпле.
- APFS Data: 117 GiB available при `df -h`, capacity used 88%. В этом проходе
  диск не чистился; это системная отчётность, не детальный storage audit.
- RAM 36 GiB; memory_pressure free 49% в первом сэмпле. Swap 5954 MiB сначала,
  8630.69 MiB в 03:31 CEST. Load1 снизился 201.68 → 17.28 между сэмплами.
  Эти показатели нельзя превращать в доказанную причину аудиоинцидента.
- Backend PID 4609, launchd runs=1, never exited. REST PID 10490, runs=2,
  previous exit=70. Agent PID 7323. PID — только моментальный snapshot.
- IPC ping OK 3.01 мс; recording=false, meeting=false; wake running=true,
  wedged=false; diagnostics вернул 13 разделов.
- REST `http://127.0.0.1:5005/health`: HTTP 200, около 9 мс. Первичная проверка
  :5000 попала на другой endpoint и не учитывается как здоровье Ear.
- Прямые runtime settings: history encryption, cloud rewrite, LLM rewrite,
  brain preload, semantic search — false. Privacy mode также false.
- REST log 03:20:28: transcription timeout 25 секунд; 03:20:29 fail-fast exit70;
  03:21:01 запуск нового REST. Поздние строки показывают успешные STT HTTP200;
  запросы в этом проходе мы не создавали, их происхождение не атрибутировано.
- [KRAB-EAR-AGENT-B](https://po-zm.sentry.io/issues/115354972/): unresolved,
  App Hanging, lastSeen 25.09 01:17:43Z (03:17 CEST), count1.
- [KRAB-EAR-BACKEND-2V](https://po-zm.sentry.io/issues/149228094/): unresolved warning,
  lastSeen 24.09 18:01:49Z, count1. Sentry API auth работает. Backend ingress
  после текущего reboot свежим событием не доказан; «ошибок нет» не заявляется.
- Optional torchcodec/FFmpeg load warnings присутствуют. Они не доказаны
  причиной timeout. REST стартовал с NO auth на localhost; внешний доступ
  и его политика в этом проходе не исследовались.

## 7. Следующие действия и ресурсы

1. A5.2a: защитить legacy archive/version/backup/restore/schema-migration
   до любых mkdir/touch/copy/rewrite/prune. Причина отказа должна быть видна
   в IPC и auto-backup status; проверка policy и sink под одним lock.
2. A5.2b: multi-file encrypted snapshot, manifest, crash recovery, restore
   с сохранением tombstones/permanent purged IDs; только synthetic fixtures.
3. A5.2c: согласованный inventory без чтения/публикации текстов, unknown не
   превращать в «пусто». Внешние каталоги — только в согласованном scope.
4. A5.3: разрешение plaintext до конца Swift+backend сессии, manual и Quick
   Capture → Obsidian; revoke при смене epoch/privacy/policy.
5. A5.4: совместимый rollback, review/CI, безопасный release; отдельно решение
   владельца о старых файлах и live activation.
6. A3/качество: разобрать свежий AppHang/timeout по фактам; A4 — запись
   эталонов владельцем в спокойное окно. Не считать health заменой этим gates.

Astra High подходит для текущих границ хранения/архитектуры. После принятой
узкой карточки обычная реализация — Sol High, CI/доки — Sol Medium;
независимый security-review — Astra High. Тяжёлые тесты последовательно и
после нового resource snapshot. Соседние процессы не останавливать.

## 8. Источники и известный дрейф

- Exact GitHub tree `a0d101638e4b91d54f430df5e59bb7cf28214b92`.
- `.remember/CODEX_ACCEPTANCE_20260920_RU.md` — история A1–A5, включая release
  23.09. Сверять её старые SHA/PID с текущим snapshot.
- `docs/superpowers/handoffs/2026-09-24-a5-history-design.md` — свежая A5.1
  source-приёмка; `docs/superpowers/specs/2026-09-24-a5-history-at-rest-design.md`.
- `docs/design-briefs/2026-09-17-q4-2026-roadmap.md` — стратегия и цели,
  не доказательство их достижения; `docs/golden/r2-scenario.md` — 50 фраз.
- `docs/NOW.md` помечен 18.09 и содержит старый current runtime e004ba3d.
  В карте заменён свежим проверенным bc09490f, без переписывания истории.
- CLAUDE ~322 IPC и старый бриф 259 настроек не совпадают со source 360/246.
  Это дрейф документации, а не исчезновение функциональности.

Ни live recording, ни migration/Keychain probe, ни новая установка зависимостей,
ни cleanup диска, ни restart/deploy при составлении карты не выполнялись.
