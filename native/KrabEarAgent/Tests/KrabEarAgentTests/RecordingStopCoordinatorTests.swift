/*
 RecordingStopCoordinatorTests.swift

 Проверяет единый R2-координатор остановки записи: независимые бюджеты
 транспортных повторов, recorder_timeout и stop_in_progress, а также
 неизменность opaque generation_token на всех повторных IPC-вызовах.
 Тесты используют внедрённые operation/sleep, поэтому не открывают сокет,
 не ждут реальные 2–300 секунд и не затрагивают живой backend.
*/

import XCTest
@testable import KrabEarAgent

final class RecordingStopCoordinatorTests: XCTestCase {

    private func source(_ name: String) throws -> String {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent/\(name)")
        return try String(contentsOf: url, encoding: .utf8)
    }

    func test_transport_error_retries_up_to_two_extra_attempts() {
        XCTAssertEqual(
            RecordingStopCoordinator.decide(
                afterTransportError: true,
                attempt: 1
            ),
            .retry
        )
        XCTAssertEqual(
            RecordingStopCoordinator.decide(
                afterTransportError: true,
                attempt: 2
            ),
            .retry
        )
        XCTAssertEqual(
            RecordingStopCoordinator.decide(
                afterTransportError: true,
                attempt: 3
            ),
            .giveUpRescuePending
        )
    }

    func test_non_transport_error_is_never_retried() {
        XCTAssertEqual(
            RecordingStopCoordinator.decide(
                afterTransportError: false,
                attempt: 1
            ),
            .surfaceAsIs
        )
    }

    func test_typed_backend_error_is_never_retried() {
        XCTAssertEqual(
            RecordingStopCoordinator.decide(
                afterStatus: "stt_failed",
                attempt: 1
            ),
            .surfaceAsIs
        )
    }

    func test_recorder_timeout_retries_same_generation_then_retains_recovery() {
        XCTAssertEqual(
            RecordingStopCoordinator.decide(
                afterStatus: "recorder_timeout",
                attempt: 1
            ),
            .retryRecorderStop(delaySec: 2)
        )
        XCTAssertEqual(
            RecordingStopCoordinator.decide(
                afterStatus: "recorder_timeout",
                attempt: 2
            ),
            .retryRecorderStop(delaySec: 4)
        )
        XCTAssertEqual(
            RecordingStopCoordinator.decide(
                afterStatus: "recorder_timeout",
                attempt: 3
            ),
            .recoveryPending
        )
    }

    func test_stop_in_progress_polls_within_budget_then_reports_slow_finalization() {
        XCTAssertEqual(
            RecordingStopCoordinator.decide(
                afterStatus: "stop_in_progress",
                attempt: 1
            ),
            .pollAgain
        )
        XCTAssertEqual(
            RecordingStopCoordinator.decide(
                afterStatus: "stop_in_progress",
                attempt: 150
            ),
            .pollAgain
        )
        XCTAssertEqual(
            RecordingStopCoordinator.decide(
                afterStatus: "stop_in_progress",
                attempt: 151
            ),
            .finalizationSlow
        )
    }

    func test_unknown_generation_points_at_rescue_not_loss() {
        XCTAssertEqual(
            RecordingStopCoordinator.decide(
                afterStatus: "unknown_generation",
                attempt: 1
            ),
            .giveUpRescuePending
        )
    }

    func test_owner_mismatch_has_its_own_branch() {
        XCTAssertEqual(
            RecordingStopCoordinator.decide(
                afterStatus: "owner_mismatch",
                attempt: 1
            ),
            .foreignOwner
        )
    }

    func test_executor_recorder_timeout_uses_exact_delays_and_same_request() async {
        let request = RecordingStopRequest(
            method: "stop_recording",
            params: [
                "source": "dictation",
                "generation_token": "G1-OPAQUE",
            ],
            timeoutSec: 120
        )
        var seenSources: [String] = []
        var seenTokens: [String] = []
        var delays: [TimeInterval] = []

        let outcome = await RecordingStopCoordinator.execute(
            request: request,
            operation: { repeatedRequest in
                seenSources.append(
                    repeatedRequest.params["source"] as? String ?? ""
                )
                seenTokens.append(
                    repeatedRequest.params["generation_token"] as? String ?? ""
                )
                return [
                    "result": [
                        "status": "recorder_timeout",
                        "preview_text": "живое превью",
                    ],
                ]
            },
            sleep: { delay in delays.append(delay) }
        )

        XCTAssertEqual(outcome.decision, .recoveryPending)
        XCTAssertEqual(seenSources, ["dictation", "dictation", "dictation"])
        XCTAssertEqual(seenTokens, ["G1-OPAQUE", "G1-OPAQUE", "G1-OPAQUE"])
        XCTAssertEqual(delays, [2, 4])
        XCTAssertEqual(outcome.result?["preview_text"] as? String, "живое превью")
    }

    func test_executor_transport_budget_is_bounded_and_keeps_request() async {
        let request = RecordingStopRequest(
            method: "meeting_stop",
            params: ["generation_token": "meeting-token"],
            timeoutSec: 120
        )
        var calls = 0
        var seenTokens: [String] = []
        var delays: [TimeInterval] = []

        let outcome = await RecordingStopCoordinator.execute(
            request: request,
            operation: { repeatedRequest in
                calls += 1
                seenTokens.append(
                    repeatedRequest.params["generation_token"] as? String ?? ""
                )
                throw IPCError.readFailed
            },
            sleep: { delay in delays.append(delay) }
        )

        XCTAssertEqual(outcome.decision, .giveUpRescuePending)
        XCTAssertEqual(calls, 3)
        XCTAssertEqual(
            seenTokens,
            ["meeting-token", "meeting-token", "meeting-token"]
        )
        XCTAssertEqual(delays, [0.25, 0.5])
    }

    func test_executor_non_transport_error_does_not_retry() async {
        let request = RecordingStopRequest(
            method: "stop_recording",
            params: [
                "source": "quick_capture",
                "generation_token": "quick-token",
            ],
            timeoutSec: 120
        )
        var calls = 0

        let outcome = await RecordingStopCoordinator.execute(
            request: request,
            operation: { _ in
                calls += 1
                throw IPCError.timeout
            },
            sleep: { _ in
                XCTFail("Для нетранспортной ошибки ожидание не вызывается")
            }
        )

        XCTAssertEqual(outcome.decision, .surfaceAsIs)
        XCTAssertEqual(calls, 1)
        XCTAssertNotNil(outcome.error)
        XCTAssertNil(outcome.response)
    }

    func test_executor_poll_budget_is_absolute_and_nonblocking() async {
        let request = RecordingStopRequest(
            method: "stop_recording",
            params: [
                "source": "dictation",
                "generation_token": "slow-token",
            ],
            timeoutSec: 120
        )
        var calls = 0
        var delays: [TimeInterval] = []
        var monotonicNow: TimeInterval = 0

        let outcome = await RecordingStopCoordinator.execute(
            request: request,
            operation: { _ in
                calls += 1
                return ["result": ["status": "stop_in_progress"]]
            },
            sleep: { delay in
                delays.append(delay)
                monotonicNow += delay
            },
            monotonicNow: { monotonicNow }
        )

        XCTAssertEqual(outcome.decision, .finalizationSlow)
        XCTAssertEqual(calls, 150)
        XCTAssertEqual(delays.count, 150)
        XCTAssertTrue(delays.allSatisfy {
            $0 == RecordingStopCoordinator.pollIntervalSec
        })
        XCTAssertEqual(
            monotonicNow,
            RecordingStopCoordinator.finalizationBudgetSec,
            accuracy: 0.000_001
        )
    }

    func test_executor_poll_deadline_counts_ipc_time_and_clamps_timeout() async {
        let request = RecordingStopRequest(
            method: "stop_recording",
            params: [
                "source": "dictation",
                "generation_token": "deadline-token",
            ],
            timeoutSec: 120
        )
        var calls = 0
        var monotonicNow: TimeInterval = 0
        var delays: [TimeInterval] = []
        var seenTimeouts: [Int] = []
        var seenTokens: [String] = []
        var seenSources: [String] = []

        let outcome = await RecordingStopCoordinator.execute(
            request: request,
            operation: { repeatedRequest in
                calls += 1
                seenTimeouts.append(repeatedRequest.timeoutSec)
                seenTokens.append(
                    repeatedRequest.params["generation_token"] as? String ?? ""
                )
                seenSources.append(
                    repeatedRequest.params["source"] as? String ?? ""
                )
                switch calls {
                case 1, 2:
                    monotonicNow += 120
                case 3:
                    monotonicNow += 56
                default:
                    break
                }
                return ["result": ["status": "stop_in_progress"]]
            },
            sleep: { delay in
                delays.append(delay)
                monotonicNow += delay
            },
            monotonicNow: { monotonicNow }
        )

        XCTAssertEqual(outcome.decision, .finalizationSlow)
        XCTAssertEqual(calls, 3)
        XCTAssertEqual(seenTimeouts, [120, 120, 56])
        XCTAssertEqual(delays, [2, 2])
        XCTAssertEqual(
            seenTokens,
            ["deadline-token", "deadline-token", "deadline-token"]
        )
        XCTAssertEqual(
            seenSources,
            ["dictation", "dictation", "dictation"]
        )
        XCTAssertEqual(request.timeoutSec, 120)
        XCTAssertEqual(
            request.params["generation_token"] as? String,
            "deadline-token"
        )
    }

    func test_executor_poll_sleep_never_exceeds_deadline_remainder() async {
        let request = RecordingStopRequest(
            method: "meeting_stop",
            params: ["generation_token": "remainder-token"],
            timeoutSec: 120
        )
        var calls = 0
        var monotonicNow: TimeInterval = 0
        var delays: [TimeInterval] = []
        var seenTimeouts: [Int] = []

        let outcome = await RecordingStopCoordinator.execute(
            request: request,
            operation: { repeatedRequest in
                calls += 1
                seenTimeouts.append(repeatedRequest.timeoutSec)
                if calls == 2 {
                    // Deadline уже создан первым ответом; второму poll остаётся
                    // лишь полсекунды, поэтому sleep обязан быть 0.5, не 2.
                    monotonicNow = 299.5
                }
                return ["result": ["status": "stop_in_progress"]]
            },
            sleep: { delay in
                delays.append(delay)
                monotonicNow += delay
            },
            monotonicNow: { monotonicNow }
        )

        XCTAssertEqual(outcome.decision, .finalizationSlow)
        XCTAssertEqual(calls, 2)
        XCTAssertEqual(seenTimeouts, [120, 120])
        XCTAssertEqual(delays, [2, 0.5])
        XCTAssertEqual(
            monotonicNow,
            RecordingStopCoordinator.finalizationBudgetSec,
            accuracy: 0.000_001
        )
    }

    func test_executor_returns_terminal_response_without_retry() async {
        let request = RecordingStopRequest(
            method: "stop_recording",
            params: [
                "source": "dictation",
                "generation_token": "done-token",
            ],
            timeoutSec: 120
        )
        var calls = 0

        let outcome = await RecordingStopCoordinator.execute(
            request: request,
            operation: { _ in
                calls += 1
                return [
                    "result": [
                        "status": "ok",
                        "history_id": "item-1",
                    ],
                ]
            },
            sleep: { _ in
                XCTFail("Терминальный ответ не должен ждать или повторяться")
            }
        )

        XCTAssertEqual(outcome.decision, .surfaceAsIs)
        XCTAssertEqual(calls, 1)
        XCTAssertEqual(outcome.result?["history_id"] as? String, "item-1")
        XCTAssertNil(outcome.error)
    }

    func test_executor_missing_result_is_not_terminal() async {
        let request = RecordingStopRequest(
            method: "stop_recording",
            params: [
                "source": "dictation",
                "generation_token": "malformed-token",
            ],
            timeoutSec: 120
        )
        var calls = 0

        let outcome = await RecordingStopCoordinator.execute(
            request: request,
            operation: { _ in
                calls += 1
                return ["ok": true, "id": "response-without-result"]
            },
            sleep: { _ in
                XCTFail("Некорректный конверт не должен автоматически крутиться")
            }
        )

        XCTAssertEqual(calls, 1)
        XCTAssertEqual(outcome.decision, .surfaceAsIs)
        XCTAssertFalse(outcome.hasTerminalResponse)
        XCTAssertNil(outcome.result)
        XCTAssertNotNil(outcome.error)
    }

    func test_agent_stores_explicit_generation_recovery_state() throws {
        let src = try source("main.swift")
        XCTAssertTrue(src.contains("var activeGenerationToken: String?"))
        XCTAssertTrue(src.contains("var activeGenerationOwner: String?"))
        XCTAssertTrue(src.contains("var recordingStopRecoveryPending = false"))
    }

    func test_hotkey_stop_uses_async_coordinator_and_token() throws {
        let src = try source("main+HotkeyRecording.swift")
        // 2026-08-11: сигнатура получила autoRetried: Bool = false (мини-волна
        // авто-дострела) — ищем по префиксу, не по точной старой сигнатуре.
        guard let range = src.range(of: "func stopRecording(") else {
            XCTFail("Hotkey stop обязан быть асинхронным")
            return
        }
        let stopBody = src[range.lowerBound...]
        XCTAssertTrue(stopBody.contains("RecordingStopCoordinator.execute"))
        XCTAssertTrue(stopBody.contains(#"params["generation_token"]"#))
        XCTAssertTrue(stopBody.contains("client.callAsync"))
        XCTAssertFalse(
            stopBody.contains("callWithRecovery("),
            "Живой stop-путь не должен возвращаться к sync IPC"
        )
        XCTAssertFalse(stopBody.contains("Thread.sleep"))
    }

    func test_hotkey_start_and_state_sync_are_async_through_socket() throws {
        let src = try source("main+HotkeyRecording.swift")

        XCTAssertTrue(
            src.contains("func syncRecordingStateWithBackend() async")
        )
        XCTAssertTrue(src.contains("func startRecording() async"))
        XCTAssertTrue(src.contains("await callAsyncWithRecovery("))
        XCTAssertFalse(
            src.contains(
                #"callWithRecovery(method: "get_recording_state""#
            )
        )
    }

    func test_pending_hotkey_start_preserves_release_as_stop_intent() throws {
        let hotkey = try source("main+HotkeyRecording.swift")
        let main = try source("main.swift")

        XCTAssertTrue(hotkey.contains("if recordingStartInFlight"))
        XCTAssertTrue(hotkey.contains("recordingStopRequestedDuringStart = true"))
        XCTAssertTrue(
            hotkey.contains(
                "if recordingStopRequestedDuringStart"
            )
        )
        XCTAssertTrue(
            main.contains(
                "if self.recordingStartInFlight"
            )
        )
        XCTAssertTrue(
            main.contains(
                "self.recordingStopRequestedDuringStart = true"
            )
        )
        XCTAssertTrue(
            main.contains(
                "await self?.performRecordToggle("
            )
        )
    }

    func test_meeting_promote_can_precede_dictation_start_response() {
        XCTAssertEqual(
            MeetingPromotionRoutingPolicy.decide(
                promoted: true,
                meetingToken: "Shared-G1",
                recordingStartInFlight: true,
                activeOwner: nil,
                activeToken: nil
            ),
            .deferUntilStart(token: "Shared-G1")
        )
        XCTAssertEqual(
            MeetingPromotionRoutingPolicy.decide(
                promoted: true,
                meetingToken: "Shared-G1",
                recordingStartInFlight: false,
                activeOwner: "dictation",
                activeToken: "Shared-G1"
            ),
            .handoffNow
        )
        XCTAssertEqual(
            MeetingPromotionRoutingPolicy.decide(
                promoted: true,
                meetingToken: "Foreign-G2",
                recordingStartInFlight: false,
                activeOwner: "dictation",
                activeToken: "Shared-G1"
            ),
            .ignore
        )
        XCTAssertEqual(
            MeetingPromotionRoutingPolicy.decide(
                promoted: true,
                meetingToken: nil,
                recordingStartInFlight: false,
                activeOwner: "dictation",
                activeToken: "Shared-G1"
            ),
            .ignore
        )
        XCTAssertEqual(
            MeetingPromotionRoutingPolicy.decide(
                promoted: true,
                meetingToken: "Shared-G1",
                recordingStartInFlight: false,
                activeOwner: "dictation",
                activeToken: nil
            ),
            .ignore
        )
    }

    func test_meeting_promote_requires_newer_owner_revision_when_both_known() {
        XCTAssertEqual(
            MeetingPromotionRoutingPolicy.decide(
                promoted: true,
                meetingToken: "Shared-G1",
                recordingStartInFlight: false,
                activeOwner: "dictation",
                activeToken: "Shared-G1",
                activeOwnerRevision: 17,
                meetingOwnerRevision: 18
            ),
            .handoffNow
        )
        XCTAssertEqual(
            MeetingPromotionRoutingPolicy.decide(
                promoted: true,
                meetingToken: "Shared-G1",
                recordingStartInFlight: false,
                activeOwner: "dictation",
                activeToken: "Shared-G1",
                activeOwnerRevision: 17,
                meetingOwnerRevision: 17
            ),
            .ignore
        )
        XCTAssertEqual(
            MeetingPromotionRoutingPolicy.decide(
                promoted: true,
                meetingToken: "Shared-G1",
                recordingStartInFlight: false,
                activeOwner: "dictation",
                activeToken: "Shared-G1",
                activeOwnerRevision: 17,
                meetingOwnerRevisionIsValid: false
            ),
            .ignore
        )
    }

    func test_ambiguous_meeting_promote_rejects_stale_owner_revision() {
        XCTAssertEqual(
            MeetingPromotionRoutingPolicy.decide(
                promoted: true,
                meetingToken: "Shared-G1",
                recordingStartInFlight: false,
                recordingStartAmbiguous: true,
                ambiguousStartToken: "Shared-G1",
                activeOwner: "dictation",
                activeToken: "Shared-G1",
                activeOwnerRevision: 18,
                meetingOwnerRevision: 17
            ),
            .rejectForeignAmbiguous
        )
    }

    func test_verified_pending_promotion_accepts_only_empty_local_route() {
        XCTAssertEqual(
            MeetingPromotionRoutingPolicy.decide(
                promoted: true,
                meetingToken: "Shared-G1",
                recordingStartInFlight: false,
                activeOwner: nil,
                activeToken: nil,
                activeOwnerRevision: nil,
                pendingPromotionToken: "Shared-G1",
                isRecording: false
            ),
            .acceptPendingMeeting(token: "Shared-G1")
        )
        XCTAssertEqual(
            MeetingPromotionRoutingPolicy.decide(
                promoted: true,
                meetingToken: "Shared-G1",
                recordingStartInFlight: true,
                activeOwner: nil,
                activeToken: nil,
                activeOwnerRevision: nil,
                pendingPromotionToken: "Shared-G1",
                isRecording: false
            ),
            .deferUntilStart(token: "Shared-G1")
        )
        XCTAssertEqual(
            MeetingPromotionRoutingPolicy.decide(
                promoted: true,
                meetingToken: "Shared-G1",
                recordingStartInFlight: false,
                activeOwner: nil,
                activeToken: nil,
                activeOwnerRevision: nil,
                pendingPromotionToken: "Foreign-G2",
                isRecording: false
            ),
            .ignore
        )
        XCTAssertEqual(
            MeetingPromotionRoutingPolicy.decide(
                promoted: true,
                meetingToken: "Shared-G1",
                recordingStartInFlight: false,
                activeOwner: nil,
                activeToken: "Foreign-G2",
                activeOwnerRevision: nil,
                pendingPromotionToken: "Shared-G1",
                isRecording: false
            ),
            .ignore
        )
        XCTAssertEqual(
            MeetingPromotionRoutingPolicy.decide(
                promoted: true,
                meetingToken: "Shared-G1",
                recordingStartInFlight: false,
                activeOwner: nil,
                activeToken: nil,
                activeOwnerRevision: nil,
                pendingPromotionToken: "Shared-G1",
                isRecording: true
            ),
            .ignore
        )
    }

    func test_pending_meeting_promote_needs_exact_late_start_token() {
        XCTAssertEqual(
            PendingMeetingPromotionConsumptionPolicy.decide(
                pendingToken: "Shared-G1",
                startToken: "Shared-G1"
            ),
            .completeHandoff(token: "Shared-G1")
        )
        XCTAssertEqual(
            PendingMeetingPromotionConsumptionPolicy.decide(
                pendingToken: "Shared-G1",
                startToken: nil
            ),
            .preservePendingAndRejectResponse
        )
        XCTAssertEqual(
            PendingMeetingPromotionConsumptionPolicy.decide(
                pendingToken: "Shared-G1",
                startToken: "Foreign-G2"
            ),
            .preservePendingAndRejectResponse
        )
    }

    func test_handoff_cleanup_cas_never_clears_other_generation() {
        XCTAssertEqual(
            MeetingPromotionHandoffCleanupPolicy.decide(
                expectedToken: "Shared-G1",
                activeOwner: "dictation",
                activeToken: "Shared-G1",
                activeOwnerRevision: 17,
                expectedOwnerRevision: 17,
                isRecording: true
            ),
            .clearCurrentGeneration
        )
        XCTAssertEqual(
            MeetingPromotionHandoffCleanupPolicy.decide(
                expectedToken: "Shared-G1",
                activeOwner: "dictation",
                activeToken: "Foreign-G2",
                activeOwnerRevision: 18,
                expectedOwnerRevision: 17,
                isRecording: true
            ),
            .reject
        )
        XCTAssertEqual(
            MeetingPromotionHandoffCleanupPolicy.decide(
                expectedToken: "Shared-G1",
                activeOwner: "dictation",
                activeToken: "Shared-G1",
                activeOwnerRevision: 18,
                expectedOwnerRevision: 17,
                isRecording: true
            ),
            .reject
        )
        XCTAssertEqual(
            MeetingPromotionHandoffCleanupPolicy.decide(
                expectedToken: "Shared-G1",
                activeOwner: nil,
                activeToken: nil,
                activeOwnerRevision: nil,
                expectedOwnerRevision: nil,
                isRecording: false
            ),
            .preserveNoLocalGeneration
        )
    }

    func test_meeting_panel_validates_promote_before_accepting_token() throws {
        let src = try source("main+MeetingPanel.swift")
        guard
            let validation = src.range(of: "let mayAcceptGeneration"),
            let accept = src.range(of: "controller.acceptGenerationToken")
        else {
            XCTFail("Не найдена проверка ограждения токена для панели встречи")
            return
        }
        XCTAssertLessThan(
            src.distance(from: src.startIndex, to: validation.lowerBound),
            src.distance(from: src.startIndex, to: accept.lowerBound)
        )
        XCTAssertTrue(src.contains("if mayAcceptGeneration"))
    }

    func test_meeting_start_blocks_pending_quick_capture() throws {
        let src = try source("main+MeetingPanel.swift")
        XCTAssertTrue(src.contains("quickCaptureStartRequestID != nil"))
        XCTAssertTrue(src.contains("quickCaptureStartAmbiguousRequestID != nil"))
    }

    func test_recovery_toggle_routes_to_stop_before_backend_state_sync() throws {
        let src = try source("main+HotkeyRecording.swift")
        guard
            let recovery = src.range(of: "if recordingStopRecoveryPending"),
            let sync = src.range(of: "syncRecordingStateWithBackend()")
        else {
            XCTFail("Не найдена recovery-проводка hotkey")
            return
        }
        XCTAssertLessThan(
            src.distance(from: src.startIndex, to: recovery.lowerBound),
            src.distance(from: src.startIndex, to: sync.lowerBound)
        )
    }

    func test_quick_capture_normal_and_orphan_stops_keep_start_token() throws {
        let src = try source("main+QuickCapture.swift")
        XCTAssertTrue(src.contains("let rawGenerationToken"))
        XCTAssertTrue(src.contains("stopOrphanQuickCapture"))
        XCTAssertTrue(src.contains("generationToken: generationToken"))
        XCTAssertTrue(src.contains("RecordingStopCoordinator.execute"))
        XCTAssertTrue(src.contains(#"params["generation_token"]"#))
        XCTAssertFalse(src.contains("Thread.sleep"))
    }

    func test_orphan_quick_recovery_cannot_capture_pending_new_start() {
        XCTAssertFalse(
            QuickCaptureOrphanRecoveryPolicy.canAdopt(
                activeToken: nil,
                startRequestPending: true,
                quickCaptureActive: true
            )
        )
        XCTAssertFalse(
            QuickCaptureOrphanRecoveryPolicy.canAdopt(
                activeToken: "Quick-G2",
                startRequestPending: false,
                quickCaptureActive: true
            )
        )
        XCTAssertTrue(
            QuickCaptureOrphanRecoveryPolicy.canAdopt(
                activeToken: nil,
                startRequestPending: false,
                quickCaptureActive: false
            )
        )
    }

    func test_meeting_stop_uses_wrapper_async_and_generation_token() throws {
        let src = try source("MeetingLivePanelController.swift")
        XCTAssertTrue(src.contains(#"method: "meeting_stop""#))
        XCTAssertTrue(src.contains(#"params["generation_token"]"#))
        XCTAssertTrue(src.contains("RecordingStopCoordinator.execute"))
        XCTAssertTrue(src.contains("client.callAsync"))
        XCTAssertFalse(
            src.contains(#"client.call(method: "meeting_stop""#),
            "Meeting stop не должен выполнять sync IPC"
        )
        XCTAssertFalse(src.contains("Thread.sleep"))
    }

    func test_meeting_start_hands_promoted_generation_and_ducking_to_panel() throws {
        let src = try source("main+MeetingPanel.swift")
        XCTAssertTrue(src.contains("controller.acceptGenerationToken"))
        XCTAssertTrue(src.contains("adoptPromotedMeetingGeneration"))
        XCTAssertTrue(src.contains("meetingInheritedDictationDucking = true"))
        XCTAssertTrue(src.contains("releasePromotedMeetingDucking"))
        XCTAssertTrue(src.contains("controller.hasUnresolvedMeetingStop"))

        guard let finished = src.range(of: "c.onFinished =") else {
            XCTFail("Не найден one-shot callback встречи")
            return
        }
        let finishedBody = src[finished.lowerBound...]
        XCTAssertTrue(finishedBody.contains("releasePromotedMeetingDucking()"))
    }

    // MARK: - R2 Task 7: неоднозначный результат старта

    func test_ambiguous_dictation_start_adopts_only_its_own_generation() {
        let decision = RecordingStartAmbiguityPolicy.decide(
            snapshot: RecordingStateSnapshot(
                isRecording: true,
                owner: "dictation",
                ownerFieldPresent: true,
                generationToken: "Dictation-G1-Opaque",
                stateVerified: true
            ),
            expectedOwner: "dictation",
            allowsMeetingPromotion: true
        )

        XCTAssertEqual(
            decision,
            .adoptExpectedOwner(generationToken: "Dictation-G1-Opaque")
        )
    }

    func test_ambiguous_dictation_start_waits_only_for_tokened_meeting_promotion() {
        let decision = RecordingStartAmbiguityPolicy.decide(
            snapshot: RecordingStateSnapshot(
                isRecording: true,
                owner: "meeting",
                ownerFieldPresent: true,
                generationToken: "Shared-G1-Opaque",
                stateVerified: true
            ),
            expectedOwner: "dictation",
            allowsMeetingPromotion: true
        )

        XCTAssertEqual(
            decision,
            .awaitPromotedMeeting(generationToken: "Shared-G1-Opaque")
        )
    }

    func test_ambiguous_quick_start_never_adopts_foreign_generation() {
        let decision = RecordingStartAmbiguityPolicy.decide(
            snapshot: RecordingStateSnapshot(
                isRecording: true,
                owner: "meeting",
                ownerFieldPresent: true,
                generationToken: "Foreign-G2-Opaque",
                stateVerified: true
            ),
            expectedOwner: "quick_capture",
            allowsMeetingPromotion: false
        )

        XCTAssertEqual(decision, .rejectAsIdleOrForeign)
    }

    func test_ambiguous_start_keeps_state_when_snapshot_is_unavailable() {
        let decision = RecordingStartAmbiguityPolicy.decide(
            snapshot: RecordingStateSnapshot(
                isRecording: false,
                owner: nil,
                ownerFieldPresent: false,
                generationToken: nil,
                stateVerified: false
            ),
            expectedOwner: "dictation",
            allowsMeetingPromotion: true
        )

        XCTAssertEqual(decision, .retryReconciliation)
    }

    func test_ambiguous_meeting_promotion_needs_exact_observed_token() {
        XCTAssertEqual(
            MeetingPromotionRoutingPolicy.decide(
                promoted: true,
                meetingToken: "Shared-G1",
                recordingStartInFlight: false,
                recordingStartAmbiguous: true,
                ambiguousStartToken: nil,
                activeOwner: nil,
                activeToken: nil
            ),
            .deferUntilStart(token: "Shared-G1")
        )
        XCTAssertEqual(
            MeetingPromotionRoutingPolicy.decide(
                promoted: true,
                meetingToken: "Shared-G1",
                recordingStartInFlight: false,
                recordingStartAmbiguous: true,
                ambiguousStartToken: "Shared-G1",
                activeOwner: nil,
                activeToken: nil
            ),
            .handoffNow
        )
        XCTAssertEqual(
            MeetingPromotionRoutingPolicy.decide(
                promoted: true,
                meetingToken: "Foreign-G2",
                recordingStartInFlight: false,
                recordingStartAmbiguous: true,
                ambiguousStartToken: "Shared-G1",
                activeOwner: nil,
                activeToken: nil
            ),
            .rejectForeignAmbiguous
        )
    }

    func test_ambiguous_start_paths_are_explicitly_reconciled_before_ui_cleanup() throws {
        let hotkey = try source("main+HotkeyRecording.swift")
        let quickCapture = try source("main+QuickCapture.swift")
        let main = try source("main.swift")

        XCTAssertTrue(main.contains("var recordingStartAmbiguous = false"))
        XCTAssertTrue(main.contains("var quickCaptureStartAmbiguousRequestID: UUID?"))
        XCTAssertTrue(hotkey.contains("await reconcileAmbiguousDictationStart"))
        XCTAssertTrue(quickCapture.contains("await reconcileAmbiguousQuickCaptureStart"))
        XCTAssertTrue(
            quickCapture.contains("quickCaptureStopRequestedDuringAmbiguousStart"),
            "Повторный тап в неоднозначном старте обязан стать stop-intent, а не tokenless stop."
        )
    }

    func test_ambiguous_start_requires_exact_request_id_and_owner_revision() {
        let ownSnapshot = RecordingStateSnapshot(
            isRecording: true,
            owner: "dictation",
            ownerFieldPresent: true,
            generationToken: "G1",
            ownerRevision: 7,
            startRequestID: "request-A",
            stateVerified: true
        )

        XCTAssertEqual(
            RecordingStartAmbiguityPolicy.decide(
                snapshot: ownSnapshot,
                expectedOwner: "dictation",
                expectedStartRequestID: "request-A",
                allowsMeetingPromotion: true
            ),
            .adoptExpectedOwner(generationToken: "G1")
        )
        XCTAssertEqual(
            RecordingStartAmbiguityPolicy.decide(
                snapshot: ownSnapshot,
                expectedOwner: "dictation",
                expectedStartRequestID: "request-B",
                allowsMeetingPromotion: true
            ),
            .rejectAsIdleOrForeign
        )
        XCTAssertEqual(
            RecordingStartAmbiguityPolicy.decide(
                snapshot: RecordingStateSnapshot(
                    isRecording: true,
                    owner: "dictation",
                    ownerFieldPresent: true,
                    generationToken: "G1",
                    ownerRevision: nil,
                    startRequestID: "request-A",
                    stateVerified: true
                ),
                expectedOwner: "dictation",
                expectedStartRequestID: "request-A",
                allowsMeetingPromotion: true
            ),
            .rejectAsIdleOrForeign
        )
    }

    func test_pending_quick_cancel_does_not_send_tokenless_stop() throws {
        let source = try source("main+QuickCapture.swift")
        guard
            let begin = source.range(of: "func stopQuickCapture()"),
            let activeRoute = source.range(
                of: "guard activeGenerationOwner == \"quick_capture\""
            )
        else {
            XCTFail("Не найдена ветка отмены pending Quick Capture")
            return
        }
        let pendingBranch = source[begin.lowerBound..<activeRoute.lowerBound]
        XCTAssertTrue(
            pendingBranch.contains("quickCaptureStopRequestedStartID = requestID")
        )
        XCTAssertFalse(pendingBranch.contains("performQuickCaptureStop"))
        XCTAssertFalse(pendingBranch.contains("quickCaptureStopRequest("))
        XCTAssertFalse(pendingBranch.contains("ipcClient.call"))
        XCTAssertTrue(source.contains("ownerRevision: ownerRevision"))
        XCTAssertTrue(source.contains(#"params["expected_owner_revision"]"#))
    }
}
