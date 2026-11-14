# Восстановление прерванного privacy-purge

База: `678a202bb8b55be6c939aea18a2b48496cbd7111` (`origin/codex/krab-ear-v2`).
Worktree: `~/.codex/worktrees/ear-purge-recovery-1114`.

Задача владельца: исправить восстановление и проверить только synthetic данные.
Рабочий app/backend, личные профили, Keychain и календари не трогать. Без deploy/merge.

## Контракт

- Durable `.purge-pending` до первого удаления; общий timeline flock остаётся тем же inode.
- Только внешний purge scope снимает marker после `complete=True`, без errors,
  после backend RAM cleanup. Исключение, отказ и partial result сохраняют marker.
- До обычного BackendService init повторить те же HistoryService wipe-шаги,
  собрав файловые collaborators без runtime/model/network/background start.
- Ошибка recovery запрещает обычный startup. Pending запрещает timeline и новые
  обычные IPC, разрешая повторный подтверждённый purge и ping.
- Сохранять permanent deletion ledger и политику encryption/privacy.
- Частичные удаления не объявлять complete; не терять адрес Obsidian vault,
  пока там остались файлы, которые не удалось удалить.
- Не расширять область удаления: внешний произвольный export/старый backup вне
  текущего purge-контракта не удаляется. Уже работающий старый backend не обновляется.

## Проверка

- [x] RED: `test_purge_crash_recovery.py` падает после crash до/после tombstones:
  остаётся история либо exports/shares.
- [x] RED: partial Obsidian purge теряет persisted vault location.
- [x] GREEN: реальные отдельные процессы и IPC, os._exit до истории, после истории,
  после HistoryService, повторное прерывание recovery; новое содержимое после
  завершения сохраняется при следующем старте.
- [x] Unit: pending/fsync/unsafe marker/nested scope/partial result/startup failure.
- [x] Python 3.12 relevant purge, export, startup и Obsidian tests, audit-all, lint.
- [x] Независимое adversarial whole-diff review; исправить подтверждённые замечания.

Команда targeted: `PYTHONPATH="$PWD/KrabEar:$PWD/KrabEar/tests" <isolated-py312>/bin/python -m pytest -q KrabEar/tests/test_purge_crash_recovery.py KrabEar/tests/test_timeline_purge_guard.py`.
Synthetic IPC fixture запрещает внешние sockets/processes, записи вне temp root и
доступ к реальному Keychain. Обычные фоновые jobs не исполняются.

## Результат локальной квалификации

- Python 3.12.11, MLX отсутствует: 44 файла по одному процессу на файл.
- 10 isolated IPC сценариев прошли: три crash-point, повторный crash/partial
  recovery, полный inventory файловых collaborators, новый контент после purge,
  запрет IPC при partial result. Внешних обращений fixture не обнаружил.
- `make audit-all` и `git diff --check` прошли; независимое Astra High review: GO
  для process-crash/restart scope. Дополнительная гонка разморозки backup закрыта:
  `clear_purged` вызывается после commit внутри того же guard lock.
- Два legacy fixture приведены к файловому guard; недоступный настроенный vault
  теперь проверяется как отказ. Timing-only AST fixture получил guard stub.
- Локальный cold-import выходил за стандартные 30 секунд (системные CA/torch).
  Для полного прогона использованы explicit certifi CA, собственный pycache в
  temp и timeout=120; IPC crash tests имеют собственный deadline=150.
- Установка, запуск рабочего приложения и личные календари не затрагивались.
  CI/PR и интеграция — отдельные доказательства; локальные результаты их не заменяют.
