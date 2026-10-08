# A5.3 — восстановление CI-контрактов тестов

База Card A: `93c7e0b0d46b730c64c4a924d48b0f124cbce369`, PR #2077.
Изолированная ветка: `codex/ear-a53-ci-contracts-20261008`.
Production-код не изменён. Merge, deploy, рестарт и активация шифрования
в этот блок не входят.

## Изменения

- Synthetic StateStore явно инициализирует политику нового профиля до создания
  истории/побочных файлов. UNKNOWN сохраняет запрет записи.
- Privacy-тесты проверяют exact bool и отсутствие мутации при неверном типе;
  обычные несвязанные переключатели сохраняют прежний контракт coercion.
- Повреждённые настройки не ремонтируются generic auto-repair; тест проверяет
  отказ и неизменность оригинала. Fault injection перенесён на actual atomic replace.
- Backend fixtures используют безопасные заглушки аудио и обязательный close.
- Четыре IPC policy/authorization метода получили отдельные заголовки справочника.
- Metrics FakeStateStore получил собственный TemporaryDirectory/data_dir:
  REST phonetic vocabulary больше не падает при независимой коллекции этого файла.
  Эта несовместимость фикстуры существует и на базовом efecb801.

## Проверка

Все 30 файлов, найденных в failed CI Card A: **731 passed, 37 subtests passed,
1 skipped**, по одному файлу/процессу, Python 3.12.11. Три первоначальных сбоя
временного runner (root sys.path и разрешение собственных коротких Unix socket
каталогов) устранены; повторены только эти три файла. Итог сохранён локально
в serial-final-results.json временного verification-каталога.

Metrics regression: до изменения AttributeError при коллекции (нет data_dir);
после изменения 15 passed. Независимый Astra Ultra source delta review: PASS
для 28 test-файлов и IPC-документации; проверенный patch SHA256:
`437df26864acd757db82c85cda999667f67f40a09ee610d7e85ab8dfe9631931`.
`git diff --check`: PASS. Документ добавлен после review как отчёт проверки.

Окружение CPU-only без MLX/Torch/pyannote: это targeted Python 3.12 проверка,
не полная Ubuntu/ML квалификация. Временный Python wrapper перенаправляет home,
backup/data paths, запрещает TCP connect и ограничивает Unix endpoints своими
fixture-каталогами; он **не является герметичным OS sandbox**. Production
backend/REST/Swift не запускаются и не перезапускаются этим набором.

Endor package-risk для pytest 9.1.1: UNKNOWN — отсутствует Endor авторизация;
решения SAFE/DENY нет. Новая авторизация/плагины не устанавливались.

## Оставшиеся ворота

1. Exact-SHA GitHub CI обновлённого A; перенести этот же test/docs commit в B/C.
2. Только после отдельных GREEN A/B/C — isolated Card D IPC/Swift E2E,
   independent review, parity/audit и exact-SHA CI. Production lifecycle отдельно.
3. Унаследованный Sentry P2: temp-dir privacy test может маскировать сломанный
   privacy gate; отдельный последующий тест с fake SDK и positive control.

Чужой незакоммиченный A/D WIP сохранён; копия подготовленного A patch сверена
по SHA256 и применена только в новом worktree. Старые handoff-проценты и
runtime PID не являются текущими доказательствами.

## Перенос в B/C

В Card B старые Wave-31 CSV tests дополнительно требуют существующий
`history_service_with_off_policy`: он подключает реальный authorizer к snapshot
синтетического StateStore. Двухстрочный fixture delta: independent Astra Ultra
source PASS; RED 13 failures (включая 6 subtests), GREEN 12 passed + 6 subtests.
Assertions и production denial без authorizer сохранены.

B: тот же 30-file набор GREEN (731 passed, 37 subtests, 1 skipped). Два IPC
файла сначала получили sandbox PermissionError на bind временного Unix-сокета;
повторены с разрешением только на собственные fixture endpoints и прошли.
Проверка C и GitHub CI всех новых HEAD выполняются отдельно.

## Полный CI B/C: ещё четыре legacy-контракта

A `00ba3300`: все 27 checks GREEN, оба backend jobs SUCCESS. Полный failed C
run 37717746118 на `55aec038` выявил ещё четыре test-файла: backwards_compat,
full_workflow, timeline_export_traversal_w19, wave37_obsidian_backup_privacy.
JSON/Obsidian fixtures теперь явно инициализируют synthetic policy и подключают
существующий реальный authorizer; workflow закрывает BackendService до cleanup.
Timeline resolver возвращает проверенный путь без mkdir до authorization:
тест проверяет точный путь и отсутствие mutation. Sibling-path тест получает
явный synthetic home вне /tmp, чтобы redirected home не оказался в другом
разрешённом корне. Production-код не изменён.

Изолированный RED воспроизвёл CI assertions; GREEN всех четырёх файлов:
44 + 49 + 7 + 8 = **108 passed**. В дополнительном runner native sounddevice
подавлен до pytest collection; общий conftest не изменён. Independent Astra
Ultra source delta PASS; patch SHA256
`06c4035fef1aa9b128e29d137a0ac6bc52321440a8aef970c3fa302d97c67b56`.
Новые exact-SHA CI B/C проверяются отдельно; этот локальный PASS их не заменяет.
