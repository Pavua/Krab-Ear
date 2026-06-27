/*
 Калибровка: секция настроек с аппаратно-зависимой рекомендацией STT.
 Показывает обнаруженный чип / RAM / tier и рекомендованную модель/движок
 с пояснением (rationale). Кнопка «Применить рекомендацию» проставляет
 quality_profile (balanced|max) через set_settings.

 IPC-контракт (только чтение железа, без приватных данных — нет privacy gate):
   - get_hardware_profile {}
       → result {ok, chip String, ram_gb Int, cores Int,
                 tier String(low|mid|high), is_apple_silicon Bool}
   - get_calibration_recommendation {}
       → result {ok, recommended_model String(balanced|max),
                 recommended_engine String, tier String,
                 mic {snr_db Double, suitable_for_stt Bool}|null,
                 rationale String}
   - set_settings {quality_profile: "balanced"|"max"}  → персистит в settings.json

 recommended_model ∈ {balanced, max} напрямую отображается на настройку
 quality_profile (allowed values совпадают, см. settings_validator._RANGE_FIELDS).

 Архитектура (зеркало HistoryPanelController+STTEnginesPicker.swift):
   - buildCalibrationSection() — Gemini-вариант (settingsBar, ThemeCardView).
   - cdBuildCalibrationSection() — Claude Design (settingsBarCD, CDSettingsCardView).
   - fetchAndRebuildCalibrationCard(isClaudeDesign:) — грузит оба IPC off-main,
     перестраивает карточку.
   - onApplyCalibrationRecommendation(_:) — set_settings off-main, перезагрузка карточек.

 Правила AGENT-3 (AppHang-класс): IPC строго в DispatchQueue.global,
 мутации UI — строго в DispatchQueue.main.
 Глифы: только ASCII + установленные SF Symbols (speedometer/checkmark.circle.fill).
*/

import AppKit
import Foundation

// MARK: - Associated-object ключи

private enum CalibrationAssocKeys {
    nonisolated(unsafe) static var card: UInt8 = 0
    nonisolated(unsafe) static var cdCard: UInt8 = 0
    nonisolated(unsafe) static var recommendedModel: UInt8 = 0
}

// MARK: - Модель калибровки (internal, single-source)

struct CalibrationData {
    let chip: String
    let ramGb: Int
    let cores: Int
    let tier: String
    let isAppleSilicon: Bool
    let recommendedModel: String   // balanced|max — отображается на quality_profile
    let recommendedEngine: String
    let micSnrDb: Double?
    let micSuitable: Bool?
    let rationale: String
}

// MARK: - HistoryPanelController+Calibration

extension HistoryPanelController {

    // MARK: - Gemini variant: секция для settingsBar

    /// Строит секцию «Калибровка» (Gemini-дизайн, settingsBar).
    /// Внутренняя карточка наполняется асинхронно при первом показе (off-main).
    @MainActor
    func buildCalibrationSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "dictation_calibration",
            title: "Калибровка",
            isExpanded: false,
            iconSymbol: "speedometer"
        )

        let card = ThemeCardView()
        let loadingLabel = NSTextField(labelWithString: "Загрузка…")
        loadingLabel.font = KrabEarTheme.Typography.caption
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary
        card.contentStackView.addArrangedSubview(loadingLabel)

        objc_setAssociatedObject(
            self,
            &CalibrationAssocKeys.card,
            card,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        section.contentStackView.addArrangedSubview(card)

        // Загрузка данных (off-main, AGENT-3).
        fetchAndRebuildCalibrationCard(isClaudeDesign: false)

        return section
    }

    // MARK: - Claude Design variant: секция для settingsBarCD

    /// Строит секцию «Калибровка» (Claude Design, settingsBarCD).
    @MainActor
    func cdBuildCalibrationSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "cd_calibration",
            title: "Калибровка",
            isExpanded: false
        )

        let card = CDSettingsCardView()
        let loadingLabel = NSTextField(labelWithString: "Загрузка…")
        loadingLabel.font = .systemFont(ofSize: 12, weight: .regular)
        loadingLabel.textColor = KrabEarTheme.Colors.textSecondary
        card.contentStackView.addArrangedSubview(loadingLabel)

        objc_setAssociatedObject(
            self,
            &CalibrationAssocKeys.cdCard,
            card,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        section.contentStackView.addArrangedSubview(card)
        fetchAndRebuildCalibrationCard(isClaudeDesign: true)

        return section
    }

    // MARK: - Загрузка данных с бэкенда

    /// Запрашивает get_hardware_profile + get_calibration_recommendation строго
    /// off-main (AGENT-3), обновляет карточку на main.
    func fetchAndRebuildCalibrationCard(isClaudeDesign: Bool) {
        let ipc = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }

            var data: CalibrationData?
            do {
                let hwResp = try ipc.call(method: "get_hardware_profile", params: [:])
                let recResp = try ipc.call(method: "get_calibration_recommendation", params: [:])
                let hw = hwResp["result"] as? [String: Any] ?? [:]
                let rec = recResp["result"] as? [String: Any] ?? [:]

                // Без chip/tier карточка бессмысленна — считаем данные отсутствующими.
                if let chip = hw["chip"] as? String, let tier = hw["tier"] as? String {
                    let mic = rec["mic"] as? [String: Any]
                    data = CalibrationData(
                        chip: chip,
                        ramGb: (hw["ram_gb"] as? Int) ?? 0,
                        cores: (hw["cores"] as? Int) ?? 0,
                        tier: tier,
                        isAppleSilicon: (hw["is_apple_silicon"] as? Bool) ?? false,
                        recommendedModel: (rec["recommended_model"] as? String) ?? "balanced",
                        recommendedEngine: (rec["recommended_engine"] as? String) ?? "",
                        micSnrDb: mic?["snr_db"] as? Double,
                        micSuitable: mic?["suitable_for_stt"] as? Bool,
                        rationale: (rec["rationale"] as? String) ?? ""
                    )
                }
            } catch {
                data = nil
            }

            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                // Запоминаем рекомендованную модель для обработчика кнопки Apply.
                objc_setAssociatedObject(
                    self,
                    &CalibrationAssocKeys.recommendedModel,
                    data?.recommendedModel as NSString?,
                    .OBJC_ASSOCIATION_RETAIN_NONATOMIC
                )
                if isClaudeDesign {
                    self.rebuildCDCalibrationCard(data: data)
                } else {
                    self.rebuildGeminiCalibrationCard(data: data)
                }
            }
        }
    }

    // MARK: - Перестройка карточки (Gemini)

    @MainActor
    private func rebuildGeminiCalibrationCard(data: CalibrationData?) {
        guard let card = objc_getAssociatedObject(
            self, &CalibrationAssocKeys.card
        ) as? ThemeCardView else { return }

        for v in card.contentStackView.arrangedSubviews {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }

        guard let data else {
            let fallback = NSTextField(labelWithString: "Нет данных — бэкенд недоступен")
            fallback.font = KrabEarTheme.Typography.caption
            fallback.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(fallback)
            return
        }

        // Железо.
        card.contentStackView.addArrangedSubview(
            makeSettingRow(label: "Чип", control: calibValueLabel(data.chip))
        )
        card.contentStackView.addArrangedSubview(calibSeparator())
        card.contentStackView.addArrangedSubview(
            makeSettingRow(label: "Память", control: calibValueLabel("\(data.ramGb) ГБ"))
        )
        card.contentStackView.addArrangedSubview(calibSeparator())
        card.contentStackView.addArrangedSubview(
            makeSettingRow(label: "Ядра", control: calibValueLabel("\(data.cores)"))
        )
        card.contentStackView.addArrangedSubview(calibSeparator())
        card.contentStackView.addArrangedSubview(
            makeSettingRow(label: "Класс", control: calibValueLabel(calibTierLabel(data.tier)),
                           badge: calibTierBadge(data.tier))
        )
        card.contentStackView.addArrangedSubview(calibSeparator())

        // Рекомендация.
        let recValue = "\(calibModelLabel(data.recommendedModel)) · \(data.recommendedEngine)"
        card.contentStackView.addArrangedSubview(
            makeSettingRow(label: "Рекомендация", control: calibValueLabel(recValue))
        )

        // Микрофон (если данные есть).
        if let snr = data.micSnrDb {
            let suitable = data.micSuitable ?? false
            let micText = String(format: "%.1f dB", snr)
            let micBadge = suitable
                ? makeBadge(text: "ОК", color: KrabEarTheme.Colors.success,
                            tooltip: nil, symbol: "checkmark.circle.fill")
                : makeBadge(text: "низкое SNR", color: KrabEarTheme.Colors.textDisabled,
                            tooltip: "Рекомендуется улучшить качество записи",
                            symbol: "exclamationmark.triangle")
            card.contentStackView.addArrangedSubview(calibSeparator())
            card.contentStackView.addArrangedSubview(
                makeSettingRow(label: "Микрофон", control: calibValueLabel(micText), badge: micBadge)
            )
        }

        // Пояснение (rationale).
        if !data.rationale.isEmpty {
            let rationale = NSTextField(wrappingLabelWithString: data.rationale)
            rationale.font = KrabEarTheme.Typography.caption
            rationale.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(rationale)
        }

        // Кнопки.
        card.contentStackView.addArrangedSubview(calibButtonRow(isClaudeDesign: false))
    }

    // MARK: - Перестройка карточки (Claude Design)

    @MainActor
    private func rebuildCDCalibrationCard(data: CalibrationData?) {
        guard let card = objc_getAssociatedObject(
            self, &CalibrationAssocKeys.cdCard
        ) as? CDSettingsCardView else { return }

        for v in card.contentStackView.arrangedSubviews {
            card.contentStackView.removeArrangedSubview(v)
            v.removeFromSuperview()
        }

        guard let data else {
            let fallback = NSTextField(labelWithString: "Нет данных — бэкенд недоступен")
            fallback.font = .systemFont(ofSize: 12, weight: .regular)
            fallback.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(fallback)
            return
        }

        card.contentStackView.addArrangedSubview(
            cdMakeRow(label: "Чип", control: calibValueLabel(data.chip))
        )
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(
            cdMakeRow(label: "Память", control: calibValueLabel("\(data.ramGb) ГБ"))
        )
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(
            cdMakeRow(label: "Ядра", control: calibValueLabel("\(data.cores)"))
        )
        card.contentStackView.addArrangedSubview(cdMakeSeparator())
        card.contentStackView.addArrangedSubview(
            cdMakeRow(label: "Класс", control: calibValueLabel(calibTierLabel(data.tier)))
        )
        card.contentStackView.addArrangedSubview(cdMakeSeparator())

        let recValue = "\(calibModelLabel(data.recommendedModel)) · \(data.recommendedEngine)"
        card.contentStackView.addArrangedSubview(
            cdMakeRow(label: "Рекомендация", control: calibValueLabel(recValue))
        )

        if let snr = data.micSnrDb {
            card.contentStackView.addArrangedSubview(cdMakeSeparator())
            card.contentStackView.addArrangedSubview(
                cdMakeRow(label: "Микрофон", control: calibValueLabel(String(format: "%.1f dB", snr)))
            )
        }

        if !data.rationale.isEmpty {
            let rationale = NSTextField(wrappingLabelWithString: data.rationale)
            rationale.font = .systemFont(ofSize: 11, weight: .regular)
            rationale.textColor = KrabEarTheme.Colors.textSecondary
            card.contentStackView.addArrangedSubview(rationale)
        }

        card.contentStackView.addArrangedSubview(calibButtonRow(isClaudeDesign: true))
    }

    // MARK: - Кнопки (Применить / Обновить)

    @MainActor
    private func calibButtonRow(isClaudeDesign: Bool) -> NSView {
        let applyButton = ThemePrimaryButton(
            title: "Применить рекомендацию",
            target: self,
            action: #selector(onApplyCalibrationRecommendation(_:))
        )
        applyButton.setAccessibilityLabel("Применить рекомендованную модель STT")

        let refreshButton = ThemeSecondaryButton(
            title: "Профиль обновить",
            target: self,
            action: #selector(onRefreshCalibrationProfile(_:))
        )
        refreshButton.setAccessibilityLabel("Перечитать аппаратный профиль")

        let stack = NSStackView()
        stack.orientation = .horizontal
        stack.spacing = KrabEarTheme.Metrics.standard
        stack.alignment = .centerY
        stack.distribution = .fill
        stack.addArrangedSubview(applyButton)
        stack.addArrangedSubview(refreshButton)
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        spacer.setAccessibilityElement(false)
        stack.addArrangedSubview(spacer)
        return stack
    }

    // MARK: - Handlers

    /// Применяет рекомендованную модель: set_settings {quality_profile} off-main,
    /// затем перезагружает обе карточки.
    @objc func onApplyCalibrationRecommendation(_ sender: NSButton) {
        let model = (objc_getAssociatedObject(
            self, &CalibrationAssocKeys.recommendedModel
        ) as? NSString) as String?
        // Допустимы только balanced|max (см. settings_validator). Иначе — no-op.
        guard let model, model == "balanced" || model == "max" else { return }

        let ipc = ipcClient
        sender.isEnabled = false
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            var applied = false
            do {
                _ = try ipc.call(method: "set_settings", params: ["quality_profile": model])
                applied = true
            } catch {
                applied = false
            }
            DispatchQueue.main.async { [weak self] in
                sender.isEnabled = true
                self?.calibShowToast(
                    applied
                        ? "Применена модель: \(self?.calibModelLabel(model) ?? model)"
                        : "Не удалось применить рекомендацию"
                )
            }
            // Перезагружаем обе карточки (отражаем новое состояние из бэкенда).
            self.fetchAndRebuildCalibrationCard(isClaudeDesign: false)
            self.fetchAndRebuildCalibrationCard(isClaudeDesign: true)
        }
    }

    /// Перечитывает аппаратный профиль и рекомендацию для обеих карточек.
    @objc func onRefreshCalibrationProfile(_ sender: NSButton) {
        fetchAndRebuildCalibrationCard(isClaudeDesign: false)
        fetchAndRebuildCalibrationCard(isClaudeDesign: true)
    }

    // MARK: - Вспомогательные элементы (только для этого extension)

    /// Значение справа в строке: tabular, вторичный цвет.
    @MainActor
    private func calibValueLabel(_ text: String) -> NSView {
        let label = NSTextField(labelWithString: text)
        label.font = KrabEarTheme.Typography.captionMedium.tabular()
        label.textColor = KrabEarTheme.Colors.textPrimary
        label.lineBreakMode = .byTruncatingTail
        return label
    }

    /// Перевод tier → русская подпись.
    private func calibTierLabel(_ tier: String) -> String {
        switch tier {
        case "high": return "высокий"
        case "mid": return "средний"
        case "low": return "низкий"
        default: return tier
        }
    }

    /// Перевод модели → подпись для UI.
    private func calibModelLabel(_ model: String) -> String {
        switch model {
        case "max": return "max"
        case "balanced": return "balanced"
        default: return model
        }
    }

    /// Цветной бейдж класса железа.
    @MainActor
    private func calibTierBadge(_ tier: String) -> NSView {
        let color: NSColor
        switch tier {
        case "high": color = KrabEarTheme.Colors.success
        case "mid": color = KrabEarTheme.Colors.accent
        default: color = KrabEarTheme.Colors.textDisabled
        }
        return makeBadge(text: calibTierLabel(tier), color: color, tooltip: nil, symbol: nil)
    }

    /// NSBox separator (аналог приватного makeSeparator()).
    @MainActor
    private func calibSeparator() -> NSView {
        let separator = NSBox()
        separator.boxType = .separator
        separator.translatesAutoresizingMaskIntoConstraints = false
        return separator
    }

    /// Лёгкий toast-фидбэк; на main thread. Falls back на нет-op при отсутствии окна.
    @MainActor
    private func calibShowToast(_ message: String) {
        BackendToast.shared.show(message)
    }
}
