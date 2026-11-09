package com.kvmlite.app

import android.graphics.Bitmap
import android.view.KeyEvent
import android.view.Surface
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import com.kvmlite.app.hid.HidKeyboard
import com.kvmlite.app.hid.HidMouse
import com.kvmlite.app.hid.HidSelfTest
import com.kvmlite.app.hid.HidWriter
import com.kvmlite.app.net.StreamStats
import com.kvmlite.app.net.VideoClient
import com.kvmlite.app.net.VideoFrame
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

/**
 * Glue between the video stream, the touch surface and the USB HID gadget.
 * Holds all UI state as Compose snapshot state so the screen stays dumb.
 */
class KvmController {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    // The gadget exposes two HID functions: hid.usb0 is the keyboard
    // interface, hid.usb1 the mouse. Each needs its own fd - a mouse report
    // written to the keyboard node is dropped by the host.
    private val kbdWriter = HidWriter(path = "/dev/hidg0")
    private val mouseWriter = HidWriter(path = "/dev/hidg1")
    val mouse = HidMouse(mouseWriter)
    val keyboard = HidKeyboard(kbdWriter)

    var sensitivity by mutableFloatStateOf(1.6f)
    var scrollStepPx by mutableFloatStateOf(48f)
    var serverUrl by mutableStateOf("ws://127.0.0.1:8080/ws")
    var textFieldFocused by mutableStateOf(false)
    var typedText by mutableStateOf("hello world")

    var hidEnabled by mutableStateOf(false)
        private set
    var hidStatus by mutableStateOf("HID closed")
        private set

    var reportLength by mutableStateOf(4)
        private set

    private val _frame = MutableStateFlow<VideoFrame?>(null)
    val frame: StateFlow<VideoFrame?> = _frame

    private val _stats = MutableStateFlow(StreamStats())
    val stats: StateFlow<StreamStats> = _stats

    private val video = VideoClient(
        onFrame = { f -> _frame.value = f },
        onStats = { s -> _stats.value = s },
    )

    init {
        scope.launch { kbdWriter.state.collect { refreshHidState() } }
        scope.launch { mouseWriter.state.collect { refreshHidState() } }
    }

    private fun refreshHidState() {
        val k = kbdWriter.state.value
        val m = mouseWriter.state.value
        hidStatus = when {
            k is HidWriter.State.Failed -> "HID error: keyboard ${k.reason}"
            m is HidWriter.State.Failed -> "HID error: mouse ${m.reason}"
            k is HidWriter.State.Open && m is HidWriter.State.Open -> "HID open: keyboard + mouse"
            k is HidWriter.State.Open -> "HID: keyboard only (mouse failed)"
            m is HidWriter.State.Open -> "HID: mouse only (keyboard failed)"
            else -> "HID closed"
        }
        hidEnabled = k is HidWriter.State.Open && m is HidWriter.State.Open
    }

    // ---- video -------------------------------------------------------------

    fun connect() {
        _stats.value = StreamStats(state = com.kvmlite.app.net.LinkState.CONNECTING, url = serverUrl)
        video.connect(serverUrl)
        // The HID gadget is the point of the app: open it with the stream so
        // touch input works without hunting for a toggle.
        setHid(true)
    }

    fun disconnect() {
        video.disconnect()
        _frame.value = null
    }

    /** SurfaceView lifecycle -> hardware decoder output surface. */
    fun onSurface(surface: Surface?) {
        video.attachSurface(surface)
    }

    /** Compose reports the frame clock tick that first displayed a JPEG frame. */
    fun onFrameDrawn(seq: Long, drawMs: Long) {
        video.noteDrawn(seq, drawMs)
    }

    // ---- hid ---------------------------------------------------------------

    fun setHidPath(path: String) {
        kbdWriter.path = path
        kbdWriter.createIfMissing = true
        // hidgN is the keyboard; the mouse sits on the next node.
        mouseWriter.path = path.replace(Regex("hidg(\\d+)$")) { m ->
            "hidg" + (m.groupValues[1].toInt() + 1)
        }
        mouseWriter.createIfMissing = true
    }

    fun setHid(enabled: Boolean) {
        if (enabled) {
            kbdWriter.open()
            mouseWriter.open()
            refreshHidState()
        } else {
            keyboard.releaseAll()
            mouse.releaseAll()
            kbdWriter.close()
            mouseWriter.close()
            hidEnabled = false
        }
    }

    fun setMouseReportLength(len: Int) {
        reportLength = len.coerceIn(3, 8)
        mouse.reportLength = reportLength
    }

    fun selfTest() {
        HidSelfTest.run(mouse, keyboard)
    }

    fun typeText(text: String) {
        if (text.isNotEmpty()) keyboard.typeText(text)
    }

    fun releaseAll() {
        keyboard.releaseAll()
        mouse.releaseAll()
    }

    /**
     * Hardware keyboard -> USB HID. Returns true when consumed.
     * Skipped while a text field has focus (the user is typing into the local
     * UI) and when the gadget is closed.
     */
    fun handleKeyEvent(event: KeyEvent): Boolean {
        if (!hidEnabled || textFieldFocused) return false
        if (event.keyCode == KeyEvent.KEYCODE_BACK) return false   // keep a way out
        return when (event.action) {
            KeyEvent.ACTION_DOWN ->
                if (event.repeatCount > 0) keyboard.handles(event.keyCode)
                else keyboard.keyDown(event.keyCode)
            KeyEvent.ACTION_UP -> keyboard.keyUp(event.keyCode)
            else -> false
        }
    }

    fun onPause() {
        releaseAll()
    }

    fun shutdown() {
        releaseAll()
        video.shutdown()
        kbdWriter.shutdown()
        mouseWriter.shutdown()
        scope.cancel()
    }
}
