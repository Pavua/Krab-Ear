/*
 LaunchOptionsBundledVendorRootTests — resolveProjectRoot приоритезирует
 bundled Python-рантайм внутри `.app` (задача упаковки #7, 2026-08-09).

 Контекст: `scripts/build_bundled_runtime.command` кладёт самодостаточный
 Python-рантайм (`.venv_krab_ear/` + копия `KrabEar/`) в staging-каталог,
 который на релизе `assemble_signed_app.sh` копирует в
 `Krab Ear.app/Contents/Resources/vendor/`. До этой волны `resolveProjectRoot`
 про этот путь не знал вовсе — для получателя DMG (без system Python >= 3.12,
 без git-репозитория рядом) единственным источником пути к backend оставался
 `project_root`-указатель bootstrap-инсталлятора, который сам по себе требует
 Terminal-флоу (T2/T3 из `docs/audit/2026-08-05-onboarding-clean-mac-audit.md`).

 Приоритет НАМЕРЕННО между env-override и cwd: bundled-копия внутри самого
 `.app` — самый авторитетный источник для настоящей дистрибуции (её нельзя
 случайно перепутать с чужим dev-checkout'ом на диске, в отличие от
 8-уровневого walk-up), но explicit/env-override всё ещё обязаны выигрывать
 для dev/CI-сценариев.

 🔴 Обратная совместимость проверена по факту, не предположением: в ТЕКУЩЕЙ
 живой настройке владельца `Krab Ear.app` лежит прямо в корне репозитория
 (`<repo>/Krab Ear.app`), `Contents/Resources/vendor` там не существует —
 значит новая проверка проваливается и код падает в прежнюю cwd/walk-up
 цепочку БЕЗ изменения поведения. Активируется только для настоящих
 bundled-дистрибутивов (после задачи #8 — build_bundled_runtime.command
 подключён в release.yml, но реальный релизный workflow ещё не запускался,
 см. коммит e8db8ae8).
*/

import XCTest
@testable import KrabEarAgent

final class LaunchOptionsBundledVendorRootTests: XCTestCase {

    private var tmpRoot: URL!

    override func setUp() {
        super.setUp()
        tmpRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("KrabEarVendorRootTests-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: tmpRoot, withIntermediateDirectories: true)
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: tmpRoot)
        tmpRoot = nil
        super.tearDown()
    }

    /// Создаёт маркер `KrabEar/backend/service.py`, по которому resolveProjectRoot
    /// узнаёт валидный projectRoot — тот же маркер, что использует продовый код.
    private func plantProjectRootMarker(at root: URL) {
        let backendDir = root.appendingPathComponent("KrabEar/backend")
        try? FileManager.default.createDirectory(at: backendDir, withIntermediateDirectories: true)
        FileManager.default.createFile(
            atPath: backendDir.appendingPathComponent("service.py").path,
            contents: Data("# fixture".utf8)
        )
    }

    /// Собирает `.app`-подобную структуру `<bundleRoot>/Contents/MacOS/KrabEarAgent`
    /// и возвращает путь исполняемого файла (то, что попадает в `arguments.first`).
    private func makeBundleExecutablePath(bundleRoot: URL) -> String {
        let macOSDir = bundleRoot.appendingPathComponent("Contents/MacOS")
        try? FileManager.default.createDirectory(at: macOSDir, withIntermediateDirectories: true)
        return macOSDir.appendingPathComponent("KrabEarAgent").path
    }

    // MARK: - Основной сценарий: bundled vendor находится и используется

    func test_usesBundledVendorRoot_whenPresentInsideAppBundle() {
        let bundleRoot = tmpRoot.appendingPathComponent("Krab Ear.app")
        let execPath = makeBundleExecutablePath(bundleRoot: bundleRoot)
        let vendorRoot = bundleRoot.appendingPathComponent("Contents/Resources/vendor")
        plantProjectRootMarker(at: vendorRoot)

        let options = LaunchOptions(arguments: [execPath])

        XCTAssertEqual(
            options.projectRoot, vendorRoot.path,
            "bundled Contents/Resources/vendor не подхвачен — DMG-получатель без "
            + "system Python снова упрётся в T2/T3 (нет backend)"
        )
    }

    // MARK: - Явный override (--project-root) обязан выигрывать у vendor

    func test_explicitProjectRootArg_winsOverBundledVendor() {
        let bundleRoot = tmpRoot.appendingPathComponent("Krab Ear.app")
        let execPath = makeBundleExecutablePath(bundleRoot: bundleRoot)
        plantProjectRootMarker(at: bundleRoot.appendingPathComponent("Contents/Resources/vendor"))

        let explicitRoot = tmpRoot.appendingPathComponent("dev-checkout")
        plantProjectRootMarker(at: explicitRoot)

        let options = LaunchOptions(arguments: [execPath, "--project-root", explicitRoot.path])

        XCTAssertEqual(
            options.projectRoot, explicitRoot.path,
            "--project-root игнорируется в пользу bundled vendor — dev-флоу сломан"
        )
    }

    // MARK: - Env-override (KRAB_EAR_PROJECT_ROOT) обязан выигрывать у vendor

    func test_envProjectRoot_winsOverBundledVendor() {
        let bundleRoot = tmpRoot.appendingPathComponent("Krab Ear.app")
        let execPath = makeBundleExecutablePath(bundleRoot: bundleRoot)
        plantProjectRootMarker(at: bundleRoot.appendingPathComponent("Contents/Resources/vendor"))

        let envRoot = tmpRoot.appendingPathComponent("env-checkout")
        plantProjectRootMarker(at: envRoot)

        // Сохраняем/восстанавливаем прежнее значение, а не безусловный unset
        // (ревью 2026-08-09, LOW-4) — если переменная уже была в окружении
        // прогона, unset её бы стёр для остальных тестов процесса.
        let previousEnvRoot = ProcessInfo.processInfo.environment["KRAB_EAR_PROJECT_ROOT"]
        setenv("KRAB_EAR_PROJECT_ROOT", envRoot.path, 1)
        defer {
            if let previousEnvRoot {
                setenv("KRAB_EAR_PROJECT_ROOT", previousEnvRoot, 1)
            } else {
                unsetenv("KRAB_EAR_PROJECT_ROOT")
            }
        }

        let options = LaunchOptions(arguments: [execPath])

        XCTAssertEqual(
            options.projectRoot, envRoot.path,
            "KRAB_EAR_PROJECT_ROOT игнорируется в пользу bundled vendor — CI/dev override сломан"
        )
    }

    // MARK: - Vendor обязан выигрывать у generic walk-up (порядок внутри chain)

    func test_bundledVendor_winsOverGenericWalkUp() {
        // Оба кандидата валидны одновременно: walk-up нашёл бы Repo (предок
        // executablePath), но vendor — более авторитетный источник и должен
        // проверяться раньше по цепочке.
        let repoRoot = tmpRoot.appendingPathComponent("Repo")
        let bundleRoot = repoRoot.appendingPathComponent("Krab Ear.app")
        let execPath = makeBundleExecutablePath(bundleRoot: bundleRoot)

        plantProjectRootMarker(at: repoRoot)  // walk-up кандидат
        let vendorRoot = bundleRoot.appendingPathComponent("Contents/Resources/vendor")
        plantProjectRootMarker(at: vendorRoot)  // vendor кандидат

        let options = LaunchOptions(arguments: [execPath])

        XCTAssertEqual(
            options.projectRoot, vendorRoot.path,
            "walk-up перебил bundled vendor — на диске с несколькими копиями "
            + "агент подхватит первую попавшуюся вместо своей собственной"
        )
    }

    // MARK: - Неправильная форма пути НЕ должна давать ложных совпадений

    func test_doesNotMatchVendor_whenExecutablePathIsNotProperBundleShape() {
        // Легаси dev-путь native/runtime/KrabEarAgent — Contents/MacOS/ вообще
        // отсутствует. Если бы код вычислял vendor от произвольного родителя
        // (а не строго требовал "Contents" как grandparent), он бы ошибочно
        // подхватил posторонний Resources/vendor рядом.
        let devRoot = tmpRoot.appendingPathComponent("Repo/native/runtime")
        try? FileManager.default.createDirectory(at: devRoot, withIntermediateDirectories: true)
        let execPath = devRoot.appendingPathComponent("KrabEarAgent").path

        // Ловушка: валидный маркер лежит там, где НЕПРАВИЛЬНАЯ реализация
        // (deletingLastPathComponent×2 без проверки "Contents") могла бы его найти.
        let trapVendor = tmpRoot.appendingPathComponent("Repo/native/Resources/vendor")
        plantProjectRootMarker(at: trapVendor)

        let options = LaunchOptions(arguments: [execPath])

        XCTAssertNotEqual(
            options.projectRoot, trapVendor.path,
            "vendor-проверка сработала на НЕ-bundle структуре пути — ложное "
            + "совпадение вне настоящего .app"
        )
    }
}
