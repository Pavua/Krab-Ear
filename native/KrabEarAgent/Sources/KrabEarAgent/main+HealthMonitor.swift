/*
 main+HealthMonitor.swift
 AgentAppDelegate extension: Phase A auto-heal wiring.

 Связывает:
 - HealthMonitor actor (continuous ping каждые 3s)
 - BackendSupervisor.restartIfDead (exp backoff + circuit breaker)
 - BackendToast (UI feedback на restart)
 - StatusIndicatorImage (menu bar dot)
*/

import AppKit
import Foundation
import ObjectiveC.runtime

// MARK: - Associated objects (Swift не позволяет stored properties в extensions)
// Уникальные ключи — pointer identity. Не shared mutable state.

private nonisolated(unsafe) var healthMonitorKey: UInt8 = 0
private nonisolated(unsafe) var statusUpdateTimerKey: UInt8 = 0

@MainActor
extension AgentAppDelegate {
    var healthMonitor: HealthMonitor? {
        get { objc_getAssociatedObject(self, &healthMonitorKey) as? HealthMonitor }
        set { objc_setAssociatedObject(self, &healthMonitorKey, newValue, .OBJC_ASSOCIATION_RETAIN) }
    }

    var statusUpdateTimer: Timer? {
        get { objc_getAssociatedObject(self, &statusUpdateTimerKey) as? Timer }
        set { objc_setAssociatedObject(self, &statusUpdateTimerKey, newValue, .OBJC_ASSOCIATION_RETAIN) }
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
        // Logger обычный singleton; supervisor — class instance, sendable safe.
        let loggerRef = AgentLogger.shared
        Task { @MainActor in
            await monitor.setOnHangDetected {
                await MainActor.run {
                    loggerRef.warn("HealthMonitor: backend hang detected → restartIfDead")
                    let restarted = supervisor.restartIfDead()
                    if restarted {
                        loggerRef.info("HealthMonitor: backend successfully restarted")
                        BackendToast.shared.show("Backend перезапущен", duration: 3.0)
                    } else if supervisor.isCircuitOpen() {
                        loggerRef.error("HealthMonitor: circuit breaker open — стоп")
                        BackendToast.shared.show("⚠ Backend не запускается — открой логи", duration: 10.0)
                    } else {
                        loggerRef.warn("HealthMonitor: restart failed — следующая попытка через backoff")
                    }
                }
            }
            await monitor.start()
        }

        self.healthMonitor = monitor

        // Periodic status indicator refresh (1s).
        // weak self для предотвращения retain cycle через Timer.
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
    /// Сохраняет существующий title (KE [preset]) и добавляет цветной dot prefix.
    func applyHealthStateToStatusItem(_ state: HealthState) {
        guard let button = statusItem?.button else { return }

        let dotColor: NSColor
        switch state {
        case .healthy: dotColor = .systemGreen
        case .hung: dotColor = .systemYellow
        case .stopped: dotColor = .systemRed
        }

        // Восстанавливаем base title через refreshStatusItemTitle.
        refreshStatusItemTitle()
        let baseTitle = button.title

        let attributed = NSMutableAttributedString()
        attributed.append(NSAttributedString(
            string: "● ",
            attributes: [.foregroundColor: dotColor]
        ))
        attributed.append(NSAttributedString(string: baseTitle))
        button.attributedTitle = attributed
    }
}
