# Turbo cache: отдельное восстановление 04.11

## Scope и факты

Владелец через текущий Gateway chat и прямо в Ear разрешил восстановление
точных отсутствующих весов в staging, SHA256, controlled cache repair и один
bounded speech probe. Свободное окно подтверждено 20:33 CET, Main/Gateway
продлили его до 20:48. Merge/deploy/restart/settings/model-ID switch не входили.

Source-фикс проверки файлов `382791e1` остаётся отдельным локальным изменением.
Работающий REST — clean release `5a09e630b389dabb723979d7345dd7ab98d7cf06`,
PID 27451. Его точный source CI проверен зелёным; новый source не выкатывался.

Read-only проверка нашла config-only snapshot и растущий `.incomplete`:
25 → 102 MB, затем 495 231 057 B на момент backup. Периодические Gateway
POST совпали с REST worker/watchdog timeout 23 s. Конкретный writer PID не
пойман; пустой разовый lsof не считался downloader drain. Полной копии в
известных HF roots не найдено; старый 4TB SSD не смонтирован и не проверен.

## Выполнение и независимые gates

- [x] Публичная HF metadata и HEAD ETag точной revision независимо проверены:
  `a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb`, `weights.safetensors`,
  1 613 977 612 B, SHA256/ETag
  `951ed3fc1203e6a62467abb2144a96ce7eafca8fa77e3704fdb8635ff3e7f8a6`.
- [x] Astra/Ultra GO на fresh isolated staging. Штатный hf_hub_download:
  pinned revision/file, token=False, отдельные HF/Xet paths, Xet disabled;
  ML/model imports отсутствовали. 144.18 s, exit 0; полный streaming SHA256
  и размер совпали до публикации.
- [x] Astra/Ultra SOURCE GO на конкретный publisher. Четыре временные FS
  фикстуры: success, missing window, dangling pointer, busy UnixFileLock — PASS.
  Первая фикстура сравнивала `/var` с `/private/var`; исправлен только её root
  через resolve(), publisher оставлен неизменным.
- [x] 20:35:42 CET: повторная hash-проверка, строгий UnixFileLock точного HF
  ETag с timeout 2 s, backup config/ref/partial. No-clobber hardlink полного
  blob и новая relative snapshot symlink. refs/config/partial не изменялись.
  SoftFileLock fallback, overwrite и автоматический rollback не применялись.
- [x] Публикация уже blob могла разрешить autoload ожидающему worker; snapshot
  fast path HF вообще не берёт blob-lock. Поэтому resource window действовало
  до первой публикации и до post-probe terminal receipt. Global drain не заявлен.
- [x] Astra/Ultra узкий C GO: один fixed synthetic screener PCM16LE mono 8 kHz,
  58 880 B / 3.68 s, RAM WAV. Gateway отправил единственный HTTP-запрос:
  request_profile=voice_gateway_call, lang_hint=es, cleanup_profile=off,
  diarize=false, persist_history=false, deadline_sec=25, без retry.
- [x] HTTP 200, 0.617 s client; STT 478 ms; engine mlx-whisper, actual model
  mlx-community/whisper-large-v3-turbo; распознано
  «Hola, ¿Quién llama y por qué motivo?».
- [x] Post-readiness 20:39:35: прежние REST PID 27451 и epoch; tracked native
  reservations/queued/inflight/unknown=0, coverage_lost=false. Whisper worker
  PID 55018, inflight=0, last_load_sec=3.006; observational model/state остаются
  unknown, это не скрывается.

## Ограничения и оставшиеся шаги

- Успех относится к одному opt-in ES REST профилю. Production Gateway wiring,
  остальные языки/модели, полная coverage/drain и PSTN этим не доказаны.
- REST success проходит pool.shutdown(wait=True); timeout running Future мог
  бы вызвать штатный self-exit/launchd recovery. Такого исхода здесь не было:
  PID/epoch сохранились. При timeout план запрещал retry и допуск PSTN.
- Sentry API read-only через Main: последний возвращённый backend issue
  KRAB-EAR-BACKEND-22 error — 11:04:25 CET, 1Q fatal — 11:03:29 CET;
  известный wake-word/shutdown класс. Причины не закрыты, ingress completeness
  не установлена. Это не новая доказанная STT-регрессия и не полная квалификация.
- Partial и backup сохранены. Удаление/rollback не выполнять при возможных
  readers/loader; отдельный safe-idle receipt нужен перед очисткой.
- PSTN — один разрешённый dispatch текущего Gateway owner, его outcome и
  дальнейшая live acceptance учитываются отдельно. Ear второй звонок не отправляет.
- Подробные JSON receipts и одноразовые reviewed scripts сохранены вне Git
  в приватных артефактах текущей Ear-сессии `hf-cache-readiness/`.

Runtime-qualification workflow найден в копии OpenCode skills; канонические
пути Codex/Claude отсутствовали. Не использовались raw env/log dumps, новые
зависимости, модельные настройки, process eviction или lifecycle-команды.
