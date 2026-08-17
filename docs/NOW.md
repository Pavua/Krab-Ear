# NOW — что делать сейчас (Krab Ear)

Обновлено: 2026-08-17. Читай **этот файл + одну карточку волны**. Не читай `ROADMAP-2026H2.md` как очередь задач — это журнал. Как работать: [`EXECUTOR_PLAYBOOK.md`](EXECUTOR_PLAYBOOK.md).

## P0 interrupt — SIGSEGV `whisper-large-v3-turbo` (2026-08-16 16:21)

Не чинить в Главном Крабе. Коалиция краша: **`ai.krab.ear.rest`**. Каскад: `balanced` (turbo) → retry `whisper-large-v3-mlx` при confidence < 0.65 → retry turbo, параллельно LM Studio на 36 ГБ.

**P0a закрыта в коде (2026-08-16):** второй MLX-чекпоинт не грузится при `kern.memorystatus_vm_pressure_level >= 1` (и turbo→turbo skip); REST `/v1/stt/transcribe` — process-wide singleflight, 503 `stt_busy`. Карточка: [`docs/superpowers/plans/2026-08-16-p0-mlx-second-checkpoint.md`](superpowers/plans/2026-08-16-p0-mlx-second-checkpoint.md). `mlx_subprocess` — in-process watchdog, не изоляция PID.

**P0c закрыта и живая (2026-08-17):** `mlx_whisper` для REST в OS-worker. Живой REST pid 84983, child `mlx_whisper_worker.py`; `POST /v1/stt/transcribe` 200 за 0.56с; P0a скипнула второй чекпоинт под vm_pressure. IPC-диктовка in-process. Карточка: [`docs/superpowers/plans/2026-08-16-p0c-mlx-whisper-worker.md`](superpowers/plans/2026-08-16-p0c-mlx-whisper-worker.md).

**P0d закрыта в коде (2026-08-17):** после парного kickstart `/internal/event` 12 мин сыпал 401 (протухший bridge-токен). REST на mismatch один раз перечитывает файл; EventBridge после неуспешного POST перечитывает токен и ретраит только если он сменился; `safe_backend_restart --with-rest` поднимает REST после IPC ping. Карточка: [`docs/superpowers/plans/2026-08-17-p0d-event-bridge-token-reload.md`](superpowers/plans/2026-08-17-p0d-event-bridge-token-reload.md). Живой REST на момент P0d мог быть на старом коде (токен уже 200, рестарт не делали).

**P0e закрыта в коде (2026-08-17):** native `/v1/stream` берёт тот же REST STT-singleflight, что POST. Гейт в `LiveSubsService._process_window` вокруг `transcribe` (`acquire(0)` — занято POST → дроп окна). `ingest()` не оборачивать (F3). IPC `LiveSubsService` без gate. Cloud `/v1/stream` не тронут. Карточка: [`docs/superpowers/plans/2026-08-17-p0e-stream-stt-singleflight.md`](superpowers/plans/2026-08-17-p0e-stream-stt-singleflight.md).

**P0f закрыта в коде (2026-08-17):** мёртвый mlx_whisper child (`poll() is not None`) больше не блокирует следующий REST STT. `MLXWhisperSession.start()` респавнит — сиблинг GigaAM W1216 F1. In-flight SEGV по-прежнему `MLXWorkerCrashed` наружу. Карточка: [`docs/superpowers/plans/2026-08-17-p0f-mlx-worker-respawn.md`](superpowers/plans/2026-08-17-p0f-mlx-worker-respawn.md).

## База

- Репозиторий: [Pavua/Krab-Ear](https://github.com/Pavua/Krab-Ear)
- Default / прод-колея: **`codex/krab-ear-v2`** (ветки `main`/`master` нет)
- HEAD на момент записи: `e33509d8` — `fix(stt): /v1/stream native STT под REST singleflight (#1923)` (P0e). Sparkle-appcast: `b2aaf527`. Код релиза v2.11.0: `064467f6`.
- Новые волны: `git worktree add .worktrees/<slug> -b feat/<slug> origin/codex/krab-ear-v2`
- Последний Sparkle-тег: **v2.11.0 (2026-08-16)** — [GitHub Release](https://github.com/Pavua/Krab-Ear/releases/tag/v2.11.0). Dev-guard: Sparkle не трогает `.app` внутри git-дерева; ежедневка владельца — только `scripts/build_and_deploy.command` по явной просьбе.

## Следующая волна

**W2a — CLAUDE.md HealthMonitor: закрыта 2026-08-16** (sticky-hang уже в коде, починили drift доков).

**W2b — замер аномалии длительности: закрыта 2026-08-16.** Opt-in `debug_keep_dictation_wav` (дефолт выкл), каталог `debug_duration_wav/` + сидкар, CLI `scripts/measure_duration_anomaly.py`. Чанкер/GigaAM не патчили.

**W2c — REST `deadline_sec`: закрыта 2026-08-16.** Optional form-поле на `POST /v1/stt/transcribe`, clamp [5, 120], глобальные 600 с не тронуты, `REST_IN_PROCESS_ENABLED` по-прежнему false. Карточка VG (`deadline_sec=25`) — в репо шлюза, не здесь.

**W3 — Sparkle v2.11.0: закрыта 2026-08-16.** `krab-ear-ci` зелёный на `064467f6` (три stale-теста подтянуты к прод: hang-kill 10с, пустые SIP-креды, spy на startup-recovery). Dispatch `release.yml -f version=2.11.0` → success. `debug_keep_dictation_wav` в проде не включать.

**P0a/P0c/P0d/P0e/P0f — SEGV turbo в REST: закрыты 2026-08-16/17.** Pressure-gate + POST singleflight (P0a); OS-worker (P0c); token reload (P0d); `/v1/stream` native STT под тот же singleflight (P0e); dead-child respawn (P0f, сиблинг GigaAM W1216 F1). Не `REST_IN_PROCESS_ENABLED`. IPC-диктовка in-process.

**Следующая:** нет назначенной волны. Живой REST подхватит P0d/P0e/P0f только после явного `scripts/safe_backend_restart.command --with-rest` (не kickstart под запись). Не включать `REST_IN_PROCESS_ENABLED`.

## Не делать

- Не чекаутить `audit/*` и не мержить PR [#1875](https://github.com/Pavua/Krab-Ear/pull/1875) (`krab_ru` hard-negatives — отрицательный результат).
- Не строить заново C2 Live Meeting / C3 Quick Capture — закрыты в июле 2026. Handoff 2026-08-15 по ним врёт.
- Не «чинить» HealthMonitor sticky-hang — вторая ступень (`setWedgeProbe` → `forceRestartBackend`) уже в проде. `CLAUDE.md` в этом месте устарел; правка — в спеке W2, не новый сторож.
- Не включать `REST_IN_PROCESS_ENABLED` в проде.
- Не запускать собранный `KrabEarAgent` из воркера (убьёт прод). Не `launchctl kickstart -k` под запись — только `scripts/safe_backend_restart.command`.
- Не `git add -A`. Не коммитить `wake_word_models/hard_negatives_raw/tts_phrases.json`.
- Не удалять remote-ветки `audit/*` пачкой. Не трогать Main Krab runtime и VG `.env`.
- Не второй EventBridge. Не возвращать wake word на SSE. Не дообучать `krab_ru` синтетикой.

## Уже закрыто (не очередь)

STT (mlx-whisper + GigaAM v3), диаризация, перевод, LLM-полировка, wake word `hey_jarvis`, Sparkle, EventBridge, C2, C3, M1/M2 (рубильник выкл), wake-word watchdog, R1/R2, 1V hang-kill 10с, LocalSIP, S56 shortcuts, ES TTS.
