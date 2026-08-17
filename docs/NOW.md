# NOW — что делать сейчас (Krab Ear)

Обновлено: 2026-08-17. Читай **этот файл + одну карточку волны**. Не читай `ROADMAP-2026H2.md` как очередь задач — это журнал. Как работать: [`EXECUTOR_PLAYBOOK.md`](EXECUTOR_PLAYBOOK.md).

## P0 interrupt — SIGSEGV `whisper-large-v3-turbo` (2026-08-16 16:21)

Не чинить в Главном Крабе. Коалиция краша: **`ai.krab.ear.rest`**. Каскад: `balanced` (turbo) → retry `whisper-large-v3-mlx` при confidence < 0.65 → retry turbo, параллельно LM Studio на 36 ГБ.

**P0a закрыта в коде (2026-08-16):** второй MLX-чекпоинт не грузится при `kern.memorystatus_vm_pressure_level >= 1` (и turbo→turbo skip); REST `/v1/stt/transcribe` — process-wide singleflight, 503 `stt_busy`. Карточка: [`docs/superpowers/plans/2026-08-16-p0-mlx-second-checkpoint.md`](superpowers/plans/2026-08-16-p0-mlx-second-checkpoint.md). `mlx_subprocess` — in-process watchdog, не изоляция PID.

**P0c закрыта и живая (2026-08-17):** `mlx_whisper` для REST в OS-worker. IPC-диктовка in-process. Карточка: [`docs/superpowers/plans/2026-08-16-p0c-mlx-whisper-worker.md`](superpowers/plans/2026-08-16-p0c-mlx-whisper-worker.md).

**P0d/P0e/P0f живые после `safe_backend_restart --with-rest` (2026-08-17 ~05:41):** backend pid 36646; REST pid **36880**, child `mlx_whisper_worker.py` pid 40064. `POST /v1/stt/transcribe` 200 за 0.53с (`persist_history=false`); P0a скипнула второй MLX-чекпоинт под vm_pressure. Карточки: P0d token reload, P0e `/v1/stream` singleflight, P0f dead-child respawn.

## База

- Репозиторий: [Pavua/Krab-Ear](https://github.com/Pavua/Krab-Ear)
- Default / прод-колея: **`codex/krab-ear-v2`** (ветки `main`/`master` нет)
- HEAD на момент записи: `a5b6f517` — `fix(stt): респавнить мёртвый mlx_whisper worker перед следующим STT (#1924)` (P0f). Sparkle-appcast: `b2aaf527`. Код релиза v2.11.0: `064467f6`.
- Новые волны: `git worktree add .worktrees/<slug> -b feat/<slug> origin/codex/krab-ear-v2`
- Последний Sparkle-тег: **v2.11.0 (2026-08-16)** — [GitHub Release](https://github.com/Pavua/Krab-Ear/releases/tag/v2.11.0). Dev-guard: Sparkle не трогает `.app` внутри git-дерева; ежедневка владельца — только `scripts/build_and_deploy.command` по явной просьбе.

## Следующая волна

**W2a — CLAUDE.md HealthMonitor: закрыта 2026-08-16** (sticky-hang уже в коде, починили drift доков).

**W2b — замер аномалии длительности: закрыта 2026-08-16.** Opt-in `debug_keep_dictation_wav` (дефолт выкл), каталог `debug_duration_wav/` + сидкар, CLI `scripts/measure_duration_anomaly.py`. Чанкер/GigaAM не патчили.

**W2c — REST `deadline_sec`: закрыта 2026-08-16 Ear + 2026-08-17 VG.** Optional form-поле на `POST /v1/stt/transcribe`, clamp [5, 120]. VG `KrabEarSTTEngine` шлёт `deadline_sec=25` при HTTP timeout 30.0 — [PR #229](https://github.com/Pavua/Krab-Voice-Gateway/pull/229), прод pid 72437.

**W3 — Sparkle v2.11.0: закрыта 2026-08-16.** `krab-ear-ci` зелёный на `064467f6` (три stale-теста подтянуты к прод: hang-kill 10с, пустые SIP-креды, spy на startup-recovery). Dispatch `release.yml -f version=2.11.0` → success. `debug_keep_dictation_wav` в проде не включать.

**P0a/P0c/P0d/P0e/P0f — SEGV turbo в REST: закрыты 2026-08-16/17.** Pressure-gate + POST singleflight (P0a); OS-worker (P0c); token reload (P0d); `/v1/stream` native STT под тот же singleflight (P0e); dead-child respawn (P0f, сиблинг GigaAM W1216 F1). Не `REST_IN_PROCESS_ENABLED`. IPC-диктовка in-process.

**Следующая:** нет назначенной волны. Живой REST на P0d/P0e/P0f (pid 36880). VG на `deadline_sec=25` (pid 72437). Не включать `REST_IN_PROCESS_ENABLED`. Не второй `safe_backend_restart` без нужды.

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
