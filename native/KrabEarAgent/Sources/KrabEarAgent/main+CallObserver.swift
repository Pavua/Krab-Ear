import AppKit
import Foundation

/// Проводка Call Observer (spec §3 комп. 6): единственный владелец —
/// AgentAppDelegate.callObserverCoordinator/callObserverWatcher. Креденшел VG —
/// через существующий IPC get_voice_gateway_credential (W1892), кэш в памяти;
/// backend, умерший мид-сессии, оставляет кэш живым.
///
/// T9: реальные CallObserverHUD/CallObserverPanelController (functional UI) —
/// заменили T8-заглушки CallObserverHUDStub/CallObserverPanelStub (файлы этой
/// задачи: CallObserverHUD.swift, CallObserverPanelController.swift).
extension AgentAppDelegate {
    func setupCallObserver() {
        let settings = IPCCallObserverSettings(ipcClient: ipcClient)
        let tokenProvider: () -> String = { settings.lastApiKey }
        let hudController = CallObserverHUD()
        let panelController = CallObserverPanelController()
        let coordinator = CallObserverCoordinator(
            hud: hudController, panel: panelController,
            poster: URLSessionVGCommandPoster(tokenProvider: tokenProvider),
            settings: settings,
            stream: VGCallStreamClient(), player: CallAudioPlayer(),
            tokenProvider: tokenProvider)
        hudController.coordinator = coordinator
        panelController.coordinator = coordinator
        self.callObserverCoordinator = coordinator
        let watcher = VGSessionWatcher(fetcher: URLSessionVGSessionFetcher(
            baseURLProvider: { settings.lastBaseURL }, tokenProvider: tokenProvider))
        watcher.delegate = coordinator
        self.callObserverWatcher = watcher
        watcher.start()
    }

    func tearDownCallObserver() {
        // Минор (ревью, Fix round 1): явно закрыть events-stream и прослушку на
        // выходе из приложения — раньше teardown трогал только watcher (poll-цикл),
        // оставляя VG WS-соединения висеть до process exit.
        callObserverCoordinator?.tearDown()
        callObserverWatcher?.stop()
    }

    // MARK: - Status-menu item «Звонок агента…» (T9, доп.скоуп 2б)

    /// Открывает панель звонка агента из пункта статус-меню — тот же путь, что
    /// клик по HUD (`openPanelFromMenu`, а не `userSelectedSession`: пункт меню
    /// не выбирает КОНКРЕТНУЮ сессию, только показывает текущий выбор).
    @objc func onOpenCallObserverPanel() {
        callObserverCoordinator?.openPanelFromMenu()
    }

    /// Обновляет enabled-состояние пункта меню — вызывается из rebuildStatusMenu
    /// (первичное построение) и menuWillOpen (main+MenuBarRecap.swift), тот же
    /// паттерн, что refreshBrainLeaseMenuItem/refreshMemoryLineMenuItem. Никакого
    /// IPC не нужно — `hasLiveCall` уже в памяти координатора.
    func refreshCallObserverMenuItem() {
        callObserverMenuItem?.isEnabled = callObserverCoordinator?.hasLiveCall ?? false
    }
}

/// GET /v1/sessions?limit=vgSessionsPageLimit (off-main, ephemeral session, timeout 5с).
final class URLSessionVGSessionFetcher: VGSessionFetching {
    private let baseURLProvider: () -> URL
    private let tokenProvider: () -> String
    private let session: URLSession

    init(baseURLProvider: @escaping () -> URL, tokenProvider: @escaping () -> String) {
        self.baseURLProvider = baseURLProvider
        self.tokenProvider = tokenProvider
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 5
        session = URLSession(configuration: cfg)
    }

    func fetchSessions(completion: @escaping (Result<(statusCode: Int, body: Data), Error>) -> Void) {
        var url = baseURLProvider()
        url.append(path: "/v1/sessions")
        url.append(queryItems: [URLQueryItem(name: "limit", value: String(vgSessionsPageLimit))])
        let req = VGWebSocketConnection.makeRequest(url: url, token: tokenProvider())
        session.dataTask(with: req) { data, resp, error in
            if let error { completion(.failure(error)); return }
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            completion(.success((code, data ?? Data())))
        }.resume()
    }
}

/// POST hangup + GET diagnostics (cost) — off-main.
final class URLSessionVGCommandPoster: VGCommandPosting {
    private let tokenProvider: () -> String
    private let session: URLSession

    init(tokenProvider: @escaping () -> String) {
        self.tokenProvider = tokenProvider
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 10
        session = URLSession(configuration: cfg)
    }

    func hangup(baseURL: URL, sessionId: String, completion: @escaping (Result<Int, Error>) -> Void) {
        var url = baseURL
        url.append(path: "/v1/telephony/calls/\(sessionId)/hangup")
        var req = VGWebSocketConnection.makeRequest(url: url, token: tokenProvider())
        req.httpMethod = "POST"
        session.dataTask(with: req) { _, resp, error in
            if let error { completion(.failure(error)); return }
            completion(.success((resp as? HTTPURLResponse)?.statusCode ?? 0))
        }.resume()
    }

    func fetchCostUsd(baseURL: URL, sessionId: String, completion: @escaping (Double?) -> Void) {
        var url = baseURL
        url.append(path: "/v1/sessions/\(sessionId)/diagnostics")
        let req = VGWebSocketConnection.makeRequest(url: url, token: tokenProvider())
        session.dataTask(with: req) { data, _, _ in
            // Реальный VG кладёт costs НА ВЕРХНИЙ уровень ({**diag, status, ...}).
            guard let data,
                  let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                  let costs = obj["costs"] as? [String: Any],
                  let total = costs["total_usd"] as? Double else { completion(nil); return }
            completion(total)
        }.resume()
    }
}

/// Настройки + креденшел через IPC, кэш в памяти (backend может умереть мид-сессии).
/// Держит `IPCClient` напрямую (тот же паттерн, что CallAutomationController/
/// ErrorActionHandler/DiagnosticsTabView), а не generic-обёртку — проект нигде не
/// заводит отдельный "off-main IPC helper" тип, каждый потребитель сам оборачивает
/// синхронный ipcClient.call(...) в DispatchQueue.global().async (см.
/// main+BrainLease.swift refreshBrainLeaseMenuItem, HistoryPanelController+Settings
/// refreshQuickCaptureSectionState).
///
/// I-3 (ревью, Fix round 1): `lastApiKey`/`lastBaseURL` записываются на фоновой
/// очереди (внутри refresh's DispatchQueue.global block), а читаются из
/// `tokenProvider`/`baseURLProvider` closures, которые дёргаются с РАЗНЫХ потоков
/// (main — из userAction-методов координатора; приватная serial-очередь
/// VGWebSocketConnection — из openLocked() при коннекте). Без синхронизации это
/// гонка данных на String/URL. Обе величины теперь под NSLock.
final class IPCCallObserverSettings: CallObserverSettingsProviding {
    private let ipcClient: IPCClient
    private let lock = NSLock()
    private var _lastBaseURL = URL(string: "http://127.0.0.1:8090")!
    private var _lastApiKey = ""

    var lastBaseURL: URL {
        lock.lock(); defer { lock.unlock() }
        return _lastBaseURL
    }
    var lastApiKey: String {
        lock.lock(); defer { lock.unlock() }
        return _lastApiKey
    }

    init(ipcClient: IPCClient) {
        self.ipcClient = ipcClient
    }

    func refresh(completion: @escaping (Bool, Bool, Bool, URL) -> Void) {
        let client = ipcClient
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            // Узкоскоуповый W1892-хендлер — единственный источник НЕредактированного
            // voice_gateway_api_key (get_settings редактирует секреты, wave-35 CRIT).
            if let resp = try? client.call(method: "get_voice_gateway_credential", params: [:]),
               let cred = resp["result"] as? [String: Any], cred["ok"] as? Bool == true {
                if let urlStr = cred["voice_gateway_url"] as? String, let url = URL(string: urlStr) {
                    self.lock.lock(); self._lastBaseURL = url; self.lock.unlock()
                }
                if let key = cred["voice_gateway_api_key"] as? String {
                    self.lock.lock(); self._lastApiKey = key; self.lock.unlock()  // IPC-провал → живёт последний успешный кэш
                }
            }
            var hudEnabled = true
            var autoplay = false
            var privacy = false
            if let resp = try? client.call(method: "get_settings", params: [:]),
               let s = resp["result"] as? [String: Any] {
                hudEnabled = s["call_observer_hud_enabled"] as? Bool ?? true
                autoplay = s["call_observer_autoplay_audio"] as? Bool ?? false
                privacy = s["privacy_mode_enabled"] as? Bool ?? false
            }
            let baseURL = self.lastBaseURL
            DispatchQueue.main.async {
                completion(hudEnabled, autoplay, privacy, baseURL)
            }
        }
    }
}
