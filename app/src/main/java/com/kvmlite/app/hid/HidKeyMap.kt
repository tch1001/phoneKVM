package com.kvmlite.app.hid

import android.view.KeyEvent

/**
 * Android KeyEvent -> USB HID keyboard usage ID (page 0x07) mapping.
 * Only the keys a remote-desktop user actually needs are mapped; anything
 * unmapped falls through to the local Android UI.
 */
object HidKeyMap {

    const val MOD_LCTRL = 0x01
    const val MOD_LSHIFT = 0x02
    const val MOD_LALT = 0x04
    const val MOD_LGUI = 0x08
    const val MOD_RCTRL = 0x10
    const val MOD_RSHIFT = 0x20
    const val MOD_RALT = 0x40
    const val MOD_RGUI = 0x80

    private val modifiers: Map<Int, Int> = mapOf(
        KeyEvent.KEYCODE_CTRL_LEFT to MOD_LCTRL,
        KeyEvent.KEYCODE_SHIFT_LEFT to MOD_LSHIFT,
        KeyEvent.KEYCODE_ALT_LEFT to MOD_LALT,
        KeyEvent.KEYCODE_META_LEFT to MOD_LGUI,
        KeyEvent.KEYCODE_CTRL_RIGHT to MOD_RCTRL,
        KeyEvent.KEYCODE_SHIFT_RIGHT to MOD_RSHIFT,
        KeyEvent.KEYCODE_ALT_RIGHT to MOD_RALT,
        KeyEvent.KEYCODE_META_RIGHT to MOD_RGUI,
    )

    private val usages: Map<Int, Int> = buildMap {
        // letters a..z -> 0x04..0x1D
        for (i in 0..25) put(KeyEvent.KEYCODE_A + i, 0x04 + i)
        // digits 1..9 -> 0x1E..0x26, 0 -> 0x27
        for (i in 0..8) put(KeyEvent.KEYCODE_1 + i, 0x1E + i)
        put(KeyEvent.KEYCODE_0, 0x27)

        put(KeyEvent.KEYCODE_ENTER, 0x28)
        put(KeyEvent.KEYCODE_NUMPAD_ENTER, 0x28)
        put(KeyEvent.KEYCODE_DPAD_CENTER, 0x28)
        put(KeyEvent.KEYCODE_ESCAPE, 0x29)
        put(KeyEvent.KEYCODE_DEL, 0x2A)          // backspace
        put(KeyEvent.KEYCODE_TAB, 0x2B)
        put(KeyEvent.KEYCODE_SPACE, 0x2C)
        put(KeyEvent.KEYCODE_MINUS, 0x2D)
        put(KeyEvent.KEYCODE_EQUALS, 0x2E)
        put(KeyEvent.KEYCODE_LEFT_BRACKET, 0x2F)
        put(KeyEvent.KEYCODE_RIGHT_BRACKET, 0x30)
        put(KeyEvent.KEYCODE_BACKSLASH, 0x31)
        put(KeyEvent.KEYCODE_SEMICOLON, 0x33)
        put(KeyEvent.KEYCODE_APOSTROPHE, 0x34)
        put(KeyEvent.KEYCODE_GRAVE, 0x35)
        put(KeyEvent.KEYCODE_COMMA, 0x36)
        put(KeyEvent.KEYCODE_PERIOD, 0x37)
        put(KeyEvent.KEYCODE_SLASH, 0x38)
        put(KeyEvent.KEYCODE_CAPS_LOCK, 0x39)

        // F1..F12 -> 0x3A..0x45
        for (i in 0..11) put(KeyEvent.KEYCODE_F1 + i, 0x3A + i)
        put(KeyEvent.KEYCODE_SYSRQ, 0x46)        // print screen
        put(KeyEvent.KEYCODE_SCROLL_LOCK, 0x47)
        put(KeyEvent.KEYCODE_BREAK, 0x48)        // pause
        put(KeyEvent.KEYCODE_INSERT, 0x49)
        put(KeyEvent.KEYCODE_MOVE_HOME, 0x4A)
        put(KeyEvent.KEYCODE_PAGE_UP, 0x4B)
        put(KeyEvent.KEYCODE_FORWARD_DEL, 0x4C)
        put(KeyEvent.KEYCODE_MOVE_END, 0x4D)
        put(KeyEvent.KEYCODE_PAGE_DOWN, 0x4E)
        put(KeyEvent.KEYCODE_DPAD_RIGHT, 0x4F)
        put(KeyEvent.KEYCODE_DPAD_LEFT, 0x50)
        put(KeyEvent.KEYCODE_DPAD_DOWN, 0x51)
        put(KeyEvent.KEYCODE_DPAD_UP, 0x52)
        put(KeyEvent.KEYCODE_NUM_LOCK, 0x53)

        // numpad
        for (i in 0..9) put(KeyEvent.KEYCODE_NUMPAD_0 + i, 0x62 + i)
        put(KeyEvent.KEYCODE_NUMPAD_DIVIDE, 0x54)
        put(KeyEvent.KEYCODE_NUMPAD_MULTIPLY, 0x55)
        put(KeyEvent.KEYCODE_NUMPAD_SUBTRACT, 0x56)
        put(KeyEvent.KEYCODE_NUMPAD_ADD, 0x57)
        put(KeyEvent.KEYCODE_NUMPAD_DOT, 0x63)

        // browser / system keys that exist on some remotes
        put(KeyEvent.KEYCODE_HOME, 0x4A)
        put(KeyEvent.KEYCODE_BACK, 0x29)         // Esc, only if explicitly used
    }

    fun usageOf(keyCode: Int): Int? = usages[keyCode]

    fun modifierBit(keyCode: Int): Int? = modifiers[keyCode]

    /**
     * US-layout character -> (usage id, modifier bitmap).
     * Returns null for characters the remote US layout cannot produce.
     */
    fun charToUsage(ch: Char): Pair<Int, Int>? {
        return when (ch) {
            in 'a'..'z' -> (0x04 + (ch - 'a')) to 0
            in 'A'..'Z' -> (0x04 + (ch - 'A')) to MOD_LSHIFT
            in '1'..'9' -> (0x1E + (ch - '1')) to 0
            '0' -> 0x27 to 0
            ' ' -> 0x2C to 0
            '\n', '\r' -> 0x28 to 0
            '\t' -> 0x2B to 0
            '-' -> 0x2D to 0
            '=' -> 0x2E to 0
            '[' -> 0x2F to 0
            ']' -> 0x30 to 0
            '\\' -> 0x31 to 0
            ';' -> 0x33 to 0
            '\'' -> 0x34 to 0
            '`' -> 0x35 to 0
            ',' -> 0x36 to 0
            '.' -> 0x37 to 0
            '/' -> 0x38 to 0
            '!' -> 0x1E to MOD_LSHIFT
            '@' -> 0x1F to MOD_LSHIFT
            '#' -> 0x20 to MOD_LSHIFT
            '$' -> 0x21 to MOD_LSHIFT
            '%' -> 0x22 to MOD_LSHIFT
            '^' -> 0x23 to MOD_LSHIFT
            '&' -> 0x24 to MOD_LSHIFT
            '*' -> 0x25 to MOD_LSHIFT
            '(' -> 0x26 to MOD_LSHIFT
            ')' -> 0x27 to MOD_LSHIFT
            '_' -> 0x2D to MOD_LSHIFT
            '+' -> 0x2E to MOD_LSHIFT
            '{' -> 0x2F to MOD_LSHIFT
            '}' -> 0x30 to MOD_LSHIFT
            '|' -> 0x31 to MOD_LSHIFT
            ':' -> 0x33 to MOD_LSHIFT
            '"' -> 0x34 to MOD_LSHIFT
            '~' -> 0x35 to MOD_LSHIFT
            '<' -> 0x36 to MOD_LSHIFT
            '>' -> 0x37 to MOD_LSHIFT
            '?' -> 0x38 to MOD_LSHIFT
            else -> null
        }
    }
}
