/*
 IPCReconnectTelemetry.swift

 Отправка `report_reconnect` — сигнала «IPC-вызов пережил перезапуск backend'а».
 Backend превращает его в info-событие `ipc.reconnect` на ErrorBus
 (`_handle_report_reconnect` в service.py), давая ответ на вопрос «как часто прод
 реально теряет и восстанавливает соединение».

 До 2026-08-08 сигнал не отправлялся ни разу: он жил внутри метода
 IPCClient.callWithReconnect (без бэктиков — он удалён; Phase C C.2), у которого
 не было ни одного продового вызова с рождения — эквивалентный `callWithRecovery` появился
 на 15 дней раньше и занял его место. Теперь телеметрия висит на живом пути
 восстановления (`main+IPCRecovery.swift`).
*/

import Foundation

enum IPCReconnectTelemetry {

    /// Best-effort отчёт об успешном восстановлении.
    ///
    /// Намеренно НЕ бросает: сбой необязательной метрики не должен ронять вызов,
    /// который только что удалось восстановить. По той же причине идёт через
    /// сырой `callAsync` с быстрым таймаутом, а не через recovery-обёртку —
    /// перезапускать backend ради метрики бессмысленно.
    ///
    /// - Parameters:
    ///   - attempts: сколько повторов понадобилось (recovery-путь делает ровно один).
    ///   - durationMs: время от первой неудачной попытки до успеха.
    static func report(client: IPCClient, attempts: Int, durationMs: Int) async {
        _ = try? await client.callAsync(
            method: "report_reconnect",
            params: [
                "attempts": attempts,
                "duration_ms": durationMs,
            ],
            timeoutSec: IPCClient.quickTimeoutSec
        )
    }
}
