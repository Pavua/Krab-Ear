/*
 HistoryPanelController+TimelineExport.swift

 Экспорт таймлайна записей истории (группировка по часу/дню/неделе) через
 IPC export_timeline_svg / export_timeline_json / export_timeline_ical.
 Секция «Экспорт таймлайна» в History-табе, рядом с другими экспорт-опциями.

 Бэкенд сам выбирает путь для файла (<data_dir>/exports/timeline) — диалогов
 сохранения не требуется. Файл открытого текста пишет сервер, поэтому запрос идёт
 через PlaintextExportCoordinator (A5.3): при включённом шифровании истории
 coordinator один раз показывает общий sheet согласия и подставляет
 plaintext_export-контекст; без него бэкенд ответил бы plaintext_confirmation_required.

 Контракт (KrabEar/backend/service.py, ~4268-4407):
   Params:   {output_dir: null, group_by: "hour"|"day"|"week", limit: Int, [width, height]}
   Success:  {"path": String, "blocks": Int}
   Failure:  {"error": {"code": "privacy_mode_active"|"privacy_mode"|"invalid_path", "message": String}}
 Ответ handle_request заворачивает результат хендлера под response["result"] —
 хендлер никогда не бросает исключение, поэтому и success, и failure приходят
 как response["result"], а не как response["error"] (см. handle_request, service.py:2061-2062).

 Паттерны:
 - IPC строго off-main (coordinator использует callAsync), AGENT-3.
 - НЕТ runModal() — согласие и ошибки идут через sheet (presentAlertSheet).
 - BackendToast.shared.show — non-modal уведомление об успехе/ошибке.
 - Glyph-free: не используем ● ○ ◉ • ▶ ◀ ▲ ▼ ★ ✕ ✓ ⏱ в labelWithString.
*/

import AppKit
import Foundation

// MARK: - Associated-object ключи

private enum TimelineExportAssocKeys {
    nonisolated(unsafe) static var groupByPopUp: UInt8 = 0
}

/// Разбор ответа export_timeline_* без UI (чистая функция, тестируется отдельно).
enum TimelineExportOutcome: Equatable {
    case privacy
    case failure(String)
    case emptyPath
    case saved(String)

    static func classify(_ result: [String: Any]) -> TimelineExportOutcome {
        if let errDict = result["error"] as? [String: Any] {
            let code = errDict["code"] as? String ?? ""
            let message = errDict["message"] as? String ?? "Неизвестная ошибка"
            // Бэкенд шлёт оба кода: "privacy_mode_active" (авторизатор, service.py
            // _timeline_export_denied) и "privacy_mode" (прямой гейт настроек).
            if code == "privacy_mode_active" || code == "privacy_mode" { return .privacy }
            if (result["reason"] as? String) == "privacy_mode_active" { return .privacy }
            return .failure(message)
        }
        guard let path = result["path"] as? String, !path.isEmpty else { return .emptyPath }
        return .saved(path)
    }
}

extension HistoryPanelController {

    // MARK: - Group-by выбор (private, конфигурация запроса на экспорт)

    private var timelineExportGroupByPopUp: NSPopUpButton? {
        get { objc_getAssociatedObject(self, &TimelineExportAssocKeys.groupByPopUp) as? NSPopUpButton }
        set { objc_setAssociatedObject(self, &TimelineExportAssocKeys.groupByPopUp, newValue, .OBJC_ASSOCIATION_RETAIN_NONATOMIC) }
    }

    /// Значение group_by, выбранное в picker'e ("hour"|"day"|"week"). Дефолт "day".
    private var selectedTimelineGroupBy: String {
        guard let title = timelineExportGroupByPopUp?.titleOfSelectedItem else { return "day" }
        switch title {
        case "Час": return "hour"
        case "Неделя": return "week"
        default: return "day"
        }
    }

    // MARK: - Setup

    /// Строит секцию «Экспорт таймлайна» для History-таба.
    /// Вызывается из setupHistoryTab() в +ApplyTheme+HistoryTab.swift.
    @MainActor
    func setupTimelineExportSection() -> CollapsibleSectionView {
        let section = CollapsibleSectionView(
            sectionId: "history_timeline_export",
            title: "Экспорт таймлайна",
            isExpanded: false,
            iconSymbol: "chart.bar.doc.horizontal"
        )

        let groupByLabel = NSTextField(labelWithString: "Группировка:")
        groupByLabel.font = KrabEarTheme.Typography.body
        groupByLabel.textColor = KrabEarTheme.Colors.textSecondary

        let groupByPopUp = NSPopUpButton(frame: .zero, pullsDown: false)
        groupByPopUp.addItems(withTitles: ["Час", "День", "Неделя"])
        groupByPopUp.selectItem(withTitle: "День")
        self.timelineExportGroupByPopUp = groupByPopUp

        let groupByRow = NSStackView(views: [groupByLabel, groupByPopUp])
        groupByRow.orientation = .horizontal
        groupByRow.spacing = KrabEarTheme.Metrics.standard
        groupByRow.alignment = .centerY
        section.contentStackView.addArrangedSubview(groupByRow)

        let svgButton = ThemeSecondaryButton(title: "Экспорт SVG", target: self, action: #selector(onExportTimelineSVG))
        svgButton.applyThemeSecondary()

        let jsonButton = ThemeSecondaryButton(title: "Экспорт JSON", target: self, action: #selector(onExportTimelineJSON))
        jsonButton.applyThemeSecondary()

        let icalButton = ThemeSecondaryButton(title: "Экспорт iCalendar", target: self, action: #selector(onExportTimelineICal))
        icalButton.applyThemeSecondary()

        let buttonsRow = NSStackView(views: [svgButton, jsonButton, icalButton])
        buttonsRow.orientation = .horizontal
        buttonsRow.spacing = KrabEarTheme.Metrics.standard
        buttonsRow.alignment = .centerY
        section.contentStackView.addArrangedSubview(buttonsRow)

        return section
    }

    // MARK: - Actions

    @objc func onExportTimelineSVG() {
        performTimelineExport(method: .exportTimelineSVG, label: "SVG")
    }

    @objc func onExportTimelineJSON() {
        performTimelineExport(method: .exportTimelineJSON, label: "JSON")
    }

    @objc func onExportTimelineICal() {
        performTimelineExport(method: .exportTimelineICal, label: "iCalendar")
    }

    /// Общая реализация для всех трёх форматов — отличаются только IPC-методом
    /// и текстом уведомления. width/height (SVG-only) бэкенд применяет через
    /// собственный дефолт (1200x400), когда не переданы — здесь не усложняем UI.
    private func performTimelineExport(method: PlaintextExportCoordinator.BackendMethod, label: String) {
        let groupBy = selectedTimelineGroupBy
        let params: [String: Any] = [
            "output_dir": NSNull(),
            "group_by": groupBy,
            "limit": 500
        ]

        // Контекст (и согласие, если шифрование включено) фиксируется ДО запроса;
        // IPC уходит через coordinator.callAsync — вне main thread.
        Task { [weak self] in
            guard let self else { return }
            do {
                let ticket = try await self.plaintextExportCoordinator.prepareBackend(
                    method: method, presenting: self.window
                )
                let result = try await self.plaintextExportCoordinator.performBackend(ticket, params: params)
                self.handleTimelineExportResult(result, label: label)
            } catch PlaintextExportCoordinator.Failure.cancelled {
                return
            } catch {
                self.showInfoAlert(
                    title: "Экспорт таймлайна (\(label))",
                    body: PlaintextExportCoordinator.errorMessage(error)
                )
            }
        }
    }

    // MARK: - Result handler (must be called on main thread)

    private func handleTimelineExportResult(_ result: [String: Any], label: String) {
        // Хендлер никогда не бросает исключение — и успех, и {"error": {...}}
        // приходят внутри response["result"] (см. handle_request, service.py:2061-2062).
        switch TimelineExportOutcome.classify(result) {
        case .privacy:
            BackendToast.shared.show("Экспорт отключён в режиме приватности", duration: 4.0)
        case .failure(let message):
            BackendToast.shared.show("Не удалось экспортировать таймлайн: \(message)", duration: 4.0)
        case .emptyPath:
            BackendToast.shared.show("Бэкенд вернул пустой путь при экспорте таймлайна (\(label))", duration: 4.0)
        case .saved(let path):
            let filename = (path as NSString).lastPathComponent
            BackendToast.shared.show("Таймлайн сохранён: \(filename)", duration: 4.0)
            NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
        }
    }
}
