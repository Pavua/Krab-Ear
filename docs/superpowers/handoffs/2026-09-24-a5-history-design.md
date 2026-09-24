# A5 — точка продолжения 24.09.2026

## Что принято владельцем

- Scope сейчас: история и её внутренние журналы; общий аудит остальных PII позже.
- Plaintext-файлы/Obsidian при encryption ON — только после подтверждения.
- Подтверждение запоминается до конца сессии и включает Quick Capture → Obsidian.
- Старые plaintext-артефакты: инвентарь, затем отдельное решение владельца.
- Активация `history_encryption_enabled`, реальные ключи/история и live migration
  не входят в текущую source-разработку.

## Артефакты

- Спека: `docs/superpowers/specs/2026-09-24-a5-history-at-rest-design.md`.
- Исполнимая первая карточка: `docs/superpowers/plans/2026-09-24-a5-journal-codec.md`.
- Worktree: `/Users/pablito/.codex/worktrees/ear-a5-history-completion/Krab Ear`.
- Ветка: `codex/ear-a5-history-completion`.
- База: `5ebfb8375392e45bd4d12da908cd8e7b47a642b0`, exact remote повторно сверена.
- Пока изменены только документы. Тестовые/production source-файлы не менялись;
  ни один pytest, build, IPC, Keychain или runtime probe в этой фазе не запускался.

## Независимый review и self-review

Reviewer прочитал код и спеку. Итог спеки APPROVE после исправления:

1. Сохранённый по решению владельца plaintext — перечисленное исключение,
   а не безусловное «вся история зашифрована».
2. Restore сохраняет объединение текущих tombstones/permanent purged IDs;
   старый snapshot не может воскресить удалённое.
3. Swift preflight разрешает одну операцию; межпроцессный race и same-UID IPC
   threat boundary названы явно, без обещания мгновенной crash-revocation.
4. Совместимый rollback должен понимать codec A5.1 и recovery A5.2.

По карточке reviewer нашёл fsync-retry hole: ID может быть уже записан,
но не durable; повтор компактации пропускает append и ошибочно очищает
tombstones. В карточку добавлены обязательный ledger fsync даже при пустом
новом append-наборе и fault-injection тест «write → fsync failure → retry».
Эта правка пока проверена статически; RED/GREEN ещё нет, карточка не является
доказательством исправленного кода. Все четыре Python code-block синтаксически
проверены `ast.parse`; diff whitespace check пройден.

## Ресурсное окно

Snapshot 24.09.2026 ~15:51 UTC: активен CI главного Краба,
`/Users/pablito/actions-runner-krab/_work/Krab-openclaw/Krab-openclaw`.
Runner.Worker PID 86805 → pytest 87068 → дочерний pytest 97747;
swap used 31254.50 MiB. Процессы не трогались. Это исторический snapshot:
перед тестами перепроверить процессы и ресурсы, не считать PID постоянными.

## Следующий шаг

1. Повторно сверить exact remote/base, собственный worktree status и ресурсы.
2. В свободное окно выполнить A5.1 Task 1 RED на synthetic временном профиле,
   затем минимальный source fix и targeted GREEN; Task 2 аналогично.
3. Не читать/копировать живую историю или Keychain для «проверки».
4. Отдельные процессы для зависимых legacy tests, ubuntu parity и audit-all;
   независимый whole-diff gate перед PR/merge. Не пересоздавать `/tmp/py312`
   автоматически: штатный parity script умеет удалять/rebuild shared venv.
5. После A5.1 остаются A5.2 transaction/backup/derived copies и A5.3 Swift/session
   exports. Полный A5, deployment и activation этим docs-коммитом не закрыты.

Исполнение экономное: основной агент последовательно; один ограниченный
независимый reviewer на финальный diff. Sol Medium подходит для A5.1 по готовой
карточке; Astra High — для сложной миграции A5.2 и финального security gate.
