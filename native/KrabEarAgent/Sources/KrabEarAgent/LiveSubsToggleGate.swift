/*
 LiveSubsToggleGate — решение «тумблер live-субтитров должен ОСТАНОВИТЬ, а не
 запустить заново» (спека 2026-08-12-live-subs-backpressure-design.md §3, F4).

 До фикса toggleLiveSubsCaptureFromMenu() смотрел ТОЛЬКО на isCapturing:
 захват отваливался сам (бэкенд захлебнулся) → isCapturing == false → нажатие
 хоткея ЗАПУСКАЛО захват заново вместо того, чтобы убрать висящее окно —
 штатного способа закрыть оверлей не оставалось (живой инцидент владельца
 2026-08-12 00:41).

 Чистая struct без AgentAppDelegate — выделена по той же причине, что
 DictationStopAutoRetryGate/LiveSubsOverlayWatchdogGate: гарды тестируются
 юнитами без конструирования делегата (NSApp/lifecycle нетестируемы в чистом
 XCTest).
*/

enum LiveSubsToggleGate {
    /// Остановить нужно, если ИДЁТ захват ИЛИ окно ещё видно (захват мог
    /// умереть сам, а окно остаться висеть) — stopLiveSubsCapture() уже
    /// идемпотентен по обеим частям.
    static func shouldStop(isCapturing: Bool, isOverlayVisible: Bool) -> Bool {
        isCapturing || isOverlayVisible
    }
}
