# A5.3 Card C — промежуточный source checkpoint, 2026-10-03

Цель A–D ACTIVE. C source review и локальные ворота приняты;
CI и Card D isolated IPC E2E ещё впереди. Card A CI выявил старые fixtures,
их исправление ведётся отдельно в ветке A; это блокирует финальный A–D gate.
Это не production acceptance; merge/deploy/restart/encryption activation не было.

## Проверенная цепочка

- Main: `efecb801aae3f3c62fa03314ba4f5b142ac8e906`.
- A: [#2077](https://github.com/Pavua/Krab-Ear/pull/2077),
  `93c7e0b0d46b730c64c4a924d48b0f124cbce369`, source Astra High PASS,
  parity56files/audit PASS. Полный CI: 22 SUCCESS, 2 FAILURE,
  3 SKIPPED; 29 тестовых файлов требуют разбора/обновления. Преобладают
  fixtures без explicit initial policy и старые сигнатуры заглушек.
  Лог `/tmp/a53-card-a-ci-failed.log`; production fail-closed не ослабляется.
- B: [#2078](https://github.com/Pavua/Krab-Ear/pull/2078),
  `9358cd7876d98e5b98eb97e5982eec31cef7a4d2`, source Astra High PASS,
  parity49files/audit PASS. Свежая сверка: 22 SUCCESS, 2 IN_PROGRESS,
  3 SKIPPED; exact-SHA CI ещё не завершён.
- Startup #2075 `3a253bc52003572471b18c6b2dae45c58c0abca2` и docs #2076
  `7a034d8f56c8c19396b5a287104625e6067aae42`: 24 SUCCESS, 3 SKIPPED,
  OPEN/unmerged.
- C: `codex/ear-a53-swift-consent` поверх B, собственный managed worktree
  `/Users/pablito/.codex/worktrees/ear-a53-swift-consent/Krab Ear`.
  C подготовлен к отдельному commit/PR; зависит от B и исправлений CI в A.

## Реализовано и принято source review

- Один RAM coordinator, explicit FIFO поверх MainActor, непрозрачные одноразовые
  tickets, строгая проверка policy/grant/validate, без recovery/retry.
- Контекст фиксируется до SavePanel/PDF render; fresh validation непосредственно
  перед синхронной write closure. Удаление ticket до await не даёт повторному
  callback выполнить вторую запись.
- History MD/NDJSON/selection/action items/meeting/stats и analytics PDF
  подключены к одному экземпляру через обязательную dependency injection.
- Markdown backend-копия получает отдельный ticket и запускается только после
  успешного локального сохранения. Отказ второй копии отображается отдельно.
- Quick Capture → Obsidian использует тот же coordinator. Отказы/partial и
  недоступность настроек копирования не скрываются сообщением об успехе истории.
- Consent через sheet, без runModal; отсутствие родительского окна означает
  отсутствие согласия. Закрытие синхронно закрывает coordinator, best-effort
  revoke ограничен watchdog; без grant приложение завершает работу сразу.
- CI Swift filter расширен `PlaintextExportCoordinatorTests`, чтобы новые
  behavioral tests действительно исполнялись.

UI-workers подтвердили только parse/diff-check. Это не full build и не UI E2E.
Лёгкий Python harness компилирует те же production IPCClient/coordinator и
исполняемые Swift tests без запуска AppDelegate/production приложения.

## Подтверждённые локальные ворота

- 17 исполняемых Swift behavioral groups PASS; Python launcher с actual swiftc
  и запуском production-кода: 1 passed, 5.69 s. Лог:
  `/tmp/a53-swift-coordinator-green.log`.
- RED→GREEN: изъятый билет в FIFO после неопределённого предыдущего RPC;
  normal quit во время grant; normal quit после утраты active grant из-за
  ошибки. Логи: `/tmp/a53-swift-queued-{red,green}.log`,
  `/tmp/a53-swift-lifecycle-{red,green}.log`, `/tmp/a53-swift-lost-grant-red.log`.
- Независимый Astra High whole-diff source review: PASS. Исправлены два
  lifecycle P2 и ложное «копия не создана» после неопределённого ответа.
- `make audit-all` PASS: `/tmp/ear-a53-card-c-audit.log`.
- Release build `-j 1` PASS, 153.97 s:
  `/tmp/ear-a53-card-c-swift-release.log`.
- Swift build-tests: первоначальный RED на actor isolation тестового callback;
  явный `@MainActor` исправил Swift6 ошибку без изменений production. Повторная
  сборка PASS, 324 выбранных Swift-теста PASS (включая 17 coordinator tests):
  `/tmp/ear-a53-card-c-swift-build-tests-green.log`,
  `/tmp/ear-a53-card-c-swift-tests.log`.
- 8 зависимых Python-файлов PASS, результаты
  `/tmp/a53-card-c-python-results.json`. Python3.12 parity нового launcher PASS:
  `/tmp/ear-a53-card-c-parity.log`. flake8 launcher и diff-check PASS.

Source freeze (19 source/test/workflow файлов, docs исключены):
manifest SHA256 `71940bcc96fedccb496e68bff24e21c41c1cc7391c7b513f6b92eb207b0cf920`;
формат отсортированных строк: `file_sha256  relative_path\n`.
Coordinator: `b48c35d342fe22d60bfdbad428005c33a961591bf4c19aa470de6aa09a5d488a`;
Swift tests: `aeea6112ce090293f6ce97677bebe39a2c39c5ae52be28bba60a198c9db2a65d`.
Отдельное Astra High mini-delta review аннотации callback PASS; остальные
18 файлов совпадают с первоначальным review.

Timeline UI не входит в Card C и не передаёт session context: при encryption
ON backend отказывает даже после согласия в другом окне. Это известная граница
UX, а не обход защиты. Live-проверка UI в production не выполнялась.

## Оставшиеся ворота

1. Исправления A CI, их перенос в зависимые ветки и exact-SHA проверка.
2. C PR + exact-SHA CI; D по исправленной карточке из ветки #2076:
   synthetic temporary profile, real isolated IPC socket, production Swift
   writer harness, deterministic barriers, actual file counts и redaction.
3. Финальная матрица требований, актуальный NOW и handoff.

Существенные ограничения B и прежний риск backup-изоляции тестов сохранены в
[Card B](2026-10-03-a53-card-b-progress.md) и
[Card A](2026-10-03-a53-card-a-verification.md). Не заявлять, что вся прежняя
сессия гарантированно не касалась живой backup-папки. Текущие тесты используют
принудительную temp backup/audit isolation из conftest.
