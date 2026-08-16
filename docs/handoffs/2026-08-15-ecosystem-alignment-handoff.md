# HANDOFF: Krab Ear & Ecosystem Alignment

> **Устарело по «что дальше» (2026-08-16).** Сводка коммитов 15.08 (1V hang, SIP, in-process REST verify) остаётся фактом. **§4 ниже врала как очередь:** C2 Live Meeting Overlay и C3 Quick Capture **закрыты в июле 2026** (v2.9.0 / последующие волны). Не брать их в работу. Оперативный фронт: [`docs/NOW.md`](../NOW.md). Журнал: [`docs/ROADMAP-2026H2.md`](../ROADMAP-2026H2.md).

**Дата:** 2026-08-15  
**Ветка Krab Ear:** `codex/krab-ear-v2` (коммиты `bc5ee07b`, `4f63fcd3`, `54c6a9a1`, `349a1eed`, `d10e09c2`, `114846ca`, `f04594f7`)  
**Статус Krab Ear:** Все тесты и аудит-гейты зелёные (1486 Swift тестов, 95 telephony тестов, 36 rest-inprocess тестов, make audit-all OK, pre-merge ubuntu-parity OK).

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

3. **In-Process REST Архитектура (Фаза 2 слияния)**:
   - Верифицирован `InProcessRestServer` и тесты (`test_rest_inprocess_*.py` 36/36 OK).
   - Починен импорт в `test_rest_inprocess_runtime_toggle_S3_task9.py`.

4. **Локальная On-Device SIP Телефония (Zero-Cloud Cost)**:
   - Реализован `LocalSIPAdapter` (`backend/sip_local_adapter.py`) со стандартным контрактом `CallProvider` (`dial`, `hangup`, `get_call_status`, `list_active_calls`).
   - Зарегистрирован провайдер `PROVIDER_SIP_LOCAL = "sip_local"` в `call_provider_factory.py`.
   - Добавлены поля и валидация настроек SIP (`SIP_SERVER`, `SIP_PORT`, `SIP_USER`, `SIP_PASSWORD`, `SIP_FROM_NUMBER`, `SIP_PROXY`).
   - Полное покрытие unit-тестами (`test_sip_local_adapter.py`, `test_call_provider_factory.py` 95/95 OK).

---

## 2. Статус для сессии «Voice Gateway»

- **WS/REST контракты:** `/v1/stt`, `/v1/tts`, `/v1/stream` работают штатно.
- **Шина событий:** `EventType.LIVE_SUBS_RESULT` генерируется через EventBus/EventBridge без блокировок.
- **Телефония:** Krab Ear теперь поддерживает как облачные провайдеры (Telnyx, Twilio), так и локальный SIP (`sip_local`).

---

## 3. Статус для сессии «Главный Краб» (OpenClaw / Userbot)

- **Интеграция:** `telegram_bridge.py` передаёт `X-Krab-Web-Key` на порт `:8080`.
- **IPC канал:** сокет свободен, зависания `handle_request` исключены.
- **Хранилище:** `StateStore` (history + settings) синхронизирован и закрыт от гонок.

---

## 4. Следующие шаги в Krab Ear (Roadmap H2-2026)

~~1. C2 Live Meeting Overlay / C3 Quick Capture~~ — **НЕ ДЕЛАТЬ.** Обе волны закрыты: C2 целиком в релизе v2.9.0 (2026-07-16), C3a/C3b — 2026-07-17/19. HUD встречи и панель быстрой заметки уже в проде.

Актуально на 2026-08-16 (см. [`docs/NOW.md`](../NOW.md)):

1. **W1 гигиена репо** — карточка `docs/superpowers/plans/2026-08-16-w1-repo-hygiene.md` (CI-auto-issues, инвентарь веток/worktree; без массового delete).
2. **W2 стабильность ежедневки** — спека `docs/superpowers/specs/2026-08-16-w2-daily-stability-design.md`, код только после явного Approve.
3. Sparkle v2.11 / S3 REST canary / Developer ID — только по отдельному «да» владельца.

