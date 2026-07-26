/*
 HotkeyOwnerGuardTests — source-contract регрессия живого бага F1 волны R2.

 Проверяет политику и реальную проводку хоткея: чужой backend-owner блокируется
 перед любой stop-веткой, promote/quick capture не маскируются под диктовку,
 а конкурентные старты не могут преждевременно снять audio ducking.
*/

import XCTest
@testable import KrabEarAgent

final class HotkeyOwnerGuardTests: XCTestCase {
    private func source(_ name: String) throws -> String {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // KrabEarAgentTests
            .deletingLastPathComponent()   // Tests
            .deletingLastPathComponent()   // KrabEarAgent
            .appendingPathComponent("Sources/KrabEarAgent/\(name)")
        return try String(contentsOf: url, encoding: .utf8)
    }

    func test_owner_policy_distinguishes_foreign_and_legacy_recordings() {
        XCTAssertTrue(
            HotkeyRecordingOwnershipPolicy.isForeignRecording(
                isRecording: true,
                owner: "meeting",
                ownerFieldPresent: true
            )
        )
        XCTAssertTrue(
            HotkeyRecordingOwnershipPolicy.isForeignRecording(
                isRecording: true,
                owner: "quick_capture",
                ownerFieldPresent: true
            )
        )
        XCTAssertFalse(
            HotkeyRecordingOwnershipPolicy.isForeignRecording(
                isRecording: true,
                owner: "dictation",
                ownerFieldPresent: true
            )
        )
        XCTAssertFalse(
            HotkeyRecordingOwnershipPolicy.isForeignRecording(
                isRecording: true,
                owner: nil,
                ownerFieldPresent: false
            ),
            "Старый backend без owner сохраняет legacy auto-heal"
        )
        XCTAssertTrue(
            HotkeyRecordingOwnershipPolicy.isForeignRecording(
                isRecording: true,
                owner: nil,
                ownerFieldPresent: true
            ),
            "owner:null нового backend означает неуправляемую запись, а не диктовку"
        )
    }

    func test_owner_policy_never_mirrors_foreign_recording_as_dictation() {
        XCTAssertTrue(
            HotkeyRecordingOwnershipPolicy.representsLocalDictation(
                isRecording: true,
                owner: "dictation",
                ownerFieldPresent: true
            )
        )
        XCTAssertTrue(
            HotkeyRecordingOwnershipPolicy.representsLocalDictation(
                isRecording: true,
                owner: nil,
                ownerFieldPresent: false
            ),
            "Старый backend без owner должен остаться совместимым"
        )
        XCTAssertFalse(
            HotkeyRecordingOwnershipPolicy.representsLocalDictation(
                isRecording: true,
                owner: nil,
                ownerFieldPresent: true
            ),
            "Новый backend с owner:null не должен маскировать unmanaged-запись"
        )
        XCTAssertFalse(
            HotkeyRecordingOwnershipPolicy.representsLocalDictation(
                isRecording: true,
                owner: "meeting",
                ownerFieldPresent: true
            )
        )
        XCTAssertFalse(
            HotkeyRecordingOwnershipPolicy.representsLocalDictation(
                isRecording: false,
                owner: "dictation",
                ownerFieldPresent: true
            )
        )
    }

    func test_foreign_owner_guard_precedes_every_local_stop_branch() throws {
        let source = try source("main+HotkeyRecording.swift")
        guard
            let ownerGuard = source.range(
                of: "HotkeyRecordingOwnershipPolicy.isForeignRecording"
            ),
            let localStop = source.range(of: "if wasRecordingLocally {")
        else {
            return XCTFail("Не найдена живая owner-проводка хоткея")
        }
        XCTAssertLessThan(
            ownerGuard.lowerBound,
            localStop.lowerBound,
            "Owner-гейт обязан выполняться до любой ветки локального stop"
        )
    }

    func test_unverified_state_refusal_precedes_owner_and_stop_branches() throws {
        let source = try source("main+HotkeyRecording.swift")
        guard
            let verificationGuard = source.range(of: "if !stateVerified {"),
            let ownerGuard = source.range(
                of: "HotkeyRecordingOwnershipPolicy.isForeignRecording",
                range: verificationGuard.upperBound..<source.endIndex
            ),
            let localStop = source.range(
                of: "if wasRecordingLocally {",
                range: ownerGuard.upperBound..<source.endIndex
            )
        else {
            return XCTFail("Не найдена fail-safe проводка непроверенного state")
        }
        XCTAssertLessThan(verificationGuard.lowerBound, ownerGuard.lowerBound)
        XCTAssertLessThan(verificationGuard.lowerBound, localStop.lowerBound)
    }

    func test_already_recording_is_not_treated_as_success() throws {
        let source = try source("main+HotkeyRecording.swift")
        guard
            let start = source.range(of: #"if status == "already_recording" {"#),
            let end = source.range(
                of: #"if status != "recording" {"#,
                range: start.upperBound..<source.endIndex
            )
        else {
            return XCTFail("Не найден живой блок already_recording")
        }
        let block = source[start.lowerBound..<end.lowerBound]
        XCTAssertFalse(
            block.contains("isRecording = true"),
            "already_recording не должен захватывать локальное состояние"
        )
        XCTAssertFalse(
            block.contains("startRealtimeOverlayPolling()"),
            "already_recording не должен запускать polling как при своём старте"
        )
        XCTAssertTrue(
            block.contains("audioDuckingService.restoreAfterRecording()"),
            "Отказ already_recording обязан откатить включённый до IPC audio ducking"
        )
    }

    func test_every_failed_start_restores_audio_ducking() throws {
        let source = try source("main+HotkeyRecording.swift")
        guard
            let catchStart = source.range(of: "} catch {"),
            let stopStart = source.range(
                of: "func stopRecording()",
                range: catchStart.upperBound..<source.endIndex
            )
        else {
            return XCTFail("Не найден catch блока startRecording")
        }
        let catchBlock = source[catchStart.lowerBound..<stopStart.lowerBound]
        XCTAssertTrue(
            catchBlock.contains("audioDuckingService.restoreAfterRecording()"),
            "Транспортный или backend-отказ старта не должен оставлять Mac приглушённым"
        )
    }

    func test_recording_start_gate_serializes_competing_starts() {
        let gate = RecordingStartGate()
        XCTAssertTrue(gate.tryAcquire())
        XCTAssertFalse(
            gate.tryAcquire(),
            "Второй start не должен снять ducking первой живой попытки"
        )
        gate.release()
        XCTAssertTrue(gate.tryAcquire())
        gate.release()
    }

    func test_startRecording_uses_start_gate() throws {
        let source = try source("main+HotkeyRecording.swift")
        XCTAssertTrue(source.contains("recordingStartGate.tryAcquire()"))
        XCTAssertTrue(source.contains("recordingStartGate.release()"))
    }

    func test_state_sync_distinguishes_absent_owner_from_null_owner() throws {
        let source = try source("main+HotkeyRecording.swift")
        XCTAssertTrue(
            source.contains(#"state.keys.contains("owner")"#),
            "Совместимость должна различать старый ответ без owner и новый owner:null"
        )
    }

    func test_hold_stop_uses_same_verified_owner_path_as_toggle() throws {
        let source = try source("main.swift")
        guard
            let holdStop = source.range(of: "manager.onHoldStop ="),
            let nextCallback = source.range(
                of: "manager.onConversationDoubleTap",
                range: holdStop.upperBound..<source.endIndex
            )
        else {
            return XCTFail("Не найдена проводка hold-stop")
        }
        let block = source[holdStop.lowerBound..<nextCallback.lowerBound]
        XCTAssertTrue(
            block.contains("performRecordToggle"),
            "Hold-stop обязан использовать тот же owner-verified путь"
        )
        XCTAssertFalse(
            block.contains("self.stopRecording()"),
            "Прямой hold-stop обходит owner-гейт и может остановить meeting"
        )
    }

    func test_quickCapture_identifies_owner_on_start() throws {
        let source = try source("main+QuickCapture.swift")
        guard let startCall = source.range(of: #"method: "start_recording""#) else {
            return XCTFail("Не найден живой start Quick Capture")
        }
        let suffix = source[startCall.lowerBound...]
        XCTAssertTrue(
            suffix.prefix(220).contains(#""source": "quick_capture""#),
            "После рестарта агента backend обязан отличать заметку от диктовки"
        )
    }
}
