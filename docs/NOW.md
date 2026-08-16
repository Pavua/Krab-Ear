# NOW — что делать сейчас (Krab Ear)

Обновлено: 2026-08-16. Читай **этот файл + одну карточку волны**. Не читай `ROADMAP-2026H2.md` как очередь задач — это журнал. Как работать: [`EXECUTOR_PLAYBOOK.md`](EXECUTOR_PLAYBOOK.md).

## P0 interrupt — SIGSEGV `whisper-large-v3-turbo` (2026-08-16 16:21)

Не чинить в Главном Крабе. Коалиция краша: **`ai.krab.ear.rest`**, Homebrew Python 3.14.6, `EXC_BAD_ACCESS` / `KERN_INVALID_ADDRESS at 0x2`, поток `whisper-large-v3-turbo`. KeepAlive уже поднял REST (`runs=2`).

Каскад в `logs/krab-ear-rest.err.log`: `balanced` → retry `whisper-large-v3-mlx` (confidence < 0.65) → retry turbo, **параллельно 10 LM Studio MLX** в личке Краба. Два Whisper + LM Studio на 36 ГБ — типичный триггер native SEGV.

Карточка: [`HANDOFF_WHISPER_TURBO_SEGV_2026-08-16_RU.md`](HANDOFF_WHISPER_TURBO_SEGV_2026-08-16_RU.md). Кратко: не грузить второй Whisper при vm_pressure; вынести `mlx_whisper` в worker-процесс (сейчас SEGV убивает весь REST); singleflight STT. Не stash/reset, не коммитить `wake_word_models/hard_negatives_raw/`.

## База

- Репозиторий: [Pavua/Krab-Ear](https://github.com/Pavua/Krab-Ear)
- Default / прод-колея: **`codex/krab-ear-v2`** (ветки `main`/`master` нет)
- HEAD на момент записи: `b2aaf527` — `release: appcast v2.11.0 [skip ci]` (код релиза = `064467f6`)
- Новые волны: `git worktree add .worktrees/<slug> -b feat/<slug> origin/codex/krab-ear-v2`
- Последний Sparkle-тег: **v2.11.0 (2026-08-16)** — [GitHub Release](https://github.com/Pavua/Krab-Ear/releases/tag/v2.11.0). Dev-guard: Sparkle не трогает `.app` внутри git-дерева; ежедневка владельца — только `scripts/build_and_deploy.command` по явной просьбе.

## Следующая волна

**W2a — CLAUDE.md HealthMonitor: закрыта 2026-08-16** (sticky-hang уже в коде, починили drift доков).

**W2b — замер аномалии длительности: закрыта 2026-08-16.** Opt-in `debug_keep_dictation_wav` (дефолт выкл), каталог `debug_duration_wav/` + сидкар, CLI `scripts/measure_duration_anomaly.py`. Чанкер/GigaAM не патчили.

**W2c — REST `deadline_sec`: закрыта 2026-08-16.** Optional form-поле на `POST /v1/stt/transcribe`, clamp [5, 120], глобальные 600 с не тронуты, `REST_IN_PROCESS_ENABLED` по-прежнему false. Карточка VG (`deadline_sec=25`) — в репо шлюза, не здесь.

**W3 — Sparkle v2.11.0: закрыта 2026-08-16.** `krab-ear-ci` зелёный на `064467f6` (три stale-теста подтянуты к прод: hang-kill 10с, пустые SIP-креды, spy на startup-recovery). Dispatch `release.yml -f version=2.11.0` → success. `debug_keep_dictation_wav` в проде не включать.

**Следующая: P0 interrupt сверху** (SEGV turbo / REST worker / singleflight STT). Не новая фича, пока P0 открыт.

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
