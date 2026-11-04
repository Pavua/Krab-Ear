# MemoryConductor: закрытие допуска воркеров

**Цель:** после `stop()` не принимать новые фоновые задачи; поток, принятый до
остановки, должен быть виден существующему ожиданию воркеров.

**База:** `origin/codex/krab-ear-v2` / `6e2043fb`.
**Ветка:** `codex/ear-conductor-stop-1104`, отдельный managed worktree.
**Архитектура:** `_stop` закрывает допуск под `_lock`. `_spawn()` проверяет
допуск, регистрирует и стартует поток в одной критической секции. Ожидание
`join()` остаётся вне lock. Существующие таймауты остановки не меняются.

## Границы

- Не менять production lifecycle, LM Studio, модели, клон, OrbStack или соседние
  репозитории. Не включать enforce-флаги и не вызывать настоящие model executors.
- Общий checkout и другие worktrees не менять. Stage — только явные пути.
- Не менять REST/IPC-контракты и не объявлять исправленной причину wake-word hang.
- Проверки по одному файлу; полный ML-heavy suite и Swift build для этого
  Python-only изменения не нужны. Новых зависимостей нет.
- Публикация, merge и deploy в эту карточку не входят.

## Task 1: атомарный допуск `_spawn()` относительно `stop()`

**Файлы:** `KrabEar/backend/memory_conductor.py`,
`KrabEar/tests/test_memory_conductor_2026_08_19.py`.

- [x] Baseline: существующие 29 тестов файла проходят на Python 3.14.
- [x] RED: добавить в `StopDriverTest` три воспроизведения:
  `test_event_bus_snapshot_cannot_spawn_after_stop`,
  `test_recording_callback_cannot_spawn_after_stop`,
  `test_stop_waits_for_worker_whose_start_is_in_progress`.
  Настоящий локальный `EventBus.emit()` сохраняет callback в snapshot, первая
  подписка вызывает stop/detach, вторая пытается начать OOM-работу.
  Для гонки старта подкласс настоящего Thread задерживает `start()` через Event;
  cleanup отпускает и join-ит все созданные тестом потоки даже после FAIL.
  Все три падают по ожидаемым assertion; прежний drain-контроль проходит.
- [x] GREEN: в `stop()` заменить `self._stop.set()` на:

  ```python
  with self._lock:
      self._stop.set()
  ```

  В `_spawn()` перенести создание и `w.start()` под существующий `with self._lock`,
  перед созданием добавить `if self._stop.is_set(): return`.
- [x] Добавить контроль явного повторного `start()` после `stop()`.
- [x] Запустить файл целиком (33 PASS):

  ```bash
  PYTHONPATH="$PWD/KrabEar" '/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear/bin/python' -B -m unittest discover -s KrabEar/tests -p test_memory_conductor_2026_08_19.py -v
  ```

- [x] Зависимые conductor/holdoff/instance-guard/semantic/shadow/wiring тесты —
  отдельными процессами; `make audit-all`; parity на Python 3.12 без MLX.
- [x] Независимое whole-diff ревью: late callback, register/start/stop ordering,
  отсутствие join под lock, явный restart и cleanup тестов.
- [x] Зафиксировать точные результаты, ограничения и локальный commit.

## Результаты 04.11

- CPython 3.14: 119 PASS в 9 отдельных pytest-процессах (conductor 33, wiring 9,
  shared-instance 16, brain-holdoff 19, semantic-idle 11, shadow-marker 4,
  distress-sensor 14, activity-diagnostics 11, EventBus detach 2), ещё 44 subtests.
- CPython 3.12.11, `mlx.core`/`mlx_whisper` отсутствуют: изменённый файл 33 PASS.
  Использован существующий `/tmp/py312`, без установки/удаления пакетов;
  полный `pre_merge_py312_check.sh` с его backend-import preflight не запускался.
- `make audit-all`: exit 0, `All audit checks passed`.
- Focused flake8 двух изменённых Python-файлов и `git diff --check`: exit 0.
- Независимое Astra/ultra whole-diff source review: GO, обязательных замечаний нет;
  reviewer дополнительно запустил 5 lifecycle-тестов (5 PASS).
- Ограничение race-теста: отрицательная проверка ожидания 0.2 с может пропустить
  старый дефект при starvation stopper; причинный RED фактически получен.
- Pytest запущен с `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` и `-o addopts=`; timeout
  config warnings ожидаемы, внешний лимит каждого тестового процесса — 60 с.
  В 3.14 также warning существующего TorchCodec/FFmpeg окружения при импортах.
  Аудиодекодирование и настоящие модели эти проверки не запускали.
- Raw-логи и `PER_FILE_RESULTS.json` сохранены в локальном каталоге артефактов
  текущей Codex-сессии `conductor-stop/`; в публичный Git не добавляются.
- Исторические runtime SHA/PID не перепроверялись. Рестартов, изменения настроек
  и моделей, публикации, hosted CI, merge, deploy и live acceptance не было.

## Предел доказательств

Это локальный контракт фоновых потоков. Даже GREEN не доказывает полный drain
backend: старые таймауты, основной tick-thread и прочие подсистемы требуют своих
контрактов. Новые тесты не загружают модели и не обращаются к живому EventBus.
