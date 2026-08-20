/*
 main+MenuBarRecap.swift
 AgentAppDelegate: компактная карточка «Сводка дня» в верху status-bar меню.

 Назначение: показывать ключевые метрики текущего дня (записи, минуты, слова,
 топ-темы) одним взглядом прямо в выпадающем меню — без открытия панели истории.

 Связи:
   - MenuBarRecapView встраивается в rebuildStatusMenu (main+StatusMenu.swift)
     через NSMenuItem.view — вставляется первым пунктом + сепаратором.
   - AgentAppDelegate.menuBarRecapView (main.swift) хранит weak-ссылку для refresh.
   - extension AgentAppDelegate: NSMenuDelegate (здесь) вызывает refresh при каждом
     открытии меню.
   - IPC: ipcClient.call вызывается ТОЛЬКО off-main (AGENT-3).
   - Glyph guard (AGENT-J/CI): глифы ● ○ • ▶ ★ ✓ ⏱ и эмодзи — запрещены
     в NSTextField. Разделители — U+00B7 MIDDLE DOT «·» или «—».
   - KrabEarTheme токены: Colors.*, Typography.*, Metrics.*.
*/

import AppKit

// MARK: - Карточка «Сводка дня» для status-bar меню

/// Самодостаточная карточка метрик дня — компактный вариант DailyRecap панели истории.
/// Ширина ~260pt, высота по контенту (плитки ~48pt). Использует только токены KrabEarTheme,
/// не импортирует private-хелперы HistoryPanelController+DailyRecap.
final class MenuBarRecapView: NSView {

    // MARK: - Состояние

    /// Лейбл статуса (Генерируем… / Готово / Ошибка / Нет данных / Приватность).
    private let statusLabel: NSTextField = {
        // Безопасный plain-текст, без глифов (AGENT-J glyph guard).
        let lbl = NSTextField(labelWithString: "Генерируем сводку...")
        lbl.font = KrabEarTheme.Typography.caption
        lbl.textColor = KrabEarTheme.Colors.textSecondary
        lbl.translatesAutoresizingMaskIntoConstraints = false
        return lbl
    }()

    /// Контейнер для динамического контента (плитки, чипы).
    private let contentStack: NSStackView = {
        let sv = NSStackView()
        sv.orientation = .vertical
        sv.alignment = .leading
        sv.spacing = KrabEarTheme.Metrics.standard
        sv.translatesAutoresizingMaskIntoConstraints = false
        return sv
    }()

    /// Корневой вертикальный stack (заголовок + статус + контент).
    private let rootStack: NSStackView = {
        let sv = NSStackView()
        sv.orientation = .vertical
        sv.alignment = .leading
        sv.spacing = KrabEarTheme.Metrics.standard
        sv.translatesAutoresizingMaskIntoConstraints = false
        // Внутренние отступы карточки из KrabEarTheme.Metrics.
        sv.edgeInsets = NSEdgeInsets(
            top:    KrabEarTheme.Metrics.comfortable,
            left:   KrabEarTheme.Metrics.comfortable,
            bottom: KrabEarTheme.Metrics.comfortable,
            right:  KrabEarTheme.Metrics.comfortable
        )
        return sv
    }()

    // MARK: - Инициализация

    override init(frame frameRect: NSRect) {
        // Начальная ширина ~260pt; высота будет определена constraints.
        super.init(frame: NSRect(x: 0, y: 0, width: 260, height: 60))
        buildLayout()
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        buildLayout()
    }

    // MARK: - Сборка иерархии views

    private func buildLayout() {
        translatesAutoresizingMaskIntoConstraints = false
        wantsLayer = true

        // Заголовок секции — без эмодзи и запрещённых глифов (AGENT-J).
        let titleLabel = NSTextField(labelWithString: "Сводка дня")
        titleLabel.font = KrabEarTheme.Typography.sectionTitle
        titleLabel.textColor = KrabEarTheme.Colors.textPrimary
        titleLabel.translatesAutoresizingMaskIntoConstraints = false

        addSubview(rootStack)
        rootStack.addArrangedSubview(titleLabel)
        rootStack.addArrangedSubview(statusLabel)
        rootStack.addArrangedSubview(contentStack)

        NSLayoutConstraint.activate([
            // Карточка фиксированной ширины ~260pt — NSMenuItem подстроится.
            widthAnchor.constraint(equalToConstant: 260),
            // rootStack заполняет всю ширину view.
            rootStack.leadingAnchor.constraint(equalTo: leadingAnchor),
            rootStack.trailingAnchor.constraint(equalTo: trailingAnchor),
            rootStack.topAnchor.constraint(equalTo: topAnchor),
            rootStack.bottomAnchor.constraint(equalTo: bottomAnchor),
            // contentStack растягивается на всю ширину rootStack минус insets.
            contentStack.widthAnchor.constraint(
                equalTo: rootStack.widthAnchor,
                constant: -(KrabEarTheme.Metrics.comfortable * 2)
            )
        ])
    }

    // MARK: - Public API

    /// Запускает фоновый IPC-запрос и обновляет карточку.
    /// AGENT-3: ipcClient.call — ТОЛЬКО внутри DispatchQueue.global; UI — ТОЛЬКО на main.
    func refresh(ipcClient: IPCClient) {
        // Немедленно показываем «Генерируем…» на main-потоке.
        DispatchQueue.main.async { [weak self] in
            self?.showStatus("Генерируем сводку...")
        }

        // Тяжёлый IPC — строго off-main (AGENT-3).
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let response = try ipcClient.call(method: "generate_daily_digest", params: [:])
                let result = (response["result"] as? [String: Any]) ?? [:]

                // Backend вернул ok:false — приватность или нет данных.
                if (result["ok"] as? Bool) == false {
                    let isPrivacy = (result["reason"] as? String) == "privacy_mode_active"
                    DispatchQueue.main.async {
                        self?.showStatus(
                            isPrivacy
                                ? "Сводка недоступна в режиме приватности"
                                : "Нет данных за сегодня"
                        )
                    }
                    return
                }

                // Данные получены — заполняем карточку на main.
                DispatchQueue.main.async { self?.populate(result) }
            } catch {
                DispatchQueue.main.async {
                    self?.showStatus("Ошибка: \(error.localizedDescription)")
                }
            }
        }
    }

    // MARK: - Private: UI updates (только на main-потоке)

    /// Показывает строку статуса, скрывает динамический контент.
    private func showStatus(_ message: String) {
        statusLabel.stringValue = message
        statusLabel.isHidden = false
        clearContent()
    }

    /// Очищает динамический контент (плитки, чипы).
    private func clearContent() {
        contentStack.arrangedSubviews.forEach { $0.removeFromSuperview() }
    }

    /// Заполняет карточку данными из backend generate_daily_digest.
    private func populate(_ data: [String: Any]) {
        clearContent()
        statusLabel.isHidden = true

        let recordings = (data["total_recordings"] as? Int) ?? 0

        // Пустой день — мягкое сообщение без плиток.
        guard recordings > 0 else {
            let emptyLbl = NSTextField(labelWithString: "За сегодня записей нет")
            emptyLbl.font = KrabEarTheme.Typography.body
            emptyLbl.textColor = KrabEarTheme.Colors.textSecondary
            emptyLbl.translatesAutoresizingMaskIntoConstraints = false
            contentStack.addArrangedSubview(emptyLbl)
            statusLabel.isHidden = true
            return
        }

        let durationMin = (data["total_duration_min"] as? Double) ?? 0.0
        let totalWords  = (data["total_words"]  as? Int) ?? 0
        let topics      = (data["top_topics"]   as? [String]) ?? []

        // Строка «Готово» в статусе.
        statusLabel.isHidden = true

        // ── Ряд плиток: Записей · Минут · Слов ──────────────────────────────
        let tilesRow = NSStackView()
        tilesRow.orientation = .horizontal
        tilesRow.distribution = .fillEqually
        tilesRow.spacing = KrabEarTheme.Metrics.standard
        tilesRow.translatesAutoresizingMaskIntoConstraints = false

        tilesRow.addArrangedSubview(makeTile(title: "Записей", value: "\(recordings)"))
        tilesRow.addArrangedSubview(makeTile(title: "Минут",   value: String(format: "%.1f", durationMin)))
        tilesRow.addArrangedSubview(makeTile(title: "Слов",    value: formatWords(totalWords)))

        contentStack.addArrangedSubview(tilesRow)
        // Ряд плиток растягивается на всю ширину contentStack.
        tilesRow.widthAnchor.constraint(equalTo: contentStack.widthAnchor).isActive = true

        // ── Чипы топ-тем (до 4) ──────────────────────────────────────────────
        let topTopics = Array(topics.prefix(4))
        if !topTopics.isEmpty {
            // Метка «Темы» — без эмодзи (AGENT-J CoreText guard).
            let topicsLabel = NSTextField(labelWithString: "Темы")
            topicsLabel.font = KrabEarTheme.Typography.captionMedium
            topicsLabel.textColor = KrabEarTheme.Colors.textSecondary
            topicsLabel.translatesAutoresizingMaskIntoConstraints = false
            contentStack.addArrangedSubview(topicsLabel)

            // Горизонтальный ряд чипов.
            let chipsRow = NSStackView()
            chipsRow.orientation = .horizontal
            chipsRow.spacing = KrabEarTheme.Metrics.tight
            chipsRow.translatesAutoresizingMaskIntoConstraints = false
            for topic in topTopics {
                chipsRow.addArrangedSubview(makeChip(text: topic))
            }
            contentStack.addArrangedSubview(chipsRow)
        }

        // Принудительно пересчитываем layout после добавления субвью.
        needsLayout = true
    }

    // MARK: - Private UI-хелперы

    /// Создаёт плитку метрики с числом (крупный) + подписью (мелкий).
    /// Высота ~48pt задаётся через edgeInsets comfortable + размер шрифта.
    private func makeTile(title: String, value: String) -> NSView {
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 2
        stack.wantsLayer = true
        // Фон плитки — семантический border-токен (полупрозрачный, работает в dark/light).
        stack.layer?.backgroundColor = KrabEarTheme.Colors.border.cgColor
        stack.layer?.cornerRadius = KrabEarTheme.Metrics.innerCornerRadius
        stack.edgeInsets = NSEdgeInsets(
            top:    KrabEarTheme.Metrics.comfortable,
            left:   KrabEarTheme.Metrics.standard,
            bottom: KrabEarTheme.Metrics.comfortable,
            right:  KrabEarTheme.Metrics.standard
        )

        // Крупное число — без эмодзи, только цифры (AGENT-J safe).
        let valLbl = NSTextField(labelWithString: value)
        valLbl.font = .systemFont(ofSize: 20, weight: .semibold)
        valLbl.textColor = KrabEarTheme.Colors.textPrimary
        valLbl.translatesAutoresizingMaskIntoConstraints = false

        // Подпись плитки.
        let titleLbl = NSTextField(labelWithString: title)
        titleLbl.font = KrabEarTheme.Typography.captionMedium
        titleLbl.textColor = KrabEarTheme.Colors.textSecondary
        titleLbl.translatesAutoresizingMaskIntoConstraints = false

        stack.addArrangedSubview(valLbl)
        stack.addArrangedSubview(titleLbl)
        return stack
    }

    /// Создаёт небольшой чип с текстом темы.
    private func makeChip(text: String) -> NSView {
        let stack = NSStackView()
        stack.orientation = .horizontal
        stack.alignment = .centerY
        stack.spacing = 4
        stack.wantsLayer = true
        stack.layer?.backgroundColor = KrabEarTheme.Colors.border.cgColor
        stack.layer?.cornerRadius = 10
        stack.edgeInsets = NSEdgeInsets(top: 3, left: 8, bottom: 3, right: 8)

        // Текст чипа — Cyrillic/ASCII, без глифов-проблем (AGENT-J safe).
        let lbl = NSTextField(labelWithString: text)
        lbl.font = KrabEarTheme.Typography.captionMedium
        lbl.textColor = KrabEarTheme.Colors.textPrimary
        lbl.translatesAutoresizingMaskIntoConstraints = false
        stack.addArrangedSubview(lbl)

        return stack
    }

    /// Форматирует количество слов с разделителем тысяч (123 456).
    /// Ручная реализация без NumberFormatter — избегаем Locale-зависимость.
    private func formatWords(_ n: Int) -> String {
        // NumberFormatter .decimal подхватит системную локаль — используем его.
        let fmt = NumberFormatter()
        fmt.numberStyle = .decimal
        fmt.locale = Locale.current
        return fmt.string(from: NSNumber(value: n)) ?? "\(n)"
    }
}

// MARK: - NSMenuDelegate: обновление карточки при каждом открытии меню

/// При открытии status-меню автоматически обновляем recap — свежие данные дня.
/// AGENT-3: refresh() сам запускает IPC off-main; здесь только триггер.
extension AgentAppDelegate: NSMenuDelegate {
    func menuWillOpen(_ menu: NSMenu) {
        menuBarRecapView?.refresh(ipcClient: ipcClient)
        // C3a Task 2: подменю «Быстрые заметки» — тот же паттерн, свой IPC off-main.
        refreshQuickNotesSubmenu()
        // B3: строка «кто держит LM Studio» — тот же паттерн (main+BrainLease.swift).
        refreshBrainLeaseMenuItem()
        // T8: строка «Память: …» — тот же паттерн (main+MemoryLine.swift).
        refreshMemoryLineMenuItem()
    }
}
