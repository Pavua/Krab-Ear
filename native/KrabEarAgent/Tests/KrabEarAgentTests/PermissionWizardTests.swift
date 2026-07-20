/*
 PermissionWizardTests — тесты машины состояний PermissionWizard.

 Подход:
 - runIfNeeded() вызывается только для раннего guard, до показа NSAlert.
 - Автозапуск заменён spy-объектом: тесты не создают plist и процессы.
 - Тестируем:
   (1) Условие раннего выхода: onboardingCompleted == true → пропустить онбординг.
   (2) applyCompletionState() тест-хук: мутации AgentSettings и persistSettings вызов.
   (3) Флаг autoStartEnabled правильно отражает параметр autostart.
   (4) onboardingCompleted выставляется в true после applyCompletionState.
   (5) persistSettings вызывается с правильными ключами payload.
   (6) AutostartManaging получает ровно ожидаемые значения.
*/

import XCTest
@testable import KrabEarAgent

// MARK: - Тесты PermissionWizard

/// Запоминает решения онбординга об автозапуске без файловых изменений.
private final class AutostartManagerSpy: AutostartManaging {
    private(set) var receivedValues: [Bool] = []

    func setAutostart(enabled: Bool) {
        receivedValues.append(enabled)
    }
}

@MainActor
final class PermissionWizardTests: XCTestCase {

    // MARK: - Вспомогательные объекты

    private func makeWizard() -> PermissionWizard {
        PermissionWizard()
    }

    // MARK: - Ранний выход при onboardingCompleted == true

    func test_runIfNeeded_alreadyCompleted_returnsUnmodifiedSettings() {
        let wizard = makeWizard()
        let autostartManager = AutostartManagerSpy()
        var settings = AgentSettings.default
        settings.onboardingCompleted = true
        settings.autoStartEnabled = true
        var persistCalled = false

        let result = wizard.runIfNeeded(
            settings: settings,
            persistSettings: { _ in persistCalled = true },
            launchAgentManager: autostartManager
        )

        XCTAssertTrue(result.onboardingCompleted)
        XCTAssertTrue(result.autoStartEnabled)
        XCTAssertFalse(persistCalled, "Ранний guard не должен повторно сохранять настройки")
        XCTAssertTrue(
            autostartManager.receivedValues.isEmpty,
            "Ранний guard не должен менять launchd-состояние"
        )
    }

    // MARK: - Мутации состояния в applyCompletionState

    func test_applyCompletionState_setsOnboardingCompleted() {
        let wizard = makeWizard()
        let launchManager = AutostartManagerSpy()
        var persistCalled = false

        var settings = AgentSettings.default
        settings.onboardingCompleted = false

        let result = wizard.applyCompletionState(
            to: settings,
            autostart: false,
            persistSettings: { _ in persistCalled = true },
            launchAgentManager: launchManager
        )

        XCTAssertTrue(result.onboardingCompleted, "applyCompletionState должен выставить onboardingCompleted=true")
        XCTAssertTrue(persistCalled, "applyCompletionState должен сохранить итоговые настройки")
        XCTAssertEqual(launchManager.receivedValues, [false])
    }

    func test_applyCompletionState_autostartTrue_reflectedInSettings() {
        let wizard = makeWizard()
        let launchManager = AutostartManagerSpy()

        let settings = AgentSettings.default
        let result = wizard.applyCompletionState(
            to: settings,
            autostart: true,
            persistSettings: { _ in },
            launchAgentManager: launchManager
        )

        XCTAssertTrue(result.autoStartEnabled, "autoStartEnabled должен быть true когда autostart=true")
        XCTAssertEqual(launchManager.receivedValues, [true])
    }

    func test_applyCompletionState_autostartFalse_reflectedInSettings() {
        let wizard = makeWizard()
        let launchManager = AutostartManagerSpy()

        var settings = AgentSettings.default
        settings.autoStartEnabled = true  // начальное значение true

        let result = wizard.applyCompletionState(
            to: settings,
            autostart: false,
            persistSettings: { _ in },
            launchAgentManager: launchManager
        )

        XCTAssertFalse(result.autoStartEnabled, "autoStartEnabled должен быть false когда autostart=false")
        XCTAssertEqual(launchManager.receivedValues, [false])
    }

    func test_applyCompletionState_callsPersistSettings() {
        let wizard = makeWizard()
        let launchManager = AutostartManagerSpy()
        var persistedPayload: [String: Any]?

        let settings = AgentSettings.default
        _ = wizard.applyCompletionState(
            to: settings,
            autostart: false,
            persistSettings: { payload in persistedPayload = payload },
            launchAgentManager: launchManager
        )

        XCTAssertNotNil(persistedPayload, "persistSettings должен быть вызван с payload")
        let onboardingValue = persistedPayload?["onboarding_completed"] as? Bool
        XCTAssertEqual(onboardingValue, true, "payload должен содержать onboarding_completed=true")
        XCTAssertEqual(launchManager.receivedValues, [false])
    }

    func test_applyCompletionState_payloadContainsAutoStartKey() {
        let wizard = makeWizard()
        let launchManager = AutostartManagerSpy()
        var persistedPayload: [String: Any]?

        _ = wizard.applyCompletionState(
            to: AgentSettings.default,
            autostart: true,
            persistSettings: { persistedPayload = $0 },
            launchAgentManager: launchManager
        )

        let autoStartValue = persistedPayload?["auto_start_enabled"] as? Bool
        XCTAssertEqual(autoStartValue, true, "payload['auto_start_enabled'] должен совпадать с переданным autostart")
        XCTAssertEqual(launchManager.receivedValues, [true])
    }

    // MARK: - Создание начального шага

    /// Wizard существует и может быть создан — базовая smoke проверка
    /// что init не крашится и экземпляр валиден без UI.
    func test_initial_step_intro_wizardCanBeCreated() {
        let wizard = makeWizard()
        XCTAssertNotNil(wizard, "PermissionWizard должен создаваться без параметров")
    }

    // MARK: - Сохранение результата в формате UserDefaults

    /// applyCompletionState проверяет что payload содержит все ожидаемые ключи
    /// для корректного сохранения в UserDefaults через persistSettings.
    func test_completion_payload_has_required_userdefaults_keys() {
        let wizard = makeWizard()
        let launchManager = AutostartManagerSpy()
        var persistedPayload: [String: Any]?

        _ = wizard.applyCompletionState(
            to: AgentSettings.default,
            autostart: true,
            persistSettings: { persistedPayload = $0 },
            launchAgentManager: launchManager
        )

        let keys = persistedPayload?.keys.map { $0 } ?? []
        XCTAssertTrue(keys.contains("onboarding_completed"),
                      "payload должен содержать ключ 'onboarding_completed'")
        XCTAssertTrue(keys.contains("auto_start_enabled"),
                      "payload должен содержать ключ 'auto_start_enabled'")
        XCTAssertEqual(launchManager.receivedValues, [true])
    }

    // MARK: - Повторное применение состояния

    /// Многократные вызовы applyCompletionState подряд не должны нарушать инварианты —
    /// каждый вызов возвращает settings с onboardingCompleted=true.
    func test_repeatedCompletionPreservesEachAutostartDecision() {
        let wizard = makeWizard()
        let launchManager = AutostartManagerSpy()
        var callCount = 0

        for i in 0..<5 {
            let autostart = i % 2 == 0
            let result = wizard.applyCompletionState(
                to: AgentSettings.default,
                autostart: autostart,
                persistSettings: { _ in callCount += 1 },
                launchAgentManager: launchManager
            )
            XCTAssertTrue(result.onboardingCompleted,
                          "Итерация \(i): onboardingCompleted должен быть true")
        }

        XCTAssertEqual(callCount, 5, "persistSettings должен быть вызван ровно 5 раз")
        XCTAssertEqual(launchManager.receivedValues, [true, false, true, false, true])
    }
}
