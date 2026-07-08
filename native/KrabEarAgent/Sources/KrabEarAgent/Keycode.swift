/*
 macOS virtual key codes (CGKeyCode) constants.

 Maps named constants to standard macOS keyCode values for improved readability
 and maintainability.

 Reference: https://developer.apple.com/documentation/carbon/key_codes
*/

import AppKit

/// Named constants for macOS virtual key codes (CGKeyCode).
enum Keycode: UInt16 {
    // Alphabetic keys
    case v = 9       // V key (used for Cmd+V paste)
    case escape = 53 // Escape key

    // Editing keys
    case delete = 51 // Backspace/Delete key (removes the character before the caret)

    // Modifier keys (virtual keyCodes)
    case leftOption = 58      // Left Option/Alt key
    case rightOption = 61     // Right Option/Alt key
    case leftCommand = 55     // Left Command/Meta key
    case rightCommand = 54    // Right Command/Meta key
}
