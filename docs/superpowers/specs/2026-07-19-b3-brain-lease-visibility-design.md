# B3: Brain-lease видимость — «кто держит LM Studio» (S-волна)

Дата: 2026-07-19 · Автор: Fable 5 (спека + самопроверка) · Статус: draft → execute
Источник: ROADMAP-2026H2 §3.3 «B3 brain-lease видимость: доделать F5 до видимого
"кто держит LM Studio" (menu-bar индикатор или строка в диагностике). S-волна,
кандидат в окно между волнами». Окно наступило: C3/S64 закрыты, M2 календарно
заблокирована до 2026-07-30.

## 1. Инвентаризация (проведена лично, 2026-07-19)

Что УЖЕ есть (анти-rebuild):
- `backend/brain_lease.py` — полный механизм: `acquire_brain_lease(owner, ttl_sec)`,
  `release_brain_lease(owner)`, **`current_lease_holder() -> dict|None`** (payload
  `{owner, pid, acquired_ts, exp_ts}` или None, если свободен/истёк; NEVER raises,
  graceful degradation задокументирована в докстринге модуля).
- Проводка: `recording_core_service.py` — start_recording → **release** («мозг
  Крабу, Ear занят STT/pyannote на том же Metal GPU»), stop_recording → **acquire**
  (TTL `llm_brain_lease_ttl_sec`=30с, под rewriter); `meeting_session_service.py`
  — acquire/release вокруг ITEMS_LLM-джобов встречи. Владельцы: `"krab_ear"`,
  `"krab"` (зеркало в Main Krab, кросс-процессный контракт
  `~/.openclaw/lm_studio_brain.lock`).
- Настройки: `llm_brain_lease_enabled` (True), `llm_brain_lease_ttl_sec` (30.0)
  в `DEFAULT_SETTINGS` (`config.py:1113-1121`).
- Swift-вкладка «Диагностика»: `onDiagnostics()` рендерит `get_diagnostics`
  generically через `formatNestedResult` → новая секция появится БЕЗ Swift-правок.

Чего НЕТ (реальный гэп — ровно видимость):
- `current_lease_holder()` не выведен ни в один IPC-метод (grep по `service.py`
  dispatch — 0 совпадений «brain»).
- В `get_diagnostics` (HealthCheckService) нет секции `brain_lease`.
- В status-меню агента нет строки о владельце мозга.

## 2. Дизайн

### 2.1 Backend — IPC `get_brain_lease_status {}` (новый, читающий)

Хендлер в `HealthCheckService` (там живут ping/health_check/get_diagnostics —
это диагностический метод той же семьи), запись в dispatch table `service.py`.

Ответ (schema-parity в обоих состояниях, все поля всегда присутствуют):
```json
{"ok": true,
 "enabled": true,            // runtime llm_brain_lease_enabled
 "held": true,               // есть непросроченный лиз
 "owner": "krab_ear",        // null когда held=false
 "pid": 12345,               // null когда held=false
 "acquired_ts": 1789000000.0,// null когда held=false
 "exp_ts": 1789000030.0,     // null когда held=false
 "seconds_left": 21.4}       // null когда held=false; max(0, exp_ts-now)
```
- Без privacy-гейта: только флаги/числа/имя процесса-владельца, никакого
  transcript-derived контента (класс `get_privacy_dashboard`).
- Абсолютный `lock_path` в ответ НЕ включать (урок `get_stt_model_status`
  #1814 — не светить абсолютные пути наружу).
- Никогда не кидает: `current_lease_holder()` сам NEVER raises; хендлер
  оборачивает чтение настроек в try/except → при сбое `enabled` честно
  best-effort, `ok:true`.

### 2.2 Backend — секция `brain_lease` в `get_diagnostics`

Тот же словарь (без внешнего `ok`) добавляется в `handle_get_diagnostics`
рядом с `event_bridge`/`wake_word_watchdog`. Один приватный билдер
`_build_brain_lease_summary()` используется обоими путями (без дублей —
класс `audit_dead_extracted_modules`/sibling-drift).

### 2.3 Swift — строка в status-меню

Паттерн `refreshQuickNotesSubmenu` (menuWillOpen → off-main IPC → main-UI),
файл `main+BrainLease.swift` (новый, extension AgentAppDelegate):
- Disabled `NSMenuItem` (info-строка, не кликабельна) в status-меню — вставка
  рядом с recap-карточкой; property `brainLeaseMenuItem` в `main.swift`
  (stored property не живёт в extension — прецедент C3a).
- `refreshBrainLeaseMenuItem()` вызывается из существующего
  `menuWillOpen` (main+MenuBarRecap.swift) — НЕТ фонового поллинга: строка
  свежая на момент открытия меню, ноль постоянного IPC-трафика.
- Текст: `LM Studio: свободен` | `LM Studio: Krab Ear · ещё 21с` |
  `LM Studio: Краб · ещё 12с`; при `enabled=false` — пункт скрыт
  (`isHidden=true`, меню не засоряем). IPC-провал → «LM Studio: —» (не
  скрывать: скрытие по ошибке маскирует умерший backend, а рядом уже есть
  status-dot).
- Маппинг owner → отображение: `krab_ear`→«Krab Ear», `krab`→«Краб»,
  прочее → как есть (forward-compat с новыми владельцами).
- Иконка: SF Symbol `brain.head.profile` через `NSImage(systemSymbolName:)`
  (глиф-гейт: только SF Symbols, никаких новых raw-глифов — класс AGENT-J/M).
  Symbol недоступен на рантайме → nil → текст без иконки (не крешит).
- AGENT-3: IPC строго off-main (`DispatchQueue.global` + `try? ipcClient.call`,
  как в `refreshQuickNotesSubmenu`); обновление NSMenuItem — `DispatchQueue.main`.

### 2.4 Вне скоупа (осознанно)
- Live-обновление строки при ОТКРЫТОМ меню (NSTimer в меню) — меню открыто
  секунды, menuWillOpen-снимка достаточно.
- Кнопка «отобрать лиз» — force-release чужого лиза опасен (GPU-конфликт,
  ради предотвращения которого лиз и построен).
- События lease.* на EventBus / SSE — поллинг-на-открытие дешевле; конвенция
  «новые события через SSE» тут не нужна, т.к. нет фонового подписчика.
- Правки Main Krab (зеркало уже пишет тот же lock-файл — контракт не меняется).

## 3. Тесты (TDD, RED→GREEN)

Python (`tests/test_brain_lease_status_B3.py`, tearDown с `service.close()` —
урок #1782; lock_path → tmpdir через `KRAB_EAR_BRAIN_LEASE_PATH` env,
обратимо в finally — урок sys.modules-стабов):
1. free → `{held:false, owner:null, ..., seconds_left:null}` + все ключи присутствуют.
2. held (пишем валидный payload напрямую через `acquire_brain_lease("krab",
   ttl_sec=60, lock_path=...)`) → owner/pid/seconds_left>0.
3. expired payload → `held:false` (не отдаём просроченного владельца).
4. `llm_brain_lease_enabled=false` в настройках → `enabled:false` (страховка,
   что читается runtime-настройка, не DEFAULT_SETTINGS — урок Wave 58).
5. dispatch: `"get_brain_lease_status"` есть в таблице (паттерн
   dispatch-invariant тестов); секция `brain_lease` присутствует в
   `handle_get_diagnostics` ответе.
Проверить registry-count/grep-класс тесты (урок «гонять source-dependent
тесты»): `make dispatch-tests` + счётчик хендлеров в CLAUDE.md (360 → 361).

Swift (`BrainLeaseMenuTests.swift`, source-contract — паттерн
QuickCaptureWiringTests):
1. `main+BrainLease.swift` содержит `refreshBrainLeaseMenuItem` и вызов
   `get_brain_lease_status` (AST-класс греп по вызову, не подстроке).
2. `menuWillOpen` в main+MenuBarRecap.swift вызывает
   `refreshBrainLeaseMenuItem()` (пин реальной проводки — класс
   «setupErrorBus определён, но не вызван»).
3. Форматтер строки (выделить чистую static-функцию
   `brainLeaseMenuTitle(from:)`) — юнит на 4 состояния: free/held-ear/
   held-krab/disabled + IPC-fail.

## 4. Порядок исполнения

Размер S → лично в сессии (worktree `feature/b3-brain-lease-visibility`,
база `4689698c`), TDD, полный цикл гейтов: py-тесты файла + ubuntu-parity
(`make pre-merge-check`) + `make audit-all` + `swift build -c release` +
`swift test` целиком + глиф-гейт диффа. Живой смок: IPC-вызов
`get_brain_lease_status` на прод-сокете до/после диктовки (лиз появляется
после стопа и истекает за TTL) + открыть status-меню, увидеть строку.
Доки: IPC_API_REFERENCE (метод), CLAUDE.md (счётчик 361 + строка про
main+BrainLease.swift), ROADMAP §3.3 отметка + журнал.

## 5. Самопроверка (Fable, до кода)

- `current_lease_holder()` может вернуть payload с отсутствующими ключами
  (сломанный JSON от чужого писателя) → хендлер обязан `.get()` с дефолтами
  и приводить типы (float/int) с fallback null — НЕ доверять schema чужого
  процесса (wire-формат урок).
- `seconds_left` считать от того же `time.time()`, что использовался в
  сравнении exp_ts — не два разных вызова (микро-гонка отрицательных значений;
  clamp max(0, …) всё равно оставить).
- Пункт меню — disabled, но НЕ скрытый при ошибках IPC (см. 2.3); скрытый
  только при enabled=false.
- В `menuWillOpen` уже два refresher'а — добавление третьего сохраняет
  порядок «каждый сам off-main», без общего блокирующего вызова.
- Счётчик IPC-методов в CLAUDE.md и dispatch-тесты — обновить В ТОМ ЖЕ
  коммите, что и dispatch-запись (два прошлых ubuntu-red были из-за этого).
