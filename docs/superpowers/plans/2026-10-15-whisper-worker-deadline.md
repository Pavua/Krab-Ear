# Whisper worker: единый срок допуска Implementation Plan

> Исполнение: superpowers:executing-plans, inline. Полномочия владельца: тесты,
> PR, merge и выкладка Ear; соседние сервисы не изменять.

**Goal:** не запускать просроченную subprocess-попытку Whisper после очереди
flock или перебора несовместимых аргументов.

**Architecture:** обе ветви AudioEngine используют один абсолютный monotonic
deadline, вычисленный до executor hop. Остаток ограничивает flock и каждый
внутренний watchdog; подтверждённый MemoryError/RuntimeError сохраняется при
истечении срока. Отдельно проверить очередь MLXWhisperSession; не объявлять
весь транспорт исправленным по одному тесту AudioEngine.

**Tech Stack:** существующие Python, pytest, stdlib; без новых зависимостей.
**База:** origin/codex/krab-ear-v2, 6f07569c99b2cdd8c952369a9148ed324e3b44dc.
**Worktree:** /Users/pablito/.codex/worktrees/ear-passive-key-20261014/Krab Ear.

**Баны:** явные git add; canonical dirty WIP не менять; не запускать второй
Swift agent; backend restart только safe_backend_restart.command. Не трогать
Main/VG lifecycle, .env, OrbStack/VPN, Keychain, миграцию/шифрование истории,
модели LM Studio; не включать REST_IN_PROCESS_ENABLED; PR1875 запрещён.
GPU и lifecycle только после свежего idle и согласованного quiet window.

### Task 1: срок subprocess-ветки AudioEngine

Files: KrabEar/core/engine.py;
KrabEar/tests/test_whisper_worker_deadline_20261015.py.

- [ ] RED: истёкший deadline до flock и после flock не вызывает worker;
  TypeError не начинает новый бюджет; ожидание flock уменьшает watchdog;
  настоящий отказ движка сохраняется. Использовать clock-double и worker-double
  только вместо Metal; вызывать настоящий AudioEngine._transcribe_model.
- [ ] Запуск: `PYTHONPATH="$PWD/KrabEar" /tmp/py312/bin/python -m pytest KrabEar/tests/test_whisper_worker_deadline_20261015.py -q`.
  До фикса ожидается FAIL: worker вернул результат после истечения срока.
- [ ] Перенести построение attempt_deadline_monotonic, last_inference_error и
  remaining_attempt_sec выше развилки worker/in-process. В subprocess-ветке:
  `with mlx_inter_process_lock(timeout_sec=min(5.0, remaining_attempt_sec())):`;
  перед каждой variant: `_fit_worker_timeout(configured_timeout_sec, remaining_attempt_sec())`;
  RuntimeError/MemoryError записывать в last_inference_error.
- [ ] GREEN: новый файл и существующие lock-deadline/worker-timeout-handoff,
  explicit-auto, OOM, worker-protocol, watchdog-generation и temp-cleanup.

### Task 2: доказательства и выпуск

Дополнение после Task1 GREEN35: session._lock удерживается на readline, но
первый захват в transcribe и start/_send не ограничен временем. Watchdog
запускается после записи stdin. Новый файл
KrabEar/tests/test_whisper_session_deadline_20261015.py проверяет реальный
stdlib child: queued timeout возвращается до освобождения активного запроса,
не отправляет поздний JSON и не убивает активный child; истёкший запрос не
spawn-ит; запись WAV расходует бюджет; зависший stdin ограничен watchdog.

- [ ] RED session: тот же Python3.12/pytest, новый файл; до фикса queued запрос
  остаётся ждать self._lock, timeout=0 запускает child.
- [ ] Отделить короткий счётчик inflight от I/O-lock, чтобы finally не
  застревал за следующим запросом. start/_send получают optional absolute
  deadline и bounded contextmanager acquire. Сохранить порядок conductor:
  I/O-lock охватывает close_if_idle; state-lock не удерживается при ожидании I/O.
- [ ] Один deadline wrapper→PCM→session→start→send. Watchdog вооружить до
  stdin.write, retire под request-local guard до освобождения I/O-lock.
- [ ] GREEN оба новых файла + существующие conductor/close, watchdog-generation,
  temp-cleanup и worker-protocol тесты. GPU при этом не используется.

- [ ] Проверить session queue отдельным воспроизведением без GPU. При наличии
  дефекта оформить конкретное дополнение к карточке до изменения session.
- [ ] `scripts/pre_merge_py312_check.sh KrabEar/tests/test_whisper_worker_deadline_20261015.py`;
  зависимые файлы запускать отдельно, без параллельного импорта MLX/Torch.
- [ ] `make audit-all`; применимые Swift guards в hosted CI.
- [ ] Независимый adversarial whole-diff review; точный SHA CI обеих workflow.
- [ ] Явные пути в commit/PR. После merge проверить CI merge SHA.
- [ ] Перед выкладкой отдельная свежая lifecycle-проверка; не использовать
  старые quiet-window разрешения. Без живой проверки Whisper не утверждать,
  что исторический REST timeout или AppHang устранён.

### AppHang: граница текущего вывода

Пять исследованных событий принадлежат старому UUID EE6DD5B0, текущий C8197C13.
Символика app-фреймов — entrypoint/MainActor; видимый busy main stack находится
в AppKit/layout/drawing. Пока нет доказанной причинной цепочки из кода Ear,
не делать спекулятивную UI-правку и не закрывать Sentry issues.
