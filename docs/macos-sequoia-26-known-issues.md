# macOS Sequoia 26 (15.x) Known Issues для Krab Ear

Документ консолидирует все Sequoia-специфичные баги, паттерны и workaround'ы,
найденные в ходе разработки (Waves 44–416).

---

## TCC permission quirks

### Симптом
- System Settings показывает toggle включённым, но TCC.db не обновляется немедленно.
- Krab Ear запрашивает permission снова через ~600 s (PasteService fallback loop).
- `AXIsProcessTrusted()` возвращает `false` несмотря на визуально зелёный toggle.

### Root cause
macOS Sequoia изменил момент записи TCC-изменений в БД — update откладывается
до следующего commit-окна (обычно несколько секунд, но может быть до нескольких
минут при нагрузке I/O или в sleep/wake цикле).

### Fix
1. **Стабильная подпись** (`Krab Ear Dev Local`, PR #235): TCC привязан к hash
   signing identity, а не к пути. Rebuild не инвалидирует grant.
   ```bash
   scripts/create_local_signing_identity.command   # one-time
   make sign
   ```
2. **Явный re-add через `+`** (не toggle):
   `System Settings → Privacy → Accessibility → (−) удалить → (+) добавить снова`.
3. **Диагностика**: `sqlite3 "$HOME/Library/Application Support/com.apple.TCC/TCC.db" "SELECT service, client, auth_value FROM access WHERE client LIKE '%krab%';"`  
   Ожидаемый `auth_value = 2` для всех строк.
4. **Автоматизация**: `scripts/repair_permissions.command` (PR #234).

### Связанные компоненты
- `native/KrabEarAgent/Sources/KrabEarAgent/PasteService.swift` — `AXIsProcessTrusted()`
- `native/KrabEarAgent/Sources/KrabEarAgent/main.swift:306-314` — `AXIsProcessTrustedWithOptions`
- `docs/TROUBLESHOOTING_PERMISSIONS.md` — полное руководство

---

## CoreText "first render" penalty (AGENT-J/K/M class)

### Механизм
На macOS Sequoia ColorSync transform, glyph-metrics build и NSVisualEffectView
layout **выполняются синхронно на main thread** при первом рендере. Если первый
рендер происходит внутри callback или IPC-обработчика — main thread блокируется
≥2 с → AppKit watchdog фиксирует AppHang.

### Три задокументированных случая

| Агент | Wave | Компонент | Триггер | Fix |
|-------|------|-----------|---------|-----|
| AGENT-J | 67 | `StatusIndicatorView.swift` | Unicode `●` (U+25CF) в NSStatusItem → CoreText system font chain при ColorSync callback | Заменить на SF Symbol `circle.fill` через `NSImage(systemSymbolName:)` |
| AGENT-K | 66 | `BackendToast.swift` | NSVisualEffectView ColorSync transform при первом `createPanel()` | `prewarmPanel()` в `applicationDidFinishLaunching` |
| AGENT-M | 266 | `BackendToast.show()` | `label.sizeToFit()` с Cyrillic+emoji строкой до `orderFront` | `prewarmPanel()` вызывает `sizeToFit()` заранее; `positionPanel()` до `orderFront` |

### Fix pattern: prewarm в applicationDidFinishLaunching

```swift
// main.swift — в applicationDidFinishLaunching
BackendToast.shared.prewarmPanel()
// NSStatusItem dot warmup через прогрев NSFont системного шрифта
_ = NSFont.systemFont(ofSize: 13)
```

### Правило для нового кода
- **НЕ** рендерить Unicode-глифы (`●◉○▲▼◀▶✓✗◉■★`) в `NSTextField.stringValue`
  внутри callbacks, ColorSync notifications, `windowDidChangeOcclusionState`.
- **Использовать SF Symbols**: `NSImage(systemSymbolName: "circle.fill", accessibilityDescription: nil)`.
- **prewarmPanel()** для любого нового `NSPanel + NSVisualEffectView`.
- **Позиционировать панель ДО `orderFront`** (избегает layout pass в `_doOrderWindow`).

### Известные оставшиеся сайты (требуют наблюдения)
- `CallAutomationController.swift:143` — `NSTextField(labelWithString: "●")` (provider status dot)
- `CallAutomationController.swift:211,963,974` — `"●  Ожидание"`, `"●  \(status)"`
- `GlobalStatusBar.swift:251,253` — `"▶"`, `"◉"` в `iconForOp()` (строки меню, не NSAttributedString в ColorSync path)
- `BackendToast.swift:58` — `"Backend перезапущен ✓"` (прогрет через `prewarmPanel()`, безопасно)

---

## macOS auto-update scheduled reboot

### Паттерн
Ежедневный reboot в **14:04 CEST (12:04 UTC)** при всех трёх флагах:
```
AutomaticCheckEnabled = 1
AutomaticDownload = 1
AutomaticallyInstallMacOSUpdates = 1
```

### Impact
- Backend (launchd Variant B) перезапускается за ~5 min (ThrottleInterval + Python warmup).
- VPN сервис `com.po.vpnserver` имеет `KeepAlive=false` → не поднимается автоматически.
- Downstream VPN-клиенты теряют соединение на неопределённое время.

### Fix (Wave 316)
```bash
sudo defaults write /Library/Preferences/com.apple.SoftwareUpdate \
  AutomaticallyInstallMacOSUpdates -bool false
```

Проверка: `defaults read /Library/Preferences/com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates`

### Дополнительно
- **Rapid Security Response (RSR)** — Apple sub-update может форсировать immediate restart
  даже при выключённом флаге, если патч критический.
- Документ: `docs/wave316-reboot-vpn-mitigation.md`

---

## launchd Variant B nuances

### Конфигурация
- `KeepAlive = true`, `ThrottleInterval = 5` — backend respawn через 5 с после crash.
- **Wave 50 bug (FIXED, PR #422)**: `ensure_agent_running.command` таймаут был 5 с —
  недостаточно для Python warmup. Увеличен до 15 с.
- **Wave 50 launchd self-recovery bug (FIXED)**: `set -e` + `pgrep` в plist script
  вызывал немедленный exit при non-zero exit code от pgrep. Исправлено в
  `scripts/install_agent_launchagent.command` (убран `set -e`, explicit check).

### Пути сокетов
| Режим | Путь |
|-------|------|
| Production (launchd Variant B) | `~/Library/Application Support/KrabEar/krabear.sock` |
| Dev standalone | `~/.krab_ear_data/backend.sock` |

### Agent launchd plist
Opt-in установка (Wave 59, PR #395):
```bash
scripts/install_agent_launchagent.command
```

---

## Two-binary drift

### Проблема
Krab Ear имеет **два бинаря**:
1. `Krab Ear.app/Contents/MacOS/KrabEarAgent` — запускается через Dock / launchd plist
2. `native/runtime/KrabEarAgent` — запускается в dev mode напрямую

При login-сессии может запускаться `native/runtime`, при Dock click — bundle.
Несинхронизированные версии → "backend недоступен" симптомы + TCC mismatch.

### Диагностика
```bash
# Сравни hash бинарей
md5 "Krab Ear.app/Contents/MacOS/KrabEarAgent" native/runtime/KrabEarAgent
```

### Fix
```bash
make release  # пересобирает + копирует в оба места + codesign
# или вручную:
cd native/KrabEarAgent && swift build -c release
cp -f .build/release/KrabEarAgent ../../"Krab Ear.app/Contents/MacOS/KrabEarAgent"
cp -f .build/release/KrabEarAgent ../runtime/KrabEarAgent
codesign -s "Krab Ear Dev Local" -f "../../Krab Ear.app/Contents/MacOS/KrabEarAgent"
codesign -s "Krab Ear Dev Local" -f ../runtime/KrabEarAgent
```

**Wave 274 (v2.0.3) lesson**: AGENT-J SF Symbol fix был в source с Wave 67,
но running binary в production оставался старым из-за two-binary drift.

---

## PyTorch MPS regression

### Симптом
`torch.backends.mps.is_available()` возвращает `False` на M4 Max + macOS 26.x
несмотря на наличие GPU (открытый issue pytorch/pytorch#167679).

### Impact
- `pyannote.audio` diarization не может использовать MPS device.
- SeamlessStreaming 2.5B деградирует на CPU (3–4× медленнее).

### Workaround
`diarization_device = "cpu"` в settings (автоматически если MPS недоступен).
Фиксить апстрим. Pin: torch≥2.11 когда доступен.

---

## NSAlert.runModal риски

### Проблема
`NSAlert.runModal()` без parent window создаёт отдельный modal run loop.
На macOS Sequoia при вызове из callback (ColorSync, accessibility, IPC dispatch)
это может заблокировать main thread → AppHang (Sentry KRAB-EAR-AGENT-E class).

### Текущие сайты в коде
- `PermissionWizard.swift:52,69,92` — три `runModal()` вызова (wizard flow,
  вызывается только из `applicationDidFinishLaunching` — приемлемо).
- `DiagnosticsTabView.swift:537` — fallback `runModal()` при nil window
  (защищён `if let window` проверкой, но fallback остаётся).

### Правило
Всегда использовать `beginSheetModal(for:completionHandler:)`.
При `window == nil` — логировать и возвращать nil, НЕ вызывать `runModal()`.
Образец: `AlertHelpers.swift`.

---

## Тестирование на macOS Sequoia

### Рекомендации
- CoreText warmup требует ≥1 с после `applicationDidFinishLaunching` перед первым
  NSAttributedString рендером в callback context.
- XCUITest Accessibility queries flaky после permission reset — запускать с `--retry-count 2`.
- TCC cache не синхронизируется немедленно — после grant ждать 2–3 с или перезапускать app.
- `AXIsProcessTrusted()` в unit test sandbox всегда возвращает `false` — защищать тесты guard'ом.

### Связанные файлы
- `native/KrabEarAgent/Tests/KrabEarAgentUITests/KrabEarAgentUITests.swift:29,233,309`
- `docs/TROUBLESHOOTING_PERMISSIONS.md`
- `scripts/repair_permissions.command`
- `scripts/create_local_signing_identity.command`
