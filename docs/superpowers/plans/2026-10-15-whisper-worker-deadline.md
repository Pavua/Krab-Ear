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

- [x] RED: истёкший deadline до flock и после flock не вызывает worker;
  TypeError не начинает новый бюджет; ожидание flock уменьшает watchdog;
  настоящий отказ движка сохраняется. Использовать clock-double и worker-double
  только вместо Metal; вызывать настоящий AudioEngine._transcribe_model.
- [ ] Запуск: `PYTHONPATH="$PWD/KrabEar" /tmp/py312/bin/python -m pytest KrabEar/tests/test_whisper_worker_deadline_20261015.py -q`.
  Плановый Python3.12 запуск остановился до тестов на preflight окружения.
  RED→GREEN получен отдельным dev Python3.14 запуском; hosted Python3.12
  проверка обоих новых файлов — в полном CI, не этим локальным harness.
- [x] Перенести построение attempt_deadline_monotonic, last_inference_error и
  remaining_attempt_sec выше развилки worker/in-process. В subprocess-ветке:
  `with mlx_inter_process_lock(timeout_sec=min(5.0, remaining_attempt_sec())):`;
  перед каждой variant: `_fit_worker_timeout(configured_timeout_sec, remaining_attempt_sec())`;
  RuntimeError/MemoryError записывать в last_inference_error.
- [x] GREEN: новый файл и существующие lock-deadline/worker-timeout-handoff,
  explicit-auto, OOM, worker-protocol, watchdog-generation и temp-cleanup.

### Task 2: доказательства и выпуск

Дополнение после Task1 GREEN35: session._lock удерживается на readline, но
первый захват в transcribe и start/_send не ограничен временем. Watchdog
запускается после записи stdin. Новый файл
KrabEar/tests/test_whisper_session_deadline_20261015.py проверяет реальный
stdlib child: queued timeout возвращается до освобождения активного запроса,
не отправляет поздний JSON и не убивает активный child; истёкший запрос не
spawn-ит; запись WAV расходует бюджет; зависший stdin ограничен watchdog.

- [x] RED session: dev Python3.14/pytest, новый файл; до фикса queued запрос
  остаётся ждать self._lock, timeout=0 запускает child.
- [x] Отделить короткий счётчик inflight от I/O-lock, чтобы finally не
  застревал за следующим запросом. start/_send получают optional absolute
  deadline и bounded contextmanager acquire. Сохранить порядок conductor:
  I/O-lock охватывает close_if_idle; state-lock не удерживается при ожидании I/O.
- [x] Один deadline wrapper→PCM→session→start→send. Watchdog вооружить до
  stdin.write, retire под request-local guard до освобождения I/O-lock.
- [x] GREEN оба новых файла + существующие conductor/close, watchdog-generation,
  temp-cleanup и worker-protocol тесты. GPU при этом не используется.

- [x] Проверить session queue отдельным воспроизведением без GPU. При наличии
  дефекта оформить конкретное дополнение к карточке до изменения session.
- [ ] `scripts/pre_merge_py312_check.sh KrabEar/tests/test_whisper_worker_deadline_20261015.py`;
  Локальный parity preflight не прошёл; пункт не отмечен PASS. Зависимые
  файлы проверены отдельно dev-интерпретатором; полный Ubuntu CI — отдельно.
- [x] `make audit-all`; применимые Swift guards в hosted CI.
- [x] Независимый adversarial whole-diff review; точный SHA CI обеих workflow.
- [x] Явные пути в commit/PR. После merge проверить CI merge SHA.
- [x] Перед выкладкой отдельная свежая lifecycle-проверка; не использовать
  старые quiet-window разрешения. Без живой проверки Whisper не утверждать,
  что исторический REST timeout или AppHang устранён.

### AppHang: граница текущего вывода

Пять исследованных событий принадлежат старому UUID EE6DD5B0; на момент
разбора до выкладки16.10 установка имела UUID C8197C13.
Символика app-фреймов — entrypoint/MainActor; видимый busy main stack находится
в AppKit/layout/drawing. Пока нет доказанной причинной цепочки из кода Ear,
не делать спекулятивную UI-правку и не закрывать Sentry issues.

## Release checkpoint16.10

Source merged и установлен e996f30e; independent whole-diff tree7b221acd PASS.
Exact-head и оба final post-merge CI SUCCESS. Swift-first controlled lifecycle,
полные idle/drain/package gates и default REST Whisper ES E2E PASS (весь probe с проверками0.916s);
история23277/parent generations неизменны, старые backups сохранены.
15min /16 снимков PASS:900.293s, без смены parent generations и потери IPC/REST. Локальный /tmp/py312 parity harness не прошёл
preflight окружения; это не local PASS. Full hosted Ubuntu Python3.12 proof
получен отдельно. Подробные результаты и открытые causal границы —
[release checkpoint](../handoffs/2026-10-16-apphang-whisper-release.md).
