import XCTest
@testable import KrabEarAgent

private final class SpyHUD: CallObserverHUDPresenting {
    var shown: [String] = []; var lingers: [String] = []; var hides = 0
    var updates: [(String, String, Int, CallAudioPlayer.ListenState, String?)] = []
    var isHUDVisible = false
    func showHUD(session: VGSessionInfo) { shown.append(session.id); isHUDVisible = true }
    func updateHUD(session: VGSessionInfo, status: String, lastEntries: [TranscriptEntry],
                   listenState: CallAudioPlayer.ListenState, listeningSessionId: String?) {
        updates.append((session.id, status, lastEntries.count, listenState, listeningSessionId))
    }
    func showLinger(message: String) { lingers.append(message) }
    func hideHUD() { hides += 1; isHUDVisible = false }
}

private final class SpyPanel: CallObserverPanelPresenting {
    var shown: [String] = []; var transcripts: [[TranscriptEntry]] = []
    var terminals: [String] = []; var lives = 0; var sheetCloses = 0
    var costs: [String] = []; var badges: [String?] = []; var hangupPrompts = 0
    var isPanelVisible = false
    func showPanel(session: VGSessionInfo) { shown.append(session.id); isPanelVisible = true }
    func updateTranscript(_ entries: [TranscriptEntry]) { transcripts.append(entries) }
    func updateStatus(status: String, muted: Bool?, held: Bool?, badge: String?) { badges.append(badge) }
    func presentHangupConfirm() { hangupPrompts += 1 }
    func updateCost(_ text: String) { costs.append(text) }
    func setTerminal(message: String) { terminals.append(message) }
    func setLive() { lives += 1 }
    func closeHangupSheetIfOpen() { sheetCloses += 1 }
}

private final class SpyPoster: VGCommandPosting {
    var hangups: [String] = []
    var hangupResult: Result<Int, Error> = .success(200)
    var costValue: Double? = 0.42
    func hangup(baseURL: URL, sessionId: String, completion: @escaping (Result<Int, Error>) -> Void) {
        hangups.append(sessionId); completion(hangupResult)
    }
    func fetchCostUsd(baseURL: URL, sessionId: String, completion: @escaping (Double?) -> Void) {
        completion(costValue)
    }
}

/// I-5 (ревью): completion ОБЯЗАН доставляться асинхронно (DispatchQueue.main.async),
/// как реальный IPCCallObserverSettings (IPC-роундтрип, никогда не синхронно
/// same-call-stack) — синхронный мок маскировал баг, где
/// applyPrivacySuppressionIfNeeded читал ещё не применённые значения.
private final class FixedSettings: CallObserverSettingsProviding {
    var hudEnabled = true; var autoplay = false; var privacy = false
    func refresh(completion: @escaping @Sendable (Bool, Bool, Bool, URL) -> Void) {
        let hudEnabled = hudEnabled, autoplay = autoplay, privacy = privacy
        DispatchQueue.main.async {
            completion(hudEnabled, autoplay, privacy, URL(string: "http://127.0.0.1:8090")!)
        }
    }
}

final class CallObserverCoordinatorTests: XCTestCase {
    private var hud = SpyHUD()
    private var panel = SpyPanel()
    private var poster = SpyPoster()
    private var settings = FixedSettings()
    private var stream = VGCallStreamClient()
    private var player = CallAudioPlayer()
    private var streamHandler: ((VGWebSocketConnection.Message, UInt64) -> Void)?

    private func resetFixtures() {
        hud = SpyHUD(); panel = SpyPanel(); poster = SpyPoster()
        settings = FixedSettings(); stream = VGCallStreamClient(); player = CallAudioPlayer()
        streamHandler = nil
    }

    private func makeCoordinator() -> CallObserverCoordinator {
        stream.connectionFactoryForTests = { _, _, onMessage in
            self.streamHandler = onMessage
            final class NoopConn: VGWebSocketConnecting { func connect() {}; func permanentStop() {} }
            return NoopConn()
        }
        player.connectionFactoryForTests = { _, _, _, _ in
            final class NoopConn: VGWebSocketConnecting { func connect() {}; func permanentStop() {} }
            return NoopConn()
        }
        let c = CallObserverCoordinator(hud: hud, panel: panel, poster: poster,
                                        settings: settings, stream: stream, player: player,
                                        tokenProvider: { "" },
                                        lingerSeconds: 0.1,
                                        costPollInterval: 0.05,
                                        uiCoalesceInterval: 0)
        // M-2 (ревью round 2): дренируем один такт здесь, централизованно, а не
        // per-test — init()'ный refreshSettings() (FixedSettings.refresh теперь
        // честно асинхронен, I-5) обязан успеть примениться ДО первого действия
        // теста. Страховка от ложно-зелёных БУДУЩИХ тестов с не-дефолтными
        // settings, которые забудут собственный drain().
        drain()
        return c
    }

    private func session(_ id: String) -> VGSessionInfo {
        VGSessionInfo(id: id, status: "running", phone: "+341", callDirection: "outbound",
                      createdAt: "2026-08-21T10:00:00Z", updatedAt: "2026-08-21T10:00:00Z",
                      srcLang: "es", tgtLang: "ru", callBrief: "")
    }

    private func drain(_ t: TimeInterval = 0.05) { RunLoop.main.run(until: Date().addingTimeInterval(t)) }

    private func emit(_ json: String, gen: UInt64) {
        streamHandler?(.text(json), gen); drain()
    }

    func test_happy_path_appear_transcript_end_once() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        XCTAssertEqual(hud.shown, ["s1"])
        emit(#"{"type":"stt.final","ts":"t","data":{"text":"hola"}}"#, gen: 1)
        emit(#"{"type":"translation.final","ts":"t","data":{"text":"привет","source_text":"hola","src_lang":"es","tgt_lang":"ru"}}"#, gen: 1)
        emit(#"{"type":"agent.response","ts":"t","data":{"text":"Claro","text_ru":"Конечно","utterance_ts":"u1"}}"#, gen: 1)
        emit(#"{"type":"call.ended","ts":"t","data":{"reason":"hangup"}}"#, gen: 1)
        // дублирующие терминалы — no-op
        c.watcherCallGone(sessionId: "s1", generation: 1); drain()
        emit(#"{"type":"call.closed","ts":"t","data":{"session_id":"s1"}}"#, gen: 1)
        XCTAssertEqual(hud.lingers.count, 1, "терминал ровно один раз")
        XCTAssertEqual(panel.terminals.count, panel.isPanelVisible ? 1 : 0)
    }

    func test_all_five_signals_each_terminal_once() {
        let signals: [(String, (CallObserverCoordinator) -> Void)] = [
            ("call.ended", { c in self.emit(#"{"type":"call.ended","ts":"t","data":{}}"#, gen: 1) }),
            ("call.closed", { c in self.emit(#"{"type":"call.closed","ts":"t","data":{}}"#, gen: 1) }),
            ("callGone", { c in c.watcherCallGone(sessionId: "s1", generation: 1); self.drain() }),
            ("vgLost", { c in c.watcherVGLost(sessionId: "s1", generation: 1); self.drain() }),
            ("hangupTerminal", { c in
                self.poster.hangupResult = .success(409)
                c.userRequestedHangupConfirmed(); self.drain()
            }),
        ]
        for (name, fire) in signals {
            resetFixtures()  // свежие спаи (инлайн-инициализация не сбрасывается setUp-ом)
            let c = makeCoordinator()
            c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
            fire(c)
            fire(c)
            XCTAssertEqual(hud.lingers.count, 1, "сигнал \(name): терминал дважды")
        }
    }

    func test_linger_hides_hud_after_delay_and_new_call_cancels_stale_linger() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        emit(#"{"type":"call.ended","ts":"t","data":{}}"#, gen: 1)
        // B появился в linger-окне A
        c.watcherCallAppeared(session("s2"), generation: 2, resurrected: false); drain()
        drain(0.2)  // linger A истёк
        XCTAssertTrue(hud.isHUDVisible, "linger A спрятал HUD живого B")
    }

    func test_vgLost_message_differs() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.watcherVGLost(sessionId: "s1", generation: 1); drain()
        XCTAssertTrue(hud.lingers[0].contains("VG") || hud.lingers[0].contains("связь"),
                      "vgLost должен отличаться от обычного конца")
    }

    func test_resurrection_preserves_transcript() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.openPanelFromMenu(); drain()
        emit(#"{"type":"stt.final","ts":"t","data":{"text":"hola"}}"#, gen: 1)
        c.watcherVGLost(sessionId: "s1", generation: 1); drain()
        c.watcherCallAppeared(session("s1"), generation: 3, resurrected: true); drain()
        XCTAssertGreaterThanOrEqual(panel.lives, 1, "панель вышла из terminal в live")
        XCTAssertEqual(panel.terminals.count, 1)
        XCTAssertFalse(panel.transcripts.isEmpty)
        XCTAssertEqual(panel.transcripts.last?.count, 1, "транскрипт сохранён при resurrection")
    }

    /// Rewritten per review I-1: прерываем ВТОРУЮ реплику (u2), а не первую —
    /// interrupting u1 (which also happens to be the first/only-so-far entry)
    /// wouldn't distinguish "matched by ts" from a buggy "always matches the
    /// first .agent entry" implementation, since both would land on the same
    /// index. Targeting u2 forces a genuine ts-based match.
    func test_agent_interrupted_matches_by_utterance_ts_not_last() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.openPanelFromMenu(); drain()
        emit(#"{"type":"agent.response","ts":"t","data":{"text":"Первая","utterance_ts":"u1"}}"#, gen: 1)
        emit(#"{"type":"agent.response","ts":"t","data":{"text":"Вторая","utterance_ts":"u2"}}"#, gen: 1)
        emit(#"{"type":"agent.interrupted","ts":"t","data":{"utterance_ts":"u2","spoken_fraction":0.5,"spoken_text":"Втор"}}"#, gen: 1)
        guard case .agent(_, _, _, let interrupted1, _, _)? =
                panel.transcripts.last?.first?.kind else { return XCTFail() }
        XCTAssertFalse(interrupted1, "первая реплика (u1) не тронута")
        guard case .agent(_, _, _, let interrupted2, let spoken2, _)? =
                panel.transcripts.last?.last?.kind else { return XCTFail() }
        XCTAssertTrue(interrupted2, "прервана ВТОРАЯ реплика (u2) — матч по ts, не по позиции")
        XCTAssertEqual(spoken2, "Втор")
    }

    func test_auto_spoken_renders_as_agent_line() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.openPanelFromMenu(); drain()
        emit(#"{"type":"agent.suggestion.auto_spoken","ts":"t","data":{"text":"Uno","text_ru":"Один"}}"#, gen: 1)
        guard case .agent(let text, _, _, _, _, _)? = panel.transcripts.last?.last?.kind else { return XCTFail() }
        XCTAssertEqual(text, "Uno")
    }

    func test_privacy_on_suppresses_auto_show_manual_stays() {
        settings.privacy = true
        let c = makeCoordinator()  // M-2: settling drain() now happens inside makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        XCTAssertTrue(hud.shown.isEmpty, "privacy: авто-показ подавлен")
        c.openPanelFromMenu(); drain()
        XCTAssertEqual(panel.shown, ["s1"], "ручной вход разрешён")
    }

    func test_privacy_flip_midcall_hides_auto_hud_keeps_manual_panel() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.openPanelFromMenu(); drain()
        settings.privacy = true
        c.watcherCallUpdated(session("s1"), generation: 1); drain()  // полл-тик перечитывает
        XCTAssertEqual(hud.hides, 1, "авто-показанный HUD скрыт")
        XCTAssertTrue(panel.isPanelVisible, "вручную открытая панель остаётся")
    }

    func test_hangup_single_flight_and_409_terminal_silent() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        poster.hangupResult = .success(409)
        c.userRequestedHangupConfirmed()
        c.userRequestedHangupConfirmed(); drain()
        XCTAssertEqual(poster.hangups.count, 1, "single-flight")
        XCTAssertEqual(hud.lingers.count, 1, "409 → терминал, без error-тоста")
    }

    func test_terminal_closes_open_hangup_sheet() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.openPanelFromMenu(); drain()
        emit(#"{"type":"call.ended","ts":"t","data":{}}"#, gen: 1)
        XCTAssertGreaterThanOrEqual(panel.sheetCloses, 1)
    }

    func test_terminal_of_selected_with_second_live_no_linger_over_live() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.watcherCallAppeared(session("s2"), generation: 2, resurrected: false); drain()
        c.openPanelFromMenu(); drain()
        c.watcherCallGone(sessionId: "s1", generation: 1); drain()
        XCTAssertTrue(hud.lingers.isEmpty, "linger при живом втором звонке запрещён")
        XCTAssertTrue(hud.isHUDVisible)
        // панель осталась на терминальной A до ручного свитча;
        // setLive: №1 — openPanelFromMenu (живой A), №2 — свитч на живой B.
        // 🔴 НЕ «чинить» реализацию под ==1, убирая setLive из openPanelFromMenu:
        // ручное открытие живого звонка навсегда потеряло бы бейдж «в эфире».
        c.userSelectedSession("s2"); drain()
        XCTAssertEqual(panel.lives, 2)
    }

    func test_manually_closed_hud_not_resurrected_by_linger() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.userClosedHUD(); drain()
        emit(#"{"type":"call.ended","ts":"t","data":{}}"#, gen: 1)
        XCTAssertTrue(hud.lingers.isEmpty, "linger не воскрешает вручную закрытый HUD")
    }

    func test_stale_generation_events_dropped() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.openPanelFromMenu(); drain()
        let before = panel.transcripts.count
        emit(#"{"type":"stt.final","ts":"t","data":{"text":"stale"}}"#, gen: 99)
        XCTAssertEqual(panel.transcripts.count, before)
    }

    func test_transcript_capped_at_500() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.openPanelFromMenu(); drain()
        for i in 0..<510 {
            streamHandler?(.text(#"{"type":"stt.final","ts":"t","data":{"text":"m\#(i)"}}"#), 1)
        }
        drain()
        XCTAssertLessThanOrEqual(panel.transcripts.last?.count ?? 0, 500)
    }

    func test_cost_polling_updates_and_stops_at_terminal() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.openPanelFromMenu(); drain(0.2)
        XCTAssertEqual(panel.costs.last, "$0.42")
        poster.costValue = nil
        drain(0.15)
        XCTAssertEqual(panel.costs.last, "—")
        emit(#"{"type":"call.ended","ts":"t","data":{}}"#, gen: 1)
        let frozen = panel.costs.count
        drain(0.2)
        XCTAssertEqual(panel.costs.count, frozen, "терминал остановил cost-поллинг")
    }

    func test_hangup_502_badge_no_terminal() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.openPanelFromMenu(); drain()
        poster.hangupResult = .success(502)
        c.userRequestedHangupConfirmed(); drain()
        XCTAssertTrue(hud.lingers.isEmpty, "502 — не терминал")
        XCTAssertTrue(panel.badges.compactMap { $0 }.contains { $0.contains("Не удалось") })
    }

    func test_hangup_404_after_end_terminal_silent() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        poster.hangupResult = .success(404)
        c.userRequestedHangupConfirmed(); drain()
        XCTAssertEqual(hud.lingers.count, 1, "404 → терминал без error-тоста")
        XCTAssertFalse(panel.badges.compactMap { $0 }.contains { $0.contains("Не удалось") })
    }

    func test_hangup_from_hud_opens_panel_with_confirm() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.userRequestedHangupFromHUD(); drain()
        XCTAssertTrue(panel.isPanelVisible)
        XCTAssertEqual(panel.hangupPrompts, 1)
    }

    // MARK: - Fix round 1 (reviewer C-1/C-2/I-2)

    /// C-1: наблюдатель НЕ одноразовый. Первый звонок завершается, второй
    /// появляется без ручного вмешательства — openPanelFromMenu обязан открыть
    /// НОВЫЙ живой звонок (не застрять на мёртвом s1), и события s2 обязаны
    /// доезжать до панели. До фикса selectedId навсегда оставался на s1, потому
    /// что `observed[selectedId!]` никогда не становится nil (терминальные
    /// записи не удаляются) — второй звонок был бы никогда не наблюдаем.
    func test_second_call_after_first_ends_is_observed_and_openable() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.watcherCallGone(sessionId: "s1", generation: 1); drain()
        c.watcherCallAppeared(session("s2"), generation: 2, resurrected: false); drain()
        c.openPanelFromMenu(); drain()
        XCTAssertEqual(panel.shown.last, "s2",
                       "openPanelFromMenu обязан открыть НОВЫЙ живой звонок, не мёртвый s1")
        emit(#"{"type":"stt.final","ts":"t","data":{"text":"hola"}}"#, gen: 2)
        XCTAssertEqual(panel.transcripts.last?.count, 1, "события s2 обязаны доезжать до панели")
    }

    /// C-2: hudManuallyClosed принадлежит КОНКРЕТНОМУ звонку, не залипает
    /// навсегда. Владелец закрывает HUD для s1 → s1 завершается → появляется
    /// НОВЫЙ (несвязанный) звонок s2 — HUD обязан авто-показаться для s2. До
    /// фикса сброс срабатывал на resurrected (наоборот), так что после первого
    /// закрытия HUD никогда бы не показался автоматически снова ни для какого
    /// будущего звонка.
    func test_hud_manually_closed_resets_for_new_call_after_previous_ends() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.userClosedHUD(); drain()
        c.watcherCallGone(sessionId: "s1", generation: 1); drain()
        c.watcherCallAppeared(session("s2"), generation: 2, resurrected: false); drain()
        XCTAssertTrue(hud.shown.contains("s2"),
                      "новый звонок после закрытия HUD прежним обязан авто-показаться")
    }

    /// I-2: HUD-контент обязан отражать НОВЕЙШИЙ живой звонок, а не selectedId
    /// (который после C-1-фикса намеренно НЕ переключается автоматически между
    /// конкурентными звонками — панель/стримы остаются на s1).
    func test_hud_content_tracks_newest_live_call_not_selected() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.watcherCallAppeared(session("s2"), generation: 2, resurrected: false); drain()
        XCTAssertEqual(hud.updates.last?.0, "s2",
                       "HUD-контент обязан отражать НОВЕЙШИЙ живой звонок (s2), не selected (s1)")
    }

    // MARK: - Fix round 2 (reviewer P-1/P-2)

    /// P-1: выбор НЕ уезжает из-под ОТКРЫТОЙ панели. Панель открыта на
    /// терминальном A; появляется живой B — панель обязана ПРОДОЛЖАТЬ
    /// показывать A (транскрипт не затёрт пустым updateTranscript от B,
    /// запись A не удалена prune'ом), пока владелец не переключится сам. До
    /// фикса C-1-условие переключало selectedId на B, потому что смотрело
    /// только на "старый выбор не живой", игнорируя открытую панель —
    /// комбинация двух половинок правила чинит это.
    func test_open_panel_on_terminal_call_not_preempted_by_new_arrival() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.openPanelFromMenu(); drain()
        emit(#"{"type":"stt.final","ts":"t","data":{"text":"hola"}}"#, gen: 1)
        c.watcherCallGone(sessionId: "s1", generation: 1); drain()
        let transcriptsBeforeArrival = panel.transcripts.count
        c.watcherCallAppeared(session("s2"), generation: 2, resurrected: false); drain()
        XCTAssertEqual(panel.transcripts.count, transcriptsBeforeArrival,
                       "появление B не должно перерисовать панель, открытую на терминальном A")
        XCTAssertTrue(c.observedSessions().contains { $0.id == "s1" },
                      "терминальная A обязана остаться в observed — панель может её показывать")
        c.userRequestedHangupConfirmed(); drain()
        XCTAssertTrue(poster.hangups.isEmpty,
                      "A уже терминальна — hangup не должен уйти вообще (не подставлять живой B)")
    }

    /// P-2: HUD рисует hudTrackedId (новейший живой), действие с HUD обязано
    /// целиться туда же, а не в неизменный selectedId. Два живых звонка;
    /// selectedId остаётся s1 (панель закрыта, s1 жив — C-1), HUD следит за s2
    /// (новейший). Хангап, инициированный ИЗ HUD, должен положить трубку s2.
    func test_hangup_from_hud_targets_hud_tracked_call_not_selected() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.watcherCallAppeared(session("s2"), generation: 2, resurrected: false); drain()
        c.userRequestedHangupFromHUD(); drain()
        c.userRequestedHangupConfirmed(); drain()
        XCTAssertEqual(poster.hangups, ["s2"],
                       "хангап из HUD обязан целиться в то, что HUD реально показывает (s2), не в selectedId (s1)")
    }

    /// P-2 (userToggledListen сиблинг): та же ре-таргетировка для прослушки —
    /// проверяем через URL, который дошёл до connectionFactoryForTests
    /// (содержит sessionId в пути).
    func test_toggle_listen_targets_hud_tracked_call_not_selected() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.watcherCallAppeared(session("s2"), generation: 2, resurrected: false); drain()
        var capturedURL: URL?
        player.connectionFactoryForTests = { url, _, _, _ in
            capturedURL = url
            final class NoopConn: VGWebSocketConnecting { func connect() {}; func permanentStop() {} }
            return NoopConn()
        }
        c.userToggledListen(); drain()
        XCTAssertEqual(capturedURL?.path, "/v1/sessions/s2/monitor/audio",
                       "прослушка из HUD обязана целиться в s2 (что HUD реально показывает), не в selectedId (s1)")
    }

    // MARK: - T9 doп.скоуп (2в/2г/2д): rebind без показа панели + listeningSessionId

    /// 2в: клик прослушки на HUD, когда hudTrackedId != selectedId, ретаргетит
    /// выбор ЧЕРЕЗ rebind (без показа панели) — панель, которую владелец не
    /// открывал, не обязана внезапно появляться от нажатия кнопки на HUD.
    func test_toggle_listen_from_hud_does_not_open_panel_when_retargeting() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.watcherCallAppeared(session("s2"), generation: 2, resurrected: false); drain()
        XCTAssertTrue(panel.shown.isEmpty, "панель ещё не была открыта владельцем")
        c.userToggledListen(); drain()
        XCTAssertTrue(panel.shown.isEmpty,
                      "клик прослушки на HUD не должен сам по себе выкатывать окно панели")
    }

    /// 2г: HUD-контент несёт listeningSessionId независимо от того, какую
    /// карточку HUD сейчас показывает (hudTrackedId) — реализация HUD обязана
    /// гасить зелёный индикатор, когда они разошлись.
    func test_hud_update_carries_listening_session_id_of_actually_played_call() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.userToggledListen(); drain()  // слушаем s1 — единственный живой на тот момент
        c.watcherCallAppeared(session("s2"), generation: 2, resurrected: false); drain()  // HUD переезжает на s2
        guard let last = hud.updates.last else { return XCTFail() }
        XCTAssertEqual(last.0, "s2", "HUD-карточка показывает новейший живой (s2)")
        XCTAssertEqual(last.4, "s1",
                       "реально слушаем всё ещё s1 — listeningSessionId обязан отражать это, не s2")
    }

    /// 2д: панель закрыл сам владелец (не кража выбора у открытого окна, P-1
    /// защищает только ОТКРЫТУЮ панель) — если выбор терминален и есть живой
    /// звонок, следующее открытие обязано показать его, а не мёртвый s1 навсегда.
    func test_user_closed_panel_reselects_newest_live_when_selected_is_terminal() {
        let c = makeCoordinator()
        c.watcherCallAppeared(session("s1"), generation: 1, resurrected: false); drain()
        c.openPanelFromMenu(); drain()
        c.watcherCallGone(sessionId: "s1", generation: 1); drain()
        c.watcherCallAppeared(session("s2"), generation: 2, resurrected: false); drain()
        c.userClosedPanel(); drain()
        c.openPanelFromMenu(); drain()
        XCTAssertEqual(panel.shown.last, "s2",
                       "после закрытия панели выбор обязан перейти на новейший живой звонок")
    }
}
