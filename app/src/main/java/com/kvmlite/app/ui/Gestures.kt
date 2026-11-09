package com.kvmlite.app.ui

import android.os.SystemClock
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.input.pointer.PointerInputChange
import androidx.compose.ui.input.pointer.PointerInputScope
import androidx.compose.ui.input.pointer.PointerEventPass
import androidx.compose.ui.input.pointer.positionChange
import com.kvmlite.app.KvmController
import kotlinx.coroutines.withTimeoutOrNull
import kotlin.math.abs

private const val LONG_PRESS_MS = 500L

/**
 * Touch surface semantics (relative pointer, like a laptop trackpad):
 *
 *  - one finger drag            -> relative mouse motion (scaled by sensitivity)
 *  - one finger tap             -> left click
 *  - one finger hold (>500 ms)  -> right click (then keeps dragging)
 *  - two finger drag            -> vertical wheel (up = scroll up)
 *
 * The first few pixels of a one-finger contact are swallowed so a tap never
 * nudges the remote cursor.
 */
suspend fun PointerInputScope.touchpad(c: KvmController) = awaitEachGesture {
    val down = awaitFirstDown(requireUnconsumed = false)
    val slop = viewConfiguration.touchSlop

    var prev = down.position
    var pointerCount = 1
    var totalMove = 0f
    val startedAt = down.uptimeMillis
    var longPressFired = false
    var scrolling = false
    var scrollAccum = 0f

    while (true) {
        val elapsed = SystemClock.uptimeMillis() - startedAt
        val mayLongPress = !longPressFired && !scrolling &&
            pointerCount == 1 && totalMove < slop && elapsed < LONG_PRESS_MS

        val event = if (mayLongPress) {
            val remaining = (LONG_PRESS_MS - elapsed).coerceAtLeast(1L)
            val e = withTimeoutOrNull(remaining) { awaitPointerEvent(PointerEventPass.Main) }
            if (e == null) {
                // finger held still long enough -> right click, keep tracking
                c.mouse.rightClick()
                longPressFired = true
                continue
            }
            e
        } else {
            awaitPointerEvent(PointerEventPass.Main)
        }

        val pressed = event.changes.filter { it.pressed }
        if (pressed.isEmpty()) break

        if (pressed.size != pointerCount) {
            pointerCount = pressed.size
            prev = centroid(pressed)
            scrollAccum = 0f
            continue
        }

        val cur = centroid(pressed)
        val delta = cur - prev
        prev = cur
        totalMove += delta.getDistance()

        if (pointerCount >= 2) {
            scrolling = true
            scrollAccum += delta.y
            val step = c.scrollStepPx.coerceAtLeast(8f)
            var guard = 0
            while (scrollAccum >= step && guard++ < 8) {
                c.mouse.wheel(-1)          // content follows the fingers
                scrollAccum -= step
            }
            while (scrollAccum <= -step && guard++ < 8) {
                c.mouse.wheel(1)
                scrollAccum += step
            }
        } else if (longPressFired || totalMove >= slop) {
            val s = c.sensitivity
            c.mouse.moveRelative(delta.x * s, delta.y * s)
        }

        event.changes.forEach { it.consume() }
    }

    val heldMs = SystemClock.uptimeMillis() - startedAt
    if (!scrolling && !longPressFired && totalMove < slop && heldMs < LONG_PRESS_MS) {
        c.mouse.leftClick()
    }
    if (scrolling) {
        scrollAccum = 0f
    }
}

private fun centroid(changes: List<PointerInputChange>): Offset {
    var x = 0f
    var y = 0f
    for (ch in changes) {
        x += ch.position.x
        y += ch.position.y
    }
    val n = changes.size.coerceAtLeast(1)
    return Offset(x / n, y / n)
}
