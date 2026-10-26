# Batch STT: владение принятыми задачами при shutdown

## Scope и ограничения

База `66e44c533761a4c274f1d69e50633604fb8cac26`, отдельная ветка
`codex/ear-batch-stt-drain-1026`. Только source/offline; без моделей, сети,
production lifecycle, аудио, тяжёлых fixtures, commit/push и соседних правок.
Владение: `recording_core_service.py`, узкий guard в `service.py`, новые
лёгкие regression tests и эта карточка. Другие сессии владеют соседними WT.

## Подтверждённый дефект

`transcribe_paths_async` возвращает `job_id` и запускает daemon без retained
handle. Завершившийся IPC-handler уже не принадлежит IPC drain, а
`close_background_workers` ждёт только capture/preview workers. Поэтому
`BackendService.close` может закрыть transcriber и вернуть True при живом
batch. Задержка перед STT (например, `vocabulary.load`) оставляет native
engine idle во время close, после чего daemon снова входит в STT.
Это старый shutdown-дефект, а не следствие ошибки конструктора.

## Минимальный контракт

1. Batch admission и публикация thread handle сериализованы существующим
   recording lifecycle lock. Shutdown latch запрещает новые batch-задачи.
2. Registry хранит принятые workers до подтверждённого завершения; ошибка
   `Thread.start` не оставляет вечный queued job или потерянный живой handle.
   Подтверждение — успешный `join(timeout=0)` и затем `is_alive() == False`.
   Сам worker себя из registry не удаляет. Ошибка создания Thread до
   публикации handle оставляет только failed job. Любое исключение из start
   после публикации оставляет unknown handle до подтверждения выхода: даже
   ident может появиться раньше завершения bootstrap. Если thread фактически
   не был запущен, этот unknown способен сохраняться до завершения процесса;
   close возвращает False, автоматического recovery эта карточка не добавляет.
3. Shutdown сигнализирует отмену, делает ограниченный общий join batch workers
   вне lifecycle lock и возвращает False, пока хотя бы один жив. Повторный
   close может подтвердить завершение сохранённых handles.
4. Отмена проверяется после подготовки файла и перед native STT. Уже начатую
   операцию не прерываем; её worker остаётся владельцем до выхода.
5. `BackendService.close` не закрывает transcriber при неподтверждённом
   recording/batch drain (тот же `all_workers_stopped` guard, что у соседней
   карточки live-subs; при интеграции совпавший guard не дублировать).

## Проверка

RED → GREEN на actual AST methods + stdlib JobTracker/Thread/Event; только
leaf зависимости заменены управляемыми fakes. Полный BackendService ctor,
conftest, ML/audio/network не исполняются. Покрыть:

- принятый batch, задержанный до STT; close False и transcriber не закрыт;
- отмена не допускает delayed vocabulary → native STT;
- admission после close и гонка shutdown с подготовкой/Thread.start;
- start failure, worker exception, успешное завершение и bounded retained retry;
- interruption до ident и между ident/started, включая обычный RuntimeError;
- уже начатый native вызов и общий join budget для нескольких workers;
- recording close False/exception не допускает transcriber.close.

Команда: существующий `.venv_krab_ear/bin/python -m pytest --noconftest`
с отключённым autoload plugins и bytecode, только новый focused файл.
Следующий gate — независимое whole-diff review; source/offline результаты
не означают runtime/CI/release acceptance.

## Результаты

Исходный RED: 12 cases, 10 причинных assertion failures и 2 normal controls
PASS. После первого фикса 12/12 GREEN. Дополнительно воспроизведены два RED
на прерванном start (до ident и между ident/started): false idle заменён на
retained unknown с публичным join-подтверждением.

Финальный focused набор: **18 PASS** на Python 3.14 (`pytest --noconftest`,
без plugins/conftest/cache) и **18 PASS** на существующем Python 3.12
(standalone `unittest`, 0.972s). Workflow-settings flake8 изменённых трёх
Python-файлов и `git diff --check` PASS. Полные imports/backend fixtures,
ML, hosted CI, runtime и lifecycle не проверялись.

Raw GREEN logs находятся во внешнем artifact-каталоге
`ear-batch-stt-drain-1026/` текущей сессии: `green-py314-pytest.log` и
`green-py312-unittest.log`. Независимый review и интеграция guard с соседним
live-subs fix остаются отдельными gate; commit/push не выполнялись.
