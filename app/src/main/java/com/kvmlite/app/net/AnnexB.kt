package com.kvmlite.app.net

/**
 * Minimal Annex-B helpers for the H.264 / HEVC access units the PC agent sends.
 *
 * Each WebSocket binary frame carries exactly one access unit (the agent emits
 * `-aud 1`), starting with an access-unit delimiter and, on keyframes, the
 * parameter sets. We need three things from it:
 *
 *  * the SPS/PPS blob for `MediaFormat` (`csd-0`), because MediaCodec in
 *    ByteBuffer mode refuses to configure an AVC/HEVC decoder without it;
 *  * a cheap "does this unit contain an IDR" check, so the first decodable
 *    frame after connect is not mistaken for a predicted one;
 *  * the NAL list itself, used only for the above.
 */
object AnnexB {

    /** (offset of NAL payload, length) for every start-code-delimited NAL. */
    fun nals(buf: ByteArray, offset: Int, length: Int): List<IntArray> {
        val out = ArrayList<IntArray>(8)
        var i = offset
        val end = offset + length
        var start = -1
        while (i + 3 < end) {
            if (buf[i].toInt() == 0 && buf[i + 1].toInt() == 0 && buf[i + 2].toInt() == 1) {
                if (start >= 0) addTrimmed(out, buf, start, i)
                start = i + 3
                i += 3
            } else {
                i++
            }
        }
        if (start >= 0 && start < end) addTrimmed(out, buf, start, end)
        return out
    }

    /** A 4-byte start code leaves a trailing 0x00 on the previous NAL: strip it. */
    private fun addTrimmed(out: ArrayList<IntArray>, buf: ByteArray, start: Int, end: Int) {
        var e = end
        while (e > start && buf[e - 1].toInt() == 0) e--
        if (e > start) out.add(intArrayOf(start, e - start))
    }

    private fun type(buf: ByteArray, off: Int, hevc: Boolean): Int =
        if (hevc) (buf[off].toInt() shr 1) and 0x3F
        else buf[off].toInt() and 0x1F

    /** Parameter sets joined with 4-byte start codes, or null if absent. */
    fun parameterSets(buf: ByteArray, offset: Int, length: Int, hevc: Boolean): ByteArray? {
        val sps = ArrayList<ByteArray>(3)
        for (n in nals(buf, offset, length)) {
            if (n[1] <= 0) continue
            val t = type(buf, n[0], hevc)
            val keep = if (hevc) (t == 32 || t == 33 || t == 34) else (t == 7 || t == 8)
            if (keep) {
                val out = ByteArray(n[1] + 4)
                out[3] = 1
                System.arraycopy(buf, n[0], out, 4, n[1])
                sps.add(out)
            }
        }
        if (sps.isEmpty()) return null
        var total = 0
        for (s in sps) total += s.size
        val blob = ByteArray(total)
        var p = 0
        for (s in sps) { System.arraycopy(s, 0, blob, p, s.size); p += s.size }
        return blob
    }

    /** True when the access unit contains an IDR (or CRA/BLA for HEVC). */
    fun hasKeyframe(buf: ByteArray, offset: Int, length: Int, hevc: Boolean): Boolean {
        for (n in nals(buf, offset, length)) {
            if (n[1] <= 0) continue
            val t = type(buf, n[0], hevc)
            if (hevc) {
                if (t in 16..21) return true
            } else if (t == 5) {
                return true
            }
        }
        return false
    }
}
