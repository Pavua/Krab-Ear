# W1375 — AuditLogger residual audit (post W1352/W1353)

**Дата:** 2026-05-27  
**Файл:** `KrabEar/backend/audit_logger.py`  
**Контекст:** Re-audit после W1351 (первичный аудит), W1352 (wiring в BackendService), W1353 (расширение _SENSITIVE_METHODS 3→50).

---

## Состояние W1352 / W1353

| PR | Ветка | Статус |
|----|-------|--------|
| #1265 | `feat/wire-audit-logger-W1352` | **OPEN — NOT MERGED** |
| #1263 | `wire-audit-logger-W1352` | **OPEN — NOT MERGED** |
| #1270 | `extend-sensitive-methods-W1353` | **OPEN — NOT MERGED** |
| #1258 | `fix-audit-sensitive-methods-W1353` | **OPEN — NOT MERGED** |

**Вывод: оба фикса W1352 и W1353 не смерджены в `codex/krab-ear-v2`.**  
`service.py` по-прежнему не импортирует `AuditLogger` и не инстанцирует его.  
`_SENSITIVE_METHODS` в production содержит 3 записи, а не 50.

---

## Новые находки (5 шт.)

### F1 — CRITICAL: W1352 и W1353 не смерджены — AuditLogger мёртв в production

**Файл:** `KrabEar/backend/service.py`  
**Серьёзность:** CRITICAL (blocker для всех последующих фиксов)

`service.py` не содержит ни `from backend.audit_logger import AuditLogger`, ни инстанцирования `self._audit_logger`. `_SENSITIVE_METHODS` — только 3 метода. Все фиксы W1352/W1353 ожидают merge.

**Ожидаемое действие:** merge PR #1265 + PR #1270 (или аналогичные PR без конфликтов) перед любыми другими фиксами.

---

### F2 — MED: `_cleanup_old_files()` вызывается вне lock на каждый `log_request`

**Файл:** `KrabEar/backend/audit_logger.py`, строка 91  
**Серьёзность:** MED (ненужная нагрузка на FS + spurious warnings при concurrent cleanup)

```python
# Текущий код:
with self._lock:
    self._rotate_if_needed(today)
    ...  # write

self._cleanup_old_files()  # вне lock — выполняется при КАЖДОМ вызове
```

`_cleanup_old_files()` выполняет `glob("audit_*.ndjson")` на каждый IPC-запрос.  
Замеренная стоимость: ~34.7 мкс/вызов (на dir с 7 файлами).  
При 100 IPC/сек → ~3.5 мс/сек расходуется на бессмысленные glob-сканы.

При параллельных вызовах (несколько потоков IPC) оба потока могут одновременно попытаться вызвать `unlink()` на одном файле → `FileNotFoundError` → `logger.warning` (шум в логах).

**Рекомендация:** Rate-limit cleanup — вызывать только при смене даты (после реальной ротации):

```python
with self._lock:
    rotated = self._rotate_if_needed(today)  # возвращает bool
    ...  # write

if rotated:
    self._cleanup_old_files()  # только после ротации, ~раз в сутки
```

Или кэшировать timestamp последнего cleanup с TTL в 1 час.

---

### F3 — MED: Ротация только по числу файлов — размер не ограничен

**Файл:** `KrabEar/backend/audit_logger.py`, строки 26, 168–177  
**Серьёзность:** MED (unbounded disk growth)

```python
_KEEP_DAYS = 7  # только 7 файлов, ≤ _KEEP_DAYS дней
```

Ротация удаляет файлы старше 7 дней (по числу файлов), но не ограничивает их размер. Оценка роста при высокой нагрузке:

| Нагрузка | Байт/запись | Строк/день | Размер файла/день |
|----------|-------------|-----------|-------------------|
| 300 req/мин (пик) | ~180 B | 432 000 | ~78 MB |
| 50 req/мин (норм.) | ~180 B | 72 000 | ~13 MB |

При пиковой нагрузке 7 файлов = ~550 MB audit-данных.  
`DiskSpaceMonitor` отслеживает `history.ndjson` отдельно (`history_large`), но audit-файлы включены только в суммарный `data_dir_mb` без выделенного алерта.

**Рекомендация:**
1. Добавить `_MAX_AUDIT_MB_PER_FILE = 50` — при превышении ротировать принудительно (переименовать и открыть новый).
2. Или добавить `disk.audit_large` событие в `DiskSpaceMonitor`.

---

### F4 — LOW: `get_audit_log()` не имеет IPC-обработчика в `service.py`

**Файл:** `KrabEar/backend/service.py`  
**Серьёзность:** LOW (неполная функциональность)

Метод `AuditLogger.get_audit_log(limit, method_filter)` реализован и покрыт тестами, но не экспонирован через IPC. В `service.py` нет ни хэндлера `handle_get_audit_log`, ни записи в диспетчерской таблице.

Для сравнения: `get_privacy_audit_log` (строки 1221, 2331) — аналогичный метод для privacy_audit — IPC-хэндлер есть.

**Рекомендация:** Добавить `"get_audit_log": self._handle_get_audit_log` в таблицу диспетчеризации после merge W1352.

```python
def _handle_get_audit_log(self, params: dict) -> dict:
    limit = min(int(params.get("limit", 100)), 500)
    method_filter = params.get("method_filter")
    entries = self._audit_logger.get_audit_log(limit=limit, method_filter=method_filter)
    return {"entries": entries, "count": len(entries)}
```

---

### F5 — LOW: audit-файлы в `data_dir` без отдельного мониторинга размера

**Файл:** `KrabEar/backend/audit_logger.py`, метод `_audit_path`; `KrabEar/backend/disk_monitor.py`  
**Серьёзность:** LOW (observability gap)

`audit_YYYY-MM-DD.ndjson` файлы хранятся в `data_dir/` рядом с `history.ndjson`. `DiskSpaceMonitor._collect_status()` включает их в `data_dir_mb`, но не отчитывается о них отдельно (нет поля `audit_mb` в статусе).

`get_disk_status` IPC-ответ не показывает аудит-метрику:
```json
{"free_space_gb": 5.0, "history_mb": 120.0, "transcripts_mb": 800.0, ...}
// audit_mb отсутствует
```

Это делает невозможным диагностику случаев, когда audit-файлы аномально растут.

**Рекомендация:** Добавить в `DiskSpaceMonitor._collect_status()`:
```python
audit_mb = sum(
    f.stat().st_size for f in self._data_dir.glob("audit_*.ndjson")
    if f.is_file()
) / (1024 * 1024)
status["audit_mb"] = round(audit_mb, 3)
```

---

## Взаимодействие с privacy_audit (W974)

`audit_logger.py` и `privacy_audit.py` — концептуально разные логи:
- `privacy_audit.log` — внешний compliance-лог (режим конфиденциальности, purge)
- `audit_YYYY-MM-DD.ndjson` — операционный IPC-журнал

Отдельные файлы, отдельные локи, отдельные форматы. **Хэш-цепочки нет ни в одном из них** — это не блокчейн-лог, а append-only журнал. Нет риска кросс-контаминации.

Существенное отличие: `privacy_audit.py` использует `fcntl.flock` (межпроцессовая защита), а `audit_logger.py` — только `threading.Lock` (только внутрипроцессовая). При двух инстанциях backend (edge case) — concurrent writes без flock. На практике не возникает из-за `SingleInstanceGuard`, но архитектурно уязвимо.

---

## Покрытие тестами (post-W1352/W1353, ещё не смерджено)

| Тест-файл | Тестов | Что проверяет |
|-----------|--------|---------------|
| `test_audit_logger.py` | 30 | Базовая функциональность, ротация, thread safety |
| `test_audit_logger_rotation_deep.py` | ~40 | Ротация edge-cases, PermissionError, recovery |
| `test_wire_audit_logger_W1352.py` | 10 | Wiring в BackendService (в PR ветке, не смердж.) |
| `test_audit_sensitive_methods_W1353.py` | 23 | Sensitive methods redaction (в PR ветке) |

**Нет тестов для F2 (cleanup throttling), F3 (size overflow), F4 (IPC handler).**

---

## Приоритет действий

1. **CRIT:** Merge W1352 (PR #1265) + W1353 (PR #1270) — без этого AuditLogger мёртв
2. **MED:** Throttle `_cleanup_old_files()` — вызывать только при ротации (F2)
3. **MED:** Добавить размерный лимит ротации ~50 MB/файл (F3)
4. **LOW:** Добавить IPC handler `get_audit_log` (F4)
5. **LOW:** Добавить `audit_mb` в `DiskSpaceMonitor.get_status()` (F5)
