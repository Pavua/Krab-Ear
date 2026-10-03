# A5.3 Card B — рабочий checkpoint, 2026-10-03

Goal ACTIVE; основная модель Sol High, независимый review Astra High.
Worktree `/Users/pablito/.codex/worktrees/ear-a53-python-sinks/Krab Ear`,
branch `codex/ear-a53-python-sinks`, base Card A `93c7e0b0d46b730c64c4a924d48b0f124cbce369`.
Card A source PR #2077 OPEN: основной CI PASS, backend chunked CI ещё выполняется.
Card B: source review PASS и локальные gates PASS; commit/PR/CI — следующий шаг.
Card C/D код не начат; read-only Swift recon завершён. Production не менялся.

## Совместимый внутренний API

Backend namespace имеет ровно четыре поля, без operation_seq. Внутренний
`precheck_backend_export` использует общий с Swift validate evaluator;
`plaintext_export_sinks.run_export_write` делает свежую проверку и один callback
после освобождения locks. Нет backend receipts и конфликтов Swift high-water.
`BackendSink` выбирается кодом writer; missing/raising authorizer fail-closed.
Дополнение карточки B разрешает это расширение, сохраняя все A RPC/signatures.
PR #2076: `7a034d8f56c8c19396b5a287104625e6067aae42`.

## Владение и изменения

- root: service wiring/timeline, scheduler, этот handoff/IPC reference.
- card_b_authorizer: общий evaluator/helper, 35 новых tests, пять scheduler fixtures.
- card_b_history: history writers и context delegates, batch partial, 12 fixtures.
- card_b_managers: Obsidian/sharing, индекс как отдельный sink, 22 fixtures.
- card_a_whole_review: независимый Astra High whole-B source review, read-only.

Все менеджеры получают один authorizer до thread startup. Timeline resolver
больше не создаёт папку; fresh-authorized callback делает mkdir+write.
Scheduler прямой writer и pruning gated, ON не заимствует ручной grant,
partial не удаляет разрешённый файл и не продвигает schedule.

## Финальные локальные проверки и source gate

- Core41 GREEN; freshOFF mismatch воспроизведён двумя RED и исправлен:
  absent namespace + свежий OFF допускается после revoke старых grants;
  supplied context/Swift, UNKNOWN/privacy/чужой профиль остаются строгими.
- History184 GREEN; dependent12 files — 258 PASS. Empty-history file requests
  проверяют policy до раннего успеха; render-only не менялся.
- Timeline/scheduler15 GREEN на реальном BackendService/dispatcher с inert audio.
  Пять scheduler dependencies — 108 PASS, без тяжёлых конструкторов.
- Managers35 GREEN; dependent22 files — 402 PASS. Baseline behavioral RED
  подтвердил actual mkdir/write ON/no grant, не только отсутствие нового API.
- Полный A regression: authorizer138, protocol12 (+subtests), IPC5, redaction14
  GREEN после shared evaluator refactor.
- Python3.12 без MLX: **49 файлов ALL GREEN**, harness exit0. Включены две
  прежние auto/legacy encryption suites. Ручной grant не открывает legacy
  backup/archive/version/record/import; encrypted auto snapshot работает.
- Финальный `make audit-all` PASS; CI-style flake851files PASS; diff-check PASS.
- Независимое **Astra High whole-B source-review PASS**, reviewer tests/runtime
  не запускал. Source привязка до git-add нового helper:
  tracked backend diff vs93c7e0b0 SHA256
  `76c1ea2c29f176c9db86c9731629a3728e29e78fadef04d48143ec5a14b8726b`;
  plaintext_export_sinks.py SHA256
  `57ab08d322d5322db1970e959c1ed2de6c4f67bc3533355ee2484d445e136b58`.
  После PASS изменялись только тесты и документация.

Логи: `/tmp/ear-a53-card-b-parity.log`, `/tmp/ear-a53-card-b-audit-final.log`,
`/tmp/a53-b-final-results.json`, `/tmp/a53-manager-deps-results.json`.
Это локальные/source доказательства; Card B exact-SHA CI ещё впереди.

## Явное ограничение sharing

`revoke_share_link` переписывает индекс, содержащий plaintext других пакетов.
При ON нужен namespace, при privacy ON отказ даже с grant; ложного success нет.
TTL всё равно ограничивает чтение. Независимое review подтвердило это как
явный fail-closed результат narrow Card B; актуальных Swift callers не найдено.
Удаление без consent потребует отдельного дизайна persistent index/revocation,
не удаления payload первым и не RAM-only tombstone.

Тестовый слот один; conftest принудительно изолирует audit и settings backups.
Риск прежних Card A тестов до исправления изоляции сохранён в
`2026-10-03-a53-card-a-verification.md`; его не считать автоматически закрытым.

## Дальше

Source PR/exact-SHA CI для B. Затем C → D
по утверждённым карточкам. Merge/deploy/restart, шифрование, удаление живых
копий, ротация и внешние сообщения в выполняемую цель не входят.

## Read-only разведка следующей Card C

- Один @MainActor coordinator с явной FIFO/inflight (await допускает reentrancy),
  canonical UUID.lowercased(), семь fixed local sinks. Одна operation ticket
  создаётся до panel/renderer callback; повтор callback не должен получать новый seq.
- Использовать существующий IPCClient.callAsync + IPCSocketProviding, оба envelope
  уровня; не callAsyncWithRecovery (он restart/retry). Ошибки только stable reasons,
  без raw localizedDescription. Echo epoch/generation/seq/sink проверить до writer.
- Сохранять pre-panel policy context; не refresh-and-accept новую policy после
  held panel. Writer после validation синхронный, immutable destination/content.
- Coordinator можно проверять лёгким swiftc harness с тем же production source
  и IPCClient, без main.swift/app startup. XCTest target уже есть; CI filter
  WiringTests|SourceContract надо расширить, иначе новые tests только соберутся.
- Injection: main.swift -> HistoryPanelController; обе Analytics launchers
  (+Analytics и +Settings+ClaudeDesign) -> window -> VC; standalone meeting через
  main+MeetingPanel -> presentMeetingReportStandalone -> MeetingReport VC.
- .md backend copy запускать только после успеха первой local write; отдельный
  backend context/validation. PDF fresh validation после renderer; temp PDF sink.
- QuickCapture Obsidian success НЕ содержит ok:true, результат counts/errors/partial/
  reason. Ранний privacy deny старой wrapper даёт error/user_msg; не терять warning
  за общим «Заметка сохранена». История сохраняется независимо от copy denial.
- Quit: WillTerminate сразу останавливает backend, поэтому bounded best-effort
  revoke лучше через ShouldTerminate/terminateLater, RAM очистить независимо от ответа.

Разведчики не запускали Swift/build/app. C изменения пока не сделаны.
