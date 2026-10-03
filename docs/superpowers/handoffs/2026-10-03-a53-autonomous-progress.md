# A5.3 — автономное продолжение, 2026-10-03

## Актуальный фронт, 2026-10-03 — Card A готов к source PR

Goal ACTIVE. Основная модель Sol High; независимое whole-diff review Astra High
завершилось PASS после исправления всех подтверждённых замечаний. Card B/C/D
ещё не реализованы. Merge, deploy/restart, включение шифрования, Keychain,
живые экспорты, ротация и соседние репозитории не входят в выполненную работу.

- Свежая удалённая база `efecb801aae3f3c62fa03314ba4f5b142ac8e906`.
- PR #2076: `0af3c67cf1f3ee88739179e5640e640921330b7b`, exact-head checks
  SUCCESS, Swift штатно SKIPPED, OPEN. Startup #2075 остаётся отдельным PR.
- Card A: четыре RPC; свежая policy/epoch/generation; terminal close;
  центральный durable commit и CAS; raw exact-bool до нормализаторов;
  безопасный PREPARE restore до мутаций; строгая startup ownership;
  redaction dispatcher/socket/audit/Sentry, в том числе encoded echoes.
- Локально: authorizer 138, protocol 12 + 9 subtests, IPC 5, settings 78,
  redaction 14; 47 зависимых файлов — 1266 passed, все per-file exit 0.
- Python 3.12 parity без MLX: **50 + 6 файлов, ALL GREEN**, оба harness exit 0.
  Полный `make audit-all` PASS, CI-style flake8 PASS, diff-check PASS.
- Независимый source PASS и hashes: см. соседний
  `2026-10-03-a53-card-a-verification.md`. Это не CI и не live acceptance.
- Новый source PR/exact-SHA CI — следующий шаг; все source-файлы заморожены
  после source-review, последние изменения только fixtures/docs.

### Следующий блок B

Read-only recon выявил прямые writers history/timeline, Obsidian, scheduler,
SharingManager. В sharing индекс тоже содержит полный content и является
отдельным plaintext sink; constructor/list/revoke могут его переписывать.
При partial Obsidian нельзя продвигать cursor поверх недописанных записей.
Backend namespace содержит четыре поля без operation_seq; требуется внутренний
per-file authorize path, не конфликтующий с Swift high-water. Публичный IPC
контракт сохраняется; дизайн проверяется независимо до реализации.

### Риск прежней изоляции тестов

`SettingsService` без injected backup создавал домашний `SettingsBackup()`;
set-settings вызывает create_backup и rolling prune. Ранее выполненные тесты
имели этот путь. Read-only проверены только метаданные: в default-каталоге
четыре JSON reason before_set с mtime 15:26:39–40 CEST. Начального inventory
нет; происхождение конкретных файлов и потеря старых копий не доказаны.
Содержимое не читали, живые backups не удаляли и не пытались исправлять.
Нельзя утверждать, что прежние тесты точно не затронули живой каталог.

Исправлено в conftest: принудительный throwaway backup-каталог до app imports,
с удалением только собственного tmp после тестов; явные test backup_dir
сохранены. Все финальные parity-проверки выполнены после этого исправления.

Ниже — исторический снимок первых коммитов, не текущая очередь.

## Scope и рабочая база

Цель активна в Codex: завершить A–D до проверенных PR и решения владельца о
merge/release. Владелец разрешил автономную разработку и субагентов. Повторное
«продолжать?» не требуется. Деплой, production restart, включение шифрования,
удаление живых данных/копий, ротация и соседние проекты в цель не входят.

- Собственный worktree: `/Users/pablito/.codex/worktrees/ear-a53-completion/Krab Ear`.
- Ветка: `codex/ear-a53-completion`, исходный HEAD `bd12fa599d7dedf210c64ecca5acf0fe3297771b`.
- Свежий `origin/codex/krab-ear-v2`: `efecb801aae3f3c62fa03314ba4f5b142ac8e906`.
- Три наследуемых коммита A: `67bc46df`, `fd9dc3d5`, `bd12fa59`.
- Общий checkout и `.worktrees/a53-card-a-authorizer` содержат чужой WIP;
  не менять, не сбрасывать, не переносить вслепую.
- Карточки и обновлённая спека: PR #2076, `9f776abd24cb625dc6bcd1b9de88ff475f9be3d4`.
- Reviewed-контракт: ветка `codex/ear-a53-reviewed-handoff`, файл
  `docs/superpowers/handoffs/2026-09-30-a53-reviewed/A53_STRONG_MODEL_HANDOFF.md`.

## Проверено в этой сессии

Read-only субагент сверил GitHub: #2074 (`b91cff7c`), #2075 (`3a253bc5`),
#2076 (`9f776abd`) OPEN/MERGEABLE/CLEAN; по три workflow на exact headSha
завершились success. GitHub reviews/comments отсутствуют. Это не merge/release
approval. #2075: actionable дефектов в diff не найдено.

У #2076 нужно исправить карточки:

1. Card A: удалить оставшиеся инструкции предыдущего docs-этапа «код не
   реализовывать» / «не менять service/state_store/history_service», которые
   противоречат исполнительному scope самой карточки.
2. Card C: Swift build запускать в subshell, иначе следующие команды остаются
   в неверном cwd: `(cd native/KrabEarAgent && swift build -c release)`.
3. Card A: единый reason `plaintext_policy_unavailable`.

## Первый воспроизведённый и исправленный дефект

Legacy startup migration брала только два policy-флага из settings и писала
`DEFAULT_SETTINGS + flags`, теряя пользовательские значения и неизвестные ключи.
Теперь helper возвращает полный проверенный JSON-объект, который передаётся
прежнему центральному commit. Проверка exact-bool и duplicate keys сохранена.

- RED: новый `test_legacy_migration_preserves_user_settings_and_extension_keys`
  упал по llm_model, llm_rewriter_enabled и вложенному future_extension.
- GREEN: `TestStartupDoesNotWashUnknownIntoKnown` — 15 тестов, exit 0.
- Использован Python 3.14 из канонического `.venv_krab_ear`, реальный StateStore,
  только TemporaryDirectory; production settings не читались.
- `git diff --check` PASS.
- Независимый субагент дал PASS только для двухфайлового diff этого исправления.
  Это не whole-diff security acceptance A5.3.
- У pytest были предупреждения существующего окружения torchcodec/FFmpeg;
  аудиодекодирование и ML не проверялись.
- Полный изменённый test-файл, Python 3.12 parity, аудит и новый CI ещё не выполнены.

## Остаток Card A

Уже есть typed snapshot, revision/save/startup, RAM authorizer, cross-process
fixtures и создание authorizer до threads. Наличие тестов не равно их свежему PASS.

1. Проверить/завершить все settings commit paths. Чужой dirty slice 3 имеет риски:
   partial import получает defaults из cache и unconditional validated_repair;
   backup restore/rollback имеют аналогичный риск; explicit commit обходит A5
   ENC1 guards; integrity repair пока не получает живой store из service.
2. Fresh policy getter, атомарный expected epoch/generation grant handshake,
   четыре RPC, namespace, причины отказов, shutdown RAM cleanup.
3. Redaction в actual dispatcher/socket/Sentry/error paths и sentinel tests;
   никаких raw payload/exception secret echo.
4. Зафиксировать receipt semantics тестом: внешняя validation завершается до
   отправки ответа; уже разрешённая единичная immutable запись может закончиться
   после revoke. Текущий core consume перечитывает policy, а revoke удаляет
   receipts. Возможная проводка validate+consume до RPC success требует проверки;
   не объявлять её реализованной. Следующая запись требует новой validation.
5. IPC reference, targeted/parity/audit и независимый Astra High whole-diff gate.

Существующий второй `read_bytes()` при legacy migration после hardened snapshot
остаётся отдельным вопросом для whole-diff review; узкий первый фикс его не менял.

## Дальнейший порядок

После принятого A API — B (Python sinks), C (Swift coordinator/writers), затем D
(synthetic real IPC + Swift harness, actual writes и линеаризация). Карточки
последовательные; владельцы service.py не пересекаются. UI не перепроектировать.
Нужен isolated E2E; mocked units не дают production/encrypted-history acceptance.

На старте load average Mac был 414–444. Не запускать массовые тесты, Swift build,
ML/GPU или private CI под этой нагрузкой. Ресурсы перечитать перед тяжёлой фазой;
не останавливать чужие процессы. В этом блоке выполнены лишь один RED и один
узкий GREEN startup-класса, последовательно.

Модель: Astra High для security решений и независимого review; Sol Medium для
ограниченной механики/документации при доступности. Согласованные policy-вопросы
не спрашивать повторно. Текущий статус всей A5.3: НЕ завершена, goal ACTIVE.
