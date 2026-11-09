package com.kvmlite.app.net

import android.media.MediaCodec
import android.media.MediaFormat
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import android.view.Surface
import java.nio.ByteBuffer

/**
 * Persistent MediaCodec decoder that renders straight onto a Surface.
 *
 * Why this and not BitmapFactory: hardware H.264/HEVC decode on the Snapdragon
 * 675 writes decoded NV12 straight into the display pipeline. There is no
 * Bitmap allocation per frame (the JPEG path allocates ~3.7 MB per frame at
 * 720p), no Compose recomposition, and no texture upload.
 *
 * Latency choices:
 *  * `KEY_LOW_LATENCY` (API 30+) tells the codec not to hold frames back for
 *    reordering or rate smoothing;
 *  * async callbacks on a dedicated HandlerThread, so an arriving access unit
 *    is queued the moment a free input buffer exists;
 *  * a single-slot pending queue: if the network delivers faster than the
 *    decoder consumes, the older frame is dropped rather than queued, which
 *    keeps latency bounded instead of growing;
 *  * frames are dropped until the first IDR arrives, so a mid-GOP join never
 *    paints garbage on screen;
 *  * `setOnFrameRenderedListener` reports when the buffer actually reached the
 *    display, which is the only honest "draw done" timestamp available.
 *
 * All timestamps are CLOCK_MONOTONIC milliseconds (System.nanoTime), matching
 * the clock the ping/pong handshake offsets against the PC's CLOCK_MONOTONIC.
 */
class VideoDecoder(
    private val mime: String,
    private val onRendered: (seq: Long, recvMs: Long, decodeMs: Long, renderMs: Long) -> Unit,
    private val onSize: (Int, Int) -> Unit,
    private val onFatal: (String) -> Unit,
) {
    companion object {
        private const val TAG = "VideoDecoder"
        private const val MAX_INPUT_SIZE = 4 shl 20
        private const val RING = 256
        private fun nowMs(): Long = System.nanoTime() / 1_000_000
    }

    /** One access unit.  The network buffer is referenced, not copied, to keep
     *  the reader thread free of per-frame allocations (GC pauses show up as
     *  multi-frame latency spikes on this device). */
    private class Pending(
        val seq: Long,
        val data: ByteArray,
        val offset: Int,
        val length: Int,
        val ptsUs: Long,
    )

    private val lock = Object()
    private val thread = HandlerThread("kvm-decode").apply { start() }
    private val handler = Handler(thread.looper)

    private var codec: MediaCodec? = null
    private var surface: Surface? = null
    private var csd: ByteArray? = null
    private var width = 0
    private var height = 0
    private var pending: Pending? = null
    private var freeIndex = -1
    private var released = false
    private var seenKey = false
    private var renderCallbacks = 0L
    private var outputsSeen = 0L
    /** Some vendor codecs never call OnFrameRenderedListener; fall back to the
     *  time the decoded buffer was handed to the display. */
    private var renderFallback = false

    private val recvAt = LongArray(RING)
    private val decAt = LongArray(RING)

    private val isHevc = mime == "video/hevc"

    fun setSize(w: Int, h: Int) {
        synchronized(lock) {
            if (w > 0 && h > 0 && (w != width || h != height)) {
                width = w
                height = h
                if (codec == null) startLocked()
            }
        }
    }

    fun setSurface(s: Surface?) {
        synchronized(lock) {
            if (surface === s) return
            surface = s
            if (codec == null) startLocked()
        }
    }

    /** Hand one access unit (Annex-B, one per WebSocket frame) to the decoder. */
    fun submit(seq: Long, buf: ByteArray, offset: Int, length: Int) {
        if (released) return
        val recvMs = nowMs()
        synchronized(lock) {
            if (released) return
            if (!seenKey) {
                if (!AnnexB.hasKeyframe(buf, offset, length, isHevc)) return
                seenKey = true
                csd = AnnexB.parameterSets(buf, offset, length, isHevc) ?: csd
            } else if (csd == null) {
                csd = AnnexB.parameterSets(buf, offset, length, isHevc)
            }
            recvAt[(seq and 0xFF).toInt()] = recvMs
            val frame = Pending(seq, buf, offset, length, (seq and 0x7FFFFFFF) * 1000L)
            val c = codec
            if (c == null) {
                pending = frame
                startLocked()
                return
            }
            if (freeIndex >= 0) {
                val idx = freeIndex
                freeIndex = -1
                queueLocked(c, idx, frame)
            } else {
                // Newest wins: an older undecoded frame is worse than useless.
                pending = frame
            }
        }
    }

    fun release() {
        synchronized(lock) {
            released = true
            pending = null
            stopLocked()
        }
        thread.quitSafely()
    }

    // ------------------------------------------------------------------ codec
    private fun startLocked() {
        val s = surface ?: return
        val c0 = csd ?: return
        if (released || width <= 0 || codec != null) return
        val format = MediaFormat.createVideoFormat(mime, width, height)
        format.setByteBuffer("csd-0", ByteBuffer.wrap(c0))
        format.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, MAX_INPUT_SIZE)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            format.setInteger(MediaFormat.KEY_LOW_LATENCY, 1)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            format.setInteger(MediaFormat.KEY_OPERATING_RATE, 60)
            format.setInteger(MediaFormat.KEY_PRIORITY, 0)
        }
        val c = try {
            MediaCodec.createDecoderByType(mime)
        } catch (t: Throwable) {
            onFatal("no decoder for $mime: ${t.message}")
            return
        }
        try {
            c.setCallback(callback, handler)
            c.configure(format, s, null, 0)
            c.start()
            // Register after start(): some vendor codecs drop a listener that
            // was installed before the codec was running.
            c.setOnFrameRenderedListener({ _, ptsUs, nanoTime ->
                val seq = ptsUs / 1000
                val slot = (seq and 0xFF).toInt()
                val recv = recvAt[slot]
                val renderMs = nanoTime / 1_000_000
                if (renderCallbacks < 1 && Math.abs(renderMs - ptsUs / 1000) >= 3) {
                    Log.i(TAG, "frame-rendered timestamps available " +
                        "(delta=${renderMs - recv}ms)")
                }
                // Some vendor codecs echo the presentation timestamp back in
                // the nanoTime argument instead of the scan-out time. If it
                // tracks the pts, the value carries no timing information --
                // fall back to the buffer hand-off time.
                if (Math.abs(renderMs - ptsUs / 1000) < 3) {
                    renderFallback = true
                    return@setOnFrameRenderedListener
                }
                if (recv > 0 && renderMs - recv in 0..2000) {
                    renderCallbacks++
                    onRendered(seq, recv, decAt[slot], renderMs)
                }
            }, handler)
            codec = c
            Log.i(TAG, "decoder started: $mime ${width}x${height} csd=${c0.size}B " +
                "lowLatency=${Build.VERSION.SDK_INT >= Build.VERSION_CODES.R}")
            val p = pending
            pending = null
            if (p != null && freeIndex >= 0) {
                val idx = freeIndex
                freeIndex = -1
                queueLocked(c, idx, p)
            }
        } catch (t: Throwable) {
            Log.w(TAG, "configure failed: $t")
            try { c.release() } catch (_: Throwable) {}
            onFatal("configure failed: ${t.message}")
        }
    }

    private fun stopLocked() {
        val c = codec ?: return
        codec = null
        freeIndex = -1
        try { c.stop() } catch (_: Throwable) {}
        try { c.release() } catch (_: Throwable) {}
    }

    private fun queueLocked(c: MediaCodec, index: Int, frame: Pending) {
        try {
            val buf = c.getInputBuffer(index) ?: return
            buf.clear()
            if (buf.capacity() < frame.length) {
                Log.w(TAG, "input buffer too small: ${buf.capacity()} < ${frame.length}")
                c.queueInputBuffer(index, 0, 0, frame.ptsUs, 0)
                return
            }
            buf.put(frame.data, frame.offset, frame.length)
            c.queueInputBuffer(index, 0, frame.length, frame.ptsUs, 0)
        } catch (t: Throwable) {
            Log.w(TAG, "queueInputBuffer failed: $t")
        }
    }

    private val callback = object : MediaCodec.Callback() {
        override fun onInputBufferAvailable(c: MediaCodec, index: Int) {
            synchronized(lock) {
                if (released || c !== codec) return
                val p = pending
                if (p == null) freeIndex = index
                else {
                    pending = null
                    queueLocked(c, index, p)
                }
            }
        }

        override fun onOutputBufferAvailable(
            c: MediaCodec,
            index: Int,
            info: MediaCodec.BufferInfo,
        ) {
            if (info.size > 0) {
                // decode finished for this buffer; record it before handing the
                // buffer to the display
                decAt[((info.presentationTimeUs / 1000) and 0xFF).toInt()] = nowMs()
            }
            // render=true: hand it straight to the display. The frame-rendered
            // listener above reports the real scan-out instant.
            val seq = info.presentationTimeUs / 1000
            val slot = (seq and 0xFF).toInt()
            val handedOverMs = nowMs()
            try {
                c.releaseOutputBuffer(index, true)
            } catch (t: Throwable) {
                Log.w(TAG, "releaseOutputBuffer: $t")
            }
            outputsSeen++
            if (!renderFallback && renderCallbacks == 0L && outputsSeen == 90L) {
                renderFallback = true
                Log.w(TAG, "no OnFrameRenderedListener callbacks: timing render " +
                    "at buffer hand-off (adds up to one vsync of error)")
            }
            if (renderFallback) {
                val recv = recvAt[slot]
                if (recv > 0) onRendered(seq, recv, decAt[slot], handedOverMs)
            }
        }

        override fun onError(c: MediaCodec, e: MediaCodec.CodecException) {
            Log.w(TAG, "codec error: ${e.diagnosticInfo} (recoverable=${e.isRecoverable})")
            synchronized(lock) {
                if (codec === c) {
                    codec = null
                    freeIndex = -1
                }
            }
            try { c.stop() } catch (_: Throwable) {}
            try { c.release() } catch (_: Throwable) {}
            seenKey = false
            onFatal("codec error: ${e.message}")
        }

        override fun onOutputFormatChanged(c: MediaCodec, format: MediaFormat) {
            val w = format.getInteger(MediaFormat.KEY_WIDTH)
            val h = format.getInteger(MediaFormat.KEY_HEIGHT)
            val cw = format.getInteger("crop-right", w - 1) + 1
            val ch = format.getInteger("crop-bottom", h - 1) + 1
            Log.i(TAG, "output format: ${cw}x$ch")
            onSize(cw, ch)
        }
    }
}
