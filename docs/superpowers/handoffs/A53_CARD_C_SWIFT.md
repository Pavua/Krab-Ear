# A53 Card C — Swift (coordinator, sheets, preflight, Quick Capture)

База: свежий `origin/codex/krab-ear-v2`. Предусловия: карточки A (API) и B
(Python gates) приняты. Контракт SHA256
`9bb260fe303b081a4883f9e4da8d7e3bffeebc8774898f69ed92acf30c5def5a`
(FINAL GO); спека §7 (особенно 7.4, 7.6–7.7). Следовать async sheet helpers, не
`runModal()` (AppHang-гейт CI).

## Scope (входит)

- Новый `PlaintextExportCoordinator.swift`: RAM `app_session_id` (UUID/launch) +
  capability; shared сериализованный coordinator (`operation_seq` монотонен);
  sheet → grant → per-write preflight → одноразовый receipt → одна closure;
  policy/epoch mismatch сбрасывает grant без autoregrant/retry.
- Один RAM instance в `main.swift`; inject в перечисленные controllers и
  QuickCapture; normal quit revoke best-effort (crash не гарантирует отзыв
  украденного токена — честно).
- Точки: `HistoryPanelController+History.swift` .md:141, .ndjson:192 +
  backend-копия:158 (две записи = две validation); `+ExportSelection.swift:325`
  (fresh preflight после NSSavePanel); `+ActionItems.swift:230/266`;
  `+MeetingMode.swift:267` (+ inject в standalone report VC);
  `+StatsReport.swift:98`; `AnalyticsDashboardViewController+PDFExport.swift:110`
  (fresh validation ПОСЛЕ renderer callback; temp тоже sink);
  `main+QuickCapture.swift:713` (`run_obsidian_sync` с session context вместо
  голого `force:true`; ошибки не игнорировать молча — показать причину).
- OFF тоже fresh validation для Swift (privacy/restart); envelope ошибок без
  секретов; `error.localizedDescription` без sentinel.

## Не входит

- Не менять Python authorizer/sinks API; не вводить peer-auth (не обещается);
  показ/чтение истории grant не выдаёт; `confirm`/`force`/`save_to_file` context
  не заменяют; CallAssist/Import/glossary вне narrow scope.
- Не запускать собранный `KrabEarAgent` из воркера; второй production агент не
  запускать (только fake-transport + counting writer harness здесь; живой IPC —
  карточка D).

## Файлы

- Создать: `native/KrabEarAgent/Sources/KrabEarAgent/PlaintextExportCoordinator.swift`,
  тесты `.../PlaintextExportCoordinatorTests.swift` (fake transport + counting
  writer, не только source-contains).
- Править: `main.swift` (один RAM instance + quit revoke),
  перечисленные `HistoryPanelController+*.swift`, `PDFExport`, QuickCapture.

## Шаги

1. Coordinator: sheet-текст «открытые файлы, включая Quick Capture→Obsidian, до
   закрытия приложения/смены backend или policy»; cancel ничего не выдаёт; grant
   многоразовый scoped, но каждая запись — свежая validation.
2. После NSSavePanel/рендера зафиксировать destination + immutable content +
   `sink_kind`; preflight; receipt ровно один раз в одной closure.
3. Revocation BEFORE validation → 0 writes; AFTER успешной validation → только
   одна запланированная запись (включая atomic temp/rename); повтор
   receipt/callback/seq → 0 дополнительных.
4. Backend Markdown + вторичная Swift-копия — отдельные операции; после отзыва
   вторая запрещена даже если первая завершилась.
5. Quick Capture: ON без consent → note history штатно, vault без изменений, UI
   показывает причину; valid session → write; смена epoch → новый sheet.
6. Swift logs без capability/receipt/app-session secret (15b-сторона).

## Behavioral RED→GREEN (полные тексты из контракта, зона C)

- п.7: Swift held SavePanel: получить content/grant, изменить privacy/encryption
  или backend epoch, закрыть panel → ноль writes. Повторить renderer callback
  для PDF и standalone MeetingReport VC.
- п.10: Swift history `.md` + backend copy: первая validation не разрешает
  вторую копию; после отмены/ошибки первой нет скрытого второго export.
- п.11: Quick Capture: ON нет consent → note history сохраняется штатно, vault
  не меняется, UI показывает причину; valid session → write; force не обход;
  смена epoch требует новый sheet.
- Swift fake-transport (п.14-сторона): исполняемые Swift tests используют fake
  transport и counting writer, а не только source contains. Python service
  fixtures обязательно `close()`; не запускать all-tests/ML/GPU ради gate.

## Команды (исполнителю карточки, не выполнять здесь)

```bash
(cd native/KrabEarAgent && swift build -c release)   # только в свободное окно при ресурсах
PYTHONPATH="$PWD/KrabEar" /Users/pablito/Antigravity_AGENTS/Krab\ Ear/.venv_krab_ear/bin/python -m pytest KrabEar/tests/test_plaintext_export_swift_harness.py -v  # если есть py-сторона harness
scripts/pre_merge_py312_check.sh KrabEar/tests/test_plaintext_export_swift_harness.py
make audit-all
```

## DoD

- Held-panel/PDF/MeetingReport: 0 writes после смены policy/epoch; двойная копия
  требует двух validation; Quick Capture ведёт себя по п.11.
- Fake-transport tests считают actual writes counting writer; source-contains
  один недостаточен.
- Никаких `runModal()`; quit revoke wired; секреты вне logs.

## Gate

Без whole-diff независимого Astra High review — BLOCK.
