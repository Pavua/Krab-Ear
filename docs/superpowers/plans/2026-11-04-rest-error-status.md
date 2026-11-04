# REST: восстановить существующий error-result gate

База origin/codex/krab-ear-v2 @6e2043fb; owned branch codex/ear-rest-errors-1104.
Подтверждено на source5a и6e: engine.transcribe broad except возвращает
status=error/error, а REST превращает это в HTTP200/statusok и может сохранять
пустую ошибку в history. Это не настоящий успех/тишина. Готовый неизлитый
фикс50cbcdc3 найден в старом139d228f. Использовать его механизм и уже написанные
регрессии, не оживлять старые чаты/не менять их worktrees.

Scope: source/tests/docs внутри Ear; no публикация/merge/deploy/restart/
настройки/модели/download/IPC/inference/phone. Sourcecache и текущийruntime
восстановлены отдельной завершённой операцией; эта карточка их не меняет.

- [x] Existing isolated Flask regression harness из50cb: сначала RED на6e,
  ожидаемые шесть ошибочных HTTP200/history/metrics side effects, два silence200.
  /tmp/py312, -I-B, disposable HOME/TMP/data, OS networkdeny, provider/file/thread
  guards, ownedchild cleanup и предел35s. Никакой модели или livebackendimport.
- [x] Перенести существующий10-line privacy-aware gate после postcallprivacy/
  deadline и доhistory/metrics. Stable503stt_failed, privacy403сохраняется;
  легитимная пустота200 и текущаяtimeout/ownershipмеханика неизменны.
- [x] Зелёный actualFlask/executor regression и применимые CI-unit tests;
  тестированиеperfile на3.14 и3.12безMLX, audit-all/lint/diffcheck.
- [x] Независимое сильное whole-diff adversarial review HTTP/privacy/audio
  boundaries. Error503 не считать доказательством nestednative drain.
- [x] Локальный commit, NOW/results среальными границами. Публикация только
  послеотдельногоразрешения; runtimeRESTfixнедеплоен.

Проверить существующие test_rest_* и ветку codex/ear-rest-error-status-1024:
первоначальныйc9e62600/#2120 и50cb уже содержат regression design. Не создавать
второй classifier/downloader/executor и не объявлятьserverdrain.

## Проверено 04.11

- Python 3.12: actual Flask sandbox RED (6 неверных 200 + 2 silence controls),
  затем GREEN (6×503 + 2×200), восемь реальных executor workers завершены,
  child reaped, group отсутствует, source hashes стабильны, violations=[]
  также после teardown. STT leaf синтетический: это не ASR/runtime acceptance.
- Новые unit-сценарии: 15 на Python 3.12 и 15 на Python 3.14; в каждом
  запуске disposable HOME/TMP, provider/file/thread guards и OS network deny.
  Existing VG contract: 5 PASS и call-profile: 12 PASS на 3.12.
  Итого 39 тестов на 3.12 и 22 на 3.14; импорт MLX/провайдеров запрещён.
- Pure AST result slice: 3 PASS (14 subcases); parent ownership: RED 2 → GREEN 2;
  receipt ordering: RED 1 + 1 control → GREEN 2. Оба harness regression-файла
  также 4 PASS на 3.14, result slice — 3 PASS на 3.14.
  AST checks не являются HTTP/process proof.
- `audit-all`, CI-policy flake8 и diff check: exit 0. pytest-timeout plugin
  отключён вместе с auto-plugins; warnings об unknown timeout options ожидаемы.
  Actual Flask subprocess имеет внешний предел 35s и bounded cleanup.
- Astra/Ultra: initial BLOCK по stale pre-cleanup receipt, причинный тест и
  исправление; повторное whole-diff review GO, иных findings нет.
- Отдельный найденный хвост: существующий `scripts/run_isolated_pytest.py`
  сигналит process group после wait/reap. Нужна отдельная source-волна с
  ownership-регрессией; текущий REST diff общий CI-helper не меняет.
