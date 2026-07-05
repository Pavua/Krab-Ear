/*
 main+SparkleUpdater.swift — автообновления через Sparkle 2 (IPC не участвует).

 Spec: docs/superpowers/specs/2026-07-05-sparkle-auto-update-design.md.

 🔴 Dev-guard (критично): прод-приложение владельца лежит ВНУТРИ git-репо
 (launchd указывает на <repo>/Krab Ear.app) — Sparkle-обновление in-place
 переписало бы рабочее дерево git и сломало parity-конвенцию бинарей.
 Поэтому updater инициализируется ТОЛЬКО когда .app установлен ВНЕ каталога
 проекта (эвристика та же, что resolveProjectRoot: рядом с бандлом нет
 KrabEar/backend/service.py). Для получателей DMG в /Applications — работает.
 На dev-машине путь обновления остаётся build_and_deploy.command.
*/

import AppKit
import Foundation
import ObjectiveC.runtime
import Sparkle

private nonisolated(unsafe) var sparkleControllerKey: UInt8 = 0

@MainActor
extension AgentAppDelegate {

    var sparkleUpdaterController: SPUStandardUpdaterController? {
        get { objc_getAssociatedObject(self, &sparkleControllerKey) as? SPUStandardUpdaterController }
        set {
            objc_setAssociatedObject(
                self, &sparkleControllerKey, newValue, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        }
    }

    /// true когда бандл — установленная копия (не dev внутри репо, не голый бинарь).
    var isSparkleEligibleInstall: Bool {
        let bundlePath = Bundle.main.bundlePath
        guard bundlePath.hasSuffix(".app") else { return false }  // голый dev-бинарь
        let repoMarker = (bundlePath as NSString).deletingLastPathComponent
            + "/KrabEar/backend/service.py"
        return !FileManager.default.fileExists(atPath: repoMarker)
    }

    /// Вызывается из completeStartupAfterBackendReady().
    func setupSparkleUpdater() {
        guard isSparkleEligibleInstall else {
            logger.info("Sparkle: пропущен (dev-запуск: бандл в каталоге проекта или голый бинарь)")
            return
        }
        let controller = SPUStandardUpdaterController(
            startingUpdater: true,
            updaterDelegate: nil,
            userDriverDelegate: nil
        )
        self.sparkleUpdaterController = controller
        logger.info("Sparkle updater запущен (SUFeedURL из Info.plist)")
    }

    @objc func onCheckForUpdates() {
        sparkleUpdaterController?.checkForUpdates(nil)
    }
}
