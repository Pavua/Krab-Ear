/*
 PasteProfileCacheTests.swift
 Unit tests for PasteProfileCache — extracted reader-writer cache (Wave 265).
 No IPC / no binary launch required.
*/

import XCTest
@testable import KrabEarAgent

final class PasteProfileCacheTests: XCTestCase {

    var cache: PasteProfileCache!

    override func setUp() {
        super.setUp()
        cache = PasteProfileCache()
    }

    override func tearDown() {
        cache = nil
        super.tearDown()
    }

    // MARK: - Basic correctness

    func test_cache_miss_returns_nil() {
        XCTAssertNil(cache.get("com.apple.finder"))
    }

    func test_set_then_get_returns_value() {
        cache.set("markdown", for: "com.apple.Notes")
        // Barrier write is async — flush by doing a sync read
        let value = cache.get("com.apple.Notes")
        // give barrier write a chance to commit
        let deadline = Date().addingTimeInterval(0.2)
        var result: String? = value
        while result == nil && Date() < deadline {
            result = cache.get("com.apple.Notes")
            if result == nil { Thread.sleep(forTimeInterval: 0.01) }
        }
        XCTAssertEqual(result, "markdown")
    }

    func test_overwrite_replaces_old_value() {
        cache.set("plain", for: "com.tdesktop.Telegram")
        flush()
        cache.set("telegram", for: "com.tdesktop.Telegram")
        flush()
        XCTAssertEqual(cache.get("com.tdesktop.Telegram"), "telegram")
    }

    func test_clear_empties_cache() {
        cache.set("html", for: "com.apple.mail")
        cache.set("markdown", for: "com.apple.Notes")
        flush()
        cache.clear()
        flush()
        XCTAssertEqual(cache.countSync, 0)
        XCTAssertNil(cache.get("com.apple.mail"))
        XCTAssertNil(cache.get("com.apple.Notes"))
    }

    func test_multiple_bundles_independent() {
        cache.set("plain", for: "com.a")
        cache.set("html", for: "com.b")
        cache.set("markdown", for: "com.c")
        flush()
        XCTAssertEqual(cache.get("com.a"), "plain")
        XCTAssertEqual(cache.get("com.b"), "html")
        XCTAssertEqual(cache.get("com.c"), "markdown")
    }

    func test_get_unknown_key_does_not_affect_existing() {
        cache.set("notes", for: "com.known")
        flush()
        _ = cache.get("com.unknown")
        XCTAssertEqual(cache.get("com.known"), "notes")
    }

    // MARK: - Unicode edge cases

    func test_unicode_bundle_id() {
        let bundleId = "com.кrab.ухо-приложение"  // RU unicode bundle id
        cache.set("plain", for: bundleId)
        flush()
        XCTAssertEqual(cache.get(bundleId), "plain")
    }

    func test_unicode_profile_value() {
        let profile = "профиль-вставки/формат"
        cache.set(profile, for: "com.test.app")
        flush()
        XCTAssertEqual(cache.get("com.test.app"), profile)
    }

    func test_emoji_bundle_id() {
        let bundleId = "com.test.🦀"
        cache.set("telegram", for: bundleId)
        flush()
        XCTAssertEqual(cache.get(bundleId), "telegram")
    }

    // MARK: - Concurrency safety

    func test_concurrent_set_reads_safe() {
        // 50 threads: half write, half read — must not crash or deadlock.
        let expectation = self.expectation(description: "concurrent_ops")
        expectation.expectedFulfillmentCount = 50
        let group = DispatchGroup()
        let concurrentQueue = DispatchQueue(
            label: "com.test.concurrent", attributes: .concurrent)

        for i in 0..<50 {
            group.enter()
            concurrentQueue.async {
                if i % 2 == 0 {
                    self.cache.set("profile_\(i)", for: "bundle_\(i)")
                } else {
                    _ = self.cache.get("bundle_\(i - 1)")
                }
                expectation.fulfill()
                group.leave()
            }
        }

        wait(for: [expectation], timeout: 5.0)
        // No crash = success. Also verify cache is in a consistent state.
        // After flush, writes from even indices should all be present.
        flush()
        for i in stride(from: 0, to: 50, by: 2) {
            XCTAssertEqual(cache.get("bundle_\(i)"), "profile_\(i)")
        }
    }

    func test_concurrent_clear_and_set_does_not_crash() {
        let expectation = self.expectation(description: "clear_concurrent")
        expectation.expectedFulfillmentCount = 20

        let q = DispatchQueue(label: "com.test.clear", attributes: .concurrent)
        for i in 0..<10 {
            q.async {
                self.cache.set("v\(i)", for: "k\(i)")
                expectation.fulfill()
            }
            q.async {
                self.cache.clear()
                expectation.fulfill()
            }
        }
        wait(for: [expectation], timeout: 5.0)
        // No crash = pass
    }

    // MARK: - Helpers

    /// Flush all pending barrier writes by performing a sync read.
    /// A queue.sync on a concurrent queue drains after all previously submitted async blocks.
    private func flush() {
        _ = cache.get("__flush_sentinel__")
    }
}
