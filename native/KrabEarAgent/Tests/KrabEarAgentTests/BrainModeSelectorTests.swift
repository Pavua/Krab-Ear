/*
 Выбор «мозга» ассистента: убрана мёртвая ручка, поставлен работающий brain_mode.

 🔴 Факт, установленный 2026-08-27 сверкой с Voice Gateway (они проверили свой
 код построчно, мы — свой): WS-эндпоинт `/v1/sessions/{id}/conversation`
 объявляет ровно два query-параметра — `lang` и `brain_mode`. Параметра `brain`
 у них НЕТ ВООБЩЕ: FastAPI молча отбрасывает его на уровне роутинга. То есть
 селектор модели мозга в наших двух экранах был декоративным — пользователь
 выбирал модель, а она никуда не доезжала. Конкретную модель VG берёт из одного
 глобального env на весь гейтвей; выбора на сессию у них сегодня нет.

 `brain_mode` (fast / krab / auto), напротив, валидируется на их стороне и
 реально меняет порядок провайдеров — его и показываем пользователю.

 Класс бага — «декоративная проводка», против которой в проекте стоят гарды;
 здесь он проехал через границу двух репозиториев, где ни один гард не смотрит.
*/

import XCTest
import Foundation
@testable import KrabEarAgent

final class BrainModeSelectorTests: XCTestCase {

    // MARK: - Мёртвый параметр больше не отправляется

    func test_websocket_request_no_longer_sends_dead_brain_param() throws {
        let source = try String(contentsOf: Self.wsSourceURL, encoding: .utf8)
        XCTAssertFalse(
            source.contains("name: \"brain\""),
            "VG не читает `brain` — отправлять его значит врать пользователю"
        )
        XCTAssertTrue(
            source.contains("name: \"brain_mode\""),
            "brain_mode обязан продолжать отправляться — он работает"
        )
    }

    func test_conversation_ui_has_no_hardcoded_model_list() throws {
        let source = try String(contentsOf: Self.conversationUISourceURL, encoding: .utf8)
        XCTAssertFalse(source.contains("\"qwen3-4b\", \"llama-3.2-3b\""),
                       "захардкоженный список моделей мозга обязан исчезнуть")
    }

    func test_settings_has_no_hardcoded_brain_model_list() throws {
        let source = try String(contentsOf: Self.settingsSourceURL, encoding: .utf8)
        XCTAssertFalse(source.contains("qwen3-30b (точнее, 17 GB)"),
                       "второй захардкоженный список (расходившийся с первым) обязан исчезнуть")
    }

    // MARK: - Работающий переключатель на его месте

    func test_settings_offers_brain_mode_choice() throws {
        let source = try String(contentsOf: Self.settingsSourceURL, encoding: .utf8)
        XCTAssertTrue(source.contains("vaBrainModeSelector"),
                      "вместо мёртвого выбора модели нужен живой выбор режима")
        for mode in ["fast", "krab", "auto"] {
            XCTAssertTrue(source.contains("\"\(mode)\""),
                          "режим \(mode) обязан быть среди вариантов (allowlist VG)")
        }
    }

    func test_brain_mode_persisted_under_stable_key() throws {
        let source = try String(contentsOf: Self.settingsSourceURL, encoding: .utf8)
        XCTAssertTrue(source.contains("KrabEar_ConversationBrainMode"),
                      "выбор режима обязан переживать перезапуск")
    }

    // MARK: - Пути

    private static var testsRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    }
    private static var wsSourceURL: URL {
        testsRoot.appendingPathComponent("Sources/KrabEarAgent/ConversationViewController+WebSocket.swift")
    }
    private static var conversationUISourceURL: URL {
        testsRoot.appendingPathComponent("Sources/KrabEarAgent/ConversationViewController+UI.swift")
    }
    private static var settingsSourceURL: URL {
        testsRoot.appendingPathComponent("Sources/KrabEarAgent/HistoryPanelController+Settings.swift")
    }
}
