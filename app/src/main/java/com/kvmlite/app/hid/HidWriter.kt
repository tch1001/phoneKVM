package com.kvmlite.app.hid

import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException

/**
 * Single serialized writer for the USB HID gadget node (usually /dev/hidg0).
 *
 * All reports are queued onto one daemon thread so that multi-report sequences
 * (button press -> delay -> release, self-test, typing) cannot interleave with
 * touch-driven mouse deltas.
 *
 * The node must be a character device exported by the f_hid function with a
 * report descriptor matching the layouts in [HidMouse] / [HidKeyboard].
 * Every write() must be exactly one full report: the kernel rejects short
 * writes / splits reports otherwise.
 */
class HidWriter(
    /**
     * Gadget node. Overridable so the HID pipeline can be exercised on a device
     * where the configfs gadget is not up yet (dev builds / instrumentation).
     */
    var path: String = "/dev/hidg0",
    /** Debug only: create [path] as a regular file when it does not exist. */
    var createIfMissing: Boolean = false,
    /**
     * Only used by unit tests, where the "device" is a regular file: a char
     * device ignores O_APPEND, a regular file would otherwise truncate on
     * every open so earlier reports would be lost.
     */
    private val append: Boolean = false,
) {

    sealed class State {
        object Closed : State()
        object Open : State()
        data class Failed(val reason: String) : State()
    }

    private val exec = Executors.newSingleThreadExecutor { r ->
        Thread(r, "hidg-writer").apply { isDaemon = true }
    }

    private val lock = Any()
    private var out: java.io.OutputStream? = null
    private var sock: java.net.Socket? = null

    /**
     * When set, reports are sent to the hidbridge daemon instead of written to
     * [path]. Android refuses untrusted apps access to /dev/hidg* even with an
     * SELinux allow rule, so the daemon (which runs as root in its own domain)
     * is the supported route.
     */
    var socketAddress: java.net.InetSocketAddress? = null

    /** Report tag for the daemon: 'K' = keyboard, 'M' = mouse. */
    var tag: Byte = 'K'.code.toByte()

    private val _state = MutableStateFlow<State>(State.Closed)
    val state: StateFlow<State> = _state

    @Volatile
    var lastError: String? = null
        private set

    val isOpen: Boolean
        get() = synchronized(lock) { out != null }

    /** Opens the node. Returns false and records [lastError] on failure. */
    fun open(): Boolean = synchronized(lock) {
        closeLocked()

        val addr = socketAddress
        if (addr != null) {
            try {
                val s = java.net.Socket()
                s.tcpNoDelay = true
                s.connect(addr, 500)
                sock = s
                out = s.getOutputStream()
                lastError = null
                _state.value = State.Open
                return true
            } catch (e: Exception) {
                // Daemon not running (or not built into this ROM yet): fall back
                // to writing the node directly, which works where SELinux is
                // permissive or the node really is writable.
                Log.w(TAG, "hidbridge unreachable (${e.message}); using $path directly")
                socketAddress = null
            }
        }

        val f = File(path)
        if (!f.exists() && createIfMissing) {
            f.parentFile?.mkdirs()
            try { f.createNewFile() } catch (e: IOException) { /* fall through to the check below */ }
        }
        when {
            !f.exists() -> {
                fail("$path does not exist (is the f_hid gadget configured?)")
                false
            }
            !f.canWrite() -> {
                fail("$path is not writable by this uid (check SELinux / group ownership)")
                false
            }
            else -> try {
                out = FileOutputStream(f, append)
                lastError = null
                _state.value = State.Open
                true
            } catch (e: Exception) {
                fail("open $path failed: ${e.message}")
                false
            }
        }
    }

    fun close() {
        synchronized(lock) { closeLocked() }
        _state.value = State.Closed
    }

    private fun closeLocked() {
        try {
            out?.close()
        } catch (e: IOException) {
            Log.w(TAG, "close: ${e.message}")
        }
        try {
            sock?.close()
        } catch (e: IOException) {
            Log.w(TAG, "close socket: ${e.message}")
        }
        sock = null
        out = null
    }

    private fun fail(reason: String) {
        lastError = reason
        Log.e(TAG, reason)
        _state.value = State.Failed(reason)
        closeLocked()
    }

    /**
     * Queues work on the writer thread. Use for anything that writes more than
     * one report or that sleeps between reports.
     */
    fun submit(block: () -> Unit) {
        try {
            exec.execute {
                try {
                    block()
                } catch (t: Throwable) {
                    fail("writer task failed: ${t.message}")
                }
            }
        } catch (e: RejectedExecutionException) {
            Log.w(TAG, "writer shut down, dropping task")
        }
    }

    /**
     * Blocking single-report write. Safe from any thread; serialized by [lock].
     * A failed write closes the node so the UI can surface "unplugged host".
     */
    fun write(report: ByteArray): Boolean = synchronized(lock) {
        val o = out ?: return false
        try {
            if (sock != null) o.write(tag.toInt())
            o.write(report)
            o.flush()
            true
        } catch (e: IOException) {
            fail("write ${report.size}B failed: ${e.message}")
            false
        }
    }

    fun shutdown() {
        close()
        exec.shutdown()
    }

    companion object {
        private const val TAG = "HidWriter"
    }
}
