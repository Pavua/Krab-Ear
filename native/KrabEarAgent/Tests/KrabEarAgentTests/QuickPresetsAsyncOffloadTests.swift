/*
 QuickPresetsAsyncOffloadTests.swift
 Wave 188 — unit tests for main+QuickPresets.swift async IPC offload (AGENT-3 fix).

 Tests cover:
 1. recordingPresets catalog is well-formed (4 entries, expected IDs)
 2. cycleToNextPreset cycles through all presets and wraps around
 3. activePresetBadge returns correct badge for stored preset
 4. applyRecordingPreset dispatches apply_profile_preset IPC asynchronously
    (the IPC call must reach the mock provider, not block main thread)
*/

import XCTest
@testable import KrabEarAgent

// MARK: - QuickPresetsCatalogTests

@MainActor
final class QuickPresetsCatalogTests: XCTestCase {

    func test_presets_count() {
        XCTAssertEqual(AgentAppDelegate.recordingPresets.count, 4)
    }

    func test_presets_expectedIds() {
        let ids = AgentAppDelegate.recordingPresets.map { $0.id }
        XCTAssertEqual(ids, ["default", "meeting", "translation", "call_recording"])
    }

    func test_presets_badges_notEmpty() {
        for preset in AgentAppDelegate.recordingPresets {
            XCTAssertFalse(preset.badge.isEmpty, "Badge for '\(preset.id)' must not be empty")
        }
    }
}

// MARK: - QuickPresetsCycleTests

@MainActor
final class QuickPresetsCycleTests: XCTestCase {

    private let key = "KrabEar_ActivePreset"
    private let defaultsDomain = IsolatedUserDefaultsDomain(scope: "QuickPresetsCycleTests")

    override func setUp() async throws {
        try await super.setUp()
        defaultsDomain.defaults.removeObject(forKey: key)
    }

    override func tearDown() async throws {
        defaultsDomain.removePersistentDomain()
        try await super.tearDown()
    }

    private func makeDelegate() -> AgentAppDelegate {
        final class NoOpProvider: IPCSocketProviding, @unchecked Sendable {
            func send(payload: Data, timeoutSec: Int) async throws -> Data {
                return Data(#"{"id":"x","ok":true,"result":{}}"#.utf8)
            }
        }
        let delegate = AgentAppDelegate(
            options: LaunchOptions(arguments: [CommandLine.arguments[0]]),
            userDefaults: defaultsDomain.defaults
        )
        delegate.ipcClient = IPCClient(socketProvider: NoOpProvider())
        return delegate
    }

    func test_cycleToNextPreset_fromDefault_goesToMeeting() async throws {
        let delegate = makeDelegate()
        defaultsDomain.defaults.set("default", forKey: key)

        delegate.cycleToNextPreset()
        // Allow Task.detached to settle
        try await Task.sleep(for: .milliseconds(300))

        XCTAssertEqual(
            defaultsDomain.defaults.string(forKey: key), "meeting",
            "Cycling from 'default' should land on 'meeting'"
        )
    }

    func test_cycleToNextPreset_wrapsAroundFromLast() async throws {
        let delegate = makeDelegate()
        let lastId = AgentAppDelegate.recordingPresets.last!.id
        defaultsDomain.defaults.set(lastId, forKey: key)

        delegate.cycleToNextPreset()
        try await Task.sleep(for: .milliseconds(300))

        XCTAssertEqual(
            defaultsDomain.defaults.string(forKey: key), AgentAppDelegate.recordingPresets.first!.id,
            "Cycling past the last preset should wrap back to the first"
        )
    }

    func test_activePresetBadge_defaultWhenNoneStored() {
        defaultsDomain.defaults.removeObject(forKey: key)
        let delegate = makeDelegate()
        XCTAssertEqual(delegate.activePresetBadge(), "D",
            "Badge should be 'D' (default) when no preset is stored in UserDefaults")
    }

    func test_activePresetBadge_reflectsStoredPreset() async throws {
        let delegate = makeDelegate()
        defaultsDomain.defaults.set("meeting", forKey: key)
        // Meeting badge
        let badge = delegate.activePresetBadge()
        let expected = AgentAppDelegate.recordingPresets.first { $0.id == "meeting" }?.badge ?? ""
        XCTAssertEqual(badge, expected)
    }
}

// MARK: - QuickPresetsIPCOffloadTests

@MainActor
final class QuickPresetsIPCOffloadTests: XCTestCase {

    private let key = "KrabEar_ActivePreset"
    private let defaultsDomain = IsolatedUserDefaultsDomain(scope: "QuickPresetsIPCOffloadTests")

    override func tearDown() async throws {
        defaultsDomain.removePersistentDomain()
        try await super.tearDown()
    }

    func test_applyRecordingPreset_sendsApplyProfilePresetIPC() async throws {
        actor ProfileRecorder {
            var profiles: [String] = []
            func append(_ p: String) { profiles.append(p) }
        }
        let recorder = ProfileRecorder()

        final class PresetProvider: IPCSocketProviding, @unchecked Sendable {
            let recorder: ProfileRecorder
            init(recorder: ProfileRecorder) { self.recorder = recorder }

            func send(payload: Data, timeoutSec: Int) async throws -> Data {
                if
                    let dict = try? JSONSerialization.jsonObject(with: payload) as? [String: Any],
                    let method = dict["method"] as? String,
                    method == "apply_profile_preset",
                    let params = dict["params"] as? [String: Any],
                    let profile = params["profile"] as? String
                {
                    await recorder.append(profile)
                }
                return Data(#"{"id":"x","ok":true,"result":{}}"#.utf8)
            }
        }

        let delegate = AgentAppDelegate(
            options: LaunchOptions(arguments: [CommandLine.arguments[0]]),
            userDefaults: defaultsDomain.defaults
        )
        delegate.ipcClient = IPCClient(socketProvider: PresetProvider(recorder: recorder))

        delegate.applyRecordingPreset("meeting", source: "test")

        // Allow the Task.detached IPC call to complete
        try await Task.sleep(for: .milliseconds(300))

        let profiles = await recorder.profiles
        XCTAssertEqual(profiles, ["meeting"],
            "applyRecordingPreset should dispatch apply_profile_preset IPC with the preset id")
    }

    func test_applyRecordingPreset_updatesUserDefaults_afterIPCSuccess() async throws {
        final class SuccessProvider: IPCSocketProviding, @unchecked Sendable {
            func send(payload: Data, timeoutSec: Int) async throws -> Data {
                return Data(#"{"id":"x","ok":true,"result":{}}"#.utf8)
            }
        }

        let delegate = AgentAppDelegate(
            options: LaunchOptions(arguments: [CommandLine.arguments[0]]),
            userDefaults: defaultsDomain.defaults
        )
        delegate.ipcClient = IPCClient(socketProvider: SuccessProvider())

        delegate.applyRecordingPreset("translation", source: "test")
        try await Task.sleep(for: .milliseconds(300))

        XCTAssertEqual(defaultsDomain.defaults.string(forKey: key), "translation",
            "UserDefaults should be updated to 'translation' after successful IPC")
    }
}
