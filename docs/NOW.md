# NOW — что делать сейчас (Krab Ear)

Обновлено: 2026-08-18 (вечер). Читай **этот файл + одну карточку волны**. Не читай `ROADMAP-2026H2.md` как очередь задач — это журнал. Как работать: [`EXECUTOR_PLAYBOOK.md`](EXECUTOR_PLAYBOOK.md).

## P0 interrupt — SIGSEGV `whisper-large-v3-turbo` (2026-08-16 16:21)

Не чинить в Главном Крабе. Коалиция краша: **`ai.krab.ear.rest`**. Каскад: `balanced` (turbo) → retry `whisper-large-v3-mlx` при confidence < 0.65 → retry turbo, параллельно LM Studio на 36 ГБ.

**P0a закрыта в коде (2026-08-16):** второй MLX-чекпоинт не грузится при `kern.memorystatus_vm_pressure_level >= 1` (и turbo→turbo skip); REST `/v1/stt/transcribe` — process-wide singleflight, 503 `stt_busy`. Карточка: [`docs/superpowers/plans/2026-08-16-p0-mlx-second-checkpoint.md`](superpowers/plans/2026-08-16-p0-mlx-second-checkpoint.md). `mlx_subprocess` — in-process watchdog, не изоляция PID.

**P0c закрыта и живая (2026-08-17):** `mlx_whisper` для REST в OS-worker. IPC-диктовка in-process. Карточка: [`docs/superpowers/plans/2026-08-16-p0c-mlx-whisper-worker.md`](superpowers/plans/2026-08-16-p0c-mlx-whisper-worker.md).

**P0d/P0e/P0f живые после `safe_backend_restart --with-rest` (2026-08-17 ~05:41):** backend pid 36646; REST pid **36880**, child `mlx_whisper_worker.py` pid 40064. `POST /v1/stt/transcribe` 200 за 0.53с (`persist_history=false`); P0a скипнула второй MLX-чекпоинт под vm_pressure. Карточки: P0d token reload, P0e `/v1/stream` singleflight, P0f dead-child respawn.

## База

- Репозиторий: [Pavua/Krab-Ear](https://github.com/Pavua/Krab-Ear)
- Default / прод-колея: **`codex/krab-ear-v2`** (ветки `main`/`master` нет)
- HEAD на момент записи: `1dad53d5` — `fix(stt): подпись GigaAM v3 в list_stt_engines, карточка W7 (#1927)`. Sparkle-appcast: `b2aaf527`. Код релиза v2.11.0: `064467f6`. P0f в истории: `a5b6f517`.
- Новые волны: `git worktree add .worktrees/<slug> -b feat/<slug> origin/codex/krab-ear-v2`
- Последний Sparkle-тег: **v2.11.0 (2026-08-16)** — [GitHub Release](https://github.com/Pavua/Krab-Ear/releases/tag/v2.11.0). Dev-guard: Sparkle не трогает `.app` внутри git-дерева; ежедневка владельца — только `scripts/build_and_deploy.command` по явной просьбе.

## Следующая волна

**W2a — CLAUDE.md HealthMonitor: закрыта 2026-08-16** (sticky-hang уже в коде, починили drift доков).

**W2b — замер аномалии длительности: закрыта 2026-08-16.** Opt-in `debug_keep_dictation_wav` (дефолт выкл), каталог `debug_duration_wav/` + сидкар, CLI `scripts/measure_duration_anomaly.py`. Чанкер/GigaAM не патчили.

**W2c — REST `deadline_sec`: закрыта 2026-08-16 Ear + 2026-08-17 VG.** Optional form-поле на `POST /v1/stt/transcribe`, clamp [5, 120]. VG `KrabEarSTTEngine` шлёт `deadline_sec=25` при HTTP timeout 30.0 — [PR #229](https://github.com/Pavua/Krab-Voice-Gateway/pull/229), прод pid 72437.

**W3 — Sparkle v2.11.0: закрыта 2026-08-16.** `krab-ear-ci` зелёный на `064467f6` (три stale-теста подтянуты к прод: hang-kill 10с, пустые SIP-креды, spy на startup-recovery). Dispatch `release.yml -f version=2.11.0` → success. `debug_keep_dictation_wav` в проде не включать.

**P0a/P0c/P0d/P0e/P0f — SEGV turbo в REST: закрыты 2026-08-16/17.** Pressure-gate + POST singleflight (P0a); OS-worker (P0c); token reload (P0d); `/v1/stream` native STT под тот же singleflight (P0e); dead-child respawn (P0f, сиблинг GigaAM W1216 F1). Не `REST_IN_PROCESS_ENABLED`. IPC-диктовка in-process.

**GigaAM = v3 (не апгрейдить).** Прод `stt_gigaam_mode=v3_e2e_rnnt`. Лейбл IPC `list_stt_engines.display_name` = `"GigaAM v3 (RU)"` после [#1927](https://github.com/Pavua/Krab-Ear/pull/1927) и `safe_backend_restart` (живой IPC проверен). Голое `"rnnt"` в git-пакете алиасится в `v3_rnnt`.

**W7 — wake-word PortAudio после диктовки: закрыта 2026-08-17.** Карточка: [`docs/superpowers/plans/2026-08-17-wakeword-portaudio-after-dictation.md`](superpowers/plans/2026-08-17-wakeword-portaudio-after-dictation.md). Give-up кап больше не сбрасывается 1–2 тиками `last_chunk_ts` после kickstart (`notePoll`, 8 тиков ≈ 6 с). `wake_word_start` отвергается, пока worker рекордера ещё жив после `stop()` (`is_start_blocked=_reinit_is_recording_gate`). Не чинили сам PortAudio / `_listen_loop`. **Parity-бинари положены и задеплоены 2026-08-18 11:00** (`LC_UUID C015A3D8-3DD4-3FF9-8D67-C3D42043C993`, подпись «Krab Ear Dev Local», агент pid 80544, dSYM в Sentry). До этого прод бегал на бинаре от 08-12, то есть Swift-половина фикса не работала, а вместе с ней в прод впервые уехали `b8198311` (шорткаты ⌘1–⌘7) и `33a6c9ca` (Local SIP) — они лежали в git с 08-16.

**W8 — наблюдаемость заблокированного `wake_word_start` + честный heartbeat: закрыта и задеплоена 2026-08-18.** Карточка: [`docs/superpowers/plans/2026-08-18-w8-blocked-start-observability.md`](superpowers/plans/2026-08-18-w8-blocked-start-observability.md). Fable-ревью диапазона `e425c5ee..39f92f8b` нашло два дефекта W7, оба подтверждены построчным гейтом. (A, HIGH) Гейт W7 отвергал старт тем же reason, что и настоящая запись → Swift ретраил вечно, сессия не создавалась, watchdog принимал это за легитимную паузу и сбрасывал эпизод → `wedged` недостижим, путь `DEFERRED_WORKER_HUNG` (введён 2026-08-09 против «тихого бессрочного простоя») обойдён; пока worker рекордера жив после `stop()`-таймаута, wake word и диктовка были мертвы без уведомления. Теперь отдельный `RECORDER_WORKER_HUNG_REASON`, watchdog ведёт по `is_worker_hung` аномалию до `wedged` + ErrorBus, Swift держит обе строки транзиентными и логирует клин WARN. (B, MEDIUM) `notePoll` проверял `last_chunk_ts` на наличие — замороженный штамп при живом треде снимал give-up кап за ~6 с; теперь здоровье = РОСТ штампа. Кросс-языковой контракт-тест фиксирует обе строки: Swift сравнивает reason ТОЧНОЙ строкой, рассинхрон сжёг бы бюджет self-heal за 3 попытки. Прод после деплоя: агент `LC_UUID 3615DACD`, backend pid 70114, `wake_word running/not wedged`, watchdog `session_active`.

**W9 — слепота Sentry: закрыта и задеплоена 2026-08-18.** Карточка: [`docs/superpowers/plans/2026-08-18-w9-sentry-quota-blindness.md`](superpowers/plans/2026-08-18-w9-sentry-quota-blindness.md). Sentry не принимал события с 13-08 не из-за кода: организация выбрала бесплатную квоту (accepted = ровно 5000/30д, rate_limited 4018, произведено 13 193). Слепота 22 дня из 30, 91% доли backend съел один issue `KRAB-EAR-BACKEND-1V` (2488, зависание stop_recording, починен `bc5ee07b` уже после выжигания). 🔴 Серверный Key Rate Limit на free-плане поставить НЕЛЬЗЯ: PUT ключа отвечает HTTP 200 и молча оставляет `rateLimit: null` — перечитывать через GET. Сделано: `backend/sentry_quota.py` (ok/blind/idle/unknown, `unknown` НИКОГДА не равен ok) + клиентский потолок в `ErrorBus` (12/час на код, 40/час суммарно, скользящее окно; локальные ring buffer и шина получают всё). Смок-рутина (`~/.claude/scheduled-tasks/krab-ear-e2e-smoke/`) переписана: сначала факт приёма, потом issues; слово «quiet» запрещено. Живая проверка после мержа: рутина выдаёт `blind ... rate_limited=117`. Курс владельца — ужиматься в бесплатный план, платный не берём; зрение вернётся ~4 сентября (сброс цикла).

**W10 — объём логов REST: PR [#1931](https://github.com/Pavua/Krab-Ear/pull/1931).** На каждый HTTP-запрос писались ДВЕ строки (своя + werkzeug) без таймстемпа: `logging.basicConfig` в `rest_server.py` ставил root-обработчик формата `"%(message)s"`, а werkzeug логировал тот же запрос своим access-логом. `backend/rest_log_config.py` — формат с временем + `werkzeug` на WARNING. Ротацию для `Krab Ear/logs` починил Главный Краб (их PR #140); объём записи — наша сторона.

**W6 — гард мёртвых Swift-методов: PR [#1932](https://github.com/Pavua/Krab-Ear/pull/1932).** Python закрыт пятью гардами мёртвого кода, Swift — ничем, при том что класс живой (`setupErrorBus`/`setupHealthMonitor` месяцами были мертвы за 100% зелёными тестами). 🔴 Критерий важнее сканера: наивное «нет `name(`» даёт 95 находок, ~78 ложных; восемь правил-исключений (override, trailing-closure, протокол ТОЛЬКО с именем требования, lifecycle, bare-reference, частые имена → needs_review, вызовы из Tests → test_only). На живом дереве **19 мёртвых + 45 test-only**, все 19 прогейчены машинно — 0 ложных. Стартует **report-only**, вне `audit-all`: удаление — отдельное решение владельца, а красный с первого дня гейт начинают игнорировать. Карточка: [`docs/superpowers/plans/2026-08-18-w6-audit-dead-swift-methods.md`](superpowers/plans/2026-08-18-w6-audit-dead-swift-methods.md).

🔴 **Счётчик Voice Gateway больше НЕ метрика здоровья нашего `:5005`** (их сообщение 2026-08-18 20:36). Они семплируют предсказуемые внешние отказы 1/20 ради общей квоты Sentry: «KrabEar STT exception: ReadTimeout» долетает ~13 вместо 263. Таймауты при этом никуда не делись — они в полном объёме в локальных логах гейтвея, сырые цифры за любое окно они отдадут по запросу. Наша сторона на момент проверки: `stt_busy` 0, singleflight-отбоев 0, ошибок транскрипции 0, но 616 срабатываний memory-pressure (это штатный скип второго MLX-чекпоинта из P0a). Источник 263 таймаутов не установлен — отдельная волна, если решим брать.

**Кандидат волны (не начата): REST не различает пустой результат.** Вскрыто разбором логов Voice Gateway 2026-08-18. `/v1/stt/transcribe` отдаёт HTTP 200 с пустой строкой И когда в аудио тишина, И когда распознать не смогли — причина внутри у нас есть (`_empty_transcription_result` с `empty_audio`/`vad_skip`, заведена 13.08), но наружу не выведена. У VG это выглядит как «KrabEar STT: '' (4846ms)» — 4.8 секунды на «тишину» подозрительны. Класс [[reference_empty_result_has_two_sources]]: по такому ответу нельзя двигать состояние клиента. Отдельно: их всплеск таймаутов 16.08 (59 из 82, окно 17:06–18:07) — ЦЕЛИКОМ до нашего первого P0-фикса (16.08 20:16), после деплоя 17.08 — 1, 18.08 — 0, то есть внешнее подтверждение, что P0a/P0c сработали. `deadline_sec` (W2c, в проде с 17.08 05:53) на живом потоке ещё НЕ проверен — 504 у VG не было ни разу, но и значимых прогонов после деплоя не было; проверяется искусственно (длинный файл с `deadline_sec=5` → ожидать 504 через ~5с).

**Следующая:** нет назначенной волны. Известный хвост: `krab-ear-rest.err.log` 179 МБ — на каждый HTTP-запрос пишутся ДВЕ строки (своя + werkzeug) при 34к запросов; ротацию для `Krab Ear/logs` починил Главный Краб (их PR #140), объём записи — наша сторона. Swift-агент в проде = HEAD `6e00b68f` (деплой W8 2026-08-18 12:38, `AX trusted: true`, хоткеи активированы, wake word `running/not wedged`). Живой REST на P0d/P0e/P0f. Не включать `REST_IN_PROCESS_ENABLED`. Не kickstart под запись.

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
