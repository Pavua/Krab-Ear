# Event-мост IPC→REST (Волна 2 роадмапа H2) — дизайн

Дата: 2026-07-07 · Статус: спека готова к writing-plans · Волна: `docs/ROADMAP-2026H2.md` §2 Волна 2
Автор: Fable 5 (финальная сессия) — архитектура проработана наперёд, исполнение — Sonnet.

## 1. Проблема

Прод = два процесса с РАЗДЕЛЬНЫМИ module-level шинами `backend/event_bus.py::bus`:
- **IPC-процесс** (`service.py`) — здесь эмитится почти всё: `stt.*`, `live_subs.result`
  (агентский путь `live_subs_ingest`), `krab_error`, `rewriter_recovered`, `model_download.progress`…
- **REST-процесс** (`rest_server.py`, :5005) — здесь живут SSE `/v1/events` и WS `/v1/stream`,
  на которые подписан Swift-агент и внешние клиенты.

События IPC-процесса НИКОГДА не доходят до SSE-подписчиков. Жертвы класса: wake word и
`krab_error` (починены обходом — IPC-поллинг), `rewriter_recovered` (flash-green мёртв,
гэп задокументирован доккомментом в `main+HealthMonitor.swift:113-122`),
`live_subs.result` агентским путём (подтверждено чтением кода: `LiveSubtitlesOverlay.swift`
подписан на `:5005 /v1/events?filter=live_subs.result`, а агентский ingest идёт через IPC →
шина IPC-процесса; живая проверка — Шаг 0 плана), и **5-я — streaming paste**
(найдена инвентаризацией A1 2026-07-07: `StreamingPasteController.swift:121` подписан на
`:5005 /v1/events?filter=realtime.partial_transcript,...`, а `RealtimePartialTranscriber`
эмитит их в IPC-процессе → фича `streaming_paste_enabled` мертва by-architecture;
из A1-пресета исключена до этой волны, после моста — кандидат обратно).

## 2. Решение (вариант А фазы 1, утверждён владельцем 2026-07-07)

Однонаправленный мост **IPC → REST**: каждый конверт, эмитнутый в IPC-процессе,
доставляется в шину REST-процесса и оттуда — существующим SSE/WS-подписчикам.
Обратное направление НЕ нужно (REST-originated события потребляются same-process;
известный пре-существующий гэп «вебхуки не фаерятся на REST-события» — вне скоупа, см. §8).

### 2.1 Компонент `EventBridge` (новый файл `backend/event_bridge.py`)

- Подключение штатным механизмом листенеров: `event_bus.add_listener(bridge.on_event)` —
  тот же паттерн, что вебхуки (wave1775). `on_event(type, payload)` обязан быть
  неблокирующим (контракт листенеров) → только `deque.append` + `Event.set`.
- Внутри: `collections.deque(maxlen=256)` (drop-oldest при переполнении) + один daemon
  sender-тред: `POST http://127.0.0.1:5005/internal/event`, `timeout=2`, батчами до 20
  конвертов за запрос (`{"events": [...]}`).
- REST недоступен → экспоненциальный backoff 1→30 с; WARN **по смене состояния**
  (down/up), не на каждое событие. Эмиттеры не блокируются никогда.
- Потерянные при даунтайме события НЕ доставляются задним числом — осознанно:
  это UI-события реального времени, replay есть отдельно (`EventReplayManager`).
- Конверт передаётся КАК ЕСТЬ (`{type, ts, data}`) + поле верхнего уровня `origin: "ipc"`.
  Оригинальный `ts` сохраняется (не перештамповывается на REST-стороне).
- Выключатель: настройка `event_bridge_enabled` (default **true** — мост чинит сломанное),
  уважает `KRAB_EAR_EVENT_BRIDGE_ENABLED`. Порт REST — из существующей настройки/константы,
  не хардкод в двух местах.
- Наблюдаемость: счётчики `sent/dropped/failed` + состояние (`up/down/disabled`) →
  новая секция `event_bridge` в `get_diagnostics`.

### 2.2 REST-сторона: `POST /internal/event`

- Loopback-only: `request.remote_addr` ∈ {127.0.0.1, ::1}, иначе 403 (REST и так слушает
  только 127.0.0.1 — belt-and-braces на случай смены bind).
- **Всегда** требует bridge-токен (независимо от пользовательского REST-auth):
  `Authorization: Bearer <token>`, сравнение `hmac.compare_digest`.
- Токен: файл `<data_dir>/event_bridge_token` (0600). Создаёт IPC-процесс при старте,
  если отсутствует (`secrets.token_hex(32)`); REST читает лениво на первый запрос
  (порядок старта процессов произволен). Файл недоступен/пуст на REST-стороне →
  401 + WARN (fail-closed, событие дропается мостом по обычному retry-пути).
  Ротация = удалить файл + рестарт обоих.
- Обработка: валидация формы конверта (`type:str`, `ts:str`, `data:dict`) →
  `bus.emit_envelope(envelope)` для каждого. Невалидный элемент — скип + WARN, не 500.

### 2.3 `EventBus.emit_envelope(envelope)` (небольшое дополнение `event_bus.py`)

Кладёт готовый конверт в очереди подписчиков (SSE/WS) БЕЗ вызова push-листенеров и без
перештамповки `ts`. Структурный no-echo guard: даже если в REST-процессе когда-нибудь
появятся листенеры (вебхуки и т.п.), мостовые события не фаерят их повторно — вебхук уже
сработал в IPC-процессе на оригинальном emit.

### 2.4 Swift-сторона

Кода НЕ требуется: `LiveSubtitlesOverlay` и `HealthMonitor.subscribeToProbeEvents`
уже подписаны правильно — мост оживляет существующие подписки. Убрать 🔴-доккомент
«известный гэп» в `main+HealthMonitor.swift` (113-122) в рамках волны.

## 3. Шаг 0 — аудит живости (до кода)

Живой проверкой зафиксировать текущее состояние (ожидание: оба пути мертвы):
1. Поднять dev-backend + rest_server на temp data-dir (`scripts/run_e2e_smokes.command` паттерн).
2. `curl -N ':5005/v1/events?filter=live_subs.result'` + IPC `live_subs_ingest` с синтетическим
   PCM (тишина 3 с, `is_final=true`).
3. То же для streaming paste пути: `curl -N ':5005/v1/events?filter=realtime.partial_transcript'`
   + старт записи через IPC (или прямой emit в dev-режиме) — событие не должно дойти.
4. Результат (событие пришло/нет, по обоим путям) — в план волны как факт. Если ВНЕЗАПНО живо —
   остановиться и разобраться (значит, модель двух шин неверна и дизайн пересматривается).

## 4. Тесты

- Юнит `event_bridge`: неблокирующий `on_event`, drop-oldest, батчинг, backoff по смене
  состояния, счётчики. Без сети — sender с инжектированным `post_fn`.
- Юнит `emit_envelope`: подписчики получают конверт как есть, листенеры НЕ вызваны, `ts` сохранён.
- Контракт `/internal/event`: 403 не-loopback (mock remote_addr), 401 без/с неверным токеном,
  200 валидный батч → событие в SSE-очереди, невалидный элемент скипается.
- E2E (двухпроцессный, по паттерну live-smoke): реальный `service.py` + `rest_server.py` на
  temp data-dir → IPC-эмит → событие получено из SSE ≤200 мс. Хаос-кейс: REST убит → эмиты
  не блокируются, WARN один; REST поднят → новые события доходят.
- ubuntu-parity: модуль чисто-Python — обязан проходить `pre_merge_py312_check.sh`;
  тесты с `BackendService(...)` — `service.close()` в tearDown (жёсткое правило).

## 5. Безопасность / приватность

- Токен-файл 0600 в data_dir; в логи не попадает; `settings_backup` его не бэкапит (файл вне settings.json).
- События могут содержать текст транскриптов (`live_subs.result.data.text`) — но REST SSE
  УЖЕ отдаёт этот тип событий локальным подписчикам, когда источник same-process;
  мост не расширяет поверхность (localhost→localhost, тот же trust domain). Privacy-mode
  гейтится у ИСТОЧНИКА (live_subs уже гейтится) — мост не создаёт обходов.
- `handle_purge_all_data`: токен НЕ содержит пользовательских данных — в purge не входит;
  зафиксировать allowlist-ом в `audit_purge_coverage`, если гард его увидит.

## 6. Definition of Done

1. Шаг 0 задокументирован фактом в плане.
2. Событие из IPC-процесса доходит до SSE-подписчика REST ≤200 мс (замер в e2e).
3. Live subs агентским путём работают (живой смок с реальным захватом — owner-assisted шаг).
4. `rewriter_recovered` flash-green достижим (проверка: ручной emit в IPC → SSE получил);
   доккомент-гэп удалён.
5. Хаос-кейс зелёный; `get_diagnostics.event_bridge` отражает up/down/счётчики.
6. Релиз v2.6.0 по колее §1 роадмапа.

## 7. Оценка

Python: `event_bridge.py` ~120 LOC + `emit_envelope` ~20 LOC + REST endpoint ~50 LOC + тесты.
Swift: 0 нового кода (минус один доккомент). Размер волны: **M**, полностью Sonnet-исполняемая.

## 8. Явно вне скоупа

- Слияние процессов — §3.1 роадмапа (гейт-решение после этой волны; мост — самостоятельная
  ценность и остаётся полезным до слияния).
- Перевод wake word / krab_error обратно с IPC-поллинга на SSE — поллинг работает, не трогать.
- Вебхуки на REST-originated события (пре-существующий гэп, отдельный chip при надобности).
- Гарантированная доставка/replay через мост — deque с drop-oldest осознанно.
