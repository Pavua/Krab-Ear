# Серия M: слияние REST-процесса в backend — дизайн

Дата: 2026-07-16 · Статус: **СПЕКА** (одобрена владельцем, гейт-решение принято) ·
Автор: Fable 5. Предшественник: скетч `2026-07-07-process-merge-sketch.md` (стратегия
и риски продуманы там; эта спека уточняет его живой разведкой и фиксирует контракты).
Роадмап: `docs/ROADMAP-2026H2.md` §3.1.

## 0. Гейт-решение (зафиксировано с владельцем 2026-07-16)

| Критерий скетча | Статус | Решение |
|---|---|---|
| Мост Волны 2 прожил ≥2 недели | ❌ мост ожил в проде только 16-07 (фикс rest.plist `KRAB_EAR_DATA_DIR`, до того `dropped 1453`) | **M1 — сейчас** (моста не касается); **старт M2 — не раньше 2026-07-30** (мост должен прожить 2 недели с 16-07 без инцидентов) |
| Прото-смок make_server+flask-sock | ✅ PASS 16-07: 3 SSE без разрывов, WS-эхо 100/100, POST p95 0.320s против baseline 0.312s, shutdown 3 мс при живом SSE-клиенте | критерий закрыт |
| Окно без конкурирующего приоритета | ✅ владелец выбрал направление сам | — |

Одна спека на всю серию (архитектура едина: решения M2/S3 определяют форму фабрики
в M1); планы исполнения — отдельные на каждую волну.

## 1. Зачем (из скетча, подтверждено разведкой 16-07)

1. Конец класса 2-EventBus навсегда (мост закрывает гэп функционально, двухшинность
   остаётся источником сюрпризов — см. историю: wake word, ErrorBus, partial-SSE).
2. RAM: `rest_server.py:530-534` держит СВОИ `AudioEngine`, `StateStore`, `Transcriber`,
   `Translator`, `TTSService` — второй комплект тяжёлого состояния.
3. Межпроцессная сериализация MLX (flock) вырождается в in-process `mlx_lock`.
4. Опс: один launchd-агент, один лог, один lifecycle, один health.
5. Тест-гигиена: module-level globals — корень задокументированного класса
   chunk-pollution флейков.

### 1a. Поверхность потребителей (живая разведка 16-07, три аудита)

- **Voice Gateway** зовёт ровно три эндпойнта: `GET /health` (httpx, timeout 1s,
  кэш доступности 30с ok / 10с down), `POST /v1/stt/transcribe` (timeout 30s, без
  ретраев), `POST /v1/tts/synthesize` (timeout 15s, без ретраев, без кэша доступности).
  WS `/v1/stream` и SSE `/v1/events` VG **не использует**. URL конфигурируем
  (`KRAB_EAR_BASE_URL`/`KRAB_TTS_LOCAL_URL`, дефолт `127.0.0.1:5005`). Семантика
  401→fallback / 403→stop-without-fallback зашита в их клиентах. `preflight_call.py`
  парсит поля `status` и `profile` из `/health`.
  ⚠️ `scripts/start_gateway*.command` VG при недоступном `/health` **сами спавнят**
  `KrabEar/backend/rest_server.py` (nohup + sleep 3) — учтено в S3.
- **Swift-агент**: только HTTP/SSE на `127.0.0.1:5005` (MeetingLivePanelController,
  LiveSubtitlesOverlay, TranslationStreamView, RealtimeOverlayController+PartialSSE,
  StreamingPasteController, GlobalStatusBar, HealthMonitor). От отдельного процесса
  не зависит ничего.
- **От ОТДЕЛЬНОГО процесса зависят**: `ai.krab.ear.rest.plist.template` +
  `install_rest_launchagent.command` (запуск), `kill_dup_gigaam.command` (pgrep по
  имени файла), `run_e2e_bridge_smoke.command` (двухпроцессный e2e моста), сам
  `EventBridge` (существует ИЗ-ЗА двухпроцессности), gunicorn-путь
  (`start_rest_production.command` + `gunicorn_config.py`).
- **Тесты**: 27 релевантных файлов / ≈752 теста. Категории: A (6 файлов, патч
  конструкторов ВОКРУГ импорта), B (12 файлов, `patch.object(rs, "store", …)` на
  живой модуль), C (8 файлов, sys.modules-стабы/reload), E (1 файл, импорт функции).
  Межпроцессных тестов **ноль**. При сохранении module-level имён и патчабельности —
  **0 обязательных правок в M1**.

## 2. Стратегия: strangler в 3 волны

- **M1 (сейчас)** — настоящая app-factory; поведение бит-в-бит; ни одной правки тестов.
- **M2 (не раньше 30-07)** — in-process режим за рубильником (default off), канарейка
  ≥2 недели на владельце.
- **S3** — флип default→on, вывод rest-агента, правка скриптов, VG-бриф.

Альтернативы отвергнуты: «остаться на мосте» (пункты §1.1-1.4 не решаются),
big-bang (752 теста + прод без пути отката).

## 3. Волна M1 — настоящая app-factory

### 3.1 Ключевая развилка: патчабельность категории B

12 файлов / ≈425 тестов патчат module-глобалы (`patch.object(rest_server, "store", …)`)
и ожидают, что хендлеры это УВИДЯТ. Замкни фабрика зависимости в closure — тесты не
упадут, а молча начнут ходить в реальные объекты («test-validates-the-hole»).
Поэтому доступ к зависимостям в standalone-пути обязан оставаться чтением живых
module-атрибутов.

### 3.2 Дизайн

```python
# backend/rest_server.py (тот же файл; новый модуль НЕ создаётся — см. 3.5)

class RestDeps(Protocol):
    engine: Any          # AudioEngine
    store: Any           # StateStore
    transcriber: Any     # Transcriber
    translator: Any      # Translator
    tts_service: Any     # TTSService
    metrics: Any         # MetricsCollector (сегодня — module-синглтон backend.metrics_collector.metrics)
    event_bus: Any       # EventBus (сегодня — module-синглтон backend.event_bus.bus)
    sse_stream: Any      # callable из backend.event_bus

@dataclass
class StaticDeps:        # путь M2: собирается BackendService'ом из его коллабораторов
    engine: Any
    store: Any
    # ... все поля протокола

class _ModuleGlobalsDeps:  # standalone-путь: живое чтение module-атрибутов
    def __getattr__(self, name):  # имена полей протокола = имена module-глобалов (1:1)
        return getattr(sys.modules[__name__], name)
```

- `create_app(deps: RestDeps | None = None, config_mapping=None) -> Flask` —
  существующая **декоративная** фабрика (`rest_server.py:2122`, `return app`)
  становится настоящей: конструирует Flask app, Api, Sock, Limiter, CORS,
  blueprints, все route-хендлеры и `ws_stream`; хендлеры читают зависимости
  ТОЛЬКО через `deps.<name>`. `deps=None` → `_ModuleGlobalsDeps()` —
  сигнатурная совместимость с gunicorn-вызовом `create_app()` из
  `gunicorn_config.py` сохраняется.
- **Module-level путь не меняется**: на импорте, как сегодня, строятся standalone
  `engine`/`store`/`transcriber`/`translator`/`tts_service` (строки 530-534) и
  module-level `app = create_app()` со всеми алиасами (`ws_stream` и т.д.).
  Тесты категорий A/B/C работают без правок: A перехватывает конструкторы вокруг
  импорта (создание синглтонов при импорте осталось), B патчит module-атрибуты
  (хендлеры через `_ModuleGlobalsDeps` читают их живьём), C стабит sys.modules
  до импорта (порядок инициализации не изменился).
- `__main__`-блок, plist, EADDRINUSE-гард W1674/W1684 — без изменений.
- `_get_rest_auth()` остаётся lazy-глобалом (читает `settings.DATA_DIR`; в слитом
  процессе тот же data-dir — инвариант сохраняется). `get_cloud_stt_provider` —
  stateless функция, DI не нужен. `LiveSubsService` создаётся per-WS-соединение
  из `deps.transcriber/translator/store` — в RestDeps не входит.

### 3.3 Тесты M1 (новые)

- Фабрика: два независимых `create_app(StaticDeps(...))` в одном процессе не делят
  состояние (изолированный app per-test — то, ради чего фабрика).
- Прокси: `patch.object(rest_server, "store", fake)` меняет поведение хендлера
  standalone-app (контракт-тест на сам механизм `_ModuleGlobalsDeps` — страж
  категории B).
- Контракт-тесты VG-поверхности (переживут M2/S3 без правок): `/health` отвечает
  и содержит поля `status`+`profile`; `/v1/stt/transcribe` 401 при плохом токене
  и 403 (`skipped: privacy_mode`) в privacy-mode; `/v1/tts/synthesize` — та же пара.

### 3.4 DoD M1

Поведение бит-в-бит: живой e2e REST-смок до/после (одинаковые ответы на одинаковые
запросы); **0 правок существующих тестов** (git diff KrabEar/tests/ пуст, кроме новых
файлов); ubuntu-parity (`make pre-merge-check`); `make audit-all`; flake8 CI-флагами.

### 3.5 Почему НЕ отдельный модуль-фабрика

Вынос route-кода в новый `rest_app_factory.py` дал бы чистый импорт для M2, но:
(а) перенос ~1500 строк — большой механический дифф с риском потерь при переносе
(класс W173-rebase); (б) прецедент W797: экстракции с оставшейся inline-копией
умирали — здесь пришлось бы держать обе точки правды на переходный период;
(в) двойного состояния при импорте из BackendService всё равно нет ничего страшного
для M2 — см. 4.2. Фабрика внутри `rest_server.py` решает задачу без переноса.

## 4. Волна M2 — in-process режим (опт-ин; старт не раньше 2026-07-30)

### 4.1 Рубильник

`rest_in_process_enabled` (default **false**) в `DEFAULT_SETTINGS` + Pydantic-поле
(`KRAB_EAR_REST_IN_PROCESS_ENABLED`). Читается **один раз при старте** backend-процесса
(сиблинг `DISK_MONITOR_ENABLED`/`event_bridge_enabled` — не live-toggle).

### 4.2 Запуск в BackendService

- `BackendService` строит `StaticDeps` из СВОИХ коллабораторов (engine из
  transcriber'а, store, translator, tts_service, module-синглтоны metrics/bus) и
  зовёт `create_app(deps)`.
- ⚠️ Импорт `backend.rest_server` из service.py исполнит module-level код и создаст
  standalone-комплект синглтонов (второй AudioEngine в ТОМ ЖЕ процессе). Недопустимо.
  Решение M2: module-level standalone-инициализация уходит за guard-функцию
  `_ensure_standalone_singletons()`, вызываемую из `__main__`-пути и лениво из
  `_ModuleGlobalsDeps.__getattr__`; импорт модуля становится лёгким. Патчабельность
  A/B/C сохраняется: конструирование по-прежнему происходит при ПЕРВОМ обращении
  тестов к module-атрибутам, т.е. внутри их патч-контекстов (A патчит вокруг импорта —
  перенести на «вокруг первого обращения» — это ЕДИНСТВЕННОЕ место, где M2 может
  задеть старые тесты; контракт-тест прокси из 3.3 + прогон полного пласта — гейт).
  Если прогон покажет >5 сломанных файлов — fallback-решение: оставить eager-импорт
  и в M2 передавать в StaticDeps ссылки на УЖЕ созданные standalone-синглтоны
  (без второго комплекта), приняв, что импорт тяжёлый.
- Сервер: `make_server("127.0.0.1", settings.REST_SERVER_PORT, app, threaded=True)`
  в daemon-треде (`serve_forever`), НЕ `app.run` (нет чистого останова).
- Останов: `.shutdown()` в `GracefulShutdownHandler` + рассылка None-сентинела
  SSE-подписчикам шины (урок прото-смока: shutdown мгновенный, но стрим-генераторы
  выходят только по сентинелу/timeout).
- **EADDRINUSE** (легаси rest-агент ещё жив): громкая ошибка ErrorBus
  (`rest.port_conflict`, новый код в ERROR_REGISTRY) + backend продолжает работу
  БЕЗ in-process REST (fail-open в сторону живой диктовки; никакого crash-loop).
  Диагностика: секция `get_diagnostics.rest_in_process` (`enabled/running/port/error`).

### 4.3 Шина и мост

`backend.event_bus.bus` — module-синглтон: в слитом процессе REST-хендлеры и
BackendService автоматически делят ОДНУ шину, инжектировать нечего. Следствия:

- `EventBridge` при `rest_in_process_enabled=true` **не стартует** (иначе echo:
  событие вернулось бы в ту же шину через `/internal/event`).
- `/internal/event` остаётся (loopback+token гейты не трогаем) — при выключенном
  мосте в него никто не постит.
- Поведенческое изменение (ЖЕЛАЕМОЕ, зафиксировать в тестах M2): события REST-происхождения
  (`live_subs.result` и т.п.) теперь видят IPC-слушатели — webhooks (wave1775),
  event_replay (W1677), error-подписчики. Проверить отсутствие двойной доставки
  при выключенном мосте (сиблинг «double-write of one side effect from two taps»).

### 4.4 Что НЕ трогаем в M2

`mlx_inter_process_lock` остаётся (защищает и от внешних процессов — скрипты,
GigaAM-venv); его упрощение — отдельный хвост после S3. Auth/CORS/limiter/схемы
ответов — без изменений. Латентная конкуренция STT: сегодня REST-transcribe и
диктовка уже сериализуются межпроцессным flock — конкуренция не новая, меняется
только уровень (GIL vs flock); гейт — перф-замер канарейки.

### 4.5 Канарейка (≥2 недели на владельце)

- Латентность диктовки (перф-гейт CI p95 +15% бюджет + ручной бенч до/после).
- RAM: `scripts/memory_baseline.py` до/после (ожидание: минус сотни MB дубля).
- Нагрузочный смок: скрипт прото-смока переезжает в
  `scripts/rest_inprocess_load_smoke.py` (3 SSE + WS + конкурентные POST против
  ЖИВОГО in-process REST).
- VG-контракт: `/health` <1 с под нагрузкой, схема `{status, profile}`, 401/403.

## 5. Волна S3 — флип и вывод

- Default `rest_in_process_enabled` → **true**.
- Скрипт `scripts/migrate_to_inprocess_rest.command`: bootout `ai.krab.ear.rest` +
  удаление plist (стиль `migrate_to_canonical_launchagent.command`).
- `install_rest_launchagent.command` → deprecated-заглушка с подсказкой.
- `kill_dup_gigaam.command`: pgrep-ветка про rest_server.py — обновить.
- `run_e2e_bridge_smoke.command` → адаптировать: проверка САМОВЫРОЖДЕНИЯ моста
  (in-process: мост не стартует, события доставляются) + сохранить двухпроцессный
  прогон для standalone-конфигурации.
- gunicorn-путь (`start_rest_production.command`, `gunicorn_config.py`) — пометить
  legacy-standalone (работает, не рекомендуется; preload_app+workers=2 с двумя
  комплектами моделей — исторический артефакт).
- **VG-бриф** (в их сессию, по образцу брифов 3b): (1) их `start_gateway*.command`
  спавнит `rest_server.py` — при живом backend их `curl /health` успешен и спавна
  не будет, но при упавшем backend спавн даст standalone-REST, который займёт :5005
  и создаст порт-конфликт при подъёме backend — рекомендация: заменить спавн на
  повторный poll `/health` с подсказкой запустить Krab Ear; (2) рекомендация
  retry-with-backoff в их STT/TTS клиентах (окно рестарта слитого процесса длиннее;
  их TTS ждёт до 15 с на каждую фразу в окне простоя); (3) `/health` схема и
  401/403 семантика гарантированы контракт-тестами Ear.
- Standalone-режим **не удаляется** (dev/тесты/аварийный откат setting'ом).
- Доки: CLAUDE.md (IPC protocol / REST-заметки), `docs/IPC_API_REFERENCE.md`,
  `DISTRIBUTION.md`/`USER_MANUAL.md` при затрагивании, ROADMAP §3.1 → журнал.

## 6. Риски

| Риск | Митигция |
|---|---|
| Категория B тестов молча теряет действие патчей | `_ModuleGlobalsDeps` (живое чтение module-атрибутов) + контракт-тест прокси (3.3) |
| M2 lazy-инициализация задевает тесты категории A | Гейт: полный прогон 27 файлов; fallback-решение в 4.2 |
| Порт-конфликт при флипе (легаси-агент жив) | EADDRINUSE → ErrorBus + fail-open (4.2); migrate-скрипт S3 сносит агент |
| Латентность диктовки при конкурентном REST-transcribe | Уже сериализуются flock'ом; перф-гейт CI + канарейка (4.5) |
| Краш REST-хендлера в общем процессе | Flask ловит per-request; демон-тред за error-boundary; Phase A supervisor перезапускает backend (осознанно принятое укрупнение зоны отказа) |
| Окно рестарта для VG длиннее | VG-бриф: retry-with-backoff; быстрый `/health`; канарейка меряет время старта |
| SSE-генераторы висят после shutdown | None-сентинел подписчикам в GracefulShutdownHandler (4.2) |
| Двойная доставка событий при недовыключенном мосте | Тест M2: мост не стартует при in-process; сверка счётчиков доставки |

## 7. Не-цели (YAGNI)

- Миграция ~16 хрупких тест-файлов на фабрику — НЕ в серии M (опциональная волна
  после S3, когда фабрика докажет себя).
- Изменение поведения любого эндпойнта, auth, CORS, rate-limit — нет.
- Удаление standalone-режима и межпроцессного MLX-flock — нет.
- Правки VG-репозитория — только бриф в их сессию.

## 8. Границы волн и модель исполнения

- **M1**: план `docs/superpowers/plans/2026-07-16-m1-rest-app-factory.md`;
  исполнение — Sonnet-воркеры по конвейеру (worktree → личный гейт → adversarial
  fresh-eyes → живой e2e REST-смок), финальный гейт диффа — дорогой моделью.
- **M2**: свой план; старт по двум условиям: мост ≥2 недели без инцидентов
  (с 2026-07-16) И M1 в проде ≥3 дней. Ожидаемо после 30-07 (пост-Fable:
  план на Opus, исполнение Sonnet — методичка §3.6).
- **S3**: свой план; старт после канарейки M2 (≥2 недели с флага on).
