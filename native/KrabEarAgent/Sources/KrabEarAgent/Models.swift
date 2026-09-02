/*
 Общие модели данных нативного агента Krab Ear.

 Связи модуля:
 1) AgentSettings синхронизируется с backend settings.json.
 2) HistoryItem отражает записи из history.ndjson.
 3) ConversationConfig — конфигурация сессии «Разговор с AI» (PR 1.3).
*/

import Foundation

// MARK: - ConversationConfig

/// Конфигурация диалоговой сессии с AI через Voice Gateway WebSocket.
///
/// `wsURLString` — полный WS-URL, например `ws://127.0.0.1:8090/v1/conversation`.
/// По умолчанию строится из `AgentSettings.voiceGatewayURL` в `HistoryPanelController+VoiceTab`.
/// Пользователь может переопределить через Settings-drawer внутри вкладки.
struct ConversationConfig {
    /// WS endpoint Voice Gateway. Placeholder — реальный GW подключается в PR 1.1.
    var wsURLString: String

    /// API-ключ Voice Gateway (может быть пустой для локального режима).
    var apiKey: String

    /// Языковой хинт для STT: "auto" | "ru" | "en" | "es".
    var languageHint: String

    /// Движок AI: "auto" | "moshi" | "seamless".
    var engine: String

    /// LLM-мозг (конкретная модель): "auto" | "qwen3-4b" | "llama-3.2-3b".
    var brain: String

    /// Режим приоритета мозга (Волна 3b): "fast" | "krab" | "auto".
    /// В отличие от `brain`, всегда передаётся в WS query-param `brain_mode`
    /// (даже при значении "auto") — контракт с Voice Gateway требует явности.
    var brainMode: String = "auto"

    /// HTTP-база Voice Gateway (без ws-схемы), например "http://127.0.0.1:8090".
    /// Используется для не-WS запросов (напр. PUT /v1/settings/conversation).
    var httpBaseURLString: String = "http://127.0.0.1:8090"

    static let `default` = ConversationConfig(
        wsURLString:  "ws://127.0.0.1:8090/v1/conversation",
        apiKey:       "",
        languageHint: "auto",
        engine:       "auto",
        brain:        "auto"
    )
}

/// Настройки агента, синхронизированные с backend JSON.
struct AgentSettings {
    var mode: String
    var showDockIcon: Bool
    var autoStartEnabled: Bool
    var autoPaste: Bool
    var playStartSound: Bool
    var qualityProfile: String
    var networkMode: String
    var hotkey: String
    var hotkeyProfile: String
    var hotkeyMode: String
    var historyPolicy: String
    var historyPageSize: Int
    var historyTextDensity: String
    var realtimePreviewEnabled: Bool
    var overlayFollowCursor: Bool
    var cleanupProfile: String
    var translationMode: String
    var translateAndPaste: Bool
    var translationStyle: String
    var translationGlossary: [String: String]
    var textTemplates: [String: String]
    var clipboardMode: String
    var audioDuckingEnabled: Bool
    var audioDuckingPercent: Int
    var overlayOpacityPercent: Int
    var voiceGatewayURL: String
    var voiceGatewayAPIKey: String
    var updateChannel: String
    var callNotifyDefault: Bool
    var callAutoSummary: Bool
    var captureSourceMode: String
    var uiLastTab: String
    var historyFocusMode: Bool
    var onboardingCompleted: Bool
    var diarizationEnabled: Bool
    var llmRewriteEnabled: Bool
    /// GigaAM-RNNT v2 — RU специализированный STT (PR feat/agent-settings-gigaam-field).
    /// Соответствует backend `STT_GIGAAM_ENABLED`. Toggle: Settings → Аудио-пайплайн.
    var gigaamEnabled: Bool
    var llmModel: String
    // Recording bookmarks hotkey (Cmd+Shift+B)
    var bookmarksHotkeyEnabled: Bool
    // Phase 3.4 — Call Automation / Telnyx
    var telnyxAPIKey: String
    var telnyxFromNumber: String
    var callMaxDurationMin: Int
    var callCostWarnUSD: Double
    var callAutoEndOnSilence: Bool
    // Phase 3 — Local SIP / On-Device Telephony
    var sipServer: String
    var sipPort: Int
    var sipUser: String
    var sipPassword: String
    var sipFromNumber: String
    var sipProxy: String
    // Quick Edit before paste
    var quickEditEnabled: Bool
    var quickEditTimeoutSec: Double
    // Privacy Mode (D.5): disables all telemetry and external network ops.
    var privacyModeEnabled: Bool
    // Voice Commands (dictation post-processing layer)
    var voiceCommandsEnabled: Bool
    var voiceCommandsStrictMode: Bool
    // Scheduled auto-purge of old history entries
    var autoPurgeEnabled: Bool
    var autoPurgeRetentionDays: Int
    // Auto-learn STT dictionary from corrections
    var autoLearnCorrectionsEnabled: Bool
    // Text Snippets
    var textSnippetsEnabled: Bool
    // Phonetic Vocabulary
    var phoneticVocabEnabled: Bool
    // Paste Undo
    var pasteUndoEnabled: Bool
    // Smart field-aware paste (AX role-based behaviour gate)
    var smartFieldFormatEnabled: Bool
    // Streaming live paste: вставляет подтверждённые куски текста по мере диктовки.
    var streamingPasteEnabled: Bool
    // Cloud Rewriter settings
    var cloudRewriterEnabled: Bool
    var cloudRewriterProvider: String
    var openaiApiKey: String
    var anthropicApiKey: String
    var cloudRewriterBaseUrl: String
    var cloudRewriterCustomModel: String
    // Модели облачных провайдеров: раньше были константами в Python и
    // пользователь не мог ни сменить их, ни увидеть.
    var cloudRewriterOpenaiModel: String
    var cloudRewriterAnthropicModel: String
    var cloudRewriterApiKey: String
    // GigaAM transport (subprocess = PyTorch-воркер / mlx = in-process MLX)
    var gigaamTransport: String

    static let `default` = AgentSettings(
        mode: "headless",
        showDockIcon: true,
        autoStartEnabled: false,
        autoPaste: true,
        playStartSound: true,
        qualityProfile: "balanced",
        networkMode: "offline_default",
        hotkey: "right_option_toggle",
        hotkeyProfile: "default",
        hotkeyMode: "toggle",
        historyPolicy: "unlimited",
        historyPageSize: 50,
        historyTextDensity: "normal",
        realtimePreviewEnabled: true,
        overlayFollowCursor: false,
        cleanupProfile: "soft",
        translationMode: "off",
        translateAndPaste: false,
        translationStyle: "neutral",
        translationGlossary: [:],
        textTemplates: [
            "follow_up_ru": "Здравствуйте! Подтверждаю: {text}. Следующий шаг: {next_step}.",
            "follow_up_es": "Hola. Confirmo: {text}. Siguiente paso: {next_step}.",
        ],
        clipboardMode: "always_copy",
        audioDuckingEnabled: true,
        audioDuckingPercent: 50,
        overlayOpacityPercent: 45,
        voiceGatewayURL: "http://127.0.0.1:8090",
        voiceGatewayAPIKey: "",
        updateChannel: "stable",
        callNotifyDefault: true,
        callAutoSummary: true,
        captureSourceMode: "mic",
        uiLastTab: "history",
        historyFocusMode: true,
        onboardingCompleted: false,
        diarizationEnabled: true,
        llmRewriteEnabled: false,
        gigaamEnabled: false,
        llmModel: "qwen3.5-9b@6bit",
        bookmarksHotkeyEnabled: true,
        telnyxAPIKey: "",
        telnyxFromNumber: "",
        callMaxDurationMin: 30,
        callCostWarnUSD: 5.0,
        callAutoEndOnSilence: true,
        sipServer: "",
        sipPort: 5060,
        sipUser: "",
        sipPassword: "",
        sipFromNumber: "",
        sipProxy: "",
        quickEditEnabled: false,
        quickEditTimeoutSec: 5.0,
        privacyModeEnabled: false,
        voiceCommandsEnabled: true,
        voiceCommandsStrictMode: true,
        autoPurgeEnabled: false,
        autoPurgeRetentionDays: 90,
        autoLearnCorrectionsEnabled: false,
        textSnippetsEnabled: false,
        phoneticVocabEnabled: false,
        pasteUndoEnabled: false,
        smartFieldFormatEnabled: false,
        streamingPasteEnabled: false,
        cloudRewriterEnabled: false,
        cloudRewriterProvider: "openai",
        openaiApiKey: "",
        anthropicApiKey: "",
        cloudRewriterBaseUrl: "http://localhost:11434/v1",
        cloudRewriterCustomModel: "qwen2.5:7b",
        cloudRewriterOpenaiModel: "gpt-4o-mini",
        cloudRewriterAnthropicModel: "claude-haiku-4-5-20251001",
        cloudRewriterApiKey: "",
        gigaamTransport: "subprocess"
    )

    init(from payload: [String: Any]) {
        self.mode = (payload["mode"] as? String) ?? Self.default.mode
        self.showDockIcon = (payload["show_dock_icon"] as? Bool) ?? Self.default.showDockIcon
        self.autoStartEnabled = (payload["auto_start_enabled"] as? Bool) ?? Self.default.autoStartEnabled
        self.autoPaste = (payload["auto_paste"] as? Bool) ?? Self.default.autoPaste
        self.playStartSound = (payload["play_start_sound"] as? Bool) ?? Self.default.playStartSound
        self.qualityProfile = (payload["quality_profile"] as? String) ?? Self.default.qualityProfile
        self.networkMode = (payload["network_mode"] as? String) ?? Self.default.networkMode
        self.hotkey = (payload["hotkey"] as? String) ?? Self.default.hotkey
        self.hotkeyProfile = (payload["hotkey_profile"] as? String) ?? Self.default.hotkeyProfile
        self.hotkeyMode = (payload["hotkey_mode"] as? String) ?? Self.default.hotkeyMode
        self.historyPolicy = (payload["history_policy"] as? String) ?? Self.default.historyPolicy
        self.historyPageSize = (payload["history_page_size"] as? Int) ?? Self.default.historyPageSize
        self.historyTextDensity = (payload["history_text_density"] as? String) ?? Self.default.historyTextDensity
        self.realtimePreviewEnabled = (payload["realtime_preview_enabled"] as? Bool) ?? Self.default.realtimePreviewEnabled
        self.overlayFollowCursor = (payload["overlay_follow_cursor"] as? Bool) ?? Self.default.overlayFollowCursor
        self.cleanupProfile = (payload["cleanup_profile"] as? String) ?? Self.default.cleanupProfile
        self.translationMode = (payload["translation_mode"] as? String) ?? Self.default.translationMode
        self.translateAndPaste = (payload["translate_and_paste"] as? Bool) ?? Self.default.translateAndPaste
        self.translationStyle = (payload["translation_style"] as? String) ?? Self.default.translationStyle
        if let rawGlossary = payload["translation_glossary"] as? [String: Any] {
            var parsed: [String: String] = [:]
            for (key, value) in rawGlossary {
                let cleanKey = key.trimmingCharacters(in: .whitespacesAndNewlines)
                let cleanValue = String(describing: value).trimmingCharacters(in: .whitespacesAndNewlines)
                if !cleanKey.isEmpty, !cleanValue.isEmpty {
                    parsed[cleanKey] = cleanValue
                }
            }
            self.translationGlossary = parsed
        } else {
            self.translationGlossary = Self.default.translationGlossary
        }
        if let rawTemplates = payload["text_templates"] as? [String: Any] {
            var parsedTemplates: [String: String] = [:]
            for (key, value) in rawTemplates {
                let cleanKey = key.trimmingCharacters(in: .whitespacesAndNewlines)
                let cleanValue = String(describing: value).trimmingCharacters(in: .whitespacesAndNewlines)
                if !cleanKey.isEmpty, !cleanValue.isEmpty {
                    parsedTemplates[cleanKey] = cleanValue
                }
            }
            self.textTemplates = parsedTemplates.isEmpty ? Self.default.textTemplates : parsedTemplates
        } else {
            self.textTemplates = Self.default.textTemplates
        }
        self.clipboardMode = (payload["clipboard_mode"] as? String) ?? Self.default.clipboardMode
        self.audioDuckingEnabled = (payload["audio_ducking_enabled"] as? Bool) ?? Self.default.audioDuckingEnabled
        self.audioDuckingPercent = (payload["audio_ducking_percent"] as? Int) ?? Self.default.audioDuckingPercent
        self.overlayOpacityPercent = (payload["overlay_opacity_percent"] as? Int) ?? Self.default.overlayOpacityPercent
        self.voiceGatewayURL = (payload["voice_gateway_url"] as? String) ?? Self.default.voiceGatewayURL
        self.voiceGatewayAPIKey = (payload["voice_gateway_api_key"] as? String) ?? Self.default.voiceGatewayAPIKey
        self.updateChannel = (payload["update_channel"] as? String) ?? Self.default.updateChannel
        self.callNotifyDefault = (payload["call_notify_default"] as? Bool) ?? Self.default.callNotifyDefault
        self.callAutoSummary = (payload["call_auto_summary"] as? Bool) ?? Self.default.callAutoSummary
        self.captureSourceMode = (payload["capture_source_mode"] as? String) ?? Self.default.captureSourceMode
        self.uiLastTab = (payload["ui_last_tab"] as? String) ?? Self.default.uiLastTab
        self.historyFocusMode = (payload["history_focus_mode"] as? Bool) ?? Self.default.historyFocusMode
        self.onboardingCompleted = (payload["onboarding_completed"] as? Bool) ?? Self.default.onboardingCompleted
        self.diarizationEnabled = (payload["diarization_enabled"] as? Bool) ?? Self.default.diarizationEnabled
        self.llmRewriteEnabled = (payload["llm_rewrite_enabled"] as? Bool) ?? Self.default.llmRewriteEnabled
        self.gigaamEnabled = (payload["stt_gigaam_enabled"] as? Bool) ?? Self.default.gigaamEnabled
        self.llmModel = (payload["llm_model"] as? String) ?? Self.default.llmModel
        self.bookmarksHotkeyEnabled = (payload["bookmarks_hotkey_enabled"] as? Bool) ?? Self.default.bookmarksHotkeyEnabled
        self.telnyxAPIKey = (payload["telnyx_api_key"] as? String) ?? Self.default.telnyxAPIKey
        self.telnyxFromNumber = (payload["telnyx_from_number"] as? String) ?? Self.default.telnyxFromNumber
        self.callMaxDurationMin = (payload["call_max_duration_min"] as? Int) ?? Self.default.callMaxDurationMin
        self.callCostWarnUSD = (payload["call_cost_warn_usd"] as? Double) ?? Self.default.callCostWarnUSD
        self.callAutoEndOnSilence = (payload["call_auto_end_on_silence"] as? Bool) ?? Self.default.callAutoEndOnSilence
        self.sipServer = (payload["sip_server"] as? String) ?? Self.default.sipServer
        self.sipPort = (payload["sip_port"] as? Int) ?? Self.default.sipPort
        self.sipUser = (payload["sip_user"] as? String) ?? Self.default.sipUser
        self.sipPassword = (payload["sip_password"] as? String) ?? Self.default.sipPassword
        self.sipFromNumber = (payload["sip_from_number"] as? String) ?? Self.default.sipFromNumber
        self.sipProxy = (payload["sip_proxy"] as? String) ?? Self.default.sipProxy
        self.quickEditEnabled = (payload["quick_edit_enabled"] as? Bool) ?? Self.default.quickEditEnabled
        self.quickEditTimeoutSec = (payload["quick_edit_timeout_sec"] as? Double) ?? Self.default.quickEditTimeoutSec
        self.privacyModeEnabled = (payload["privacy_mode_enabled"] as? Bool) ?? Self.default.privacyModeEnabled
        self.voiceCommandsEnabled = (payload["voice_commands_enabled"] as? Bool) ?? Self.default.voiceCommandsEnabled
        self.voiceCommandsStrictMode = (payload["voice_commands_strict_mode"] as? Bool) ?? Self.default.voiceCommandsStrictMode
        self.autoPurgeEnabled = (payload["auto_purge_enabled"] as? Bool) ?? Self.default.autoPurgeEnabled
        self.autoPurgeRetentionDays = (payload["auto_purge_retention_days"] as? Int) ?? Self.default.autoPurgeRetentionDays
        self.autoLearnCorrectionsEnabled = (payload["auto_learn_corrections_enabled"] as? Bool) ?? Self.default.autoLearnCorrectionsEnabled
        self.textSnippetsEnabled = (payload["text_snippets_enabled"] as? Bool) ?? Self.default.textSnippetsEnabled
        self.phoneticVocabEnabled = (payload["phonetic_vocab_enabled"] as? Bool) ?? Self.default.phoneticVocabEnabled
        self.pasteUndoEnabled = (payload["paste_undo_enabled"] as? Bool) ?? Self.default.pasteUndoEnabled
        self.smartFieldFormatEnabled = (payload["smart_field_format_enabled"] as? Bool) ?? Self.default.smartFieldFormatEnabled
        self.streamingPasteEnabled = (payload["streaming_paste_enabled"] as? Bool) ?? Self.default.streamingPasteEnabled
        self.cloudRewriterEnabled = (payload["cloud_rewriter_enabled"] as? Bool) ?? Self.default.cloudRewriterEnabled
        self.cloudRewriterProvider = (payload["cloud_rewriter_provider"] as? String) ?? Self.default.cloudRewriterProvider
        self.openaiApiKey = (payload["openai_api_key"] as? String) ?? Self.default.openaiApiKey
        self.anthropicApiKey = (payload["anthropic_api_key"] as? String) ?? Self.default.anthropicApiKey
        self.cloudRewriterBaseUrl = (payload["cloud_rewriter_base_url"] as? String) ?? Self.default.cloudRewriterBaseUrl
        self.cloudRewriterCustomModel = (payload["cloud_rewriter_custom_model"] as? String) ?? Self.default.cloudRewriterCustomModel
        self.cloudRewriterOpenaiModel = (payload["cloud_rewriter_openai_model"] as? String) ?? Self.default.cloudRewriterOpenaiModel
        self.cloudRewriterAnthropicModel = (payload["cloud_rewriter_anthropic_model"] as? String) ?? Self.default.cloudRewriterAnthropicModel
        self.cloudRewriterApiKey = (payload["cloud_rewriter_api_key"] as? String) ?? Self.default.cloudRewriterApiKey
        self.gigaamTransport = (payload["stt_gigaam_transport"] as? String) ?? Self.default.gigaamTransport
    }

    init(
        mode: String,
        showDockIcon: Bool,
        autoStartEnabled: Bool,
        autoPaste: Bool,
        playStartSound: Bool,
        qualityProfile: String,
        networkMode: String,
        hotkey: String,
        hotkeyProfile: String,
        hotkeyMode: String,
        historyPolicy: String,
        historyPageSize: Int,
        historyTextDensity: String,
        realtimePreviewEnabled: Bool,
        overlayFollowCursor: Bool,
        cleanupProfile: String,
        translationMode: String,
        translateAndPaste: Bool,
        translationStyle: String,
        translationGlossary: [String: String],
        textTemplates: [String: String],
        clipboardMode: String,
        audioDuckingEnabled: Bool,
        audioDuckingPercent: Int,
        overlayOpacityPercent: Int,
        voiceGatewayURL: String,
        voiceGatewayAPIKey: String,
        updateChannel: String,
        callNotifyDefault: Bool,
        callAutoSummary: Bool,
        captureSourceMode: String,
        uiLastTab: String,
        historyFocusMode: Bool,
        onboardingCompleted: Bool,
        diarizationEnabled: Bool,
        llmRewriteEnabled: Bool,
        gigaamEnabled: Bool,
        llmModel: String,
        bookmarksHotkeyEnabled: Bool = true,
        telnyxAPIKey: String,
        telnyxFromNumber: String,
        callMaxDurationMin: Int,
        callCostWarnUSD: Double,
        callAutoEndOnSilence: Bool,
        sipServer: String = "",
        sipPort: Int = 5060,
        sipUser: String = "",
        sipPassword: String = "",
        sipFromNumber: String = "",
        sipProxy: String = "",
        quickEditEnabled: Bool,
        quickEditTimeoutSec: Double,
        privacyModeEnabled: Bool = false,
        voiceCommandsEnabled: Bool = true,
        voiceCommandsStrictMode: Bool = true,
        autoPurgeEnabled: Bool = false,
        autoPurgeRetentionDays: Int = 90,
        autoLearnCorrectionsEnabled: Bool = false,
        textSnippetsEnabled: Bool = false,
        phoneticVocabEnabled: Bool = false,
        pasteUndoEnabled: Bool = false,
        smartFieldFormatEnabled: Bool = false,
        streamingPasteEnabled: Bool = false,
        cloudRewriterEnabled: Bool = false,
        cloudRewriterProvider: String = "openai",
        openaiApiKey: String = "",
        anthropicApiKey: String = "",
        cloudRewriterBaseUrl: String = "http://localhost:11434/v1",
        cloudRewriterCustomModel: String = "qwen2.5:7b",
        cloudRewriterOpenaiModel: String = "gpt-4o-mini",
        cloudRewriterAnthropicModel: String = "claude-haiku-4-5-20251001",
        cloudRewriterApiKey: String = "",
        gigaamTransport: String = "subprocess"
    ) {
        self.mode = mode
        self.showDockIcon = showDockIcon
        self.autoStartEnabled = autoStartEnabled
        self.autoPaste = autoPaste
        self.playStartSound = playStartSound
        self.qualityProfile = qualityProfile
        self.networkMode = networkMode
        self.hotkey = hotkey
        self.hotkeyProfile = hotkeyProfile
        self.hotkeyMode = hotkeyMode
        self.historyPolicy = historyPolicy
        self.historyPageSize = historyPageSize
        self.historyTextDensity = historyTextDensity
        self.realtimePreviewEnabled = realtimePreviewEnabled
        self.overlayFollowCursor = overlayFollowCursor
        self.cleanupProfile = cleanupProfile
        self.translationMode = translationMode
        self.translateAndPaste = translateAndPaste
        self.translationStyle = translationStyle
        self.translationGlossary = translationGlossary
        self.textTemplates = textTemplates
        self.clipboardMode = clipboardMode
        self.audioDuckingEnabled = audioDuckingEnabled
        self.audioDuckingPercent = audioDuckingPercent
        self.overlayOpacityPercent = overlayOpacityPercent
        self.voiceGatewayURL = voiceGatewayURL
        self.voiceGatewayAPIKey = voiceGatewayAPIKey
        self.updateChannel = updateChannel
        self.callNotifyDefault = callNotifyDefault
        self.callAutoSummary = callAutoSummary
        self.captureSourceMode = captureSourceMode
        self.uiLastTab = uiLastTab
        self.historyFocusMode = historyFocusMode
        self.onboardingCompleted = onboardingCompleted
        self.diarizationEnabled = diarizationEnabled
        self.llmRewriteEnabled = llmRewriteEnabled
        self.gigaamEnabled = gigaamEnabled
        self.llmModel = llmModel
        self.bookmarksHotkeyEnabled = bookmarksHotkeyEnabled
        self.telnyxAPIKey = telnyxAPIKey
        self.telnyxFromNumber = telnyxFromNumber
        self.callMaxDurationMin = callMaxDurationMin
        self.callCostWarnUSD = callCostWarnUSD
        self.callAutoEndOnSilence = callAutoEndOnSilence
        self.sipServer = sipServer
        self.sipPort = sipPort
        self.sipUser = sipUser
        self.sipPassword = sipPassword
        self.sipFromNumber = sipFromNumber
        self.sipProxy = sipProxy
        self.quickEditEnabled = quickEditEnabled
        self.quickEditTimeoutSec = quickEditTimeoutSec
        self.privacyModeEnabled = privacyModeEnabled
        self.voiceCommandsEnabled = voiceCommandsEnabled
        self.voiceCommandsStrictMode = voiceCommandsStrictMode
        self.autoPurgeEnabled = autoPurgeEnabled
        self.autoPurgeRetentionDays = autoPurgeRetentionDays
        self.autoLearnCorrectionsEnabled = autoLearnCorrectionsEnabled
        self.textSnippetsEnabled = textSnippetsEnabled
        self.phoneticVocabEnabled = phoneticVocabEnabled
        self.pasteUndoEnabled = pasteUndoEnabled
        self.smartFieldFormatEnabled = smartFieldFormatEnabled
        self.streamingPasteEnabled = streamingPasteEnabled
        self.cloudRewriterEnabled = cloudRewriterEnabled
        self.cloudRewriterProvider = cloudRewriterProvider
        self.openaiApiKey = openaiApiKey
        self.anthropicApiKey = anthropicApiKey
        self.cloudRewriterBaseUrl = cloudRewriterBaseUrl
        self.cloudRewriterCustomModel = cloudRewriterCustomModel
        self.cloudRewriterOpenaiModel = cloudRewriterOpenaiModel
        self.cloudRewriterAnthropicModel = cloudRewriterAnthropicModel
        self.cloudRewriterApiKey = cloudRewriterApiKey
        self.gigaamTransport = gigaamTransport
    }

    func toPayload() -> [String: Any] {
        [
            "mode": mode,
            "show_dock_icon": showDockIcon,
            "auto_start_enabled": autoStartEnabled,
            "auto_paste": autoPaste,
            "play_start_sound": playStartSound,
            "quality_profile": qualityProfile,
            "network_mode": networkMode,
            "hotkey": hotkey,
            "hotkey_profile": hotkeyProfile,
            "hotkey_mode": hotkeyMode,
            "history_policy": historyPolicy,
            "history_page_size": historyPageSize,
            "history_text_density": historyTextDensity,
            "realtime_preview_enabled": realtimePreviewEnabled,
            "cleanup_profile": cleanupProfile,
            "translation_mode": translationMode,
            "translate_and_paste": translateAndPaste,
            "translation_style": translationStyle,
            "translation_glossary": translationGlossary,
            "text_templates": textTemplates,
            "clipboard_mode": clipboardMode,
            "audio_ducking_enabled": audioDuckingEnabled,
            "audio_ducking_percent": audioDuckingPercent,
            "overlay_opacity_percent": overlayOpacityPercent,
            "overlay_follow_cursor": overlayFollowCursor,
            "voice_gateway_url": voiceGatewayURL,
            "voice_gateway_api_key": voiceGatewayAPIKey,
            "update_channel": updateChannel,
            "call_notify_default": callNotifyDefault,
            "call_auto_summary": callAutoSummary,
            "capture_source_mode": captureSourceMode,
            "ui_last_tab": uiLastTab,
            "history_focus_mode": historyFocusMode,
            "onboarding_completed": onboardingCompleted,
            "diarization_enabled": diarizationEnabled,
            "llm_rewrite_enabled": llmRewriteEnabled,
            "stt_gigaam_enabled": gigaamEnabled,
            "llm_model": llmModel,
            "telnyx_api_key": telnyxAPIKey,
            "telnyx_from_number": telnyxFromNumber,
            "call_max_duration_min": callMaxDurationMin,
            "call_cost_warn_usd": callCostWarnUSD,
            "call_auto_end_on_silence": callAutoEndOnSilence,
            "sip_server": sipServer,
            "sip_port": sipPort,
            "sip_user": sipUser,
            "sip_password": sipPassword,
            "sip_from_number": sipFromNumber,
            "sip_proxy": sipProxy,
            "bookmarks_hotkey_enabled": bookmarksHotkeyEnabled,
            "quick_edit_enabled": quickEditEnabled,
            "quick_edit_timeout_sec": quickEditTimeoutSec,
            "privacy_mode_enabled": privacyModeEnabled,
            "voice_commands_enabled": voiceCommandsEnabled,
            "voice_commands_strict_mode": voiceCommandsStrictMode,
            "auto_purge_enabled": autoPurgeEnabled,
            "auto_purge_retention_days": autoPurgeRetentionDays,
            "auto_learn_corrections_enabled": autoLearnCorrectionsEnabled,
            "text_snippets_enabled": textSnippetsEnabled,
            "phonetic_vocab_enabled": phoneticVocabEnabled,
            "paste_undo_enabled": pasteUndoEnabled,
            "smart_field_format_enabled": smartFieldFormatEnabled,
            "streaming_paste_enabled": streamingPasteEnabled,
            "cloud_rewriter_enabled": cloudRewriterEnabled,
            "cloud_rewriter_provider": cloudRewriterProvider,
            "openai_api_key": openaiApiKey,
            "anthropic_api_key": anthropicApiKey,
            "cloud_rewriter_base_url": cloudRewriterBaseUrl,
            "cloud_rewriter_custom_model": cloudRewriterCustomModel,
            "cloud_rewriter_openai_model": cloudRewriterOpenaiModel,
            "cloud_rewriter_anthropic_model": cloudRewriterAnthropicModel,
            "cloud_rewriter_api_key": cloudRewriterApiKey,
            "stt_gigaam_transport": gigaamTransport,
        ]
    }
}

/// Одна задача (action item), извлечённая LLM (PR #289).
/// Зеркалит backend `ActionItem` dataclass из backend/action_items_extractor.py.
struct ActionItem {
    let text: String
    let assignee: String
    let due: String
    /// "high" | "medium" | "low"
    let priority: String

    init?(payload: [String: Any]) {
        guard let text = payload["text"] as? String, !text.isEmpty else { return nil }
        self.text = text
        self.assignee = (payload["assignee"] as? String) ?? ""
        self.due = (payload["due"] as? String) ?? ""
        let p = (payload["priority"] as? String)?.lowercased() ?? "medium"
        self.priority = ["high", "medium", "low"].contains(p) ? p : "medium"
    }
}

/// Элемент истории транскрибации для нативной панели.
struct HistoryItem {
    let id: String
    let ts: String
    let text: String
    let pasteStatus: String
    let sourceText: String
    let translatedText: String
    let translationMode: String
    let translationStatus: String
    /// Уверенность STT: 0.0–1.0, nil если метаданные отсутствуют (например импорт без анализа).
    let confidence: Double?
    /// Длительность аудио в секундах (живые записи и импорты); nil если неизвестна.
    /// Нужна для «Темп речи» (analyze_speech_pace требует duration_sec).
    let audioDurationSec: Double?
    /// Извлечённые LLM-ом задачи (PR #289). Пусто если ещё не запускали extract.
    let actionItems: [ActionItem]
    /// Извлечённые решения (строки).
    let decisions: [String]
    /// Извлечённые вопросы (строки).
    let questions: [String]

    init?(payload: [String: Any]) {
        guard
            let id = payload["id"] as? String,
            let ts = payload["ts"] as? String,
            let text = payload["text"] as? String
        else {
            return nil
        }

        self.id = id
        self.ts = ts
        self.text = text
        self.pasteStatus = (payload["paste_status"] as? String) ?? "failed"
        self.sourceText = (payload["source_text"] as? String) ?? ""
        self.translatedText = (payload["translated_text"] as? String) ?? ""
        self.translationMode = (payload["translation_mode"] as? String) ?? "off"
        self.translationStatus = (payload["translation_status"] as? String) ?? "not_requested"
        // confidence может быть Float или Double в зависимости от backend JSON сериализации
        if let c = payload["confidence"] as? Double {
            self.confidence = c
        } else if let c = payload["confidence"] as? Float {
            self.confidence = Double(c)
        } else {
            self.confidence = nil
        }
        // audio_duration_sec может прийти Double или Float (JSONSerialization), либо null.
        if let d = payload["audio_duration_sec"] as? Double {
            self.audioDurationSec = d
        } else if let d = payload["audio_duration_sec"] as? Float {
            self.audioDurationSec = Double(d)
        } else {
            self.audioDurationSec = nil
        }
        // Action items / decisions / questions (PR #289 backend, опциональные поля).
        // Пустой массив вместо nil — упрощает UI код (.isEmpty всегда работает).
        if let raw = payload["action_items"] as? [[String: Any]] {
            self.actionItems = raw.compactMap { ActionItem(payload: $0) }
        } else {
            self.actionItems = []
        }
        self.decisions = (payload["decisions"] as? [String]) ?? []
        self.questions = (payload["questions"] as? [String]) ?? []
    }
}
