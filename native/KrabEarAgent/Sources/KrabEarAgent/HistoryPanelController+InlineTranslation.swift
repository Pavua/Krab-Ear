/*
 HistoryPanelController+InlineTranslation.swift
 Inline translation toggle для элементов истории.

 Функциональность:
 - Кнопка 🌐 справа от текста item'а
 - Click → вызов IPC `translate_text` → результат показывается italic строкой под оригиналом
 - Повторный click → скрыть перевод
 - NSCache: кэш per-item-id чтобы повторный toggle был мгновенным
 - Spinner во время IPC in-flight

 Связи:
 1) ipcClient: translate_text
 2) InlineTranslationCache: singleton NSCache
*/

import AppKit
import Foundation

// MARK: - Translation cache

/// In-memory кэш переводов: ключ = history item ID, значение = переведённый текст.
/// NSCache автоматически вытесняет объекты при нехватке памяти.
final class InlineTranslationCache: @unchecked Sendable {
    static let shared = InlineTranslationCache()
    private let cache = NSCache<NSString, NSString>()

    private init() {
        cache.countLimit = 500
    }

    func get(itemID: String) -> String? {
        return cache.object(forKey: itemID as NSString) as String?
    }

    func set(itemID: String, translation: String) {
        cache.setObject(translation as NSString, forKey: itemID as NSString)
    }

    func remove(itemID: String) {
        cache.removeObject(forKey: itemID as NSString)
    }
}

// MARK: - Inline translate cell view

/// NSTableCellView subclass с встроенным inline-translation toggle.
/// Содержит: оригинальный текст + кнопку 🌐 + строку перевода (italic) + spinner.
final class InlineTranslateCellView: NSTableCellView {

    // MARK: - Subviews
    let originalLabel: NSTextField
    let translateButton: NSButton
    let translationLabel: NSTextField
    private let spinner: NSProgressIndicator

    // MARK: - State
    var itemID: String = ""
    var sourceText: String = ""
    var isShowingTranslation = false
    weak var ipcClient: IPCClient?
    var translationStyle: String = "neutral"
    var networkMode: String = "offline_default"

    // MARK: - Constraints (toggled visibility)
    private var translationLabelHeightConstraint: NSLayoutConstraint!

    override init(frame: NSRect) {
        originalLabel = NSTextField(wrappingLabelWithString: "")
        originalLabel.translatesAutoresizingMaskIntoConstraints = false
        originalLabel.maximumNumberOfLines = 0
        originalLabel.lineBreakMode = .byWordWrapping

        translateButton = NSButton(frame: .zero)
        translateButton.translatesAutoresizingMaskIntoConstraints = false
        translateButton.isBordered = false
        translateButton.bezelStyle = .inline
        translateButton.title = "🌐"
        translateButton.font = .systemFont(ofSize: 11)
        translateButton.setContentHuggingPriority(.required, for: .horizontal)
        translateButton.setContentCompressionResistancePriority(.required, for: .horizontal)
        translateButton.toolTip = "Показать/скрыть перевод"

        translationLabel = NSTextField(wrappingLabelWithString: "")
        translationLabel.translatesAutoresizingMaskIntoConstraints = false
        translationLabel.maximumNumberOfLines = 0
        translationLabel.lineBreakMode = .byWordWrapping
        translationLabel.font = NSFontManager.shared.font(
            withFamily: NSFont.systemFont(ofSize: 11).familyName ?? "System",
            traits: .italicFontMask,
            weight: 5,
            size: 11
        ) ?? .systemFont(ofSize: 11)
        translationLabel.alphaValue = 0.8
        translationLabel.textColor = KrabEarTheme.Colors.textSecondary
        translationLabel.isHidden = true

        spinner = NSProgressIndicator(frame: .zero)
        spinner.translatesAutoresizingMaskIntoConstraints = false
        spinner.style = .spinning
        spinner.isIndeterminate = true
        spinner.controlSize = .mini
        spinner.isHidden = true

        super.init(frame: frame)

        addSubview(originalLabel)
        addSubview(translateButton)
        addSubview(translationLabel)
        addSubview(spinner)

        setupConstraints()

        translateButton.target = self
        translateButton.action = #selector(onToggleTranslation)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) not supported") }

    private func setupConstraints() {
        translationLabelHeightConstraint = translationLabel.heightAnchor.constraint(equalToConstant: 0)

        NSLayoutConstraint.activate([
            // Кнопка: прижата к правому верхнему углу
            translateButton.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -4),
            translateButton.topAnchor.constraint(equalTo: topAnchor, constant: 4),
            translateButton.widthAnchor.constraint(equalToConstant: 20),

            // Spinner рядом с кнопкой
            spinner.trailingAnchor.constraint(equalTo: translateButton.leadingAnchor, constant: -2),
            spinner.centerYAnchor.constraint(equalTo: translateButton.centerYAnchor),
            spinner.widthAnchor.constraint(equalToConstant: 14),
            spinner.heightAnchor.constraint(equalToConstant: 14),

            // Оригинальный текст: слева, справа не заходим под кнопку
            originalLabel.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 4),
            originalLabel.trailingAnchor.constraint(equalTo: translateButton.leadingAnchor, constant: -4),
            originalLabel.topAnchor.constraint(equalTo: topAnchor, constant: 4),

            // Строка перевода: под оригиналом, полная ширина
            translationLabel.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 4),
            translationLabel.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -4),
            translationLabel.topAnchor.constraint(equalTo: originalLabel.bottomAnchor, constant: 2),
            translationLabel.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -4),
        ])
    }

    // MARK: - Toggle action

    @objc private func onToggleTranslation() {
        guard !itemID.isEmpty else { return }

        // Если перевод уже показан — скрываем
        if isShowingTranslation {
            hideTranslation()
            return
        }

        // Если есть кэш — показываем мгновенно
        if let cached = InlineTranslationCache.shared.get(itemID: itemID) {
            showTranslation(cached)
            return
        }

        // Запрашиваем перевод через IPC
        requestTranslation()
    }

    private func requestTranslation() {
        guard let ipcClient = ipcClient else { return }
        let text = sourceText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }

        spinner.isHidden = false
        spinner.startAnimation(nil)
        translateButton.isEnabled = false

        let capturedID = itemID
        let capturedStyle = translationStyle
        let capturedNetworkMode = networkMode

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else { return }
            let result: String?
            do {
                let response = try ipcClient.call(
                    method: "translate_text",
                    params: [
                        "text": text,
                        "translation_mode": "auto",
                        "translation_style": capturedStyle,
                        "network_mode": capturedNetworkMode,
                    ]
                )
                if let res = response["result"] as? [String: Any],
                   let status = res["status"] as? String,
                   status == "ok",
                   let translated = res["text"] as? String,
                   !translated.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                {
                    result = translated.trimmingCharacters(in: .whitespacesAndNewlines)
                } else {
                    result = nil
                }
            } catch {
                result = nil
            }

            DispatchQueue.main.async { [weak self] in
                guard let self = self, self.itemID == capturedID else { return }
                self.spinner.stopAnimation(nil)
                self.spinner.isHidden = true
                self.translateButton.isEnabled = true

                if let text = result {
                    InlineTranslationCache.shared.set(itemID: capturedID, translation: text)
                    self.showTranslation(text)
                } else {
                    // Тихо — кнопка остаётся активной для повторной попытки
                }
            }
        }
    }

    // MARK: - Show / hide helpers

    func showTranslation(_ text: String) {
        translationLabel.stringValue = text
        translationLabel.isHidden = false
        isShowingTranslation = true
        translateButton.title = "🌐✓"

        // Мягкая анимация появления
        translationLabel.alphaValue = 0
        KrabEarTheme.Motion.animate(
            duration: KrabEarTheme.Motion.Duration.short,
            easing: KrabEarTheme.Motion.Easing.easeOut
        ) {
            self.translationLabel.alphaValue = 0.8
        }

        // Перерисовываем строку таблицы чтобы высота пересчиталась
        invalidateTableRow()
    }

    func hideTranslation() {
        translationLabel.isHidden = true
        translationLabel.stringValue = ""
        isShowingTranslation = false
        translateButton.title = "🌐"
        invalidateTableRow()
    }

    private func invalidateTableRow() {
        // Ищем родительский NSTableView и перезапрашиваем высоту строки
        var parent: NSView? = superview
        while let v = parent {
            if let tableView = v as? NSTableView {
                let row = tableView.row(for: self)
                if row >= 0 {
                    NSAnimationContext.runAnimationGroup { ctx in
                        ctx.duration = KrabEarTheme.Motion.Duration.short
                        tableView.noteHeightOfRows(withIndexesChanged: IndexSet(integer: row))
                    }
                }
                return
            }
            parent = v.superview
        }
    }
}
