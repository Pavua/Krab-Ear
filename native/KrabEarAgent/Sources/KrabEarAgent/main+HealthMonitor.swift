/*
 main+HealthMonitor.swift
 AgentAppDelegate extension: Phase A auto-heal wiring.

 Связывает:
 - HealthMonitor actor (continuous ping каждые 3s)
 - BackendSupervisor.restartIfDead (max consecutive restarts guard)
 - BackendToast (UI feedback на restart)
 - StatusIndicatorImage (menu bar dot)

 Phase B.1:
 - setupHealthMonitor вызывает subscribeToProbeEvents после старта.
 - applyHealthStateToStatusItem обновляет StatusIndicatorView и menu bar dot.
*/

import AppKit
import Foundation
import ObjectiveC.runtime

// MARK: - Associated objects (Swift не позволяет stored properties в extensions)
// Уникальные ключи — pointer identity. Не shared mutable state.

private nonisolated(unsafe) var healthMonitorKey: UInt8 = 0
private nonisolated(unsafe) var statusUpdateTimerKey: UInt8 = 0
private nonisolated(unsafe) var statusIndicatorViewKey: UInt8 = 0
private nonisolated(unsafe) var privacyModeEnabledKey: UInt8 = 0
private nonisolated(unsafe) var lastHealthStateKey: UInt8 = 0

@MainActor
extension AgentAppDelegate {
    var healthMonitor: HealthMonitor? {
        get { objc_getAssociatedObject(self, &healthMonitorKey) as? HealthMonitor }
        set { objc_setAssociatedObject(self, &healthMonitorKey, newValue, .OBJC_ASSOCIATION_RETAIN) }
    }

    var privacyModeEnabled: Bool {
        get { objc_getAssociatedObject(self, &privacyModeEnabledKey) as? Bool ?? false }
        set { objc_setAssociatedObject(self, &privacyModeEnabledKey, newValue, .OBJC_ASSOCIATION_RETAIN) }
    }

    var lastHealthState: HealthState {
        get { objc_getAssociatedObject(self, &lastHealthStateKey) as? HealthState ?? .stopped }
        set { objc_setAssociatedObject(self, &lastHealthStateKey, newValue, .OBJC_ASSOCIATION_RETAIN) }
    }

    func setPrivacyMode(_ on: Bool) {
        self.privacyModeEnabled = on
        self.statusIndicatorView.setPrivacyMode(on)
        self.applyHealthStateToStatusItem(self.lastHealthState)
        // Wake word не должен держать микрофон в privacy mode. Backend тоже
        // откажет в wake_word_start (гейт живой после проводки settings_get) —
        // двойная защита, агентская сторона срабатывает первой.
        if on {
            wakeWordPoller?.pause(.privacyMode)
        } else {
            wakeWordPoller?.resume(.privacyMode)
        }
    }

    var statusUpdateTimer: Timer? {
        get { objc_getAssociatedObject(self, &statusUpdateTimerKey) as? Timer }
        set { objc_setAssociatedObject(self, &statusUpdateTimerKey, newValue, .OBJC_ASSOCIATION_RETAIN) }
    }

    /// Phase B.1: StatusIndicatorView для menu bar dot + flashGreen support.
    /// Создаётся один раз в setupHealthMonitor.
    var statusIndicatorView: StatusIndicatorView {
        get {
            if let existing = objc_getAssociatedObject(self, &statusIndicatorViewKey) as? StatusIndicatorView {
                return existing
            }
            let view = StatusIndicatorView(frame: NSRect(x: 0, y: 0, width: 12, height: 12))
            objc_setAssociatedObject(self, &statusIndicatorViewKey, view, .OBJC_ASSOCIATION_RETAIN)
            return view
        }
    }

    /// Запускает HealthMonitor + status update timer.
    /// Вызывается из completeStartupAfterBackendReady() после успешного backend ping.
    func setupHealthMonitor() {
        let monitor = HealthMonitor(pingInterval: 3.0, hangThreshold: 2)
        let socketPath = backendSupervisor.socketPath
        let supervisor = backendSupervisor

        monitor.setPingProvider {
            let client = IPCClient(socketPath: socketPath)
            return ((try? await client.callAsync(method: "ping", timeoutSec: 2)) != nil)
        }

        // Локальный nonisolated reference для Sendable closure compliance.
        let loggerRef = AgentLogger.shared
        Task { @MainActor in
            await monitor.setOnHangDetected {
                await MainActor.run {
                    loggerRef.warn("HealthMonitor: backend hang detected → restartIfDead")
                    // Различаем ложную тревогу от настоящего события (2026-08-03):
                    // тугой таймаут пинга HealthMonitor (2с) под нагрузкой иногда не
                    // укладывается, хотя backend отвечает за доли секунды. Тост
                    // «Backend перезапущен» на процессе, который никто не трогал,
                    // вводит в заблуждение — restartIfDeadDetailed() отличает случаи.
                    switch supervisor.restartIfDeadDetailed() {
                    case .alreadyAlive:
                        loggerRef.info("HealthMonitor: backend был жив — ложная тревога (таймаут пинга)")
                    case .recovered:
                        loggerRef.info("HealthMonitor: backend successfully restarted")
                        BackendToast.shared.show("Backend перезапущен", duration: 3.0)
                    case .failed:
                        loggerRef.warn("HealthMonitor: restart failed — лимит перезапусков достигнут")
                        BackendToast.shared.show("⚠ Backend не запускается — открой логи", duration: 10.0)
                    }
                }
            }
            await monitor.start()
            // Wave 656: log first health ping milestone.
            AgentRecoveryLogger.shared.logStage("first_health_ping")
        }

        self.healthMonitor = monitor

        // Phase B.1: подписываемся на rewriter_recovered SSE events → flashGreen.
        // Событие доставляется REST-процессу через EventBridge (2026-07-07,
        // backend/event_bridge.py) — подписка больше не мёртвый путь.
        let indicator = self.statusIndicatorView
        Task {
            await monitor.subscribeToProbeEvents(
                restBaseURL: "http://127.0.0.1:5005",
                statusIndicator: indicator
            )
        }

        // Periodic status indicator refresh (1s).
        let timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { _ in
            Task { @MainActor [weak self] in
                guard let self, let monitor = self.healthMonitor else { return }
                let state = await monitor.currentState()
                self.applyHealthStateToStatusItem(state)
            }
        }
        self.statusUpdateTimer = timer

        logger.info("HealthMonitor запущен: pingInterval=3s, hangThreshold=2")
    }

    /// Останавливает HealthMonitor + timer. Вызывается из applicationWillTerminate.
    func tearDownHealthMonitor() {
        statusUpdateTimer?.invalidate()
        statusUpdateTimer = nil

        if let monitor = healthMonitor {
            Task {
                await monitor.stop()
            }
        }
        healthMonitor = nil
    }

    /// Обновляет визуал menu bar status item на основе HealthState.
    func applyHealthStateToStatusItem(_ state: HealthState) {
        self.lastHealthState = state
        guard let button = statusItem?.button else { return }

        // Создаем image для menu-bar с учетом privacyModeEnabled
        let dotImage = StatusIndicatorImage.image(for: state, privacyMode: self.privacyModeEnabled, size: 14)
        button.image = dotImage
        button.imagePosition = .imageLeft  // dot слева от title

        refreshStatusItemTitle()
        button.title = button.title  // plain text — без ● prefix

        // Phase B.1: синхронизируем StatusIndicatorView
        statusIndicatorView.updateState(state)
        
        // Phase B.2: синхронизируем Privacy Mode
        statusIndicatorView.setPrivacyMode(self.privacyModeEnabled)
    }
}
