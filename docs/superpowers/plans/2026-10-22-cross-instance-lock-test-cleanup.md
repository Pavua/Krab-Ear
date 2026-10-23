# Cross-instance lock test — исправление ложного CI успеха

База: origin/codex/krab-ear-v2 @3f08c9dc, isolated ear-lock-test-cleanup-1022.
Scope: только тест и эта карточка; production StateStore не меняется.
Нет моделей/сетевых вызовов/production lifecycle или соседних изменений.

- [x] Подтвердить источник: SUCCESS backend job120096826801 содержит поздний
  StateStoreLockTimeout из worker теста cross_instance_lock_S3.py:67.
- [x] RED actual unittest + before/after thread snapshot: unittest OK, но
  UNJOINED_TEST_WORKERS=1 и общий exit1 (Python3.14).
- [x] Заменить устаревшую forever-thread демонстрацию real flock + timeout0;
  проверять повторный отказ, откат reentry и повторный захват после release.
- [x] GREEN: snapshot-runner UNJOINED_TEST_WORKERS=0; 29 lock regressions
  PASS на Python3.14 и Python3.12, Ruff check/format и diff PASS.
  В 3.12 использован минимальный uv env (pydantic-settings/cryptography),
  без backend/ML dependency pack. Это не полный ubuntu parity harness.
  Первоначальный unittest pack ошибся PYTHONPATH helper import; повтор с
  KrabEar:KrabEar/tests прошёл29/29 на обеих версиях.
  Один pytest в dev-venv также PASS с thread-exception-as-error; существующий
  warning TorchCodec/FFmpeg dependency compatibility отдельно, runtime не проверен.
- [x] Независимое whole-diff SOURCE PASS; production source неизменён.
- [ ] Ubuntu3.12 parity и exact-head hosted CI в следующем согласованном окне.

Причина прошлых owner smoke exit1 этим тестовым дефектом не объясняется.
