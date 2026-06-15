# ТЗ: Menu-bar «Сводка дня» (Daily Recap в выпадающем меню status bar)

## Цель / UX
Агент Krab Ear — `LSUIElement` с иконкой в menu bar (NSStatusItem). Добавить в
ВЕРХ выпадающего status-меню компактную карточку «Сводка дня» — сегодняшние
метрики одним взглядом, без открытия большой History-панели:
- 3 metric-плитки в ряд: **Записей** / **Минут** / **Слов**.
- Чипы топ-тем (до 4).
- Строка-статус (генерируем / готово / приватность / ошибка / нет данных).
Данные — из backend IPC `generate_daily_digest` (за сегодня). Обновляется при
каждом открытии меню.

## Файлы
1. **СОЗДАТЬ** `native/KrabEarAgent/Sources/KrabEarAgent/main+MenuBarRecap.swift`:
   - класс `MenuBarRecapView: NSView` (фиксированная ширина ~260pt) — карточка.
   - extension `AgentAppDelegate: NSMenuDelegate` с `menuWillOpen(_:)`.
   - хранимое свойство для ссылки на текущую view (для refresh при открытии).
2. **ПРАВИТЬ ТОЧЕЧНО** `native/KrabEarAgent/Sources/KrabEarAgent/main+StatusMenu.swift`
   — функция `rebuildStatusMenu()` (строка ~174). СРАЗУ после `let menu = NSMenu()`
   (строка 179) вставить recap-пункт В НАЧАЛО + сепаратор, сохранить ссылку,
   назначить delegate, запустить первый fetch. НИЧЕГО больше в этой функции не
   трогать (она большая — остальные пункты/сабменю оставить как есть).

## 🔴 Карта проводки (НЕ ломать)
- `AgentAppDelegate` имеет `var ipcClient: IPCClient` (main.swift:107). Метод
  `ipcClient.call(method: String, params: [String: Any]) throws -> [String: Any]`
  — СИНХРОННЫЙ, звать ТОЛЬКО off-main.
- `statusItem: NSStatusItem?` — свойство AppDelegate. Меню финализируется на
  строке 641: `statusItem.menu = menu`.
- `MenuBarRecapView` получает `ipcClient` через параметр метода `refresh(ipcClient:)`
  (НЕ хранить сильную ссылку на AppDelegate — передавать в refresh).

### Точный IPC-паттерн (зеркалить HistoryPanelController+DailyRecap.swift:104-134):
```swift
func refresh(ipcClient: IPCClient) {
    // показать «Генерируем…» на main
    DispatchQueue.global(qos: .userInitiated).async { [weak self] in
        do {
            let response = try ipcClient.call(method: "generate_daily_digest", params: [:])
            let result = (response["result"] as? [String: Any]) ?? [:]
            if (result["ok"] as? Bool) == false {
                let privacy = (result["reason"] as? String) == "privacy_mode_active"
                DispatchQueue.main.async {
                    self?.showStatus(privacy ? "Сводка недоступна в режиме приватности"
                                             : "Нет данных за сегодня")
                }
                return
            }
            DispatchQueue.main.async { self?.populate(result) }
        } catch {
            DispatchQueue.main.async { self?.showStatus("Ошибка: \(error.localizedDescription)") }
        }
    }
}
```
- Поля `result` (когда НЕ privacy): `total_recordings`(Int), `total_duration_min`(Double),
  `total_words`(Int), `top_topics`([String]), `languages_used`([String:Int]),
  `highlights`([String]), `date`(String). Пустой день → `total_recordings == 0`
  (показать мягкое «За сегодня записей нет», без плиток).
- `total_duration_min` форматировать 1 знак (`String(format: "%.1f", v)`).
- `total_words` — с разделителем тысяч (NumberFormatter `.decimal` или вручную).

### Wiring в rebuildStatusMenu (вставить после строки 179 `let menu = NSMenu()`):
```swift
let recapView = MenuBarRecapView()
let recapItem = NSMenuItem()
recapItem.view = recapView
menu.addItem(recapItem)          // в начало меню
menu.addItem(.separator())
self.menuBarRecapView = recapView
menu.delegate = self
recapView.refresh(ipcClient: ipcClient)   // первичный fetch
```
(остальной существующий код функции — recordItem, historyItem и т.д. — НЕ трогать,
он добавится ПОСЛЕ recap-блока.)

### menuWillOpen (в новом файле):
```swift
extension AgentAppDelegate: NSMenuDelegate {
    func menuWillOpen(_ menu: NSMenu) {
        menuBarRecapView?.refresh(ipcClient: ipcClient)
    }
}
```
Свойство `var menuBarRecapView: MenuBarRecapView?` объявить в новом файле как
extension-stored нельзя (Swift) → объяви его в main.swift рядом с `var statusItem`
(строка ~123) ОДНОЙ строкой: `var menuBarRecapView: MenuBarRecapView?`. Это
единственная правка main.swift кроме неё ничего там не менять.

## 🔴 Жёсткие правила (CI-гейты и AppHang — НЕ нарушать)
1. **AGENT-3**: IPC `ipcClient.call` — ТОЛЬКО внутри `DispatchQueue.global(...).async`;
   любое касание UI (NSTextField.stringValue, addSubview, layout) — ТОЛЬКО на
   `DispatchQueue.main.async`. Никогда не звать `.call` на main.
2. **AGENT-J glyph guard (CI-тест `test_swift_no_unicode_glyphs`)**: ЗАПРЕЩЕНЫ
   глифы `● ○ ◉ • ▶ ◀ ⇄ ▲ ▼ ★ ✕ ✓ ⏱` в `NSTextField(labelWithString:)` и
   `NSAttributedString(string:)`. Если нужен разделитель/маркер — использовать
   `·` (U+00B7 MIDDLE DOT) или `—`. НИКАКИХ bullet `•`.
3. **CoreText/AppHang (AGENT-K/M)**: НЕ класть emoji в `NSTextField(labelWithString:)`
   кастомной view (Cyrillic-текст в лейблах — ОК, menus строятся на main штатно).
   Текст лейблов держать простым.
4. **KrabEarTheme**: использовать токены `KrabEarTheme.Colors.*`, `.Typography.*`,
   `.Metrics.*` (есть `Metrics.standard`=8, `.comfortable`=12, `.cardPadding`,
   `.innerCornerRadius`, `.cardCornerRadius`). НЕ хардкодить цвета/шрифты/отступы.
   Визуально — в духе `HistoryPanelController+DailyRecap.swift` (metric-плитки +
   чипы), но СОБСТВЕННЫЕ self-contained сабвью (private-хелперы DailyRecap НЕ
   импортировать — построй свои `makeTile(title:value:)` / `makeChip(text:)`).
5. Auto Layout: `translatesAutoresizingMaskIntoConstraints = false`, явные
   constraints; view должна иметь intrinsic/заданную ширину ~260 и высоту по
   контенту (плитки ~48pt высотой). NSMenuItem с custom view сам по ширине меню.

## Приёмка
- `cd native/KrabEarAgent && swift build -c release` — БЕЗ ошибок (pre-existing
  warning про `NSLock.unlock` в BackendSupervisor — игнор, не трогать).
- НЕ коммить, НЕ пересобирай бинари, НЕ трогай другие файлы (только новый
  `main+MenuBarRecap.swift` + точечные правки `main+StatusMenu.swift` и одна
  строка в `main.swift`). Оставь изменения в рабочем дереве.
- Заверши кратким отчётом: какие файлы тронул + результат `swift build`.
