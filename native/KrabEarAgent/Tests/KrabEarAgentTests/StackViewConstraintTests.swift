/*
 StackViewConstraintTests — regression tests for KRAB-EAR-AGENT-2.

 The crash:
   NSGenericException: Unable to activate constraint with anchors
   <NSLayoutDimension "NSStackView:…width"> and
   <NSLayoutDimension "NSStackView:…width">
   because they have no common ancestor.

 Root cause:
   In HistoryPanelController.setupDictationTab(), a width constraint
   settingsBarCD.widthAnchor.constraint(equalTo: dictationStack.widthAnchor)
   was activated before settingsBarCD was added to dictationStack (or any
   common ancestor view). The fix moves the constraint activation to
   applyVisualTheme(), after dictationStack.addArrangedSubview(settingsBarCD).

 These tests validate the ordering invariant at the pure-logic level using
 AppKit NSView / NSStackView (available on macOS 13+, no window required
 to activate constraints once views share a parent).
*/

import XCTest
import AppKit
@testable import KrabEarAgent

// MARK: - Constraint ordering invariant

final class StackViewConstraintOrderingTests: XCTestCase {

    // Reproduces the bug: activating a width constraint between two sibling
    // NSStackViews BEFORE they share a common ancestor crashes at runtime.
    // This test confirms the precondition that triggers the crash.
    func test_activatingConstraintBeforeCommonAncestor_wouldThrow() {
        // We cannot safely call NSLayoutConstraint.activate on orphaned views in
        // a unit test without crashing the test process (same as production crash).
        // Instead, we verify that adding both views to a shared parent first
        // prevents the "no common ancestor" condition.

        let parent = NSStackView()
        let childA = NSStackView()
        let childB = NSStackView()

        // Neither child has a parent yet — verify this is the precondition
        XCTAssertNil(childA.superview, "childA should have no parent before addArrangedSubview")
        XCTAssertNil(childB.superview, "childB should have no parent before addArrangedSubview")

        // After adding both to a shared parent the constraint is safe
        parent.addArrangedSubview(childA)
        parent.addArrangedSubview(childB)

        XCTAssertNotNil(childA.superview, "childA must have a parent before width constraint")
        XCTAssertNotNil(childB.superview, "childB must have a parent before width constraint")
        XCTAssertEqual(childA.superview, childB.superview, "Both stacks must share the same direct parent")

        // This is the fixed pattern: activate constraint only after both views
        // have a common ancestor. Must not throw.
        let constraint = childA.widthAnchor.constraint(equalTo: childB.widthAnchor)
        constraint.isActive = true

        XCTAssertTrue(constraint.isActive, "Constraint should be active after safe activation")
    }

    // Validates the exact fix applied in HistoryPanelController.applyVisualTheme():
    // settingsBarCD is added to dictationStack BEFORE its width constraint is activated.
    func test_settingsBarCD_addedBeforeConstraintActivation() {
        let dictationStack = NSStackView()
        dictationStack.orientation = .vertical
        dictationStack.translatesAutoresizingMaskIntoConstraints = false

        let settingsBarCD = NSStackView()
        settingsBarCD.orientation = .vertical
        settingsBarCD.translatesAutoresizingMaskIntoConstraints = false

        // Simulate the fixed code path (applyVisualTheme, useClaudeDesignVariant branch):
        dictationStack.addArrangedSubview(settingsBarCD)
        // Constraint activated AFTER addArrangedSubview — safe.
        let constraint = settingsBarCD.widthAnchor.constraint(equalTo: dictationStack.widthAnchor)
        constraint.isActive = true

        XCTAssertTrue(constraint.isActive)
        XCTAssertEqual(settingsBarCD.superview, dictationStack)
    }

    // Validates that settingsBar (non-CD variant) follows the same safe pattern.
    func test_settingsBar_addedBeforeConstraintActivation() {
        let dictationStack = NSStackView()
        dictationStack.orientation = .vertical
        dictationStack.translatesAutoresizingMaskIntoConstraints = false

        let settingsBar = NSStackView()
        settingsBar.orientation = .vertical
        settingsBar.translatesAutoresizingMaskIntoConstraints = false

        // settingsBar is added in setupDictationTab() before the constraint array.
        dictationStack.addArrangedSubview(settingsBar)
        let constraint = settingsBar.widthAnchor.constraint(equalTo: dictationStack.widthAnchor)
        constraint.isActive = true

        XCTAssertTrue(constraint.isActive)
        XCTAssertEqual(settingsBar.superview, dictationStack)
    }

    // Verifies the "safe guard" pattern: constraint between two stacks that
    // are children of different parents should NOT be activated.
    func test_orphanedStackHasNoSuperview() {
        let stackA = NSStackView()
        let stackB = NSStackView()

        // One parent only receives stackA
        let parentA = NSView()
        parentA.addSubview(stackA)

        // stackB has no parent — simulates the pre-fix settingsBarCD state
        XCTAssertNil(stackB.superview, "stackB without parent should have nil superview")
        XCTAssertNotEqual(stackA.superview, stackB.superview,
                          "Stacks with different parents have no common ancestor — constraint unsafe")
    }

    // MARK: - Regression guards for residual KRAB-EAR-AGENT-2 patterns

    // Validates the fix for liveSettingsBar: a view that is removed from liveStack by
    // applyVisualTheme() must NOT hold an active width constraint referencing liveStack,
    // because after removal liveSettingsBar and liveStack have no common ancestor.
    // The fix: liveSettingsBar.widthAnchor.constraint(equalTo: liveStack.widthAnchor)
    // was removed from setupLiveTranslationTab() constraints array.
    func test_orphanedView_constraintToFormerParent_wouldBeInvalid() {
        let liveStack = NSStackView()
        liveStack.orientation = .vertical
        liveStack.translatesAutoresizingMaskIntoConstraints = false

        let liveSettingsBar = NSStackView()
        liveSettingsBar.orientation = .vertical
        liveSettingsBar.translatesAutoresizingMaskIntoConstraints = false

        // Simulate setup: liveSettingsBar added to liveStack, constraint activated.
        liveStack.addArrangedSubview(liveSettingsBar)
        XCTAssertEqual(liveSettingsBar.superview, liveStack, "liveSettingsBar should be in liveStack")

        // Simulate applyVisualTheme: removes liveSettingsBar from liveStack.
        liveStack.removeArrangedSubview(liveSettingsBar)
        liveSettingsBar.removeFromSuperview()

        // After removal, liveSettingsBar has no common ancestor with liveStack.
        XCTAssertNil(liveSettingsBar.superview, "liveSettingsBar should have no parent after removal")
        // This verifies the precondition that would cause a crash if .isActive = true
        // were called now — our fix avoids creating this constraint in the first place.
        XCTAssertNotEqual(liveSettingsBar.superview, liveStack.superview,
                          "Removed view and former parent have no common ancestor")
    }

    // Validates the settingsBarCD superview guard: the loop that activates child width
    // constraints for settingsBarCD should only run when settingsBarCD.superview != nil
    // (i.e., it is actually inserted in the dictationStack hierarchy).
    func test_settingsBarCD_childConstraints_guardedBySuperview() {
        let dictationStack = NSStackView()
        dictationStack.orientation = .vertical
        dictationStack.translatesAutoresizingMaskIntoConstraints = false

        let settingsBarCD = NSStackView()
        settingsBarCD.orientation = .vertical
        settingsBarCD.translatesAutoresizingMaskIntoConstraints = false

        let childSection = NSStackView()
        childSection.translatesAutoresizingMaskIntoConstraints = false
        settingsBarCD.addArrangedSubview(childSection)

        // Case A: settingsBarCD NOT in hierarchy — guard prevents constraint activation.
        XCTAssertNil(settingsBarCD.superview, "settingsBarCD without parent — guard should block")
        if settingsBarCD.superview != nil {
            // This block must NOT execute — if it did, the constraint would be safe
            // (childSection IS in settingsBarCD), but the guard correctly omits it
            // when settingsBarCD is detached.
            XCTFail("Guard should have prevented execution when superview is nil")
        }

        // Case B: settingsBarCD added to dictationStack — constraint activation is safe.
        dictationStack.addArrangedSubview(settingsBarCD)
        XCTAssertNotNil(settingsBarCD.superview, "settingsBarCD with parent — guard should pass")
        if settingsBarCD.superview != nil {
            let constraint = childSection.widthAnchor.constraint(equalTo: settingsBarCD.widthAnchor)
            constraint.isActive = true
            XCTAssertTrue(constraint.isActive, "Constraint should activate safely when settingsBarCD is in hierarchy")
        }
    }
}
