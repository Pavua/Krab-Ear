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
        let httpBase = settings.voiceGatewayURL
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        let config = ConversationConfig(
            wsURLString:  buildConversationWSURL(from: settings),
            apiKey:       settings.voiceGatewayAPIKey,
            languageHint: "auto",
            engine:       "auto",
            brain:        "auto",
            brainMode:    ConversationViewController.savedBrainMode,
            httpBaseURLString: httpBase
        )
        let vc = ConversationViewController(config: config)
        conversationVC = vc

        // Волна 3c: локальная озвучка ошибок — синтез через IPC synthesize_speech
        // (строго off-main, AGENT-3), воспроизведение через AVAudioPlayer.
        // Пустой wav_bytes_b64 (privacy mode / TTS недоступен) → тихая text-only деградация.
        let ipcClient = self.ipcClient
        vc.errorAnnouncer.speak = { phrase in
            DispatchQueue.global(qos: .userInitiated).async {
                nonisolated(unsafe) let response = try? ipcClient.call(
                    method: "synthesize_speech",
                    params: ["text": phrase, "language": "ru"]
                )
                guard let result = response?["result"] as? [String: Any],
                      let b64 = result["wav_bytes_b64"] as? String, !b64.isEmpty,
                      let wav = Data(base64Encoded: b64)
                else { return }
                Task { @MainActor in
                    ConversationErrorAnnouncer.playWav(wav)
                }
            }
        }

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
    /// Контракт VG (verified live 2026-06-20): `WS /v1/sessions/{session_id}/conversation`.
    /// Пример: http://127.0.0.1:8090 → ws://127.0.0.1:8090/v1/sessions/vs_<uuid>/conversation
    /// session_id свободный (vs_-префикс); `?lang=` добавляется в startWebSocketSession; auth — Bearer apiKey.
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

        // VG требует session-scoped путь. Генерируем стабильный per-launch id.
        let sessionId = "vs_" + UUID().uuidString.lowercased().replacingOccurrences(of: "-", with: "")
        return base + "/v1/sessions/\(sessionId)/conversation"
    }

    // MARK: - PR 1.5 hook points

    /// Вызывается из HotkeyManager (PR 1.5) для старта разговора по hotkey.
    /// При menu-bar mode panel может быть закрытым — открываем + tab switch.
    /// showPanel() имеет async block который сбрасывает selection на History.
    /// Поэтому tab switch + startConversation deferred через main.async чтобы
    /// run AFTER showPanel's internal async (FIFO main queue).
    func triggerConversationStart() {
        showPanel()
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        // Defer tab switch + conversation start чтобы run после showPanel's
        // async tab-sync block. Без этого showPanel resets to History tab.
        DispatchQueue.main.async { [weak self] in
            guard let self = self else { return }
            self.mainTabView.selectTabViewItem(at: 3)
            self.tabSelector.selectedSegment = 3
            self.conversationVC?.startConversation()
        }
    }

    /// Вызывается из wake-word detector (PR 1.5) для старта разговора.
    func triggerConversationFromWakeWord() {
        triggerConversationStart()
    }
}
