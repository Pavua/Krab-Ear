# Phonetic seed: однократная миграция и два writer — source GO

## Подтверждённый дефект

Whole runtime-diff review выявил P2: после успешного удаления новой curated
записи следующий list/provider снова добавлял её через `_merge_curated_defaults`.
Повторная проверка wiring подтвердила независимые экземпляры сервиса в backend
и REST; instance thread lock не защищал общий read-modify-write файл.

## Исправление

- `curated_seed_version=1` сохраняется вместе с entries; legacy store мигрирует
  один раз. Remove выполняет legacy миграцию до удаления в той же critical section.
  Последующие чтения не возвращают удалённые записи/варианты. Неизвестные metadata
  и future version сохраняются, непрочитанный store не перезаписывается.
- Все public read/write/clear операции используют instance lock и общий POSIX
  flock с timeout 2s. При недоступном lock нет fail-open записи; lock должен
  быть owned regular file, symlink отвергается. Пустой lock-файл не удаляется:
  его inode обеспечивает координацию процессов, персональных данных в нём нет.
- Пользовательский живой словарь при разработке не читался и не менялся.
  Privacy purge продолжает удалять JSON существующим механизмом.

## Доказательства

Исходный sequential RED воспроизвёл возврат удаления/потерю metadata. Отдельный
RED воспроизвёл stale REST read против backend delete и отсутствие OS lock.
После исправления: 13 новых regression tests + 33 существующих vocabulary tests
+ 8 golden tests = 54 на Python 3.12 и 54 на Python 3.14. Включены реальная
блокировка flock в точке чтения, timeout без записи, отказ symlink и два отдельных
Python процесса с завершением/reap. Guarded прогоны не загружали ML и не обращались
к сети/живому runtime. Независимый Astra/Ultra review: оба P2 закрыты, source GO.

Итоговый `make audit-all` чист. Real Flask upload/executor probe составного
candidate: 8 HTTP cases, 8 workers завершены, child reaped/group empty,
нарушений boundary нет. Transcriber leaf синтетический: это не ASR acceptance.

## Граница отката и выпуска

Старый `5a09e630` читает entries нового JSON, но его CRUD удаляет seed metadata.
Редактирование словаря после rollback требует отдельной квалификации следующего
forward upgrade; нельзя автоматически вернуть старый entries snapshot.
Source GO не заменяет exact combined/post-merge CI, runtime qualification,
свежий quiet/idle gate и речевую/PSTN приёмку. Выкатка этим исправлением не выполнена.
