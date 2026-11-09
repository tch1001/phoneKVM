package com.kvmlite.app.hid

/**
 * Tiny bring-up check: draws a square with the pointer and types "hello".
 * Both sequences are queued on the single HID writer thread, so the square
 * always completes before the typing starts.
 */
object HidSelfTest {

    fun run(mouse: HidMouse, keyboard: HidKeyboard, onQueued: () -> Unit = {}) {
        mouse.square(side = 120, steps = 24, stepDelayMs = 8L)
        keyboard.typeText("hello")
        // a harmless trailing report leaves nothing pressed
        mouse.releaseAll()
        keyboard.releaseAll()
        onQueued()
    }
}
