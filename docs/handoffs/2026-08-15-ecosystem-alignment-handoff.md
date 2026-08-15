# HANDOFF: Krab Ear & Ecosystem Alignment

**Дата:** 2026-08-15  
**Ветка Krab Ear:** `codex/krab-ear-v2` (коммиты `bc5ee07b`, `4f63fcd3`)  
**Статус Krab Ear:** Все тесты и аудит-гейты зелёные (66/66 Python тестов, 1486 Swift тестов, make audit-all OK, pre-merge ubuntu-parity OK).

---

## 1. Сводка изменений в Krab Ear (что сделано)

1. **Устранено 180с зависание `stop_recording` (Sentry KRAB-EAR-BACKEND-1V)**:
   - `MLX_HANG_HARD_KILL_SEC` снижен со 120.0с до 10.0с (`core/mlx_subprocess.py`).
   - `MLX_TRANSCRIBE_TIMEOUT_SEC` установлен в 45.0с (`core/config.py`).
   - В `core/engine.py` при `MLXTimeoutError` цикл по вариантам параметров прерывается немедленно без повторных попыток на зависшем GPU.
   - В `core/pipeline/stt_whisper_mlx_adapter.py` добавлен вызов через `MLXWatchdog.run_with_timeout`.
   - В `core/mlx_lock.py` добавлен `acquire_mlx_lock(timeout_sec=...)` с `MLXLockTimeoutError`.
   - В `backend/telegram_bridge.py` прокинут заголовок `X-Krab-Web-Key`.

2. **UI / Swift Realtime Overlay (Liquid Glass)**:
   - Аудит и проверка `RealtimeOverlayController.swift` и `KrabEarTheme.swift`.
   - Все 7 публичных сигнатур сохранены, `CALayer` без глифов (Glyph-Guard OK), `reduceMotion` соблюдён, сборка `swift build -c release` чистая.

3. **Стабильность Live-субтитров (`LiveSubsService`)**:
   - Асинхронный воркер STT со слотом «последний выигрывает» и таймаутом финального флаша 15с (`test_live_subs_service.py` 23/23 OK).

4. **Устранён flaky-тест**:
   - `test_models.py::TzAwareTimestampTestCase::test_timestamp_lexicographic_sort_still_works` переведён на `timedelta(seconds=1)`.

---

## 2. Статус для сессии «Voice Gateway»

- **WS/REST контракты:** `/v1/stt`, `/v1/tts`, `/v1/stream` работают штатно.
- **Шина событий:** `EventType.LIVE_SUBS_RESULT` генерируется через EventBus/EventBridge без блокировок.
- **Готовность:** Krab Ear готов обслуживать голосовой поток без зависания при пиковых нагрузках.

---

## 3. Статус для сессии «Главный Краб» (OpenClaw / Userbot)

- **Интеграция:** `telegram_bridge.py` передаёт `X-Krab-Web-Key` на порт `:8080`.
- **IPC канал:** сокет свободен, зависания `handle_request` исключены.
- **Хранилище:** `StateStore` (history + settings) синхронизирован и закрыт от гонок.

---

## 4. Следующие шаги в Krab Ear (Roadmap H2-2026)

1. **Фаза 2 архитектуры:** слияние REST-сервера в единый backend-процесс (`create_app(service)`).
2. **Локальная телефония (On-Device SIP):** подключение локального PJSIP/SIP-клиента через существующий `CallProvider` протокол.
3. **C2 Live Meeting Overlay / C3 Quick Capture:** дальнейшее расширение возможностей продуктивности.
