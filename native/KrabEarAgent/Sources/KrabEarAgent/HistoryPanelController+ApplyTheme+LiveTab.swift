/*
 HistoryPanelController+ApplyTheme+LiveTab.swift

 Live Translation tab assembly extracted из applyVisualTheme.
 Continuing PR #327 incremental split pattern.

 Tab включает:
 - Translation settings section (build helper)
 - Voice Gateway section (with reparented toolsRow + gatewayRow + assistConfigRow)
 - Call Assist section (with reparented 5 views)
 - Realtime preview card
 - Width constraints applied to all liveStack children
*/

import AppKit

extension HistoryPanelController {

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

        // Assemble liveStack.
        liveStack.addArrangedSubview(liveHeaderRow)
        liveStack.addArrangedSubview(translationSection)
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
