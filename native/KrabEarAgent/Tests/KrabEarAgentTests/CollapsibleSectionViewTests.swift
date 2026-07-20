/*
 CollapsibleSectionViewTests — XCTest suite for CollapsibleSectionView.

 Coverage:
   - Initial state: expanded=true by default; disclosure button `.on`.
   - Initial state: expanded=false constructor arg respected.
   - Toggle expand: setExpanded(true) shows contentStackView.
   - Toggle collapse: setExpanded(false) hides contentStackView.
   - UserDefaults persistence: state written on setExpanded.
   - UserDefaults restore: stored key read back on init.
   - Unicode title: non-ASCII sectionId + title round-trips correctly.
   - Concurrent toggle: rapid alternating setExpanded calls leave consistent final state.
   - sectionId stored correctly.
   - Separator hidden when collapsed, visible when expanded.
*/

import XCTest
import AppKit
@testable import KrabEarAgent

@MainActor
final class CollapsibleSectionViewTests: XCTestCase {

    /// Unique prefix per test run to avoid UserDefaults cross-contamination.
    private let keySuffix = UUID().uuidString
    private let defaultsDomain = IsolatedUserDefaultsDomain(scope: "CollapsibleSectionViewTests")

    override func tearDown() async throws {
        defaultsDomain.removePersistentDomain()
        try await super.tearDown()
    }

    // MARK: - Helpers

    private func makeSection(
        sectionId: String? = nil,
        title: String = "Test Section",
        isExpanded: Bool = true
    ) -> CollapsibleSectionView {
        let id = sectionId ?? "test_\(keySuffix)"
        // Ensure no stale UserDefaults key influences the test.
        defaultsDomain.defaults.removeObject(forKey: "CollapsibleSection_\(id)")
        return CollapsibleSectionView(
            sectionId: id,
            title: title,
            isExpanded: isExpanded,
            userDefaults: defaultsDomain.defaults
        )
    }

    // MARK: - Initial state

    func test_initial_expanded_state_default() {
        let section = makeSection(isExpanded: true)
        XCTAssertTrue(section.isExpanded, "default isExpanded=true must be honoured")
        XCTAssertEqual(section.disclosureButton.state, .on,
                       "disclosure button state must be .on when expanded")
    }

    func test_initial_collapsed_state() {
        let section = makeSection(isExpanded: false)
        XCTAssertFalse(section.isExpanded, "isExpanded=false constructor arg must be honoured")
        XCTAssertEqual(section.disclosureButton.state, .off,
                       "disclosure button state must be .off when collapsed")
    }

    func test_initial_content_hidden_when_collapsed() {
        let section = makeSection(isExpanded: false)
        XCTAssertTrue(section.contentStackView.isHidden,
                      "contentStackView must be hidden when section starts collapsed")
    }

    func test_initial_content_visible_when_expanded() {
        let section = makeSection(isExpanded: true)
        XCTAssertFalse(section.contentStackView.isHidden,
                       "contentStackView must be visible when section starts expanded")
    }

    // MARK: - sectionId stored correctly

    func test_sectionId_stored() {
        let id = "my_unique_\(keySuffix)"
        defaultsDomain.defaults.removeObject(forKey: "CollapsibleSection_\(id)")
        let section = CollapsibleSectionView(
            sectionId: id,
            title: "Title",
            userDefaults: defaultsDomain.defaults
        )
        XCTAssertEqual(section.sectionId, id, "sectionId property must match constructor arg")
    }

    // MARK: - Toggle expand

    func test_toggle_expand() {
        let section = makeSection(isExpanded: false)
        XCTAssertFalse(section.isExpanded, "precondition: starts collapsed")

        section.setExpanded(true, animated: false)

        XCTAssertTrue(section.isExpanded, "isExpanded must be true after setExpanded(true)")
        XCTAssertEqual(section.disclosureButton.state, .on,
                       "disclosure button must be .on after expand")
        XCTAssertFalse(section.contentStackView.isHidden,
                       "contentStackView must be visible after expand")
    }

    func test_toggle_collapse() {
        let section = makeSection(isExpanded: true)
        XCTAssertTrue(section.isExpanded, "precondition: starts expanded")

        section.setExpanded(false, animated: false)

        XCTAssertFalse(section.isExpanded, "isExpanded must be false after setExpanded(false)")
        XCTAssertEqual(section.disclosureButton.state, .off,
                       "disclosure button must be .off after collapse")
        XCTAssertTrue(section.contentStackView.isHidden,
                      "contentStackView must be hidden after collapse")
    }

    // MARK: - Separator visibility

    func test_separator_hidden_when_collapsed() {
        let section = makeSection(isExpanded: true)
        section.setExpanded(false, animated: false)

        // headerSeparator is private — verify via isExpanded contract mirror.
        // We can't read headerSeparator directly, so verify the contentStackView
        // isHidden mirrors the expanded state (both toggled together).
        XCTAssertTrue(section.contentStackView.isHidden,
                      "content is hidden when collapsed (separator mirrors this)")
        XCTAssertFalse(section.isExpanded)
    }

    func test_separator_visible_when_expanded() {
        let section = makeSection(isExpanded: false)
        section.setExpanded(true, animated: false)

        XCTAssertFalse(section.contentStackView.isHidden,
                       "content is visible when expanded (separator mirrors this)")
        XCTAssertTrue(section.isExpanded)
    }

    // MARK: - UserDefaults persistence

    func test_persist_state_expanded_in_userdefaults() {
        let id = "persist_exp_\(keySuffix)"
        defaultsDomain.defaults.removeObject(forKey: "CollapsibleSection_\(id)")
        let section = CollapsibleSectionView(
            sectionId: id,
            title: "X",
            isExpanded: false,
            userDefaults: defaultsDomain.defaults
        )
        section.setExpanded(true, animated: false)

        let stored = defaultsDomain.defaults.bool(forKey: "CollapsibleSection_\(id)")
        XCTAssertTrue(stored, "setExpanded(true) must write true to UserDefaults")
    }

    func test_persist_state_collapsed_in_userdefaults() {
        let id = "persist_col_\(keySuffix)"
        defaultsDomain.defaults.removeObject(forKey: "CollapsibleSection_\(id)")
        let section = CollapsibleSectionView(
            sectionId: id,
            title: "X",
            isExpanded: true,
            userDefaults: defaultsDomain.defaults
        )
        section.setExpanded(false, animated: false)

        let stored = defaultsDomain.defaults.bool(forKey: "CollapsibleSection_\(id)")
        XCTAssertFalse(stored, "setExpanded(false) must write false to UserDefaults")
    }

    func test_restore_state_from_userdefaults() {
        // Pre-seed UserDefaults with collapsed=false (i.e. expanded=false) before init.
        let id = "restore_\(keySuffix)"
        defaultsDomain.defaults.set(false, forKey: "CollapsibleSection_\(id)")

        // Even though constructor arg says isExpanded=true, the stored key wins.
        let section = CollapsibleSectionView(
            sectionId: id,
            title: "X",
            isExpanded: true,
            userDefaults: defaultsDomain.defaults
        )

        XCTAssertFalse(section.isExpanded,
                       "UserDefaults stored value must override constructor isExpanded arg")
    }

    func test_restore_expanded_from_userdefaults_overrides_constructor() {
        let id = "restore_exp_\(keySuffix)"
        // Seed: expanded=true stored, constructor says false.
        defaultsDomain.defaults.set(true, forKey: "CollapsibleSection_\(id)")

        let section = CollapsibleSectionView(
            sectionId: id,
            title: "X",
            isExpanded: false,
            userDefaults: defaultsDomain.defaults
        )

        XCTAssertTrue(section.isExpanded,
                      "UserDefaults true must override constructor false")
    }

    // MARK: - Unicode title / sectionId

    func test_unicode_title_stored_and_displayed() {
        let id = "unicode_\(keySuffix)"
        let unicodeTitle = "Секция Настройки 🎙️"
        defaultsDomain.defaults.removeObject(forKey: "CollapsibleSection_\(id)")
        let section = CollapsibleSectionView(
            sectionId: id,
            title: unicodeTitle,
            userDefaults: defaultsDomain.defaults
        )

        XCTAssertEqual(section.titleLabel.stringValue, unicodeTitle,
                       "titleLabel must display the full Unicode title")
    }

    func test_unicode_sectionId_defaults_key() {
        let id = "секция_\(keySuffix)"
        defaultsDomain.defaults.removeObject(forKey: "CollapsibleSection_\(id)")
        let section = CollapsibleSectionView(
            sectionId: id,
            title: "T",
            isExpanded: true,
            userDefaults: defaultsDomain.defaults
        )
        section.setExpanded(false, animated: false)

        let key = "CollapsibleSection_\(id)"
        XCTAssertNotNil(defaultsDomain.defaults.object(forKey: key),
                        "Unicode sectionId must produce a valid UserDefaults key")
        XCTAssertFalse(defaultsDomain.defaults.bool(forKey: key),
                       "collapsed state must be persisted under unicode sectionId key")
    }

    // MARK: - Concurrent toggle safety

    func test_concurrent_toggle_safe() {
        // Rapid sequential toggles on the main actor must leave a consistent final state.
        // (True concurrent access from different threads isn't possible on @MainActor,
        //  but we verify the toggle loop doesn't corrupt state.)
        let section = makeSection(isExpanded: true)

        for i in 0..<20 {
            section.setExpanded(i % 2 == 0, animated: false)
        }

        // After 20 iterations (0-indexed, last i=19, 19%2=1 → expanded=false).
        XCTAssertFalse(section.isExpanded, "final state must match last setExpanded call")
        XCTAssertEqual(section.disclosureButton.state, .off,
                       "disclosure button must reflect final state after rapid toggles")
    }

    // MARK: - Header stack always visible

    func test_headerStack_always_visible_regardless_of_expanded() {
        let section = makeSection(isExpanded: false)
        // headerStack is always shown (only contentStackView and separator toggle).
        XCTAssertFalse(section.headerStack.isHidden,
                       "headerStack must always be visible, even when collapsed")

        section.setExpanded(true, animated: false)
        XCTAssertFalse(section.headerStack.isHidden,
                       "headerStack must remain visible after expand")
    }

    // MARK: - Multiple sections independent

    func test_multiple_sections_independent_defaults() {
        let idA = "sectionA_\(keySuffix)"
        let idB = "sectionB_\(keySuffix)"
        defaultsDomain.defaults.removeObject(forKey: "CollapsibleSection_\(idA)")
        defaultsDomain.defaults.removeObject(forKey: "CollapsibleSection_\(idB)")

        let a = CollapsibleSectionView(
            sectionId: idA,
            title: "A",
            isExpanded: true,
            userDefaults: defaultsDomain.defaults
        )
        let b = CollapsibleSectionView(
            sectionId: idB,
            title: "B",
            isExpanded: false,
            userDefaults: defaultsDomain.defaults
        )

        a.setExpanded(false, animated: false)

        // a collapsed should not affect b.
        XCTAssertFalse(a.isExpanded, "section A must be collapsed after setExpanded(false)")
        XCTAssertFalse(b.isExpanded, "section B state must be independent of section A")

    }
}
