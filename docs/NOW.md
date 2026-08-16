# NOW — что делать сейчас (Krab Ear)

Обновлено: 2026-08-16. Читай **этот файл + одну карточку волны**. Не читай `ROADMAP-2026H2.md` как очередь задач — это журнал. Как работать: [`EXECUTOR_PLAYBOOK.md`](EXECUTOR_PLAYBOOK.md).

## База

- Репозиторий: [Pavua/Krab-Ear](https://github.com/Pavua/Krab-Ear)
- Default / прод-колея: **`codex/krab-ear-v2`** (ветки `main`/`master` нет)
- HEAD на момент записи: `5a6559df` — `feat(tts): add Spanish (es) voice synthesis and Mónica macOS fallback routing`
- Новые волны: `git worktree add .worktrees/<slug> -b feat/<slug> origin/codex/krab-ear-v2`
- Последний Sparkle-тег: **v2.10.0 (2026-07-19)** — канал отстаёт от HEAD; релиз не начинать без явного «релизы»

## Следующая волна

**W2a — CLAUDE.md HealthMonitor: закрыта 2026-08-16** (sticky-hang уже в коде, починили drift доков).

**W2b — замер аномалии длительности: закрыта 2026-08-16.** Opt-in `debug_keep_dictation_wav` (дефолт выкл), каталог `debug_duration_wav/` + сидкар, CLI `scripts/measure_duration_anomaly.py`. Чанкер/GigaAM не патчили.

**Следующая: W2c — per-request `deadline_sec` на REST STT** (бюджет VG, глобальные 600 с не трогать):
[`docs/superpowers/specs/2026-08-16-w2-daily-stability-design.md`](superpowers/specs/2026-08-16-w2-daily-stability-design.md) §2.

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
