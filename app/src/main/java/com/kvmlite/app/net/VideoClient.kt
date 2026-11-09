package com.kvmlite.app.net

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import android.view.Surface
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import org.json.JSONObject
import java.util.concurrent.TimeUnit

enum class LinkState { IDLE, CONNECTING, CONNECTED, RECONNECTING, ERROR, CLOSED }

data class StreamStats(
    val state: LinkState = LinkState.IDLE,
    val url: String = "",
    val fps: Float = 0f,
    val kbps: Int = 0,
    val frames: Long = 0,
    val width: Int = 0,
    val height: Int = 0,
    val note: String = "",
    val codec: String = "",
    /** End-to-end latency of the last frame, capture -> scan-out, milliseconds. */
    val latencyMs: Int = 0,
    /** Median end-to-end latency over the last second. */
    val latencyAvgMs: Int = 0,
    /** Phone-side decode time of the last frame. */
    val decodeMs: Float = 0f,
    /** True when frames are drawn by MediaCodec straight onto a SurfaceView. */
    val surfaceMode: Boolean = false,
)

/**
 * A decoded JPEG frame plus the timestamps needed for latency accounting.
 * All timestamps are CLOCK_MONOTONIC milliseconds (System.nanoTime).
 */
data class VideoFrame(
    val bitmap: Bitmap,
    val seq: Long,
    val recvMs: Long,
    val decodeDoneMs: Long,
)

/**
 * Video client for the kvm-lite PC agent (binary-frame-v1).
 *
 * Binary message = 24-byte little-endian header + payload:
 *
 *   off size field
 *   0   4    magic "KVM1"
 *   4   1    protocol version (1)
 *   5   1    codec (1 = JPEG, 2 = H.264 Annex-B AU, 3 = HEVC Annex-B AU)
 *   6   2    flags (bit0 = payload identical to previous frame)
 *   8   4    frame sequence
 *   12  4    width
 *   16  4    height
 *   20  4    capture timestamp, ms (PC CLOCK_MONOTONIC)
 *   24  ...  payload
 *
 * JPEG goes through BitmapFactory + Compose; H.264/HEVC goes through a
 * persistent [VideoDecoder] that renders onto a Surface. Both paths report
 * per-frame latency back to the agent ({"t":"lat",...}) which is what the
 * agent's `--lat-log` breakdown is built from.
 *
 * Latency accounting spans two clocks: the phone's System.nanoTime() and the
 * agent's CLOCK_MONOTONIC. They are tied together by a ping/pong handshake
 * every two seconds ({"t":"ping"} / {"t":"pong","pc_ms":...}); over the USB
 * tunnel the round trip is well under a millisecond, so the derived offset is
 * sub-millisecond too.
 */
class VideoClient(
    private val onFrame: (VideoFrame) -> Unit,
    private val onStats: (StreamStats) -> Unit,
) {
    companion object {
        private const val TAG = "VideoClient"
        private const val MAX_DECODE_FPS = 60
        private const val HEADER_SIZE = 24
        private const val CODEC_JPEG = 1
        private const val CODEC_H264 = 2
        private const val CODEC_HEVC = 3
        private const val FLAG_DUPLICATE = 0x01
        private const val PING_INTERVAL_MS = 2000L
        private const val RING = 256
        private val MAGIC = byteArrayOf('K'.code.toByte(), 'V'.code.toByte(), 'M'.code.toByte(), '1'.code.toByte())
        private fun nowMs(): Long = System.nanoTime() / 1_000_000
        private fun wrap32(v: Long): Long = v and 0xFFFFFFFFL
        private fun signed32(v: Long): Long {
            val w = wrap32(v)
            return if (w >= (1L shl 31)) w - (1L shl 32) else w
        }
    }

    private val http = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .writeTimeout(5, TimeUnit.SECONDS)
        .pingInterval(10, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    private val main = Handler(Looper.getMainLooper())

    @Volatile private var ws: WebSocket? = null
    @Volatile private var running = false

    private var url = ""
    private var attempt = 0
    private var state = LinkState.IDLE
    private var note = ""
    private var codecName = ""
    private var streamCodec = 0

    private var totalFrames = 0L
    private var winFrames = 0
    private var winBytes = 0L
    private var lastTick = 0L
    private var fps = 0f
    private var kbps = 0

    private var width = 0
    private var height = 0
    private var lastDecodeAt = 0L

    // latency
    private var clockOffset = 0L          // pcMs = phoneMs + clockOffset
    private var pingPendingMs = 0L
    private var pingSentAt = 0L
    private var lastLatencyMs = 0
    private var lastLatencyAvg = 0
    private var lastDecodeMs = 0f
    private var latRejected = 0L
    private val latWindow = ArrayDeque<Int>()

    // per-frame rings, indexed by seq & 0xFF
    private val recvRing = LongArray(RING)
    private val decRing = LongArray(RING)
    private val capRing = LongArray(RING)

    // h264/hevc
    private var decoder: VideoDecoder? = null
    private var pendingSurface: Surface? = null

    private val ticker = object : Runnable {
        override fun run() {
            if (!running) return
            val now = SystemClock.elapsedRealtime()
            val dt = (now - lastTick).coerceAtLeast(1L)
            fps = winFrames * 1000f / dt
            kbps = (winBytes * 8 / dt).toInt()
            winFrames = 0
            winBytes = 0
            lastTick = now
            val lat = latWindow.toList()
            if (lat.isNotEmpty()) {
                lastLatencyAvg = lat.sorted()[lat.size / 2]
                latWindow.clear()
            }
            publish()
            main.postDelayed(this, 1000L)
        }
    }

    private val pinger = object : Runnable {
        override fun run() {
            if (!running) return
            val sock = ws
            if (sock != null) {
                pingPendingMs = nowMs()
                pingSentAt = SystemClock.elapsedRealtime()
                sock.send("{\"t\":\"ping\",\"at_ms\":${wrap32(pingPendingMs)}}")
            }
            main.postDelayed(this, PING_INTERVAL_MS)
        }
    }

    fun connect(rawUrl: String) {
        main.post {
            val normalized = normalize(rawUrl)
            if (normalized == null) {
                setState(LinkState.ERROR, "bad url: $rawUrl")
                return@post
            }
            running = true
            attempt = 0
            url = normalized
            note = ""
            totalFrames = 0
            width = 0
            height = 0
            streamCodec = 0
            codecName = ""
            clockOffset = 0L
            lastLatencyMs = 0
            releaseDecoder()
            openSocket()
            main.removeCallbacks(ticker)
            lastTick = SystemClock.elapsedRealtime()
            winFrames = 0
            winBytes = 0
            latWindow.clear()
            main.postDelayed(ticker, 1000L)
            main.removeCallbacks(pinger)
            main.postDelayed(pinger, 200L)
        }
    }

    fun disconnect() {
        main.post {
            running = false
            main.removeCallbacks(ticker)
            main.removeCallbacks(pinger)
            attempt = 0
            ws?.close(1000, "client disconnect")
            ws = null
            releaseDecoder()
            setState(LinkState.CLOSED, "disconnected")
        }
    }

    fun shutdown() {
        running = false
        main.removeCallbacks(ticker)
        main.removeCallbacks(pinger)
        main.removeCallbacksAndMessages(null)
        ws?.cancel()
        ws = null
        releaseDecoder()
    }

    /** SurfaceView surface for hardware-rendered streams. */
    fun attachSurface(surface: Surface?) {
        pendingSurface = surface
        decoder?.setSurface(surface)
    }

    /**
     * JPEG path only: the Compose frame clock reports when the bitmap was first
     * drawn, which completes the latency chain for that pipeline.
     */
    fun noteDrawn(seq: Long, drawMs: Long) {
        if (seq < 0) return
        val slot = (seq and 0xFF).toInt()
        val recv = recvRing[slot]
        if (recv <= 0) return
        finishFrame(seq, recv, decRing[slot], drawMs)
    }

    private fun releaseDecoder() {
        decoder?.release()
        decoder = null
    }

    private fun openSocket() {
        if (!running) return
        setState(
            if (attempt == 0) LinkState.CONNECTING else LinkState.RECONNECTING,
            if (attempt == 0) "connecting" else "retry #$attempt",
        )
        val request = try {
            Request.Builder().url(url).build()
        } catch (e: IllegalArgumentException) {
            setState(LinkState.ERROR, "bad url: ${e.message}")
            running = false
            return
        }
        ws?.cancel()
        ws = http.newWebSocket(request, listener)
    }

    private fun scheduleReconnect(reason: String) {
        if (!running) return
        attempt++
        val delay = (500L shl (attempt - 1).coerceAtMost(4)).coerceAtMost(8000L)
        setState(LinkState.RECONNECTING, "$reason - retry in ${delay}ms")
        main.postDelayed({ if (running) openSocket() }, delay)
    }

    private val listener = object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            attempt = 0
            setState(LinkState.CONNECTED, "connected")
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            winBytes += text.length
            try {
                val json = JSONObject(text)
                when (json.optString("t")) {
                    "hello" -> {
                        val w = json.optInt("width", 0)
                        val h = json.optInt("height", 0)
                        if (w > 0 && h > 0) {
                            width = w
                            height = h
                        }
                        val c = json.optString("codec", "jpeg")
                        codecName = if (c == "h264") "video/avc" else if (c == "hevc") "video/hevc" else c
                        setState(
                            LinkState.CONNECTED,
                            "hello v${json.optInt("protocol", 0)} $c ${w}x$h",
                        )
                    }
                    "pong" -> onPong(json)
                    "error" -> setState(state, "server: ${json.optString("msg")}")
                    "bye" -> webSocket.close(1000, "server bye")
                    else -> Unit // stats / ack
                }
            } catch (e: Exception) {
                Log.w(TAG, "bad text message: ${e.message}")
            }
        }

        override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
            winBytes += bytes.size
            handleBinary(bytes.toByteArray())
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            Log.w(TAG, "ws failure: $t")
            ws = null
            releaseDecoder()
            scheduleReconnect(t.message ?: t.javaClass.simpleName)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            ws = null
            releaseDecoder()
            if (running) scheduleReconnect("server closed ($code $reason)")
            else setState(LinkState.CLOSED, "closed")
        }
    }

    // ---------------------------------------------------------------- clocks
    private fun onPong(json: JSONObject) {
        if (pingPendingMs == 0L) return
        val rtt = SystemClock.elapsedRealtime() - pingSentAt
        val pcMs = json.optLong("pc_ms", 0L)
        if (pcMs == 0L) return
        clockOffset = signed32(wrap32(pcMs) - wrap32(pingPendingMs + rtt / 2))
        pingPendingMs = 0L
        if (clockOffset % 60L == 0L) Log.v(TAG, "pong rtt=${rtt}ms offset=$clockOffset")
    }

    private fun toPcMs(phoneMs: Long): Long = wrap32(phoneMs + clockOffset)

    // -------------------------------------------------------------- binaries
    private fun handleBinary(msg: ByteArray) {
        var data = msg
        var w = 0
        var h = 0
        var seq = -1L
        var captureMs = 0L

        if (msg.size >= HEADER_SIZE && hasMagic(msg)) {
            val version = msg[4].toInt() and 0xFF
            val codec = msg[5].toInt() and 0xFF
            val flags = (msg[6].toInt() and 0xFF) or ((msg[7].toInt() and 0xFF) shl 8)
            seq = le32(msg, 8).toLong() and 0xFFFFFFFFL
            w = le32(msg, 12)
            h = le32(msg, 16)
            captureMs = le32(msg, 20).toLong() and 0xFFFFFFFFL
            if (version != 1) {
                Log.w(TAG, "unsupported protocol version $version")
                return
            }
            if (codec != CODEC_JPEG) {
                if (codec != CODEC_H264 && codec != CODEC_HEVC) {
                    Log.w(TAG, "unsupported codec $codec")
                    return
                }
                streamCodec = codec
                if (w > 0 && h > 0) {
                    width = w
                    height = h
                }
                totalFrames++
                winFrames++
                val slot = (seq and 0xFF).toInt()
                capRing[slot] = captureMs
                ensureDecoder(codec, w, h)
                decoder?.submit(seq, msg, HEADER_SIZE, msg.size - HEADER_SIZE)
                return
            }
            data = msg.copyOfRange(HEADER_SIZE, msg.size)
            if (flags and FLAG_DUPLICATE != 0) {
                // Host says the pixels are unchanged: keep the current bitmap.
                totalFrames++
                winFrames++
                if (w > 0) { width = w; height = h }
                return
            }
        } else if (data.size < 4 || data[0] != 0xFF.toByte() || data[1] != 0xD8.toByte()) {
            Log.w(TAG, "ignoring ${msg.size}B binary message (not KVM1, not JPEG)")
            return
        }

        val now = SystemClock.elapsedRealtime()
        if (now - lastDecodeAt < 1000L / MAX_DECODE_FPS) return
        lastDecodeAt = now

        val recvMs = nowMs()
        val t0 = System.nanoTime()
        val bitmap = try {
            BitmapFactory.decodeByteArray(data, 0, data.size)
        } catch (t: Throwable) {
            Log.w(TAG, "jpeg decode failed: ${t.message}")
            null
        }
        if (bitmap == null) return
        val decodeDoneMs = nowMs()
        val decodeMs = (decodeDoneMs - recvMs).toFloat()

        totalFrames++
        winFrames++
        if (w <= 0 || h <= 0) {
            w = bitmap.width
            h = bitmap.height
        }
        width = w
        height = h
        lastDecodeMs = decodeMs
        if (seq >= 0) {
            val slot = (seq and 0xFF).toInt()
            recvRing[slot] = recvMs
            decRing[slot] = decodeDoneMs
            capRing[slot] = captureMs
        }
        onFrame(VideoFrame(bitmap, seq, recvMs, decodeDoneMs))
    }

    private fun ensureDecoder(codec: Int, w: Int, h: Int) {
        val mime = if (codec == CODEC_HEVC) "video/hevc" else "video/avc"
        val d = decoder
        if (d != null && codecName == mime) {
            if (w > 0 && h > 0) d.setSize(w, h)
            return
        }
        decoder?.release()
        codecName = mime
        val nd = VideoDecoder(
            mime = mime,
            onRendered = { s, recv, decAbs, rend -> finishFrame(s, recv, decAbs, rend) },
            onSize = { gw, gh -> main.post { width = gw; height = gh; publish() } },
            onFatal = { reason ->
                Log.w(TAG, "decoder fatal: $reason")
                main.post { setState(state, "decoder: $reason") }
            },
        )
        decoder = nd
        if (w > 0 && h > 0) nd.setSize(w, h)
        nd.setSurface(pendingSurface)
    }

    /** Common tail of both pipelines: account for the frame and report it. */
    private fun finishFrame(seq: Long, recvMs: Long, decodeDoneMs: Long, renderMs: Long) {
        val slot = (seq and 0xFF).toInt()
        val capture = capRing[slot]
        if (capture == 0L) return
        val total = signed32(wrap32(toPcMs(renderMs)) - wrap32(capture))
        if (total < -100 || total > 5000) {
            if (latRejected++ % 60 == 0L) {
                Log.w(TAG, "lat rejected: total=$total off=$clockOffset recv=$recvMs " +
                    "rend=$renderMs cap=$capture")
            }
            return
        }
        lastLatencyMs = total.toInt()
        latWindow.addLast(lastLatencyMs)
        if (seq % 120 == 0L) {
            Log.d(TAG, "lat=${total}ms recv->dec=${decodeDoneMs - recvMs}ms " +
                "dec->rend=${renderMs - decodeDoneMs}ms")
        }
        if (streamCodec == CODEC_JPEG || streamCodec == 0) {
            lastDecodeMs = (decodeDoneMs - recvMs).coerceAtLeast(0).toFloat()
        }
        val sock = ws ?: return
        sock.send(
            "{\"t\":\"lat\",\"seq\":$seq,\"recv\":${wrap32(toPcMs(recvMs))}," +
                "\"dec\":${wrap32(toPcMs(decodeDoneMs))},\"rend\":${wrap32(toPcMs(renderMs))}," +
                "\"off\":$clockOffset,\"tot\":$total}"
        )
    }

    private fun hasMagic(b: ByteArray): Boolean =
        b[0] == MAGIC[0] && b[1] == MAGIC[1] && b[2] == MAGIC[2] && b[3] == MAGIC[3]

    private fun le32(b: ByteArray, off: Int): Int =
        (b[off].toInt() and 0xFF) or
            ((b[off + 1].toInt() and 0xFF) shl 8) or
            ((b[off + 2].toInt() and 0xFF) shl 16) or
            ((b[off + 3].toInt() and 0xFF) shl 24)

    private fun publish() = onStats(
        StreamStats(
            state = state,
            url = url,
            fps = fps,
            kbps = kbps,
            frames = totalFrames,
            width = width,
            height = height,
            note = note,
            codec = codecName.removePrefix("video/"),
            latencyMs = lastLatencyMs,
            latencyAvgMs = lastLatencyAvg,
            decodeMs = lastDecodeMs,
            surfaceMode = streamCodec == CODEC_H264 || streamCodec == CODEC_HEVC,
        )
    )

    private fun setState(newState: LinkState, newNote: String) {
        state = newState
        note = newNote
        publish()
    }

    private fun normalize(raw: String): String? {
        var s = raw.trim()
        if (s.isEmpty()) return null
        s = when {
            s.startsWith("ws://") || s.startsWith("wss://") -> s
            s.startsWith("http://") -> "ws://" + s.removePrefix("http://")
            s.startsWith("https://") -> "wss://" + s.removePrefix("https://")
            else -> "ws://$s"
        }
        return try {
            Request.Builder().url(s).build()
            s
        } catch (e: IllegalArgumentException) {
            null
        }
    }
}
