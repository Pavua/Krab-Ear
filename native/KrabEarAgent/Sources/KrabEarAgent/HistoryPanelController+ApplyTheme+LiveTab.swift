/*
 HistoryPanelController+ApplyTheme+LiveTab.swift

 Live Translation tab assembly extracted из applyVisualTheme.
 Continuing PR #327 incremental split pattern.

 Tab включает:
 - **Live перевод (поток)** — Phase 2 PR 2.3 dual-pane TranslationStreamView (top)
 - Translation settings section (build helper)
 - Voice Gateway section (with reparented toolsRow + gatewayRow + assistConfigRow)
 - Call Assist section (with reparented 5 views)
 - Realtime preview card
 - Width constraints applied to all liveStack children
*/

import AppKit
import ObjectiveC.runtime

// Swift 6 strict concurrency: associated-object keys — `nonisolated(unsafe)`
// поскольку ObjC runtime использует address как identity (статический + thread-safe
// для setAssociatedObject API). Реальная «мутация» через ObjC, не Swift.
nonisolated(unsafe) private var translationStreamSectionKey: UInt8 = 0
nonisolated(unsafe) private var translationStreamViewKey: UInt8 = 0

extension HistoryPanelController {

    // MARK: - TranslationStreamView (Phase 2 PR 2.3)

    /// Lazy TranslationStreamView — single instance per controller, reused между
    /// applyVisualTheme rebuilds. Stored через objc_associated objects (та же
    /// техника что используется для другого extension state).
    var translationStreamView: TranslationStreamView {
        if let existing = objc_getAssociatedObject(self, &translationStreamViewKey) as? TranslationStreamView {
            return existing
        }
        let view = TranslationStreamView(frame: .zero)
        // Делегируем toggle на AppDelegate.toggleLiveSubsCaptureFromMenu (Phase 2B
        // pipeline). Существующий path: SystemAudioCapture.start/stop + IPC
        // live_subs_push_chunk. UI просто отражает state.
        view.onToggleCapture = { [weak self] in
            guard let _ = self else { return }
            if let app = NSApp.delegate as? AgentAppDelegate {
                app.toggleLiveSubsCaptureFromMenu()
                // Синхронизируем UI с capture state.
                let isCap = app.systemAudioCapture.isCapturing
                if let panel = NSApp.delegate as? AgentAppDelegate {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
                        view.isCapturing = panel.systemAudioCapture.isCapturing
                    }
                }
                _ = isCap
            }
        }
        objc_setAssociatedObject(self, &translationStreamViewKey, view, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return view
    }

    /// Assemble Live Translation tab. Mutates self.liveStack +
    /// self.themeWidthConstraints. Caller просто зовёт после dictation tab
    /// готов; sequence важен потому что некоторые views reparent'ятся из
    /// других мест.
    func setupLiveTranslationTab() {
        // Wire targets/actions для controls used in buildTranslationSection().
        translationSelector.target = self
        translationSelector.action = #selector(onTranslationModeChanged)
        networkSelector.target = self
        networkSelector.action = #selector(onNetworkModeChanged)
        translationStyleSelector.target = self
        translationStyleSelector.action = #selector(onTranslationStyleChanged)
        let translationSection = buildTranslationSection()

        // Phase 2 PR 2.3 — dual-pane TranslationStreamView вверху таба.
        let streamView = self.translationStreamView
        streamView.removeFromSuperview()
        let streamCard = ThemeCardView()
        streamCard.title = ""
        streamCard.contentStackView.addArrangedSubview(streamView)
        let streamSection = CollapsibleSectionView(
            sectionId: "live_stream_translation",
            title: "Live перевод (поток)",
            isExpanded: true
        )
        streamSection.contentStackView.addArrangedSubview(streamCard)
        objc_setAssociatedObject(self, &translationStreamSectionKey, streamSection, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)

        // Voice Gateway section — reparenting tools + gateway + call assist config rows.
        let gatewayCard = ThemeCardView()
        gatewayCard.title = ""
        for view in [toolsRow, voiceGatewayRow, callAssistConfigRow] as [NSView] {
            view.removeFromSuperview()
            gatewayCard.contentStackView.addArrangedSubview(view)
        }
        let gatewaySection = CollapsibleSectionView(
            sectionId: "live_gateway",
            title: "Voice Gateway",
            isExpanded: true
        )
        gatewaySection.contentStackView.addArrangedSubview(gatewayCard)

        // Call Assist section — reparenting 5 control rows + output scroll.
        let callAssistCard = ThemeCardView()
        callAssistCard.title = ""
        for view in [callAssistControlRow, callPhrasePresetRow, callPhraseActionRow, callTimelineRow, callAssistOutputScroll] as [NSView] {
            view.removeFromSuperview()
            callAssistCard.contentStackView.addArrangedSubview(view)
        }
        let callAssistSection = CollapsibleSectionView(
            sectionId: "live_call_assist",
            title: "Call Assist",
            isExpanded: false
        )
        callAssistSection.contentStackView.addArrangedSubview(callAssistCard)
        self.liveCallAssistSection = callAssistSection

        // Glossary search section (feat/phase-d-batch9 glossary search).
        let glossarySection = setupGlossarySearchSection()

        // Assemble liveStack — Phase 2 stream view вверху, потом header + sections.
        liveStack.addArrangedSubview(streamSection)
        liveStack.addArrangedSubview(liveHeaderRow)
        liveStack.addArrangedSubview(translationSection)
        liveStack.addArrangedSubview(glossarySection)
        liveStack.addArrangedSubview(gatewaySection)
        liveStack.addArrangedSubview(callAssistSection)

        let realtimeCard = ThemeCardView()
        realtimeCard.contentStackView.addArrangedSubview(realtimeScroll)
        liveStack.addArrangedSubview(realtimeCard)

        // Width constraints для всех live translation children
        // (consistent с historyStack pattern).
        for child in liveStack.arrangedSubviews {
            let c = child.widthAnchor.constraint(equalTo: liveStack.widthAnchor)
            c.isActive = true
            themeWidthConstraints.append(c)
        }
    }
}
