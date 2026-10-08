# A5.3 Card A — проверка исходников, 2026-10-03

База: `efecb801aae3f3c62fa03314ba4f5b142ac8e906`.
Ветка: `codex/ear-a53-completion`. Card B/C/D здесь не реализованы.
Это основание для проверки PR, не разрешение на merge/release и не live acceptance.

## Матрица требований

| Требование | Проверка | Подтверждение |
|---|---|---|
| Typed UNKNOWN, bounded/no-follow чтение, fingerprint, явный startup | `test_plaintext_export_authorization.py` | 138 тестов Python 3.14, включая spawn-процессы 5a–5c |
| Fresh getter, stale consent, epoch/generation, shutdown, malformed provider, lifetime receipts | `test_plaintext_export_policy_protocol.py` | 12 тестов и 9 subtests; startup race и custom socket проверены на реальном файловом хранилище |
| Четыре RPC и честный same-UID trust boundary | `test_plaintext_export_ipc.py` | 5 тестов настоящего BackendService; аудиоадаптеры заменены |
| Единый commit, CAS, raw policy, repair, restore PREPARE и размер | `test_plaintext_settings_commit_paths.py` | 78 тестов, подтверждённые RED перед исправлениями |
| Секреты в dispatcher/socket/audit/Sentry, включая encoded echoes | `test_plaintext_export_redaction.py` | 14 тестов; настоящий Sentry SDK/LoggingIntegration с локальным transport |
| Действующие settings/history/restore/glossary сценарии | 47 зависимых файлов | 1266 passed; конечные per-file процессы exit 0 |
| Статические проверки | `make audit-all`, CI-compatible flake8, diff-check | PASS; полный финальный audit exit 0 |
| Python 3.12 без MLX | `pre_merge_py312_check.sh`, изменённые test-файлы | PASS: 50 изменённых/зависимых файлов + 6 файлов dispatcher/audit/Sentry, оба процесса exit 0 |
| Exact-SHA CI source PR | GitHub Actions | Ещё не запускался для нового source-коммита |

Python 3.14 окружение выдаёт существующее предупреждение torchcodec/FFmpeg.
Эти тесты не проверяют аудиодекодирование, MLX, живую диктовку или production.
Тяжёлые конструкторы в затронутых service fixtures заменены, `service.close()`
сохраняется. Проверки идут по одному файлу, без xdist.

## Независимый gate

Агент `card_a_whole_review`, **Astra High**, проверил весь production diff
относительно базы и новые IPC/redaction-модули. Финальный source verdict **PASS**.
Повторно проверены исправления raw privacy coercion, restore до первой mutation,
CAS, UNKNOWN, startup ownership, receipt lifetime и Sentry redaction/quota.
Ревьюер тесты не запускал: runtime-доказательства выше принадлежат исполнителям.

Привязка просмотренного source:

- `git diff origin/codex/krab-ear-v2 -- KrabEar/backend` SHA256:
  `fb28135aaf9dd23622ac456d095b347cf12e6dd9daafa9ef8ea7ca5ef5d960e3`.
- Новый `plaintext_export_ipc.py` SHA256:
  `b85c946d2500bbe67c980f437365f00ec19a6cbe526211e719916989aa71fadc`.
- Новый `plaintext_export_redaction.py` SHA256:
  `1910c23613910bbccfd665a41235bbc677a314115b5c144f570ca421708daf94`.

После gate менялись только тестовые fixtures и документация. Source hashes
сверены координатором. После commit новые файлы войдут в tracked diff, поэтому
hash всего diff изменится по составу; их собственные hashes остаются проверкой.

## Изменения поведения и границы

- Частичный save сохраняет отсутствующие действующие policy-флаги; stale CAS
  возвращает явный отказ. Повтор со старым словарём не выполняется.
- Полный restore требует исходную пару JSON boolean, не значения после
  validator/defaults. Повреждённый settings не сбрасывается автоматически в `{}`.
- Successful RPC validation разрешает одну локальную запись до ответа;
  последующий revoke запрещает следующие операции. Сам Swift writer и его
  одноразовый closure будут проверяться в Card C/D.
- Same-UID caller может вызвать grant напрямую: это trusted-client consent,
  не доказательство клика UI и не защита от локального владельца процесса.
- Python файловые sinks ещё требуют Card B. Наличие RPC не означает, что
  весь экспорт уже защищён.

## Риск прежней тестовой изоляции

Обнаружен существовавший путь `SettingsService → SettingsBackup()` к домашнему
settings_backups с rolling prune. Глобальный conftest теперь принудительно
задаёт throwaway-каталог до импорта приложения. Явные временные backup_dir
тестов не меняются.

В default-каталоге read-only проверены только метаданные: четыре JSON с reason
before_set, mtime 15:26:39–40 CEST. Начального inventory нет; происхождение
конкретных файлов и потеря прежних копий не доказаны. Содержимое не читали,
живые backups не удаляли и не пытались «исправлять». Этот риск остаётся
отдельным пунктом handoff и не закрывается source-review PASS.

## Следующая интеграция Card B

Backend namespace не содержит operation_seq: он обязателен только для Swift-local
validate RPC. Перед подключением Python writers требуется отдельный внутренний
путь per-file authorization, не расходующий Swift high-water и не добавляющий
обязательные IPC-поля. Это ещё не реализовано; source PASS выше относится к Card A.

Полный текущий handoff: `2026-10-03-a53-autonomous-progress.md`.
