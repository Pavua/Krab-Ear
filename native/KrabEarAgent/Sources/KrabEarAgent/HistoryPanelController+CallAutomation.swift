/*
 HistoryPanelController+CallAutomation — интеграция вкладки «Автозвонки».

 Создаёт CallAutomationController, встраивает его view в NSTabViewItem.
 Вкладка добавляется в mainTabView рядом с «Разговор с AI».

 Примечание: PanelTab.callAutomation добавлен в основной enum в этом расширении
 через отдельный static helper, чтобы не трогать HistoryPanelController.swift.
*/

import AppKit

// MARK: - Associated object key

extension HistoryPanelController {
    nonisolated(unsafe) static var callAutomationVCKey: UInt8 = 0

    /// Контроллер вкладки «Автозвонки». Создаётся один раз в setupCallAutomationTab().
    var callAutomationVC: CallAutomationController? {
        get { objc_getAssociatedObject(self, &HistoryPanelController.callAutomationVCKey) as? CallAutomationController }
        set { objc_setAssociatedObject(self, &HistoryPanelController.callAutomationVCKey, newValue, .OBJC_ASSOCIATION_RETAIN_NONATOMIC) }
    }

    // MARK: - Setup

    /// Создать вкладку «Автозвонки» и добавить её в mainTabView.
    /// Вызывается из HistoryPanelController.setupUI() после conversationTab.
    func setupCallAutomationTab(contentView: NSView) {
        let vc = CallAutomationController(ipcClient: ipcClient)
        callAutomationVC = vc

        vc.view.translatesAutoresizingMaskIntoConstraints = false
        contentView.addSubview(vc.view)
        NSLayoutConstraint.activate([
            vc.view.topAnchor.constraint(equalTo: contentView.topAnchor),
            vc.view.leadingAnchor.constraint(equalTo: contentView.leadingAnchor),
            vc.view.trailingAnchor.constraint(equalTo: contentView.trailingAnchor),
            vc.view.bottomAnchor.constraint(equalTo: contentView.bottomAnchor),
        ])

        // Trigger viewDidLoad
        _ = vc.view
    }

    // MARK: - Segment index helper

    /// Индекс сегмента «Автозвонки» в tabSelector (после «Разговор с AI»).
    var callAutomationSegmentIndex: Int { 4 }
}
