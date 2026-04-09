/*
 Общие модели данных нативного агента Krab Ear.

 Связи модуля:
 1) AgentSettings синхронизируется с backend settings.json.
 2) HistoryItem отражает записи из history.ndjson.
*/

import Foundation

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
    var historyPolicy: String
    var historyPageSize: Int
    var historyTextDensity: String
    var realtimePreviewEnabled: Bool
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
    var llmModel: String

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
        historyPolicy: "unlimited",
        historyPageSize: 50,
        historyTextDensity: "normal",
        realtimePreviewEnabled: true,
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
        llmModel: "qwen3.5-9b@6bit"
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
        self.historyPolicy = (payload["history_policy"] as? String) ?? Self.default.historyPolicy
        self.historyPageSize = (payload["history_page_size"] as? Int) ?? Self.default.historyPageSize
        self.historyTextDensity = (payload["history_text_density"] as? String) ?? Self.default.historyTextDensity
        self.realtimePreviewEnabled = (payload["realtime_preview_enabled"] as? Bool) ?? Self.default.realtimePreviewEnabled
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
        self.llmModel = (payload["llm_model"] as? String) ?? Self.default.llmModel
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
        historyPolicy: String,
        historyPageSize: Int,
        historyTextDensity: String,
        realtimePreviewEnabled: Bool,
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
        llmModel: String
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
        self.historyPolicy = historyPolicy
        self.historyPageSize = historyPageSize
        self.historyTextDensity = historyTextDensity
        self.realtimePreviewEnabled = realtimePreviewEnabled
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
        self.llmModel = llmModel
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
            "llm_model": llmModel,
        ]
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
    }
}
