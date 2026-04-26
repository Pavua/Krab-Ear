/*
 HistoryPanelController+VoiceTab — интеграция вкладки «Разговор с AI».

 Создаёт ConversationViewController, встраивает его contentView в NSTabViewItem.
 PR 1.5 (triggers) будет вызывать conversationVC.startConversation() напрямую
 через свойство conversationVC этого расширения.
*/

import AppKit

extension HistoryPanelController {

    // MARK: - Accessor

    /// Контроллер вкладки «Разговор с AI». Создаётся один раз в setupConversationTab().
    var conversationVC: ConversationViewController? {
        get { objc_getAssociatedObject(self, &HistoryPanelController.conversationVCKey) as? ConversationViewController }
        set { objc_setAssociatedObject(self, &HistoryPanelController.conversationVCKey, newValue, .OBJC_ASSOCIATION_RETAIN_NONATOMIC) }
    }

    nonisolated(unsafe) static var conversationVCKey: UInt8 = 0

    // MARK: - Setup (called from setupUI in HistoryPanelController)

    /// Создать вкладку «Разговор с AI» и добавить её в mainTabView.
    /// Вызывается из HistoryPanelController.setupUI() после существующих трёх табов.
    func setupConversationTab(contentView voiceContentView: NSView) {
        let settings = settingsProvider()
        let config = ConversationConfig(
            wsURLString: buildConversationWSURL(from: settings),
            apiKey:       settings.voiceGatewayAPIKey,
            languageHint: "auto",
            engine:       "auto",
            brain:        "auto"
        )
        let vc = ConversationViewController(config: config)
        conversationVC = vc

        // Встроить view VC в content view таба.
        vc.view.translatesAutoresizingMaskIntoConstraints = false
        voiceContentView.addSubview(vc.view)
        NSLayoutConstraint.activate([
            vc.view.topAnchor.constraint(equalTo: voiceContentView.topAnchor),
            vc.view.leadingAnchor.constraint(equalTo: voiceContentView.leadingAnchor),
            vc.view.trailingAnchor.constraint(equalTo: voiceContentView.trailingAnchor),
            vc.view.bottomAnchor.constraint(equalTo: voiceContentView.bottomAnchor),
        ])

        // Trigger viewDidLoad (load view hierarchy).
        _ = vc.view
    }

    // MARK: - URL builder

    /// Строит WS-URL для Voice Gateway из настроек.
    /// Пример: http://127.0.0.1:8090 → ws://127.0.0.1:8090/v1/conversation
    private func buildConversationWSURL(from settings: AgentSettings) -> String {
        var base = settings.voiceGatewayURL
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))

        // Заменяем http(s) → ws(s) если нужно.
        if base.hasPrefix("http://") {
            base = "ws://" + base.dropFirst("http://".count)
        } else if base.hasPrefix("https://") {
            base = "wss://" + base.dropFirst("https://".count)
        }

        // Если уже ws:// — оставляем как есть.
        if !base.hasPrefix("ws://") && !base.hasPrefix("wss://") {
            base = "ws://" + base
        }

        return base + "/v1/conversation"
    }

    // MARK: - PR 1.5 hook points

    /// Вызывается из HotkeyManager (PR 1.5) для старта разговора по hotkey.
    /// При menu-bar mode panel может быть закрытым — открываем + tab switch.
    func triggerConversationStart() {
        // Ensure panel visible (no-op если уже видим). При menu-bar mode без
        // showPanel() нет mainTabView в hierarchy → tab switch silent.
        showPanel()
        // Activate window чтобы user видел переключение (panel может быть behind).
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        // Переключить на вкладку «Разговор с AI» (индекс 3).
        mainTabView.selectTabViewItem(at: 3)
        tabSelector.selectedSegment = 3
        conversationVC?.startConversation()
    }

    /// Вызывается из wake-word detector (PR 1.5) для старта разговора.
    func triggerConversationFromWakeWord() {
        triggerConversationStart()
    }
}
