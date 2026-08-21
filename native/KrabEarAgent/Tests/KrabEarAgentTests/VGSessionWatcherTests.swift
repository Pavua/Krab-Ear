import XCTest
@testable import KrabEarAgent

private final class ScriptedFetcher: VGSessionFetching {
    // Тест I1 (loopEpoch) реально гоняет внутренний таймерный цикл watcher'а на
    // его собственной серийной очереди, а читает счётчик — тест-поток. Без лока это
    // была бы гонка данных на _calls/_script (пусть и обычно безобидная для Int, но
    // не гарантированная).
    private let lock = NSLock()
    private var _script: [Result<(statusCode: Int, body: Data), Error>] = []
    private var _calls = 0

    var script: [Result<(statusCode: Int, body: Data), Error>] {
        get { lock.lock(); defer { lock.unlock() }; return _script }
        set { lock.lock(); _script = newValue; lock.unlock() }
    }
    var calls: Int { lock.lock(); defer { lock.unlock() }; return _calls }

    func fetchSessions(completion: @escaping (Result<(statusCode: Int, body: Data), Error>) -> Void) {
        lock.lock()
        _calls += 1
        let result = _script.isEmpty ? .failure(URLError(.cannotConnectToHost)) : _script.removeFirst()
        lock.unlock()
        completion(result)
    }
}

private final class SpyDelegate: VGSessionWatcherDelegate {
    var appeared: [(String, UInt64, Bool)] = []
    var updated: [(String, UInt64)] = []
    var gone: [(String, UInt64)] = []
    var lost: [(String, UInt64)] = []
    var authRejects = 0
    // M6(a): каждый колбэк должен приходить строго на main — записываем факт, тест
    // ассертит значение, а не полагается на то, что XCTest сам крутится на main.
    var appearedOnMainThread: [Bool] = []
    func watcherCallAppeared(_ s: VGSessionInfo, generation: UInt64, resurrected: Bool) {
        appearedOnMainThread.append(Thread.isMainThread)
        appeared.append((s.id, generation, resurrected))
    }
    func watcherCallUpdated(_ s: VGSessionInfo, generation: UInt64) { updated.append((s.id, generation)) }
    func watcherCallGone(sessionId: String, generation: UInt64) { gone.append((sessionId, generation)) }
    func watcherVGLost(sessionId: String, generation: UInt64) { lost.append((sessionId, generation)) }
    func watcherAuthRejected() { authRejects += 1 }
}

final class VGSessionWatcherTests: XCTestCase {
    private var fetcher = ScriptedFetcher()
    private var spy = SpyDelegate()
    private var fakeUptime: TimeInterval = 1000
    private var fakeNow = Date(timeIntervalSince1970: 1_755_800_000)

    private func makeWatcher() -> VGSessionWatcher {
        let w = VGSessionWatcher(fetcher: fetcher,
                                 now: { self.fakeNow },
                                 monotonic: { self.fakeUptime })
        w.delegate = spy
        return w
    }

    private func body(_ sessions: [[String: Any]]) -> Data {
        try! JSONSerialization.data(withJSONObject: ["ok": true, "count": sessions.count, "items": sessions])
    }

    private func session(_ id: String, status: String = "running", phone: String = "+341",
                         direction: String = "outbound", updatedSecondsAgo: Double = 60) -> [String: Any] {
        let iso = ISO8601DateFormatter()
        return ["id": id, "status": status, "phone": phone, "call_direction": direction,
                "created_at": iso.string(from: fakeNow.addingTimeInterval(-300)),
                "updated_at": iso.string(from: fakeNow.addingTimeInterval(-updatedSecondsAgo)),
                "src_lang": "es", "tgt_lang": "ru", "source": "twilio_pstn_outbound", "call_brief": ""]
    }

    private func poll(_ w: VGSessionWatcher) {
        let exp = expectation(description: "poll")
        w.pollOnce { exp.fulfill() }
        wait(for: [exp], timeout: 2)
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))  // дренаж main-доставки
    }

    func test_appear_immediate_and_updated_on_next_poll() {
        let w = makeWatcher()
        fetcher.script = [.success((200, body([session("s1")]))), .success((200, body([session("s1")])))]
        poll(w); poll(w)
        XCTAssertEqual(spy.appeared.map(\.0), ["s1"])
        XCTAssertEqual(spy.updated.map(\.0), ["s1"])
    }

    func test_predicate_rejects_no_phone_no_direction_and_stale() {
        let w = makeWatcher()
        fetcher.script = [.success((200, body([
            session("tg", phone: "", direction: ""),                // telegram-чат и т.п.
            session("old", updatedSecondsAgo: 7 * 3600),            // stale > 6h
            session("term", status: "stopped"),                     // терминальная
        ])))]
        poll(w)
        XCTAssertTrue(spy.appeared.isEmpty)
    }

    func test_unparseable_updated_at_fails_open_to_visible() {
        var s = session("s1"); s["updated_at"] = "garbage"
        let w = makeWatcher()
        fetcher.script = [.success((200, body([s])))]
        poll(w)
        XCTAssertEqual(spy.appeared.map(\.0), ["s1"])
    }

    func test_gone_requires_streak_2_and_only_on_success() {
        let w = makeWatcher()
        fetcher.script = [
            .success((200, body([session("s1")]))),
            .failure(URLError(.timedOut)),                       // fail ≠ gone
            .success((200, body([session("s1", status: "stopped")]))),  // предикат упал: streak 1
            .success((200, body([]))),                           // streak 2 → gone
        ]
        poll(w); poll(w); poll(w)
        XCTAssertTrue(spy.gone.isEmpty)
        poll(w)
        XCTAssertEqual(spy.gone.map(\.0), ["s1"])
    }

    func test_vgLost_needs_3_fails_AND_30s() {
        let w = makeWatcher()
        fetcher.script = [.success((200, body([session("s1")]))),
                          .failure(URLError(.timedOut)), .failure(URLError(.timedOut)), .failure(URLError(.timedOut))]
        poll(w)
        poll(w); fakeUptime += 5
        poll(w); fakeUptime += 5      // 3 фейла, но лишь 10с — рано
        poll(w)
        XCTAssertTrue(spy.lost.isEmpty)
        fetcher.script = [.failure(URLError(.timedOut))]
        fakeUptime += 25              // теперь ≥30с с последнего успеха
        poll(w)
        XCTAssertEqual(spy.lost.map(\.0), ["s1"])
        // one-shot: ещё фейлы не дублируют
        fetcher.script = [.failure(URLError(.timedOut))]
        poll(w)
        XCTAssertEqual(spy.lost.count, 1)
    }

    func test_resurrection_same_id_new_generation() {
        let w = makeWatcher()
        fetcher.script = [.success((200, body([session("s1")])))]
        poll(w)
        let firstGen = spy.appeared[0].1
        // vgLost
        fetcher.script = Array(repeating: .failure(URLError(.timedOut)), count: 3)
        poll(w); fakeUptime += 15; poll(w); fakeUptime += 20; poll(w)
        XCTAssertEqual(spy.lost.count, 1)
        // VG вернулся, звонок жив
        fetcher.script = [.success((200, body([session("s1")])))]
        poll(w)
        XCTAssertEqual(spy.appeared.count, 2)
        XCTAssertTrue(spy.appeared[1].2, "resurrected flag")
        XCTAssertGreaterThan(spy.appeared[1].1, firstGen)
    }

    func test_auth_reject_fires_once_and_counts_as_failure() {
        let w = makeWatcher()
        fetcher.script = [.success((403, Data())), .success((401, Data()))]
        poll(w); poll(w)
        XCTAssertEqual(spy.authRejects, 1)
    }

    func test_terminal_status_no_resurrection() {
        let w = makeWatcher()
        fetcher.script = [
            .success((200, body([session("s1")]))),
            .success((200, body([session("s1", status: "failed")]))),
            .success((200, body([session("s1", status: "failed")]))),  // streak 2 → gone
            .success((200, body([session("s1", status: "failed")]))),  // терминальная не воскресает
        ]
        poll(w); poll(w); poll(w); poll(w)
        XCTAssertEqual(spy.gone.count, 1)
        XCTAssertEqual(spy.appeared.count, 1)
    }

    func test_iso_both_variants_parse() {
        XCTAssertNotNil(VGSessionWatcher.parseISO("2026-08-21T10:00:00Z"))
        XCTAssertNotNil(VGSessionWatcher.parseISO("2026-08-21T10:00:00.123Z"))
    }

    // MARK: - M6(a) main-thread delivery

    func test_delegate_callback_fires_on_main_thread() {
        let w = makeWatcher()
        fetcher.script = [.success((200, body([session("s1")])))]
        poll(w)
        XCTAssertEqual(spy.appearedOnMainThread, [true])
    }

    // MARK: - M6(b) 401 counts toward failedStreak like any other failure

    func test_401_counts_as_failure_and_reaches_vgLost() {
        let w = makeWatcher()
        fetcher.script = [.success((200, body([session("s1")]))),
                          .success((401, Data())), .success((401, Data())), .success((401, Data()))]
        poll(w)
        poll(w); fakeUptime += 5
        poll(w); fakeUptime += 5      // 3 фейла (все 401), но лишь 10с — рано
        poll(w)
        XCTAssertTrue(spy.lost.isEmpty)
        fetcher.script = [.success((401, Data()))]
        fakeUptime += 25              // теперь ≥30с с последнего успеха
        poll(w)
        XCTAssertEqual(spy.lost.map(\.0), ["s1"], "401 должен продвигать failedStreak точно как generic failure")
        XCTAssertEqual(spy.authRejects, 1, "auth-хинт one-shot даже когда 401 повторяется много поллов")
    }

    // MARK: - M6(c) 5xx и битый JSON — тоже фейлы, не callGone

    func test_5xx_and_malformed_json_are_failures_not_gone() {
        let w = makeWatcher()
        fetcher.script = [
            .success((200, body([session("s1")]))),
            .success((500, Data())),
            .success((200, Data("not json".utf8))),
        ]
        poll(w); poll(w); poll(w)
        XCTAssertTrue(spy.gone.isEmpty, "5xx/битый JSON — фейл, не авторитетное отсутствие")
        XCTAssertTrue(spy.lost.isEmpty, "всего 2 фейла — ниже порога vgLost")
    }

    // MARK: - I1 loopEpoch: stop()→start() не должен запускать второй самоподдерживающийся цикл

    func test_stop_then_start_does_not_double_poll_loop() {
        // Инжектируем маленькую каденцию, чтобы за короткое окно теста набежало
        // достаточно поллов для различения одного цикла от двух.
        // Прогонялось вручную 5 раз подряд при гейте (флейк-проверка) — стабильно.
        fetcher.script = Array(repeating: .success((200, body([]))), count: 400)
        let w = VGSessionWatcher(fetcher: fetcher,
                                  now: { self.fakeNow },
                                  monotonic: { self.fakeUptime },
                                  idleCadence: 0.05,
                                  liveCadence: 0.05)
        w.delegate = spy

        w.start()
        Thread.sleep(forTimeInterval: 0.15)   // даём первому поллу цикла отработать
        w.stop()
        w.start()                             // старый отложенный таймер/completion не должен ожить

        let before = fetcher.calls
        Thread.sleep(forTimeInterval: 0.5)
        w.stop()
        let callsInWindow = fetcher.calls - before

        // Один цикл при cadence=0.05 за 0.5с даёт ~9-10 поллов; двойной — ~18-20+.
        // Запас ×1.5 над однопоточной оценкой (см. бриф) — потолок 13.
        XCTAssertLessThanOrEqual(callsInWindow, 13,
            "stop()→start() породил второй поллинг-цикл (\(callsInWindow) поллов за 0.5с)")
    }
}
