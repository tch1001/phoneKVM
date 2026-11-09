package com.kvmlite.app.hid

/**
 * Boot-protocol mouse reports written to /dev/hidg0.
 *
 * Standard report (4 bytes) - matches a boot-mouse descriptor plus wheel:
 *   [0] buttons  bit0 left, bit1 right, bit2 middle
 *   [1] dx       signed int8, relative, right = positive
 *   [2] dy       signed int8, relative, down  = positive
 *   [3] wheel    signed int8, up = +1, down = -1 ("vertical wheel")
 * Strict 3-byte boot protocol parsers only read bytes 0..2, so byte 3 is
 * harmless for them; the 4th byte is what your report descriptor must declare
 * if you want scrolling to reach the host.
 *
 * Extended report (8 bytes) - only for gadgets whose descriptor declares it:
 *   [0] buttons, [1] dx, [2] dy, [3] wheel, [4] pan (AC pan, signed),
 *   [5..7] reserved zero.
 *
 * NOTE: if the gadget's descriptor is the classic 3-byte boot mouse, set
 * `reportLength = 3`. `reportLength` is authoritative: we always write exactly
 * that many bytes so the host parser stays in sync.
 */
class HidMouse(private val writer: HidWriter) {

    @Volatile
    var reportLength: Int = 4

    private var accX = 0f
    private var accY = 0f
    private var buttons = 0

    /** Relative motion in pixels; fractions are carried into later reports. */
    fun moveRelative(dx: Float, dy: Float) {
        if (dx == 0f && dy == 0f) return
        writer.submit {
            accX += dx
            accY += dy
            flushMotion()
        }
    }

    /** One wheel tick per unit; positive = scroll up (away from user). */
    fun wheel(ticks: Int) {
        if (ticks == 0) return
        writer.submit { send(0, 0, ticks.coerceIn(-127, 127)) }
    }

    fun leftClick() = click(BTN_LEFT)

    fun rightClick() = click(BTN_RIGHT)

    fun click(button: Int, holdMs: Long = 28L) {
        writer.submit {
            buttons = buttons or button
            send(0, 0, 0)
            Thread.sleep(holdMs)
            buttons = buttons and button.inv()
            send(0, 0, 0)
        }
    }

    /** Drops any stuck button (called on pause / disconnect). */
    fun releaseAll() {
        writer.submit {
            buttons = 0
            accX = 0f
            accY = 0f
            send(0, 0, 0)
        }
    }

    /**
     * Self-test: draws a ~[side]px square with [steps] reports per edge, so the
     * pointer visibly completes a loop and returns to its starting point.
     */
    fun square(side: Int = 120, steps: Int = 24, stepDelayMs: Long = 8L) {
        writer.submit {
            val perStep = (side.toFloat() / steps).coerceAtLeast(1f)
            val plan = listOf(
                perStep to 0f,   // right
                0f to perStep,   // down
                -perStep to 0f,  // left
                0f to -perStep,  // up
            )
            for ((dx, dy) in plan) {
                repeat(steps) {
                    rawMove(dx, dy)
                    Thread.sleep(stepDelayMs)
                }
            }
            // land exactly back on the origin
            send(0, 0, 0)
        }
    }

    private fun flushMotion() {
        var guard = 0
        while (guard++ < 16) {
            val x = accX.toInt()
            val y = accY.toInt()
            if (x == 0 && y == 0) return
            val cx = x.coerceIn(-127, 127)
            val cy = y.coerceIn(-127, 127)
            accX -= cx
            accY -= cy
            send(cx, cy, 0)
        }
    }

    /** Blocking residual-free move used by test sequences (writer thread only). */
    private fun rawMove(dx: Float, dy: Float) {
        accX += dx
        accY += dy
        flushMotion()
    }

    private fun send(dx: Int, dy: Int, wheel: Int) {
        val len = reportLength.coerceIn(3, 8)
        val report = ByteArray(len)
        report[0] = buttons.toByte()
        report[1] = dx.toByte()
        report[2] = dy.toByte()
        if (len >= 4) report[3] = wheel.toByte()
        writer.write(report)
    }

    companion object {
        const val BTN_LEFT = 0x01
        const val BTN_RIGHT = 0x02
        const val BTN_MIDDLE = 0x04
    }
}
