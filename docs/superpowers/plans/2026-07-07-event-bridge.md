# Event-мост IPC→REST — имплементационный план (Волна 2 роадмапа H2)

> **Для агентных воркеров:** РЕКОМЕНДУЕМЫЙ САБ-СКИЛЛ: superpowers:subagent-driven-development
> (или superpowers:executing-plans) для исполнения плана задача-за-задачей. Шаги —
> чекбоксы (`- [ ]`) для трекинга. **ТЕСТЫ ПИШУТСЯ ПЕРЕД КОДОМ** в каждой задаче
> (fail-before → implementation → pass-after).

**Цель:** закрыть класс багов «событие эмитится в IPC-процессе (`service.py`), а
подписчик слушает REST-процесс (`rest_server.py`, :5005)» — прод состоит из двух
процессов с РАЗДЕЛЬНЫМИ module-level шинами `backend/event_bus.py::bus` (два разных
Python-интерпретатора, общий исходный код, НЕ общая память). Жертвы: wake word и
`krab_error` (уже починены обходом — IPC-поллинг), `rewriter_recovered` flash-green
(доккомент «известный гэп» в `main+HealthMonitor.swift`), `live_subs.result`
агентским путём (гипотеза — подтверждается/опровергается в Задаче 1).

**Архитектура (вариант А, однонаправленно IPC→REST):** новый компонент `EventBridge`
(`backend/event_bridge.py`) подписывается как push-листенер на ЛОКАЛЬНУЮ
(IPC-процесса) шину через `event_bus.add_listener(bridge.on_event)` — тот же
механизм, что вебхуки (wave1775). `on_event()` неблокирующий: кладёт конверт в
bounded `deque(maxlen=256)` и будит daemon sender-тред. Sender-тред батчами (≤20)
POST-ит `http://127.0.0.1:{REST_SERVER_PORT}/internal/event` с bridge-токеном;
REST принимает (loopback-only + токен, fail-closed) и вызывает НОВЫЙ
`EventBus.emit_envelope()`, который кладёт конверт в очереди SSE/WS-подписчиков
БЕЗ повторного вызова push-листенеров (структурный no-echo guard — REST-сторона
никогда не эмитит вебхуки повторно на то же событие) и без перештамповки `ts`.

**Спека:** `docs/superpowers/specs/2026-07-07-event-bridge-design.md` (единственный
источник требований — этот план её не должен противоречить; расхождения см. в
финальной секции «Открытые вопросы к контролёру»).

**Размер волны:** M, полностью Sonnet-исполняемая (по спеке §7).

---

## ⚖️ Поправки контролёра (гейт Fable, 2026-07-07 — исполнять КАК ЧАСТЬ плана)

Все 5 самостоятельных решений воркера из «Открытых вопросов к контролёру» — **ПРИНЯТЫ**
(REST_SERVER_PORT-поле; EVENT_BRIDGE_ENABLED как Pydantic startup-поле, НЕ DEFAULT_SETTINGS;
`ts` в `on_event()` — листенер синхронный, skew микросекундный, а REST не перештамповывает —
интент спеки соблюдён; peek-then-pop retry; limiter 600/min — батчинг ≤20 держит
POST-rate ≤ ~90/мин даже на всплесках `recording.audio_level`). Плюс ДВЕ поправки:

1. **T3 (обязательное дополнение): stale-TTL при отправке.** Sender-тред ПЕРЕД включением
   конверта в батч сверяет возраст: `now - parse(ts) > MAX_EVENT_AGE_SEC` → конверт
   отбрасывается со счётчиком `dropped_stale` (в diagnostics рядом с `sent/dropped/failed`).
   Новая константа `MAX_EVENT_AGE_SEC = 30.0` (рядом с QUEUE_MAXLEN и пр.). Юнит-тест:
   после «даунтайма» (post_fn падает) конверты старше 30с не уходят при восстановлении,
   свежие — уходят. Обоснование: peek-then-pop без TTL после долгого даунтайма REST
   выплюнет burst стухших UI-событий (старые субтитры поверх свежих) — спека
   («потерянные при даунтайме не доставляются задним числом») трактуется именно так.
   Невалидный/отсутствующий `ts` при парсинге → конверт считается свежим (fail-open,
   не терять события из-за формата).
2. **T6 (обязательное дополнение): e2e покрывает realtime.partial_transcript.** После
   спеки-амендмента 2026-07-07 у гэпа есть 5-я жертва — streaming paste
   (`StreamingPasteController.swift:121` слушает `realtime.partial_transcript` на REST-SSE,
   эмиттер — IPC-процесс). Отдельный аудит-прогон в T1 НЕ нужен (гэп шинный, live_subs
   его уже доказывает), но T6-e2e обязан прогнать конверт `realtime.partial_transcript`
   через мост до SSE-подписчика — это доказывает пищевую цепочку streaming paste.
   В T8-доки добавить: `streaming_paste_enabled` разблокирован мостом (кандидат обратно
   в A1-пресет, см. `2026-07-07-recommended-setup-DRAFT.md`).

## Критичные факты и константы (НЕ изобретать заново, использовать буква-в-букву)

**Константы `backend/event_bridge.py` (фиксированы спекой §2.1, НЕ настройки):**
- `QUEUE_MAXLEN = 256` — bounded deque, drop-oldest при переполнении.
- `BATCH_MAX = 20` — максимум конвертов за один POST.
- `POST_TIMEOUT_SEC = 2.0` — таймаут одного POST.
- `MAX_EVENT_AGE_SEC = 30.0` — stale-TTL при отправке (поправка контролёра №1).
- `BACKOFF_MIN_SEC = 1.0` / `BACKOFF_MAX_SEC = 30.0` — экспоненциальный backoff 1→30с.
- `SENDER_POLL_SEC = 1.0` — верхняя граница ожидания в sender-цикле (держит
  `stop()`/backoff отзывчивыми; НЕ влияет на задержку доставки — будится немедленно
  через `threading.Event.set()` из `on_event()`).
- `EVENT_BRIDGE_TOKEN_FILENAME = "event_bridge_token"`, `secrets.token_hex(32)` (64 hex-символа).
- Токен-файл: права `0o600`, атомарная запись (tempfile + rename) — паттерн
  идентичен `backend/privacy_audit.py::_load_or_create_key`.
- Auth: `hmac.compare_digest`, loopback-проверка `request.remote_addr in ("127.0.0.1", "::1")`.
- Настройка `EVENT_BRIDGE_ENABLED: bool = True` (`core/config.py::Settings`,
  env `KRAB_EAR_EVENT_BRIDGE_ENABLED` — автоматически через `env_prefix="KRAB_EAR_"`,
  без `validation_alias`) — killswitch, читается ОДИН РАЗ при конструировании
  `EventBridge` (сиблинг-паттерн `DiskSpaceMonitor`/`DISK_MONITOR_ENABLED` —
  **НЕ** live-toggle через `set_settings`/`DEFAULT_SETTINGS`, см. «Открытые вопросы»).

**🔴 Постоянные правила проекта (действуют на ВСЕ задачи этого плана):**
- Воркерам **НЕЛЬЗЯ запускать собранный `KrabEarAgent`-бинарь напрямую** (убьёт прод
  через `SingleInstanceGuard`). `swift build`/`swift test` — можно и нужно (Задача 7).
- Каждый тест, конструирующий `BackendService(...)` напрямую, ОБЯЗАН вызывать
  `service.close()` в `tearDown` (иначе daemon-треды валят весь CI-чанк — см.
  `feedback_backendservice_teardown_ci.md`).
- Новый модуль (`event_bridge.py`) обязан пройти `make audit-dead-modules` (не
  осиротевший — реально импортируется и используется в `service.py`).
- flake8 CI-командой: `max-line-length=150`, `E501` игнорируется проектно;
  `KrabEar/tests/*.py` дополнительно игнорирует `E402`.
- ubuntu-parity: ЛЮБОЙ новый/изменённый тестовый файл — обязательно через
  `scripts/pre_merge_py312_check.sh <файлы>` перед тем, как считать задачу готовой
  (Python 3.14 dev-venv имеет mlx — ubuntu CI его не имеет; событийный код здесь
  чистый Python без mlx-зависимостей, но правило безусловное для всех новых тестов).

**Файлы, которые этот план создаёт или трогает** (для ориентира по задачам):
- НОВЫЕ: `KrabEar/backend/event_bridge.py`, `KrabEar/tests/test_event_bus_emit_envelope.py`,
  `KrabEar/tests/test_event_bridge.py`, `KrabEar/tests/test_rest_internal_event.py`,
  `KrabEar/tests/test_event_bridge_wiring.py`, `scripts/e2e_event_bridge_smoke.py`,
  `scripts/run_e2e_bridge_smoke.command`.
- ИЗМЕНЯЕМЫЕ: `KrabEar/backend/event_bus.py`, `KrabEar/backend/rest_server.py`,
  `KrabEar/backend/service.py`, `KrabEar/backend/health_check_service.py`,
  `KrabEar/core/config.py`, `scripts/purge_coverage_allowlist.txt`,
  `native/KrabEarAgent/Sources/KrabEarAgent/main+HealthMonitor.swift`,
  `docs/IPC_API_REFERENCE.md`, `CLAUDE.md`.
- **НЕ трогать:** `KrabEar/backend/telegram_bridge.py` (чужая незакоммиченная правка).

---

### Задача 1: Шаг 0 — аудит живости (до любого кода)

**Цель:** живой проверкой зафиксировать текущее состояние 4-й предполагаемой
жертвы (live subs агентским путём) — ОЖИДАНИЕ: путь мёртв (спека §3). Если
ВНЕЗАПНО жив — **остановиться**, не писать код Задач 2-8, эскалировать: модель
двух раздельных шин неверна, дизайн требует пересмотра.

**Файлы:** ничего не создаёт/не коммитит постоянно — только заполняет секцию
«Факт Шага 0» ниже, в ЭТОМ ЖЕ файле плана.

- [x] **Шаг 1: Поднять оба процесса на общий temp data-dir**

```bash
DATADIR="$(mktemp -d /tmp/krab_ear_bridge_audit.XXXXXX)"
SOCK="$DATADIR/krabear.sock"
VENV=".venv_krab_ear"

PYTHONPATH="$(pwd)/KrabEar" "$VENV/bin/python" KrabEar/main.py --data-dir "$DATADIR" \
  > "$DATADIR/ipc.log" 2>&1 &
IPC_PID=$!

# Ждём появления сокета (до ~20с)
for _ in $(seq 1 40); do [ -S "$SOCK" ] && break; sleep 0.5; done
[ -S "$SOCK" ] || { echo "FATAL: сокет не появился"; tail -30 "$DATADIR/ipc.log"; exit 1; }
sleep 2  # прогрев

KRAB_EAR_DATA_DIR="$DATADIR" PYTHONPATH="$(pwd)/KrabEar" "$VENV/bin/python" \
  KrabEar/backend/rest_server.py > "$DATADIR/rest.log" 2>&1 &
REST_PID=$!

# Ждём готовности REST (до ~20с)
for _ in $(seq 1 40); do
  curl -s -o /dev/null -w '' "http://127.0.0.1:5005/health" && break
  sleep 0.5
done
curl -s "http://127.0.0.1:5005/health" | head -c 200; echo
```

- [x] **Шаг 2: Открыть SSE-подписку на `live_subs.result` в фоне**

```bash
curl -N --max-time 15 'http://127.0.0.1:5005/v1/events?filter=live_subs.result' \
  > "$DATADIR/sse_capture.txt" 2>&1 &
SSE_PID=$!
sleep 1  # дать SSE-подписке зарегистрироваться в EventBus ДО ingest
```

- [x] **Шаг 3: `live_subs_ingest` с синтетическим PCM (3с тишины, `is_final=true`)**

Точный shape параметров подтверждён чтением `backend/live_subs_service.py::handle_ingest`
(строка 142) — `{audio_chunk: base64 PCM16, sample_rate, target_lang, is_final}`.
`is_final=True` форсирует немедленный `_flush()` вне зависимости от порога буфера
(`live_subs_service.py:92`, `should_flush = is_final or buffer_sec >= _FLUSH_THRESHOLD_SEC or over_cap`),
и `_flush()` эмитит `live_subs.result` БЕЗУСЛОВНО (даже при `text=""` от тишины —
нет гейта на пустой текст, строка 264: `event_bus.emit_typed(EventType.LIVE_SUBS_RESULT, event_payload)`).

```bash
"$VENV/bin/python" - "$SOCK" <<'PYEOF'
import base64, json, socket, sys
sock_path = sys.argv[1] if len(sys.argv) > 1 else None
silence = b"\x00" * (16000 * 2 * 3)  # 3s @ 16kHz, 16-bit PCM, тишина
audio_b64 = base64.b64encode(silence).decode()

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(30)
s.connect(sock_path)
req = json.dumps({
    "id": "audit-live-subs",
    "method": "live_subs_ingest",
    "params": {"audio_chunk": audio_b64, "sample_rate": 16000,
               "target_lang": "off", "is_final": True},
}) + "\n"
s.sendall(req.encode())
buf = b""
while b"\n" not in buf:
    chunk = s.recv(65536)
    if not chunk:
        break
    buf += chunk
s.close()
print("IPC response:", buf.decode())
PYEOF
```

- [x] **Шаг 4: Проверить, пришло ли событие**

```bash
sleep 3   # дать STT+SSE время (mlx-транскрипция тишины — быстрая, но не мгновенная)
kill "$SSE_PID" 2>/dev/null
wait "$SSE_PID" 2>/dev/null
echo "--- SSE capture ---"
cat "$DATADIR/sse_capture.txt"
```

Ожидание (гипотеза спеки): `sse_capture.txt` содержит ТОЛЬКО `: keepalive` строки,
`event: live_subs.result` НИКОГДА не появляется — подтверждает 2-EventBus гэп.

- [x] **Шаг 5: Cleanup**

```bash
kill -TERM "$IPC_PID" "$REST_PID" 2>/dev/null; sleep 1
kill -KILL "$IPC_PID" "$REST_PID" 2>/dev/null
rm -rf "$DATADIR"
```

- [x] **Шаг 6: Записать факт в этот план (заполнить секцию ниже) и закоммитить
      ТОЛЬКО этот файл** (`git add docs/superpowers/plans/2026-07-07-event-bridge.md`)
      перед тем, как переходить к Задаче 2.

#### Факт Шага 0 (заполняется исполнителем Задачи 1)

> **Статус: событие НЕ пришло — гипотеза ПОДТВЕРЖДЕНА.**
>
> Дата/время прогона: 2026-07-07 22:20–22:29 (локальное время машины; точные
> монотонные метки — ниже).
>
> **🔴 Методологическая находка по ходу прогона (задокументирована для
> прозрачности):** первая попытка (буквально по команде из плана, порт 5005
> хардкодом) дала бы ЛОЖНОЕ подтверждение — на этой машине уже запущен РЕАЛЬНЫЙ
> прод REST-сервер на порту 5005 (launchd, PID 2825, из основного checkout, а
> не из этого worktree). Мой тестовый REST-процесс (`KrabEar/backend/rest_server.py`
> напрямую) тихо падал на старте с `OSError: [Errno 48] Address already in use`
> (см. F3-guard в `rest_server.py:2066-2081`; `rest.log` содержал `Address already
> in use`), а все curl на `127.0.0.1:5005` в первой попытке на самом деле били в
> ЖИВОЙ прод-REST (другой data-dir, другой IPC-процесс) — "событие не пришло" в
> той попытке было артефактом обращения не к той инстанции, а не доказательством
> 2-EventBus гэпа. Прод-процесс НЕ был тронут (не убивался, не рестартовался) —
> только `ps -p 2825` для диагностики.
>
> **Исправление и повторный чистый прогон:** REST-инстанс для аудита поднят
> ИЗОЛИРОВАННО — тот же `KRAB_EAR_DATA_DIR=<temp>`, но на СЛУЧАЙНОМ свободном
> порту (`50078`, тем же приёмом, что и в Задаче 6: `socket.bind(("127.0.0.1",0))`)
> через прямой импорт модуля (`import backend.rest_server as rs;
> rs.app.run(host="127.0.0.1", port=50078)`, минуя захардкоженный `port=5005`
> внутри `if __name__ == "__main__":` — НИКАКИХ изменений в `rest_server.py` не
> вносилось, это чисто runtime-обход для теста). Подтверждено: тестовый REST PID
> (60389) ≠ прод PID (2825), тестовый процесс слушал ИМЕННО 50078
> (`curl 127.0.0.1:50078/health` → 200, `rest.log` содержит
> `Running on http://127.0.0.1:50078`).
>
> Финальный чистый прогон (один bash-вызов, без разрывов между фазами): SSE-подписка
> на `http://127.0.0.1:50078/v1/events?filter=live_subs.result` открыта в фоне
> (`curl -N --max-time 22`), через 2с — `live_subs_ingest` по Unix-сокету
> тестового IPC-процесса (`is_final=true`, 3с тишины 16kHz PCM16).
>
> Вывод IPC-ответа на `live_subs_ingest`:
> `{"id": "audit-live-subs-clean", "ok": true, "result": {"status": "flushed",
> "buffer_duration_sec": 0.0, "text": "Субтитры создавал DimaTorzok",
> "translation": null}}` (elapsed=1.005s) — `_flush()` реально отработал и, по коду
> `live_subs_service.py:264` (`event_bus.emit_typed(EventType.LIVE_SUBS_RESULT,
> event_payload)`, безусловный вызов, без гейта на пустой текст), событие было
> эмитнуто на ЛОКАЛЬНУЮ шину IPC-процесса примерно на t≈3с от начала SSE-подписки.
>
> Содержимое `sse_capture_clean.txt` (полный 22-секундный захват, без обрывов, с
> финальным curl-трейлером): за всё окно наблюдения (t=0…22с) на SSE пришёл РОВНО
> ОДИН фрейм — рутинный `: keepalive\n\n` (13 байт) на отметке t≈15с (штатный
> `_SSE_POLL_TIMEOUT_SEC=15.0` из `event_bus.py`). `event: live_subs.result` НИ
> РАЗУ не появился, хотя событие было эмитнуто на t≈3с — то есть за ~12 секунд ДО
> рутинного keepalive было достаточно времени для доставки, если бы мост
> существовал. Финальная строка: `curl: (28) Operation timed out after 22004
> milliseconds with 13 bytes received` — 13 байт = ровно длина одного
> keepalive-фрейма, никаких других данных за всё окно.
>
> Заключение: **гипотеза спеки ПОДТВЕРЖДЕНА** — событие `live_subs.result`,
> эмитнутое в IPC-процессе (`backend/live_subs_service.py` → локальный
> `backend/event_bus.py::bus`), НЕ достигает SSE-подписчика REST-процесса
> (отдельный Python-интерпретатор, отдельный `bus = EventBus()` синглтон, общий
> исходный код — не общая память). Два прогона (первый — методологически
> испорченный портовым конфликтом с прод-REST, см. находку выше; второй — на
> чисто изолированной паре IPC+REST на случайном порту) дали ОДИНАКОВЫЙ
> результат: событие не доходит. Продолжаю к Задачам 2-8 БЕЗ пересмотра дизайна —
> 2-EventBus модель верна, как и предполагает план.

**Критерий готовности:** секция «Факт Шага 0» заполнена реальными данными
(не placeholder-текстом); если факт = «путь жив», задачи 2-8 приостановлены до
решения контролёра.

---

### Задача 2: `EventBus.emit_envelope()` + юнит-тесты

**Цель:** добавить в `backend/event_bus.py` метод, который доставляет УЖЕ ГОТОВЫЙ
конверт подписчикам БЕЗ вызова push-листенеров и БЕЗ перештамповки `ts` (спека §2.3) —
структурный no-echo guard для будущей REST-стороны моста.

**Файлы:**
- Modify: `KrabEar/backend/event_bus.py`
- Create: `KrabEar/tests/test_event_bus_emit_envelope.py`

- [x] **Шаг 1: Failing-тест первым**

`KrabEar/tests/test_event_bus_emit_envelope.py`:

```python
"""test_event_bus_emit_envelope.py — EventBus.emit_envelope() (event-bridge design,
spec docs/superpowers/specs/2026-07-07-event-bridge-design.md §2.3).

emit_envelope() доставляет УЖЕ ГОТОВЫЙ конверт {type, ts, data[, origin]}
подписчикам (SSE/WS) КАК ЕСТЬ — БЕЗ вызова push-листенеров (структурный
no-echo guard: REST-сторона моста не должна повторно триггерить вебхуки —
исходный emit() в IPC-процессе их уже вызвал) и БЕЗ перештамповки ts.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_event_bus_emit_envelope.py -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.event_bus import EventBus  # noqa: E402


class EmitEnvelopeTestCase(unittest.TestCase):
    def test_subscriber_receives_envelope_as_is(self):
        bus = EventBus()
        q = bus.subscribe()
        envelope = {
            "type": "krab_error",
            "ts": "2026-07-07T00:00:00+00:00",
            "data": {"code": "x"},
            "origin": "ipc",
        }
        bus.emit_envelope(envelope)
        received = q.get_nowait()
        self.assertEqual(received, envelope)

    def test_ts_not_restamped(self):
        bus = EventBus()
        q = bus.subscribe()
        original_ts = "2020-01-01T00:00:00+00:00"  # заведомо не "сейчас"
        bus.emit_envelope({"type": "x", "ts": original_ts, "data": {}})
        received = q.get_nowait()
        self.assertEqual(received["ts"], original_ts)

    def test_listeners_not_invoked(self):
        bus = EventBus()
        calls = []
        bus.add_listener(lambda et, pl: calls.append((et, pl)))
        bus.emit_envelope({"type": "stt.final", "ts": "t", "data": {"text": "secret"}})
        self.assertEqual(calls, [], "emit_envelope НЕ должен вызывать push-листенеры (no-echo guard)")

    def test_regular_emit_still_invokes_listeners(self):
        """Контроль: emit() (нативный путь) листенеры всё ещё вызывает — только
        emit_envelope() их пропускает."""
        bus = EventBus()
        calls = []
        bus.add_listener(lambda et, pl: calls.append((et, pl)))
        bus.emit("stt.final", {"text": "x"})
        self.assertEqual(len(calls), 1)

    def test_missing_type_key_is_ignored_defensively(self):
        bus = EventBus()
        q = bus.subscribe()
        bus.emit_envelope({"ts": "t", "data": {}})  # нет "type"
        self.assertTrue(q.empty(), "конверт без 'type' должен быть проигнорирован, не упасть")

    def test_full_subscriber_queue_drops_without_raising(self):
        bus = EventBus()
        bus.subscribe()  # подписчик с maxsize=64, ничего не читает
        for i in range(100):
            bus.emit_envelope({"type": "x", "ts": str(i), "data": {}})
        # Не должно поднять исключение — переполнение просто логируется и дропается.


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [x] **Шаг 2: Прогнать — убедиться что падает**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_event_bus_emit_envelope.py -v`
Expected: FAIL (`AttributeError: 'EventBus' object has no attribute 'emit_envelope'`)

- [x] **Шаг 3: Реализация — добавить метод в `backend/event_bus.py`**

Вставить между `emit()` (кончается строкой 188) и `emit_typed()` (строка 190):

```python
    def emit_envelope(self, envelope: dict[str, Any]) -> None:
        """Доставляет УЖЕ ГОТОВЫЙ конверт подписчикам (SSE/WS) КАК ЕСТЬ.

        Используется REST-стороной event-моста (backend/event_bridge.py,
        docs/superpowers/specs/2026-07-07-event-bridge-design.md §2.3) для
        ре-эмита событий, доставленных из IPC-процесса. В отличие от emit():
          - НЕ вызывает push-листенеров self._listeners (структурный no-echo
            guard — исходный emit() в IPC-процессе их уже вызвал; повторный
            вызов здесь задвоил бы доставку, например, вебхуков).
          - НЕ перештамповывает envelope["ts"] — конверт передаётся как есть.
          - НЕ пишет в self._event_replay (реплей — забота IPC-процесса,
            где _event_replay реально wired; на REST-стороне он всегда None).

        Args:
            envelope: {"type": str, "ts": str, "data": dict, ...} — форма
                уже провалидирована вызывающей стороной (REST /internal/event).
        """
        if "type" not in envelope:
            logger.warning("EventBus.emit_envelope: конверт без 'type' проигнорирован: %r", envelope)
            return
        with self._lock:
            active = list(self._subscribers)
        dropped = 0
        for q in active:
            try:
                q.put_nowait(envelope)
            except queue.Full:
                dropped += 1
        if dropped:
            logger.warning(
                "EventBus: %d подписчик(ов) пропустили bridged-событие %s (очередь полна)",
                dropped, envelope.get("type"),
            )
```

- [x] **Шаг 4: Тесты зелёные**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_event_bus_emit_envelope.py -v`
Expected: 6 passed.

- [x] **Шаг 5: Регрессия — существующие event_bus тесты не сломаны**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_event_bus.py KrabEar/tests/test_event_bus_extras.py KrabEar/tests/test_event_bus_max_subscribers_wave33.py KrabEar/tests/test_webhook_fire_wiring_wave1775.py -v`
Expected: все passed (emit_envelope — чистое дополнение, не трогает emit()/add_listener()).

- [x] **Шаг 6: flake8 + ubuntu-parity**

Run: `.venv_krab_ear/bin/flake8 KrabEar/backend/event_bus.py KrabEar/tests/test_event_bus_emit_envelope.py --max-line-length=150`
Expected: пусто.
Run: `bash scripts/pre_merge_py312_check.sh KrabEar/tests/test_event_bus_emit_envelope.py`
Expected: ALL GREEN.

- [x] **Шаг 7: Commit**

```bash
git add KrabEar/backend/event_bus.py KrabEar/tests/test_event_bus_emit_envelope.py
git commit -m "feat(event-bridge): EventBus.emit_envelope() — no-echo bridged delivery"
```

**Критерий готовности:** 6 новых тестов + вся существующая event_bus-тест-группа зелёные, flake8 чист, ubuntu-parity пройден.

---

### Задача 3: `backend/event_bridge.py` — EventBridge (IPC-сторона) + юнит-тесты

**Цель:** новый модуль — компонент, который слушает локальную шину и отправляет
конверты батчами на REST через инжектируемый `post_fn` (без сети в тестах).

**Файлы:**
- Create: `KrabEar/backend/event_bridge.py`
- Create: `KrabEar/tests/test_event_bridge.py`

- [x] **Шаг 1: Failing-тесты первыми**

`KrabEar/tests/test_event_bridge.py`:

```python
"""test_event_bridge.py — backend/event_bridge.py::EventBridge
(spec docs/superpowers/specs/2026-07-07-event-bridge-design.md §2.1).

Тестирует IPC-сторону в изоляции: инжектируемый post_fn (БЕЗ реальной сети —
жёсткое требование), drop-oldest deque, батчинг, backoff/смена состояния,
форма диагностики, disabled-killswitch, start/stop lifecycle. Реальное
REST-поведение (сеть, /internal/event) покрыто отдельно в Задаче 4 (контракт)
и Задаче 6 (двухпроцессный e2e) — НЕ дублируется здесь.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_event_bridge.py -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.event_bridge import (  # noqa: E402
    EventBridge,
    QUEUE_MAXLEN,
    BATCH_MAX,
    BACKOFF_MIN_SEC,
    BACKOFF_MAX_SEC,
    EVENT_BRIDGE_TOKEN_FILENAME,
    read_bridge_token,
)


def _fake_settings(enabled: bool = True, port: int = 5005) -> SimpleNamespace:
    return SimpleNamespace(EVENT_BRIDGE_ENABLED=enabled, REST_SERVER_PORT=port)


class EventBridgeUnitTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    # -- on_event: неблокирующий, никогда не бросает исключения ----------------

    def test_on_event_appends_envelope(self):
        bridge = EventBridge(settings=_fake_settings(), data_dir=Path(self._tmpdir))
        bridge.on_event("stt.final", {"text": "x"})
        self.assertEqual(bridge.get_diagnostics()["queue_depth"], 1)

    def test_on_event_disabled_is_noop(self):
        bridge = EventBridge(settings=_fake_settings(enabled=False), data_dir=Path(self._tmpdir))
        bridge.on_event("stt.final", {"text": "x"})
        diag = bridge.get_diagnostics()
        self.assertEqual(diag["queue_depth"], 0)
        self.assertEqual(diag["state"], "disabled")
        self.assertFalse(diag["enabled"])

    def test_queue_drop_oldest_at_maxlen(self):
        bridge = EventBridge(settings=_fake_settings(), data_dir=Path(self._tmpdir))
        for i in range(QUEUE_MAXLEN + 10):
            bridge.on_event("x", {"i": i})
        diag = bridge.get_diagnostics()
        self.assertEqual(diag["queue_depth"], QUEUE_MAXLEN)
        self.assertEqual(diag["dropped"], 10)

    # -- sender: инжектируемый post_fn, без сети --------------------------------

    def test_drain_and_send_success_pops_batch_and_updates_counters(self):
        calls = []

        def fake_post(url, payload, token, timeout):
            calls.append((url, payload, token, timeout))
            return True

        bridge = EventBridge(settings=_fake_settings(), data_dir=Path(self._tmpdir), post_fn=fake_post)
        for i in range(BATCH_MAX + 5):
            bridge.on_event("x", {"i": i})
        bridge._token = "test-token"  # обходим файловый I/O в этом юнит-тесте
        bridge._drain_and_send()

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0][1]["events"]), BATCH_MAX)
        diag = bridge.get_diagnostics()
        self.assertEqual(diag["sent"], BATCH_MAX)
        self.assertEqual(diag["queue_depth"], 5)  # остаток остаётся в очереди
        self.assertEqual(diag["state"], "up")

    def test_drain_and_send_failure_requeues_and_backs_off(self):
        bridge = EventBridge(settings=_fake_settings(), data_dir=Path(self._tmpdir),
                              post_fn=lambda *a, **k: False)
        bridge._token = "test-token"
        bridge.on_event("x", {"i": 1})
        bridge._drain_and_send()

        diag = bridge.get_diagnostics()
        self.assertEqual(diag["state"], "down")
        self.assertEqual(diag["failed"], 1)
        self.assertEqual(diag["queue_depth"], 1, "неотправленный батч должен остаться в очереди, не быть выброшенным")
        self.assertEqual(bridge._current_backoff, BACKOFF_MIN_SEC * 2)

    def test_backoff_caps_at_max(self):
        bridge = EventBridge(settings=_fake_settings(), data_dir=Path(self._tmpdir),
                              post_fn=lambda *a, **k: False)
        bridge._token = "test-token"
        bridge.on_event("x", {})
        for _ in range(10):
            bridge._next_retry_ts = 0.0  # форсируем обход backoff-гейта для прямого вызова
            bridge._drain_and_send()
        self.assertLessEqual(bridge._current_backoff, BACKOFF_MAX_SEC)

    def test_state_change_logged_once_not_per_event(self):
        post_results = iter([False, False, False, True])
        bridge = EventBridge(settings=_fake_settings(), data_dir=Path(self._tmpdir),
                              post_fn=lambda *a, **k: next(post_results))
        bridge._token = "test-token"
        with self.assertLogs("KrabEar.Backend.EventBridge", level="WARNING") as cm:
            for _ in range(3):
                bridge.on_event("x", {})
                bridge._next_retry_ts = 0.0
                bridge._drain_and_send()
        down_warnings = [m for m in cm.output if "недоступен" in m]
        self.assertEqual(len(down_warnings), 1, "ровно ОДИН WARN на 3 подряд неудачи — по смене состояния, не по событию")

    # -- форма диагностики -------------------------------------------------------

    def test_get_diagnostics_shape(self):
        bridge = EventBridge(settings=_fake_settings(), data_dir=Path(self._tmpdir))
        diag = bridge.get_diagnostics()
        for key in ("enabled", "state", "queue_depth", "sent", "dropped", "failed"):
            self.assertIn(key, diag)

    # -- lifecycle: start/stop ----------------------------------------------------

    def test_start_disabled_does_not_spawn_thread(self):
        bridge = EventBridge(settings=_fake_settings(enabled=False), data_dir=Path(self._tmpdir))
        bridge.start()
        self.assertIsNone(bridge._thread)

    def test_start_creates_token_file_with_0600(self):
        bridge = EventBridge(settings=_fake_settings(), data_dir=Path(self._tmpdir),
                              post_fn=lambda *a, **k: True)
        bridge.start()
        try:
            token_path = Path(self._tmpdir) / EVENT_BRIDGE_TOKEN_FILENAME
            self.assertTrue(token_path.exists())
            mode = token_path.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)
            self.assertEqual(read_bridge_token(Path(self._tmpdir)), bridge._token)
        finally:
            bridge.stop()

    def test_read_bridge_token_returns_none_when_absent(self):
        self.assertIsNone(read_bridge_token(Path(self._tmpdir)))

    def test_stop_is_idempotent_and_joins_thread(self):
        bridge = EventBridge(settings=_fake_settings(), data_dir=Path(self._tmpdir),
                              post_fn=lambda *a, **k: True)
        bridge.start()
        bridge.stop()
        bridge.stop()  # второй вызов не должен бросать
        self.assertFalse(bridge._thread.is_alive())


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [x] **Шаг 2: Прогнать — убедиться что падает**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_event_bridge.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'backend.event_bridge'`)

- [x] **Шаг 3: Реализация `KrabEar/backend/event_bridge.py`**

```python
"""EventBridge — доставляет события из IPC-процесса в REST-процесс (Krab Ear).

Прод = два процесса (`service.py` IPC + `rest_server.py` :5005) с РАЗДЕЛЬНЫМИ
module-level шинами `backend/event_bus.py::bus` (два разных Python-интерпретатора,
общий исходный код `EventBus()`, НЕ общая память). События, эмитнутые в
IPC-процессе, никогда не доходили до SSE/WS-подписчиков REST-процесса — жертвы:
wake word / krab_error (обходной путь — IPC-поллинг), rewriter_recovered
(flash-green мёртв), live_subs.result агентским путём (см. Задача 1 плана волны).

Спека: docs/superpowers/specs/2026-07-07-event-bridge-design.md §2.1.

Архитектура: EventBridge подключается как push-листенер (event_bus.add_listener)
к ЛОКАЛЬНОЙ (IPC-процесса) шине. on_event() — неблокирующий (контракт
add_listener): кладёт готовый конверт в bounded deque(maxlen=256, drop-oldest)
и будит daemon sender-тред. Sender-тред батчами (<=20) POST-ит на
127.0.0.1:{settings.REST_SERVER_PORT}/internal/event с bridge-токеном; при
недоступности REST — экспоненциальный backoff 1->30s, WARN только по смене
состояния (up/down), эмиттеры никогда не блокируются. Однонаправленно
(IPC -> REST) — см. спека §2 "вариант А".
"""

from __future__ import annotations

import logging
import os
import secrets
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from core.config import Settings

logger = logging.getLogger("KrabEar.Backend.EventBridge")

# --- Константы (спека §2.1, фиксированы — НЕ настройки) ---------------------
QUEUE_MAXLEN = 256               # bounded deque, drop-oldest при переполнении
BATCH_MAX = 20                   # максимум конвертов за один POST
POST_TIMEOUT_SEC = 2.0           # requests timeout на один POST
BACKOFF_MIN_SEC = 1.0            # стартовый backoff при недоступности REST
BACKOFF_MAX_SEC = 30.0           # потолок backoff
SENDER_POLL_SEC = 1.0            # верхняя граница ожидания в sender-цикле
                                  # (держит stop()/backoff отзывчивыми; будится
                                  # немедленно по wake_event.set() из on_event())

EVENT_BRIDGE_TOKEN_FILENAME = "event_bridge_token"
_TOKEN_BYTES = 32                # secrets.token_hex(32) -> 64 hex-символа


# ---------------------------------------------------------------------------
# Token file helpers — используются И EventBridge (IPC-сторона, создаёт при
# отсутствии), И rest_server.py (REST-сторона, ТОЛЬКО читает — никогда не
# создаёт, спека §2.2: порядок старта процессов произволен).
# ---------------------------------------------------------------------------

def read_bridge_token(data_dir: Path | str) -> str | None:
    """Читает токен моста, НЕ создавая файл. None если отсутствует/пуст/битый.

    Вызывается REST-стороной лениво на первый запрос — REST может стартовать
    раньше IPC-процесса, который единственный создаёт файл.
    """
    token_path = Path(data_dir) / EVENT_BRIDGE_TOKEN_FILENAME
    try:
        content = token_path.read_text(encoding="utf-8").strip()
        return content or None
    except Exception:
        return None


def _load_or_create_token(data_dir: Path) -> str:
    """Читает токен из <data_dir>/event_bridge_token или создаёт новый.

    Вызывается ТОЛЬКО из EventBridge.start() (IPC-сторона). Атомарная запись
    (tempfile + rename), права 0600 — паттерн идентичен
    backend/privacy_audit.py::_load_or_create_key.
    """
    existing = read_bridge_token(data_dir)
    if existing:
        return existing

    token_path = data_dir / EVENT_BRIDGE_TOKEN_FILENAME
    token = secrets.token_hex(_TOKEN_BYTES)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=data_dir, prefix=".event_bridge_token.")
        try:
            os.write(fd, token.encode("utf-8"))
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp_path, token_path)
        token_path.chmod(0o600)
    except Exception:
        logger.exception("EventBridge: не удалось записать event_bridge_token")
        if tmp_path is not None:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass
    return token


def _default_post_fn(url: str, payload: dict[str, Any], token: str, timeout: float) -> bool:
    """Реальный сетевой POST. True на 2xx, False на любой сбой (никогда не бросает).

    Тесты ВСЕГДА инжектируют свой post_fn и никогда не проходят через эту
    функцию (спека требует "без сети" в юнит-тестах).
    """
    import requests  # локальный импорт — держим event_bridge.py дешёвым при disabled

    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        return 200 <= resp.status_code < 300
    except Exception:
        return False


class EventBridge:
    """Мост IPC -> REST: подписывается на локальную шину, батчами POST-ит на REST.

    Конструктор НЕ запускает поток — вызывающая сторона обязана вызвать start()
    (симметрично stop()), как DiskSpaceMonitor/LLMHttpProbe.
    """

    def __init__(
        self,
        settings: "Settings",
        data_dir: Path,
        post_fn: Callable[[str, dict[str, Any], str, float], bool] | None = None,
    ) -> None:
        self._settings = settings
        self._data_dir = Path(data_dir)
        self._post_fn = post_fn or _default_post_fn

        self._enabled = bool(getattr(settings, "EVENT_BRIDGE_ENABLED", True))
        self._rest_port = int(getattr(settings, "REST_SERVER_PORT", 5005))
        self._url = f"http://127.0.0.1:{self._rest_port}/internal/event"

        self._token: str | None = None  # ленивое создание — только в start()

        self._queue: deque[dict[str, Any]] = deque(maxlen=QUEUE_MAXLEN)
        self._lock = threading.Lock()
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._state = "disabled" if not self._enabled else "unknown"  # unknown|up|down|disabled
        self._current_backoff = BACKOFF_MIN_SEC
        self._next_retry_ts = 0.0

        self._sent_count = 0
        self._dropped_count = 0
        self._failed_count = 0

    # ------------------------------------------------------------------
    # EventBus listener (add_listener contract: неблокирующий, без I/O)
    # ------------------------------------------------------------------

    def on_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Push-листенер EventBus. НЕ делает I/O — только deque.append + wake."""
        if not self._enabled:
            return
        envelope = {
            "type": event_type,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "data": payload,
        }
        with self._lock:
            if len(self._queue) >= self._queue.maxlen:
                self._dropped_count += 1
            self._queue.append(envelope)
        self._wake_event.set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Запускает daemon sender-тред. Idempotent. No-op если EVENT_BRIDGE_ENABLED=False."""
        if not self._enabled:
            logger.info("EventBridge отключён (EVENT_BRIDGE_ENABLED=False)")
            return
        if self._token is None:
            self._token = _load_or_create_token(self._data_dir)
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="EventBridge", daemon=True)
        self._thread.start()
        logger.info("EventBridge запущен (url=%s)", self._url)

    def stop(self) -> None:
        """Graceful shutdown: дожидается завершения потока (до 5с). Idempotent."""
        self._stop_event.set()
        self._wake_event.set()  # немедленно будим поток, не дожидаясь SENDER_POLL_SEC
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        logger.debug("EventBridge остановлен")

    # ------------------------------------------------------------------
    # Sender loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=SENDER_POLL_SEC)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            now = time.monotonic()
            if self._state == "down" and now < self._next_retry_ts:
                continue
            self._drain_and_send()

    def _drain_and_send(self) -> None:
        """Отправляет до BATCH_MAX конвертов. Peek (не pop) до подтверждения
        успеха — на неудаче батч ОСТАЁТСЯ в очереди для следующей попытки
        (спека: эмиттеры не блокируются, но события, которые ещё В ОЧЕРЕДИ,
        не считаются "потерянными при даунтайме" — потеря только через
        drop-oldest при переполнении в on_event())."""
        with self._lock:
            batch = list(self._queue)[:BATCH_MAX]
        if not batch:
            return
        ok = self._post_fn(self._url, {"events": batch}, self._token or "", POST_TIMEOUT_SEC)
        if ok:
            with self._lock:
                for _ in range(len(batch)):
                    if self._queue:
                        self._queue.popleft()
                self._sent_count += len(batch)
            self._current_backoff = BACKOFF_MIN_SEC
            self._next_retry_ts = 0.0
            self._set_state("up")
        else:
            with self._lock:
                self._failed_count += len(batch)
            self._next_retry_ts = time.monotonic() + self._current_backoff
            self._current_backoff = min(self._current_backoff * 2, BACKOFF_MAX_SEC)
            self._set_state("down")

    def _set_state(self, new_state: str) -> None:
        with self._lock:
            old_state = self._state
            if old_state == new_state:
                return
            self._state = new_state
        if new_state == "down":
            logger.warning(
                "EventBridge: REST недоступен (%s) — backoff=%.0fs", self._url, self._current_backoff
            )
        elif new_state == "up" and old_state != "unknown":
            logger.info("EventBridge: REST снова доступен (%s)", self._url)

    # ------------------------------------------------------------------
    # Diagnostics (get_diagnostics.event_bridge, Задача 5)
    # ------------------------------------------------------------------

    def get_diagnostics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "state": self._state,
                "queue_depth": len(self._queue),
                "sent": self._sent_count,
                "dropped": self._dropped_count,
                "failed": self._failed_count,
            }
```

- [x] **Шаг 4: Тесты зелёные**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_event_bridge.py -v`
Expected: 12 passed.

- [x] **Шаг 5: flake8 + ubuntu-parity + dead-module guard**

Run: `.venv_krab_ear/bin/flake8 KrabEar/backend/event_bridge.py KrabEar/tests/test_event_bridge.py --max-line-length=150`
Expected: пусто.
Run: `bash scripts/pre_merge_py312_check.sh KrabEar/tests/test_event_bridge.py`
Expected: ALL GREEN (модуль чисто-Python, `requests` уже в requirements.txt).
Run: `python3 scripts/audit_dead_extracted_modules.py`
Expected: на этом шаге `event_bridge.py` ЕЩЁ будет висеть как "не импортирован нигде в проде" —
это ОЖИДАЕМО до Задачи 5 (там модуль подключается в service.py). Не блокирует эту задачу;
финальная проверка — в Задаче 8.

- [x] **Шаг 6: Commit**

```bash
git add KrabEar/backend/event_bridge.py KrabEar/tests/test_event_bridge.py
git commit -m "feat(event-bridge): EventBridge sender (deque+backoff+diagnostics), unit tests"
```

**Критерий готовности:** 12 юнит-тестов зелёные, flake8 чист, ubuntu-parity пройден,
никакого реального сетевого вызова в тестах (все через инжектируемый `post_fn`).

---

### Задача 4: REST `POST /internal/event` (loopback + токен, fail-closed) + контракт-тесты

**Цель:** новый эндпоинт в `backend/rest_server.py`, принимающий батч конвертов от
моста и ре-эмитящий их через `EventBus.emit_envelope()` (Задача 2).

**Файлы:**
- Modify: `KrabEar/backend/rest_server.py`
- Create: `KrabEar/tests/test_rest_internal_event.py`

- [x] **Шаг 1: Failing-тесты первыми**

`KrabEar/tests/test_rest_internal_event.py`:

```python
"""test_rest_internal_event.py — POST /internal/event контракт
(spec docs/superpowers/specs/2026-07-07-event-bridge-design.md §2.2).

Loopback-only (403) + bridge-токен (401), НЕЗАВИСИМО от
REST_API_AUTH_ENABLED/REST_API_KEY. Валидный батч -> EventBus.emit_envelope()
на каждый элемент; невалидный элемент — скип + WARN, не 500.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_internal_event.py -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_REST_AVAILABLE = False
_rest_mod = None
try:
    import flask  # noqa: F401

    _import_engine = MagicMock()
    _import_store = MagicMock()
    _import_store.load_vocabulary.return_value = []
    _import_store.load_settings.return_value = {}
    _import_transcriber = MagicMock()

    with patch("core.engine.AudioEngine", return_value=_import_engine), \
            patch("backend.state_store.StateStore", return_value=_import_store), \
            patch("backend.transcriber.Transcriber", return_value=_import_transcriber):
        import backend.rest_server as _rest_mod

    _REST_AVAILABLE = True
except Exception:  # pragma: no cover
    pass


@unittest.skipUnless(_REST_AVAILABLE, "Flask/rest_server зависимости недоступны")
class InternalEventEndpointTestCase(unittest.TestCase):
    def setUp(self):
        _rest_mod.app.config["TESTING"] = True
        self.client = _rest_mod.app.test_client()
        _rest_mod._event_bridge_token_cache = None  # сброс module-level lazy cache между тестами
        self._token = "test-bridge-token-0123456789abcdef"

    def _post(self, events, token=None, remote_addr="127.0.0.1"):
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return self.client.post(
            "/internal/event",
            json={"events": events},
            headers=headers,
            environ_overrides={"REMOTE_ADDR": remote_addr},
        )

    def test_non_loopback_rejected_403(self):
        with patch("backend.rest_server._get_event_bridge_token", return_value=self._token):
            resp = self._post([], token=self._token, remote_addr="8.8.8.8")
        self.assertEqual(resp.status_code, 403)

    def test_missing_token_file_rejected_401(self):
        with patch("backend.rest_server._get_event_bridge_token", return_value=None):
            resp = self._post([], token="whatever")
        self.assertEqual(resp.status_code, 401)

    def test_missing_authorization_header_rejected_401(self):
        with patch("backend.rest_server._get_event_bridge_token", return_value=self._token):
            resp = self._post([], token=None)
        self.assertEqual(resp.status_code, 401)

    def test_wrong_token_rejected_401(self):
        with patch("backend.rest_server._get_event_bridge_token", return_value=self._token):
            resp = self._post([], token="wrong-token-value")
        self.assertEqual(resp.status_code, 401)

    def test_valid_batch_emits_envelope_per_item(self):
        captured = []
        with patch("backend.rest_server._get_event_bridge_token", return_value=self._token), \
                patch.object(_rest_mod.event_bus, "emit_envelope", side_effect=lambda e: captured.append(e)):
            resp = self._post(
                [{"type": "krab_error", "ts": "2026-07-07T00:00:00+00:00", "data": {"code": "x"}}],
                token=self._token,
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["accepted"], 1)
        self.assertEqual(body["skipped"], 0)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["origin"], "ipc")
        self.assertEqual(captured[0]["type"], "krab_error")

    def test_malformed_item_skipped_not_500(self):
        with patch("backend.rest_server._get_event_bridge_token", return_value=self._token), \
                patch.object(_rest_mod.event_bus, "emit_envelope") as mock_emit:
            resp = self._post(
                [{"type": "ok", "ts": "t", "data": {}}, {"type": 123, "ts": "t", "data": {}}],
                token=self._token,
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["accepted"], 1)
        self.assertEqual(body["skipped"], 1)
        self.assertEqual(mock_emit.call_count, 1)

    def test_events_not_a_list_rejected_400(self):
        with patch("backend.rest_server._get_event_bridge_token", return_value=self._token):
            resp = self.client.post(
                "/internal/event",
                json={"events": "not-a-list"},
                headers={"Authorization": f"Bearer {self._token}"},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [x] **Шаг 2: Прогнать — убедиться что падает**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_internal_event.py -v`
Expected: FAIL (404 — маршрут ещё не существует; либо `AttributeError` на `_get_event_bridge_token`).

- [x] **Шаг 3: Реализация в `backend/rest_server.py`**

Добавить СРАЗУ ПОСЛЕ блока `require_api_key` (кончается строкой 332, перед секцией
"F1: Magic byte validation" на строке 335):

```python
# ---------------------------------------------------------------------------
# EventBridge internal endpoint (spec 2026-07-07-event-bridge-design.md §2.2).
# Loopback-only + bridge-token auth — ВСЕГДА требуется, независимо от
# REST_API_AUTH_ENABLED/REST_API_KEY (require_api_key выше). Fail-closed.
# ---------------------------------------------------------------------------

_event_bridge_token_cache: str | None = None


def _get_event_bridge_token() -> str | None:
    """Ленивый кэшируемый читатель — REST НИКОГДА не создаёт токен, только читает.

    Кэшируется ТОЛЬКО успешный (непустой) результат: если IPC-процесс ещё не
    создал файл (порядок старта процессов произволен), последующие запросы
    продолжают проверять файл заново, а не залипают на None навсегда.
    """
    global _event_bridge_token_cache
    if _event_bridge_token_cache:
        return _event_bridge_token_cache
    from backend.event_bridge import read_bridge_token
    token = read_bridge_token(settings.DATA_DIR)
    if token:
        _event_bridge_token_cache = token
    return _event_bridge_token_cache


def _require_loopback_and_bridge_token(f):
    """Декоратор: /internal/event — loopback-only (403) + bridge-токен (401).

    Независим от REST_API_AUTH_ENABLED/REST_API_KEY — этот эндпоинт ВСЕГДА
    требует токен, даже если пользовательский REST auth выключен. Fail-closed:
    любая проверка не пройдена -> f() не вызывается.
    """
    @functools.wraps(f)
    def _wrapper(*args, **kwargs):
        remote_addr = request.remote_addr or ""
        if remote_addr not in ("127.0.0.1", "::1"):
            logger.warning("event_bridge: non-loopback remote_addr=%r отклонён", remote_addr)
            return jsonify({"error": "loopback only"}), 403
        token = _get_event_bridge_token()
        if not token:
            logger.warning("event_bridge: bridge-токен недоступен на REST-стороне")
            return jsonify({"error": "bridge token unavailable"}), 401
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid Authorization header"}), 401
        supplied = auth_header[len("Bearer "):]
        try:
            match = hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8"))
        except Exception:
            match = False
        if not match:
            logger.warning("event_bridge: неверный bridge-токен")
            return jsonify({"error": "invalid bridge token"}), 401
        return f(*args, **kwargs)
    return _wrapper
```

Затем найти блок `monitoring_blp = Blueprint(...)` (~строка 611) и добавить НОВЫЙ
маршрут в этот же блок, рядом с любым другим `@monitoring_blp.route(...)`
(например, сразу после `/health` или `/metrics`), ОБЯЗАТЕЛЬНО ДО строки
`api.register_blueprint(monitoring_blp)` (~строка 1626):

```python
@monitoring_blp.route("/internal/event", methods=["POST"])
@limiter.limit("600 per minute")  # щедрый лимит — легитимные батчи моста могут быть частыми
@_require_loopback_and_bridge_token
def internal_event():
    """Приём батча событий от EventBridge (IPC-процесс) -> re-emit в REST-шину.

    Body: {"events": [{"type": str, "ts": str, "data": dict}, ...]}
    Невалидный элемент — скип + WARN, не 500 (один плохой элемент не должен
    ронять весь батч).
    """
    body = request.get_json(silent=True) or {}
    events = body.get("events")
    if not isinstance(events, list):
        return jsonify({"error": "events must be a list"}), 400

    accepted = 0
    skipped = 0
    for env in events:
        if not isinstance(env, dict):
            skipped += 1
            continue
        etype = env.get("type")
        ts = env.get("ts")
        data = env.get("data")
        if not isinstance(etype, str) or not isinstance(ts, str) or not isinstance(data, dict):
            skipped += 1
            logger.warning("event_bridge: malformed envelope skipped: %r", env)
            continue
        try:
            event_bus.emit_envelope({"type": etype, "ts": ts, "data": data, "origin": "ipc"})
            accepted += 1
        except Exception:
            skipped += 1
            logger.warning("event_bridge: emit_envelope failed for type=%s", etype, exc_info=True)

    return jsonify({"ok": True, "accepted": accepted, "skipped": skipped}), 200
```

**Важно:** `event_bus` здесь — уже импортированный модуль-level объект
(`from backend.event_bus import bus as event_bus, sse_stream` на строке 30) —
НЕ создавать новый импорт.

- [x] **Шаг 4: Тесты зелёные**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_internal_event.py -v`
Expected: 7 passed.

- [x] **Шаг 5: Регрессия — существующие REST-тесты не сломаны**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_server_endpoints.py KrabEar/tests/test_rest_server_unit.py KrabEar/tests/test_rest_server.py -v`
Expected: все passed (новый код — чистое дополнение: новые module-level имена
`_event_bridge_token_cache`/`_get_event_bridge_token`/`_require_loopback_and_bridge_token`/`internal_event`,
не переопределяет ничего существующего).

- [x] **Шаг 6: flake8 + ubuntu-parity**

Run: `.venv_krab_ear/bin/flake8 KrabEar/backend/rest_server.py KrabEar/tests/test_rest_internal_event.py --max-line-length=150`
Expected: пусто.
Run: `bash scripts/pre_merge_py312_check.sh KrabEar/tests/test_rest_internal_event.py`
Expected: ALL GREEN.

- [x] **Шаг 7: Commit**

```bash
git add KrabEar/backend/rest_server.py KrabEar/tests/test_rest_internal_event.py
git commit -m "feat(event-bridge): POST /internal/event — loopback+token auth, batch ingest"
```

**Критерий готовности:** 7 контракт-тестов зелёные, existing REST test suite
не регрессирует, flake8/ubuntu-parity чисты.

---

### Задача 5: Проводка в `service.py` + настройки + diagnostics + close() + purge-allowlist

**Цель:** реально ПОДКЛЮЧИТЬ EventBridge к жизненному циклу `BackendService`
(конструктор → add_listener → start(); close() → stop()), добавить настройки
`EVENT_BRIDGE_ENABLED`/`REST_SERVER_PORT`, диагностику, и закрыть
`audit_purge_coverage` гард, если он увидит `event_bridge_token`.

**Файлы:**
- Modify: `KrabEar/core/config.py`
- Modify: `KrabEar/backend/service.py`
- Modify: `KrabEar/backend/health_check_service.py`
- Modify: `scripts/purge_coverage_allowlist.txt` (условно, см. Шаг 6)
- Create: `KrabEar/tests/test_event_bridge_wiring.py`

- [x] **Шаг 1: Failing source-контракт тест первым (урок setupErrorBus/setupHealthMonitor:
      collaborator сконструирован, но не вызывается = декоративная проводка — тот же
      класс бага, что и на Swift-стороне, здесь механически проверяется через grep
      исходника, не конструируя тяжёлый `BackendService`)**

`KrabEar/tests/test_event_bridge_wiring.py`:

```python
"""test_event_bridge_wiring.py — source-контракт: EventBridge реально ПОДКЛЮЧЁН
к жизненному циклу BackendService (класс бага setupErrorBus/setupHealthMonitor,
Swift-сторона 2026-07-05: collaborator существовал, но никогда не вызывался в
проде при 100% зелёных изолированных тестах). Механическая grep-проверка —
дополняет (не заменяет) end-to-end доказательство в scripts/e2e_event_bridge_smoke.py
(Задача 6) и scripts/audit_decorative_wiring.py --strict (CI guard).

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_event_bridge_wiring.py -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_SERVICE_SRC = (PROJECT_ROOT / "backend" / "service.py").read_text(encoding="utf-8")
_HEALTH_SRC = (PROJECT_ROOT / "backend" / "health_check_service.py").read_text(encoding="utf-8")


class EventBridgeWiringSourceContractTestCase(unittest.TestCase):
    def test_event_bridge_constructed_in_init(self):
        self.assertIn("self._event_bridge = EventBridge(", _SERVICE_SRC)

    def test_event_bridge_registered_as_listener(self):
        self.assertIn("event_bus.add_listener(self._event_bridge.on_event)", _SERVICE_SRC)

    def test_event_bridge_started(self):
        self.assertIn("self._event_bridge.start()", _SERVICE_SRC)

    def test_event_bridge_stopped_in_close(self):
        close_start = _SERVICE_SRC.index("def close(self)")
        close_body = _SERVICE_SRC[close_start:close_start + 3000]
        self.assertIn("_event_bridge", close_body)
        self.assertIn(".stop()", close_body)

    def test_event_bridge_passed_to_health_check_service(self):
        self.assertIn("event_bridge=self._event_bridge", _SERVICE_SRC)

    def test_health_check_service_exposes_event_bridge_in_diagnostics(self):
        self.assertIn('"event_bridge"', _HEALTH_SRC)


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [x] **Шаг 2: Прогнать — убедиться что падает**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_event_bridge_wiring.py -v`
Expected: FAIL (все 6 assertion — ни одна строка ещё не существует в исходниках).

- [x] **Шаг 3: `core/config.py` — новые настройки**

Добавить в класс `Settings` рядом с блоком `DISK_MONITOR_ENABLED` (~строка 72-76):

```python
    # --- Event-мост IPC->REST (backend/event_bridge.py, spec 2026-07-07) -------
    # True = EventBridge доставляет события из IPC-процесса в REST-процесс.
    # Killswitch, читается ОДИН РАЗ при старте (как DISK_MONITOR_ENABLED) —
    # НЕ live-toggle через set_settings.
    EVENT_BRIDGE_ENABLED: bool = True
```

Добавить где-то в верхней части класса (например, рядом с `DATA_DIR`, ~строка 95) —
**самостоятельное решение плана**, спека НЕ называет конкретное имя настройки для
порта, только требует не хардкодить его в двух местах (см. «Открытые вопросы»):

```python
    # --- REST server bind port (backend/rest_server.py) -------------------------
    # ОДИН источник правды для порта REST — event_bridge.py читает то же самое
    # значение, чтобы не дублировать литерал 5005 в двух модулях.
    REST_SERVER_PORT: int = 5005
```

- [x] **Шаг 4: `rest_server.py` — использовать `settings.REST_SERVER_PORT` вместо литерала**

Заменить (строка ~2067):
```python
        app.run(host="127.0.0.1", port=5005)
```
на:
```python
        app.run(host="127.0.0.1", port=settings.REST_SERVER_PORT)
```
(Опционально, не блокирует критерий готовности: также заменить литерал `5005` в
двух log-сообщениях рядом, строки ~2047/2054, на `settings.REST_SERVER_PORT` —
косметика, не функциональность.)

- [x] **Шаг 5: `health_check_service.py` — принять `event_bridge` коллаборатора**

В `TYPE_CHECKING`-блок импортов (~строка 19-27) добавить:
```python
    from backend.event_bridge import EventBridge
```

В `__init__` (~строка 35-50) добавить параметр (после `metrics_collector`):
```python
        event_bridge: "EventBridge | None" = None,
```
и сохранить: `self._event_bridge = event_bridge` (рядом с `self._metrics_collector = metrics_collector`).

Добавить новый метод (рядом с `_get_metrics_summary`, ~строка 189, тот же
защитный паттерн W1685 F5 — никогда не роняет `get_diagnostics`):
```python
    def _get_event_bridge_summary(self) -> dict[str, Any]:
        """Возвращает EventBridge.get_diagnostics() либо schema-parity fallback.

        Никогда не роняет get_diagnostics (аналог _get_metrics_summary, W1685 F5).
        """
        if self._event_bridge is None:
            return {
                "enabled": False, "state": "disabled",
                "queue_depth": 0, "sent": 0, "dropped": 0, "failed": 0,
            }
        try:
            return self._event_bridge.get_diagnostics()
        except Exception:
            logger.warning("HealthCheckService: EventBridge.get_diagnostics() упал", exc_info=True)
            return {
                "enabled": False, "state": "error",
                "queue_depth": 0, "sent": 0, "dropped": 0, "failed": 0,
            }
```

В `handle_get_diagnostics`'s возвращаемый dict (~строка 155-187) добавить новый
ключ (рядом с `"metrics_summary"`):
```python
            "event_bridge": self._get_event_bridge_summary(),
```

- [x] **Шаг 6: `service.py` — проводка в `__init__` и `close()`**

Импорт (рядом с `from backend.event_bus import bus as event_bus`, ~строка 75):
```python
from backend.event_bridge import EventBridge
```

В `__init__`, СРАЗУ ПОСЛЕ существующей строки
`self._settings_svc.register_after_save_hook(_on_privacy_mode_webhooks)`
(конец wave1775-блока, ~строка 674, ДО `self._sharing = SharingManager(...)`):

```python
        # Event-мост IPC -> REST (spec 2026-07-07-event-bridge-design.md): доставляет
        # события ЛОКАЛЬНОЙ (IPC-процесса) шины в REST-процесс, откуда их уже
        # раздают существующие SSE/WS подписчики. Закрывает класс багов
        # "эмитится в IPC, слушается REST" (wake word/krab_error чинились
        # IPC-поллингом; rewriter_recovered/live_subs агентским путём — нет,
        # см. Задача 1 плана волны).
        self._event_bridge = EventBridge(settings=settings, data_dir=self.store.data_dir)
        try:
            event_bus.add_listener(self._event_bridge.on_event)
        except Exception:
            logger.exception("event-bridge: failed to wire EventBus listener")
        self._event_bridge.start()
```

В вызове конструктора `HealthCheckService(...)` (~строка 1037-1051) добавить
новый kwarg (после `metrics_collector=_metrics_singleton,`):
```python
            event_bridge=self._event_bridge,
```

В `close()`, СРАЗУ ПОСЛЕ блока остановки `PurgeScheduler` (~строка 1409-1415):
```python
        # Stop EventBridge sender daemon thread — mirrors DiskSpaceMonitor/
        # RecapScheduler/PurgeScheduler stop above (та же CI daemon-thread
        # teardown rule, feedback_backendservice_teardown_ci.md).
        event_bridge = getattr(self, "_event_bridge", None)
        if event_bridge is not None:
            try:
                event_bridge.stop()
            except Exception:
                logger.exception("EventBridge.stop() raised during close()")
```

- [x] **Шаг 7: Тесты зелёные**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_event_bridge_wiring.py -v`
Expected: 6 passed.

- [x] **Шаг 8: Регрессия — полный backend-тест-сьют (проверяет, что новый
      constructor-параметр/wiring не сломал ничего существующего)**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_backend_service.py KrabEar/tests/ -k "health_check or diagnostics" -v`
Expected: все passed. Если существующие тесты конструируют `HealthCheckService(...)`
напрямую без `event_bridge=` — они обязаны продолжать работать (параметр опционален,
default `None`).

- [x] **Шаг 9: Purge-coverage — запустить гард, добавить allowlist ТОЛЬКО если он реально нашёл гэп**

Run: `python3 scripts/audit_purge_coverage.py`

Если в выводе появляется `event_bridge_token` как gap (сканер ищет паттерн
`<data_dir-подобное имя> / "имя"` — `EventBridge` использует `self._data_dir /
EVENT_BRIDGE_TOKEN_FILENAME`, что должно матчиться) — добавить строку в
`scripts/purge_coverage_allowlist.txt`, в секцию "Compliance / security
infrastructure" (рядом с `privacy_audit.key`, ~строка 41):

```
event_bridge_token       # HMAC bridge-auth секрет между IPC/REST процессами (localhost-only trust boundary); не пользовательские данные, аналог privacy_audit.key
```

Затем перепрогнать: Run: `python3 scripts/audit_purge_coverage.py --fail-on-found`
Expected: exit 0 (либо `event_bridge_token` не был найден сканером вообще — тогда
allowlist НЕ нужен, ничего не менять в этом файле).

Примечание (спека §5): `handle_purge_all_data` НЕ должен удалять этот файл —
токен не пользовательские данные. Никакой код в `history_service.py` не меняется.

- [x] **Шаг 10: `make audit-wiring` (decorative-wiring guard)**

Run: `python3 scripts/audit_decorative_wiring.py --strict`
Expected: `self._event_bridge` не должен всплыть как "сконструирован, но нигде
не вызывается" — он вызывается в `close()` и передаётся в `HealthCheckService`.

- [x] **Шаг 11: flake8 + ubuntu-parity**

Run: `.venv_krab_ear/bin/flake8 KrabEar/core/config.py KrabEar/backend/service.py KrabEar/backend/health_check_service.py KrabEar/backend/rest_server.py KrabEar/tests/test_event_bridge_wiring.py --max-line-length=150`
Expected: пусто.
Run: `bash scripts/pre_merge_py312_check.sh KrabEar/tests/test_event_bridge_wiring.py`
Expected: ALL GREEN.

- [x] **Шаг 12: Commit**

```bash
git add KrabEar/core/config.py KrabEar/backend/service.py KrabEar/backend/health_check_service.py \
        KrabEar/backend/rest_server.py KrabEar/tests/test_event_bridge_wiring.py
# добавить scripts/purge_coverage_allowlist.txt ТОЛЬКО если Шаг 9 его реально изменил:
git add scripts/purge_coverage_allowlist.txt 2>/dev/null || true
git commit -m "feat(event-bridge): wire EventBridge into BackendService lifecycle + diagnostics"
```

**Критерий готовности:** все 6 source-контракт тестов зелёные, полный backend
regression suite не падает, `audit_decorative_wiring.py --strict` и
`audit_purge_coverage.py --fail-on-found` чисты.

---

### Задача 6: Двухпроцессный E2E + хаос-кейс

**Цель:** живое доказательство (не мок): реальный `service.py` + реальный
`rest_server.py` на temp data-dir → событие из IPC доходит до SSE ≤200мс (DoD
спеки, пункт 2); хаос-кейс REST убит/поднят — эмиттеры не блокируются, событие
доходит после восстановления.

**Файлы:**
- Create: `scripts/e2e_event_bridge_smoke.py`
- Create: `scripts/run_e2e_bridge_smoke.command`

- [x] **Шаг 1: `scripts/e2e_event_bridge_smoke.py` — клиент**

Структура (паттерн `scripts/e2e_ipc_smoke.py::call()` для IPC, `requests` для SSE-чтения):

```python
#!/usr/bin/env python3
"""e2e_event_bridge_smoke.py — двухпроцессный e2e для EventBridge.

Использование (вызывается из run_e2e_bridge_smoke.command, НЕ напрямую):
    python3 scripts/e2e_event_bridge_smoke.py <socket_path> <rest_base_url> <phase>

phase: "normal" | "after-kill" | "after-recovery"
  normal          — оба процесса живы: emit -> SSE приходит <=200мс, latency печатается.
  after-kill      — REST убит: IPC-emit не блокируется (быстрый ok=True ответ),
                    команда не проверяет SSE (некому слушать).
  after-recovery  — REST поднят заново (тот же порт/data-dir): новое событие
                    доходит в течение <= BACKOFF_MAX_SEC + запас (см. константы
                    backend/event_bridge.py).

Триггер события: report_paste_failure IPC-метод (безопасный, без side-effects
на реальные данные — просто пушит KrabError в error_bus, который эмитит
"krab_error" на шину; backend/error_bus.py::push() -> event_bus.emit("krab_error", ...)).
"""
import json
import socket
import sys
import time

import requests


def call(sock_path: str, method: str, params: dict, timeout: int = 10) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(sock_path)
    req = json.dumps({"id": f"bridge-smoke-{method}", "method": method, "params": params}) + "\n"
    s.sendall(req.encode("utf-8"))
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))


def wait_for_sse_event(base_url: str, filter_type: str, timeout_sec: float) -> tuple[dict | None, float]:
    """Открывает SSE, возвращает (первый data-payload с этим типом | None, elapsed_sec)."""
    start = time.monotonic()
    try:
        with requests.get(f"{base_url}/v1/events?filter={filter_type}",
                           stream=True, timeout=timeout_sec + 2) as resp:
            for line in resp.iter_lines(decode_unicode=True):
                if time.monotonic() - start > timeout_sec:
                    return None, time.monotonic() - start
                if line and line.startswith("data: "):
                    return json.loads(line[len("data: "):]), time.monotonic() - start
    except requests.exceptions.RequestException:
        pass
    return None, time.monotonic() - start


def trigger_event(sock_path: str, marker: str) -> dict:
    """Вызывает report_paste_failure — безопасный, детерминированный триггер krab_error."""
    return call(sock_path, "report_paste_failure",
                {"reason": "ax_denied", "app_bundle": f"com.test.e2e.{marker}"})


def main() -> int:
    sock_path, base_url, phase = sys.argv[1], sys.argv[2], sys.argv[3]

    if phase == "normal":
        # Открыть SSE ДО эмита (иначе подписка не успеет зарегистрироваться).
        import threading
        result_holder = {}

        def _listen():
            result_holder["data"], result_holder["elapsed"] = wait_for_sse_event(
                base_url, "krab_error", timeout_sec=5.0
            )

        t = threading.Thread(target=_listen, daemon=True)
        t.start()
        time.sleep(0.5)  # дать SSE зарегистрироваться в EventBus
        t_emit = time.monotonic()
        resp = trigger_event(sock_path, "normal")
        if not resp.get("ok"):
            print(f"FAIL: report_paste_failure вернул ok=False: {resp}")
            return 1
        t.join(timeout=6.0)
        elapsed_ms = (time.monotonic() - t_emit) * 1000
        if result_holder.get("data") is None:
            print("FAIL: SSE-событие krab_error не пришло за 5с")
            return 1
        print(f"OK: событие пришло за {elapsed_ms:.1f}мс")
        if elapsed_ms > 200:
            print(f"WARN: latency {elapsed_ms:.1f}мс > 200мс (DoD-порог) — расследовать")
            return 1
        return 0

    if phase == "after-kill":
        t0 = time.monotonic()
        resp = call(sock_path, "report_paste_failure",
                    {"reason": "ax_denied", "app_bundle": "com.test.e2e.chaos"}, timeout=5)
        elapsed = time.monotonic() - t0
        if not resp.get("ok"):
            print(f"FAIL: IPC-вызов не вернул ok=True при мёртвом REST: {resp}")
            return 1
        if elapsed > 3.0:
            print(f"FAIL: IPC-вызов заблокировался на {elapsed:.1f}с при мёртвом REST (эмиттер НЕ должен блокироваться)")
            return 1
        print(f"OK: IPC-вызов не заблокирован при мёртвом REST ({elapsed:.2f}с)")
        return 0

    if phase == "after-recovery":
        # Backoff-потолок 30с (backend/event_bridge.py::BACKOFF_MAX_SEC) + запас.
        result, elapsed = wait_for_sse_event(base_url, "krab_error", timeout_sec=40.0)
        # Триггерим НОВОЕ событие ПОСЛЕ того как SSE начал слушать (иначе гонка).
        # (run_e2e_bridge_smoke.command вызывает trigger ПЕРЕД этой фазой — см. скрипт).
        if result is None:
            print("FAIL: событие не дошло после восстановления REST за 40с")
            return 1
        print(f"OK: событие дошло после восстановления REST за {elapsed:.1f}с")
        return 0

    print(f"FAIL: неизвестная фаза {phase!r}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

**Примечание по гонке в `after-recovery`:** т.к. SSE-подписка и IPC-триггер должны
запускаться в правильном порядке (подписка ПЕРВОЙ), Шаг 2 (командный скрипт)
запускает `wait_for_sse_event` в фоне (```&```) ПЕРЕД повторным вызовом
`report_paste_failure`, аналогично `phase=normal`. Реализация ЭТОЙ детали (запуск
слушателя в фоне из shell, затем эмит) — на усмотрение исполнителя Задачи 6;
альтернатива — добавить `phase=after-recovery` внутреннюю логику, аналогичную
`phase=normal` (слушать в отдельном треде, затем эмитить), что проще и надёжнее —
**рекомендуется** вместо описанного выше внешнего разделения.

- [x] **Шаг 2: `scripts/run_e2e_bridge_smoke.command` — оркестратор**

```bash
#!/bin/bash
# run_e2e_bridge_smoke.command — двухпроцессный e2e-смок EventBridge (Волна 2).
#
# Поднимает THROWAWAY dev-backend (service.py) + rest_server.py на общий temp
# data-dir и СЛУЧАЙНЫЙ свободный порт (избегает конфликта с прод-REST на 5005,
# если он уже запущен через launchd). Проверяет: (1) нормальную доставку
# IPC->REST->SSE <=200мс, (2) хаос-кейс (REST убит -> IPC не блокируется),
# (3) восстановление (REST поднят заново -> новое событие доходит).
#
# Exit 0 только если ВСЕ три фазы прошли.

set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

VENV="$REPO/.venv_krab_ear"
PY="$VENV/bin/python"
[ -x "$PY" ] || { echo "ERROR: venv python не найден: $PY"; exit 1; }

DATADIR="$(mktemp -d /tmp/krab_ear_bridge_e2e.XXXXXX)"
SOCK="$DATADIR/krabear.sock"

# Свободный порт — не хардкодим 5005, чтобы не конфликтовать с реальным launchd REST.
REST_PORT="$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"

IPC_PID=""; REST_PID=""
cleanup() {
  for pid in "$IPC_PID" "$REST_PID"; do
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && kill -TERM "$pid" 2>/dev/null
  done
  sleep 1
  for pid in "$IPC_PID" "$REST_PID"; do
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null
  done
  rm -rf "$DATADIR"
}
trap cleanup EXIT INT TERM

start_ipc() {
  PYTHONPATH="$REPO/KrabEar" KRAB_EAR_REST_SERVER_PORT="$REST_PORT" \
    "$PY" KrabEar/main.py --data-dir "$DATADIR" > "$DATADIR/ipc.log" 2>&1 &
  IPC_PID=$!
  for _ in $(seq 1 40); do [ -S "$SOCK" ] && break; sleep 0.5; done
  [ -S "$SOCK" ] || { echo "FATAL: IPC-сокет не появился"; tail -30 "$DATADIR/ipc.log"; exit 1; }
  sleep 2
}

start_rest() {
  KRAB_EAR_DATA_DIR="$DATADIR" KRAB_EAR_REST_SERVER_PORT="$REST_PORT" \
    PYTHONPATH="$REPO/KrabEar" "$PY" KrabEar/backend/rest_server.py \
    > "$DATADIR/rest.log" 2>&1 &
  REST_PID=$!
  for _ in $(seq 1 40); do
    curl -s -o /dev/null "http://127.0.0.1:$REST_PORT/health" && break
    sleep 0.5
  done
}

echo "==> data-dir=$DATADIR rest-port=$REST_PORT"
echo "==> Запуск IPC-процесса"
start_ipc
echo "==> Запуск REST-процесса"
start_rest

rc=0

echo ""
echo "==> Фаза 1: нормальная доставка (latency <=200мс)"
PYTHONPATH="$REPO/KrabEar" "$PY" scripts/e2e_event_bridge_smoke.py "$SOCK" "http://127.0.0.1:$REST_PORT" normal || rc=1

echo ""
echo "==> Хаос: убиваем REST-процесс"
kill -TERM "$REST_PID" 2>/dev/null; sleep 1; kill -KILL "$REST_PID" 2>/dev/null; REST_PID=""

echo "==> Фаза 2: IPC не блокируется при мёртвом REST"
PYTHONPATH="$REPO/KrabEar" "$PY" scripts/e2e_event_bridge_smoke.py "$SOCK" "http://127.0.0.1:$REST_PORT" after-kill || rc=1

# Проверяем ровно один WARN о смене состояния (down) в IPC-логе.
down_warns=$(grep -c "EventBridge: REST недоступен" "$DATADIR/ipc.log" || true)
if [ "$down_warns" -ne 1 ]; then
  echo "FAIL: ожидался 1 WARN о переходе в down, найдено: $down_warns"
  rc=1
else
  echo "OK: ровно 1 WARN о смене состояния (down)"
fi

echo ""
echo "==> Восстановление: поднимаем REST заново на том же порту/data-dir"
start_rest

echo "==> Фаза 3: событие доходит после восстановления (<= ~35с backoff-потолок + запас)"
PYTHONPATH="$REPO/KrabEar" "$PY" scripts/e2e_event_bridge_smoke.py "$SOCK" "http://127.0.0.1:$REST_PORT" after-recovery || rc=1

echo ""
if [ "$rc" -eq 0 ]; then
  echo "============================================================"
  echo "  EVENT BRIDGE E2E: ALL GREEN"
  echo "============================================================"
else
  echo "============================================================"
  echo "  EVENT BRIDGE E2E: FAILURE — см. вывод выше; логи в $DATADIR"
  echo "============================================================"
  cp "$DATADIR/ipc.log" "/tmp/krab_ear_bridge_e2e_ipc_last_failure.log" 2>/dev/null
  cp "$DATADIR/rest.log" "/tmp/krab_ear_bridge_e2e_rest_last_failure.log" 2>/dev/null
fi
exit "$rc"
```

`chmod +x scripts/run_e2e_bridge_smoke.command`

- [x] **Шаг 3: Прогнать полный e2e**

Run: `bash scripts/run_e2e_bridge_smoke.command`
Expected: `EVENT BRIDGE E2E: ALL GREEN` (все 3 фазы + ровно 1 WARN на переход в down).

Если latency в Фазе 1 периодически превышает 200мс на медленной машине —
ЗАДОКУМЕНТИРОВАТЬ фактическое значение в этом плане (секция «Открытые вопросы»)
и не считать это блокером самого по себе (DoD спеки измеряет ЭТУ величину как
подтверждение работоспособности, а не как жёсткий SLA-гейт CI).

- [x] **Шаг 4: Регрессия — существующий e2e-смок не сломан**

Run: `bash scripts/run_e2e_smokes.command`
Expected: `ALL E2E SMOKES GREEN` (этот план не трогает `run_e2e_smokes.command`
и не меняет поведение существующих 37+5 проверок).

- [x] **Шаг 5: flake8 на новом скрипте**

Run: `.venv_krab_ear/bin/flake8 scripts/e2e_event_bridge_smoke.py --max-line-length=150`
Expected: пусто.

- [x] **Шаг 6: Commit**

```bash
git add scripts/e2e_event_bridge_smoke.py scripts/run_e2e_bridge_smoke.command
git commit -m "test(event-bridge): two-process e2e + chaos case (REST kill/recover)"
```

**Критерий готовности:** `run_e2e_bridge_smoke.command` зелёный (все 3 фазы),
существующий `run_e2e_smokes.command` не регрессирует. Это ОДНОВРЕМЕННО
переоценивает факт Задачи 1 через тот же механизм (`live_subs.result` конкретно
НЕ повторно проверяется здесь синтетическим PCM — этот e2e использует
`report_paste_failure`/`krab_error` как более простой детерминированный триггер;
если нужно ИМЕННО повторно доказать `live_subs.result` после того как код готов,
это дополнительный ручной прогон Задачи-1-style команд, не обязательный для DoD
этой задачи).

---

### Задача 7: Swift — удалить доккомент «известный гэп» + сборка

**Цель:** `rewriter_recovered` flash-green теперь оживает мостом — удалить
доккомент, который документировал его как известный мёртвый путь (спека §2.4, DoD п.4).

**Файлы:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/main+HealthMonitor.swift`

- [x] **Шаг 1: Удалить блок строк 113-122 (проверено чтением — точный текст ниже)**

Найти и удалить ЭТОТ ТОЧНЫЙ блок (10 строк, включая начальную и конечную):

```swift
        // Phase B.1: подписываемся на rewriter_recovered SSE events → flashGreen.
        // 🔴 ИЗВЕСТНЫЙ ГЭП (2026-07-05, не в скоупе фикса setupHealthMonitor):
        // rewriter_recovered эмиттится LLMHttpProbe в IPC-процессе (service.py),
        // эта подписка слушает SSE /v1/events REST-процесса (:5005) — тот же
        // 2-EventBus гэп, что был у wake word и krab_error. Событие никогда не
        // доходит, flashGreen никогда не триггерится этим путём. Низкая
        // серьёзность (чисто косметический индикатор, не влияет на ping/
        // restart-логику HealthMonitor выше) — оставлено как есть; при фиксе
        // нужен IPC-поллинг аналог (нет готового IPC-метода статуса probe,
        // в отличие от list_recent_errors/wake_word_status).
```

Заменить одной нейтральной строкой (гэп закрыт, событие теперь доходит через мост):

```swift
        // Phase B.1: подписываемся на rewriter_recovered SSE events → flashGreen.
        // Событие доставляется REST-процессу через EventBridge (2026-07-07,
        // backend/event_bridge.py) — подписка больше не мёртвый путь.
```

Код ПОСЛЕ доккомента (подписка `monitor.subscribeToProbeEvents(...)`, строки
123-129 в исходной нумерации) **НЕ трогать** — он уже правильный, просто теперь
реально работает.

- [x] **Шаг 2: Сборка**

Run: `cd native/KrabEarAgent && swift build -c release 2>&1 | tail -5`
Expected: `Build complete!`

**🔴 НЕ запускать собранный бинарь напрямую** (`native/runtime/KrabEarAgent` или
`Krab Ear.app/Contents/MacOS/KrabEarAgent`) — `SingleInstanceGuard` убьёт
реальный работающий прод-агент пользователя. Допустимо: `codesign --verify`/`otool`.

- [x] **Шаг 3: Тесты не сломаны**

Run: `cd native/KrabEarAgent && swift test 2>&1 | tail -10`
Expected: 0 failures (ни один существующий тест не проверяет буквальный текст
удаляемого доккомента).

- [x] **Шаг 4: Commit**

```bash
git add native/KrabEarAgent/Sources/KrabEarAgent/main+HealthMonitor.swift
git commit -m "docs(swift): remove stale rewriter_recovered dead-gap comment (event-bridge closes it)"
```

**Критерий готовности:** `swift build -c release` и `swift test` зелёные, доккомент
удалён, никакой бинарь не запускался.

---

### Задача 8: Документация + финальная верификация всей волны

**Цель:** задокументировать новую поверхность (REST-эндпоинт + настройка +
диагностика — НЕ IPC-метод, поэтому не таблица методов, а прозаическая секция/callout)
и прогнать полный набор проверок по всей волне разом.

**Файлы:**
- Modify: `docs/IPC_API_REFERENCE.md`
- Modify: `CLAUDE.md`

- [ ] **Шаг 1: `docs/IPC_API_REFERENCE.md` — новая секция**

Добавить новую секцию `## Event-мост IPC→REST (2026-07-07)` (например, перед `## Misc`,
~строка 2683, или сразу после секции `## Launch Readiness (2026-06-27)`,
~строка 2423-2494 — рядом по духу, обе про инфраструктурные, не транскрипт-читающие
фичи) со следующим содержанием (полный текст, копировать буква-в-букву):

```markdown
## Event-мост IPC→REST (2026-07-07)

**НЕ IPC-метод** (не в dispatch table `service.py`) — REST-only внутренний
эндпоинт. Закрывает класс багов «событие эмитится в IPC-процессе (`service.py`),
подписчик слушает REST-процесс (`rest_server.py` :5005)» — жертвы: wake word /
`krab_error` (чинились IPC-поллингом), `rewriter_recovered` flash-green,
`live_subs.result` агентским путём.

`backend/event_bridge.py::EventBridge` подписывается на локальную шину
IPC-процесса (`event_bus.add_listener`), батчами (≤20 конвертов) POST-ит на
`POST /internal/event` (REST-процесс, loopback-only + bridge-токен
`<data_dir>/event_bridge_token`, права 0600, ВСЕГДА требуется независимо от
`REST_API_AUTH_ENABLED`/`REST_API_KEY`) → `EventBus.emit_envelope()` на
REST-стороне доставляет конверт КАК ЕСТЬ существующим SSE/WS подписчикам без
повторного вызова push-листенеров (структурный no-echo guard — вебхуки не
фаерятся дважды на одно событие).

Настройка `event_bridge_enabled` (default `True`, `KRAB_EAR_EVENT_BRIDGE_ENABLED`)
— killswitch, читается один раз при старте (как `disk_monitor_enabled`, НЕ
live-toggle через `set_settings`).

Наблюдаемость: `get_diagnostics.event_bridge` →
`{enabled, state, queue_depth, sent, dropped, failed}`
(`state`: `"unknown"`|`"up"`|`"down"`|`"disabled"`).

REST недоступен → экспоненциальный backoff 1→30с, WARN только по смене состояния
(не на каждое событие), эмиттеры никогда не блокируются, deque(256) drop-oldest
при переполнении. Однонаправлено (IPC→REST) — REST-originated события вебхуками
не форвардятся (известный пре-существующий гэп, вне скоупа этой волны).
```

Добавить соответствующую строку в оглавление документа (рядом с существующим
нумерованным списком секций, ~строка 62, формат `NN. [Название](#якорь)`).

- [ ] **Шаг 2: `CLAUDE.md` — одна запись в разделе Important Patterns**

Добавить bullet (после существующего "IPC dispatch error contract" или рядом с
"Voice Gateway bridge endpoints"):

```markdown
- **Event-мост IPC→REST (2026-07-07)**: `backend/event_bridge.py::EventBridge` закрывает класс багов «событие эмитится в IPC-процессе, подписчик слушает REST-процесс (`:5005`)» — подписывается на локальную (IPC) шину, батчами (≤20) POST-ит `POST /internal/event` (REST, loopback-only + bridge-токен `<data_dir>/event_bridge_token`, 0600, всегда требуется независимо от `REST_API_AUTH_ENABLED`) → `EventBus.emit_envelope()` доставляет конверт КАК ЕСТЬ существующим SSE/WS подписчикам без повторного вызова push-листенеров (no-echo guard). Killswitch `event_bridge_enabled`/`KRAB_EAR_EVENT_BRIDGE_ENABLED` (default `True`) — читается один раз при старте (сиблинг `DISK_MONITOR_ENABLED`, не live-toggle). Диагностика: `get_diagnostics.event_bridge`. REST недоступен → backoff 1→30с, WARN по смене состояния, эмиттеры не блокируются, deque(256) drop-oldest. `main+HealthMonitor.swift` доккомментарий про мёртвый `rewriter_recovered`-гэп удалён — подписка теперь живая.
```

- [ ] **Шаг 3: Верификация доки**

Run: `python3 scripts/verify_claude_md.py`
Expected: OK.

- [ ] **Шаг 4: Commit доки**

```bash
git add docs/IPC_API_REFERENCE.md CLAUDE.md
git commit -m "docs(event-bridge): IPC_API_REFERENCE section + CLAUDE.md entry"
```

- [ ] **Шаг 5: Финальная верификация ВСЕЙ волны разом**

```bash
# Полный набор новых/изменённых тестов
PYTHONPATH=$(pwd)/KrabEar python -m pytest \
  KrabEar/tests/test_event_bus_emit_envelope.py \
  KrabEar/tests/test_event_bridge.py \
  KrabEar/tests/test_rest_internal_event.py \
  KrabEar/tests/test_event_bridge_wiring.py \
  -v

# flake8 по всем изменённым/новым файлам разом
.venv_krab_ear/bin/flake8 \
  KrabEar/backend/event_bridge.py KrabEar/backend/event_bus.py \
  KrabEar/backend/rest_server.py KrabEar/backend/service.py \
  KrabEar/backend/health_check_service.py KrabEar/core/config.py \
  KrabEar/tests/test_event_bus_emit_envelope.py KrabEar/tests/test_event_bridge.py \
  KrabEar/tests/test_rest_internal_event.py KrabEar/tests/test_event_bridge_wiring.py \
  scripts/e2e_event_bridge_smoke.py \
  --max-line-length=150

# ubuntu-parity автообнаружением изменённых тестовых файлов
make pre-merge-check

# Все статические аудиты разом (orphans, dead-modules, decorative-wiring,
# purge-coverage, path-containment, dispatch-test-targets, ipc-drift, ...)
make audit-all

# Существующий e2e-смок — не регрессировал
bash scripts/run_e2e_smokes.command

# Новый двухпроцессный e2e — зелёный
bash scripts/run_e2e_bridge_smoke.command

# Swift — билд и тесты
cd native/KrabEarAgent && swift build -c release 2>&1 | tail -5 && swift test 2>&1 | tail -10 && cd ../..
```

Expected: все шаги зелёные. Если `make audit-all` находит НЕ связанные с этой
волной pre-existing gaps (маловероятно, но возможно на зрелой кодовой базе) —
задокументировать их отдельно, не блокировать эту волну ими.

- [ ] **Шаг 6 (опционально, не блокирует DoD): обновление памяти/роадмапа**

Волна закрывает `docs/ROADMAP-2026H2.md` §2 «Волна 2». Исполняющая сессия может
(не обязана в рамках этого плана) обновить статус волны в роадмапе и записать
итог в `.remember`/memory-файлы проекта по существующей конвенции сессий — это
вне строгого DoD данного плана, но соответствует практике предыдущих волн.

**Критерий готовности всей волны:** все команды Шага 5 зелёные; DoD спеки §6
пункты 1 (Задача 1), 2 (Задача 6, latency), 4 (Задача 7), 5 (Задача 5+6) —
выполнены и подтверждены командами выше. Пункт 3 спеки (live subs агентским
путём с РЕАЛЬНЫМ захватом) — явно помечен спекой как owner-assisted, см.
«Открытые вопросы» ниже. Пункт 6 (релиз v2.6.0) — вне скоупа этого плана
(отдельный релизный шаг через существующий `release.yml`, см.
`docs/DISTRIBUTION.md`).

---

## Self-review (выполнен при написании плана)

- **Покрытие спеки:** §2.1 (EventBridge) → Задача 3; §2.2 (REST endpoint) →
  Задача 4; §2.3 (emit_envelope) → Задача 2; §2.4 (Swift) → Задача 7; §3 (Шаг 0
  аудит) → Задача 1; §4 (тесты) → распределены по Задачам 2/3/4/6; §5
  (безопасность/purge) → Задача 5 (Шаг 9); §6 (DoD) → сведено в критерий
  готовности Задачи 8; §7 (оценка) — не действие, справочно; §8 (вне скоупа) —
  ничего из перечисленного не реализовано (слияние процессов, wake
  word/krab_error обратно на SSE, вебхуки на REST-originated события,
  гарантированная доставка/replay моста).
- **Type consistency:** имена/сигнатуры согласованы между задачами —
  `EventBridge(settings, data_dir, post_fn=None)` одинаков в Задаче 3
  (реализация+тесты), Задаче 5 (вызов в service.py), Задаче 6 (косвенно, через
  реальный процесс); `read_bridge_token(data_dir)` используется идентично в
  Задаче 3 (определение) и Задаче 4 (импорт в rest_server.py);
  `get_diagnostics()` shape `{enabled, state, queue_depth, sent, dropped, failed}`
  одинаков в Задаче 3 (EventBridge) и Задаче 5 (fallback в HealthCheckService).
- **Константы:** все шесть констант из брифа (deque maxlen=256, батч ≤20,
  timeout=2, backoff 1→30s, WARN по смене состояния, origin:"ipc",
  hmac.compare_digest, 0600, secrets.token_hex(32)) перенесены буква-в-букву в
  Задачи 2-4 — сверено построчно с исходным брифом.
- **Placeholder-скан:** единственное намеренное «не полностью написанное» место —
  Задача 1 «Факт Шага 0» (обязан заполниться РЕАЛЬНЫМ результатом прогона, не
  придуман заранее — я не могу запускать серверы в рамках написания этого плана)
  и Задача 8 Шаг 6 (опциональный, явно помечен как necessary-не-blocking).

---

## Открытые вопросы к контролёру

Ниже — места, где план принял самостоятельное решение (спека либо не
специфицировала деталь, либо есть несколько разумных прочтений). Ни одно из них
не противоречит спеке буквально, но контролёр может пожелать другой выбор.

1. **Новая настройка `REST_SERVER_PORT` (core/config.py, Задача 5, Шаг 3).**
   Спека требует «порт REST — из существующей настройки/константы, не хардкод в
   двух местах», но такой настройки в кодовой базе НЕ существовало (`rest_server.py`
   хардкодил `5005` напрямую в `app.run()`). План добавляет новое поле
   `Settings.REST_SERVER_PORT: int = 5005`, используемое ОБОИМИ модулями
   (`rest_server.py` и `event_bridge.py`) — единственный способ избежать
   циклического импорта `event_bridge.py → rest_server.py → service.py →
   event_bridge.py` (rest_server.py уже импортирует `backend.service`). Если
   контролёр предпочитает другое место (например, module-level константа в
   отдельном файле типа `ipc_constants.py`) — легко перенести.

2. **`EVENT_BRIDGE_ENABLED` — Pydantic Settings-поле (читается один раз при
   старте), НЕ запись в `DEFAULT_SETTINGS`/live-toggle через `set_settings`
   (Задача 5).** Спека говорит только «уважает `KRAB_EAR_EVENT_BRIDGE_ENABLED`»,
   не требуя live UI-переключатель. План смоделировал это ТОЧНО как
   `DiskSpaceMonitor`/`DISK_MONITOR_ENABLED` (ближайший архитектурный сиблинг:
   background-daemon с on/off флагом). Если нужен live-toggle в будущей волне —
   потребуется отдельная миграция на `_get_runtime_setting`.

3. **`ts` в конверте `EventBridge.on_event()` — захватывается заново в момент
   вызова листенера, а НЕ исходный `ts`, который `EventBus.emit()` вычисляет
   внутри себя (Задача 3).** Текущий контракт `add_listener` передаёт листенерам
   только `(event_type, payload)`, БЕЗ `ts` — менять эту сигнатуру означало бы
   также трогать существующий `_forward_event_to_webhooks` (wave1775). План
   сохранил контракт нетронутым; `emit_envelope()` на REST-стороне честно НЕ
   перештамповывает то, что уже пришло от моста — гарантия «ts сохраняется»
   выполняется от точки захвата мостом, а не от точки исходного `emit()`.
   Разница — единицы миллисекунд, не наносекунд.

4. **Семантика ретрая при неудачном POST — конверты ОСТАЮТСЯ в очереди и
   переотправляются (peek-then-pop-on-success), а не выбрасываются после первой
   неудачи (Задача 3, `_drain_and_send`).** Прочтение спеки «потерянные при
   даунтайме события не доставляются задним числом» как относящегося ТОЛЬКО к
   естественной potere через `drop-oldest` при переполнении bounded deque, а не
   как «выбрасывать после первого неудачного POST» — иначе весь механизм
   backoff/ретрая был бы бессмысленным (зачем ждать и повторять, если данные уже
   выброшены на первой неудаче?).

5. **`@limiter.limit("600 per minute")` на `/internal/event`, не полный
   `@limiter.exempt` (Задача 4).** Спека вообще не упоминает rate-limiting для
   этого эндпоинта. Дефолтный лимит REST-сервера (60/мин) реалистично тесен для
   батчей легитимного трафика моста; план выбрал щедрый явный оверрайд (по
   образцу существующих `@limiter.limit(...)` на других роутах) вместо полного
   исключения — сохраняет defense-in-depth на случай бага в самом мосте
   (например, tight-loop без backoff).

6. **DoD пункт 3 спеки («Live subs агентским путём работают доказуемо — живой
   смок с реальным захватом») — спека САМА помечает это как owner-assisted шаг**
   (реальный `SystemAudioCapture`/ScreenCaptureKit захват на живом железе, не
   синтетический PCM). Задача 1 и Задача 6 доказывают backend-половину цепочки
   (IPC `live_subs_ingest`/`report_paste_failure` → мост → REST → SSE) с
   синтетическими данными; Swift-агентская половина (реальный системный звук →
   IPC) остаётся отдельным ручным шагом ПОСЛЕ мержа этой волны — не
   автоматизирована ни одной задачей этого плана, как и предписано спекой.

7. **`scripts/run_e2e_bridge_smoke.command` НЕ добавлен ни в один CI workflow**
   (остаётся manual/on-demand инструментом, как его сиблинг
   `run_e2e_smokes.command`, который тоже не в CI). Если контролёр хочет
   автоматический прогон на каждый push, затрагивающий `event_bridge.py`/
   `rest_server.py`/`service.py` — потребуется отдельное решение (доп. job в
   `krabear-ci.yml`), вне скоупа этого плана.
