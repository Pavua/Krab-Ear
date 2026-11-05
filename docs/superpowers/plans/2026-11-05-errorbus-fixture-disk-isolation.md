# ErrorBus IPC: независимость fixture от диска runner

**Goal:** сохранить строгие empty-buffer проверки при любом свободном месте хоста и проверить, что намеренное disk warning по-прежнему попадает в IPC.

**База:** `origin/codex/krab-ear-v2` = `823a99ced4e8dd64bef07663bfd4616dd26205f6`.
**Worktree:** `/Users/pablito/Documents/Codex/2026-11-05/task/ear-errorbus-fixture`.
**Владелец:** координатор; write-scope — `KrabEar/tests/test_error_bus_integration.py` и эта карточка. Во всех 100 существующих worktree WIP по test-файлу отсутствовал.

## Доказательство и границы

В CI #2178 run `42030922313` два empty-buffer теста получили `disk.low_space` от реального DiskSpaceMonitor при 3.95 GB free. Это отдельная зависимость fixture от runner, не причина всех красных shutdown PR. Пятифайловое исправление из #2176 и 1000-cycle timeout ведутся отдельно.

Production-код, монитор диска, пороги, Sentry-конфигурация и runtime не меняются. Не запускать приложение, модели, реальные звонки или рестарты. Не трогать Main runtime, Gateway `.env`, чужой WIP; stage только явные пути. Набор тестов, asserts и timeouts не сокращать. Использовать Python 3.12 без MLX и существующий parity harness без перестройки shared venv.

## Изоляция проверок

Во всех test-процессах задать `KRAB_EAR_BRAIN_LEASE_PATH=/Users/pablito/Documents/Codex/2026-11-05/task/ear-errorbus-fixture-brain.lock` и `SENTRY_DSN=''`. Это документированный временный lease только для тестового процесса, не правка общего lock или production-конфигурации. Первый RED обнаружил заблокированную sandbox попытку записи общего lease; она не повторяется. EventBridge.start заглушён в обоих test cases, поэтому синтетические события не отправляются в работающий REST.

В существующем Python3.12 venv отсутствуют runtime leaves `mlx.core` и `mlx_whisper`; namespace `mlx` сам по себе есть. Проверять именно leaves, не перестраивать и не очищать shared venv.

## План

- [x] RED: два существующих empty-buffer теста с внешним `ear_low_disk_probe`: реальный DiskSpaceMonitor.check_now после конструктора, синтетический low-disk OS result, Sentry mock. Команда: `KRAB_EAR_BRAIN_LEASE_PATH=/Users/pablito/Documents/Codex/2026-11-05/task/ear-errorbus-fixture-brain.lock SENTRY_DSN='' PYTHONPATH=<task>:<worktree>/KrabEar /tmp/py312/bin/python -m pytest -p ear_low_disk_probe KrabEar/tests/test_error_bus_integration.py::ErrorBusIntegrationTestCase::test_list_recent_errors_empty KrabEar/tests/test_error_bus_integration.py::ErrorBusIntegrationTestCase::test_send_diagnostics_to_sentry_empty_buffer --timeout=60 -p no:cacheprovider -q`.
- [x] Fixture: задать синтетический healthy disk_usage только на время test case; зарегистрировать service.close до удаления temp data и снятия patch.
- [x] Добавить положительный контроль: принудительный low-disk probe через настоящий монитор после constructor → disk.low_space виден через list_recent_errors. Не фильтровать и не очищать ring перед assert.
- [x] GREEN: повторить точный RED probe; затем весь изменённый test-файл и смежные disk-monitor/ErrorBus тесты с timeout60/cacheoff.
- [x] Выполнить `KRAB_EAR_BRAIN_LEASE_PATH=/Users/pablito/Documents/Codex/2026-11-05/task/ear-errorbus-fixture-brain.lock SENTRY_DSN='' scripts/pre_merge_py312_check.sh KrabEar/tests/test_error_bus_integration.py`, Ruff/Flake8/diff-check и независимое source review. Без новых worker leaks.
- [ ] Опубликовать отдельный reviewable source/test PR только после успешных локальных gates; exact-head CI обязателен до merge. Runtime admission/drain остаётся HOLD.

## Полученные результаты

- 2 причинных RED → 2 GREEN при том же forced3GiB probe.
- Полный изменённый файл:14PASS,13прежних test bodies AST-идентичны базе.
- Пять последовательных именованных файлов ErrorBus/DiskMonitor:88PASS, безskip/xfail; проверена очистка disk mock между файлами.
- Штатный Python3.12/noMLX parity harness:PASS, новых workers нет.
- Flake8 с точными прежними CI flags:PASS, diff-check:PASS. Ruff содержит только3исходных E402 от path bootstrap, новых findings0; ignore/guard не добавлялись.
- Независимое bounded SOURCE GO; test SHA25642deeef2b8a4ca001a868fde5173f477d0a20317a3d09fec2f6bc912306fe521. Production diff отсутствует.

Это не исправление 1000-cycle performance timeout, не квалификация нижнего shutdown stack и не runtime acceptance.
