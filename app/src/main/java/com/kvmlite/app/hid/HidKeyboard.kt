package com.kvmlite.app.hid

/**
 * Boot-protocol keyboard reports (8 bytes, no report ID):
 *   [0] modifier bitmap: bit0 LCtrl, bit1 LShift, bit2 LAlt, bit3 LGui,
 *       bit4 RCtrl, bit5 RShift, bit6 RAlt, bit7 RGui
 *   [1] reserved (always 0)
 *   [2..7] up to six pressed HID usage IDs (0 = empty slot)
 *
 * Usage IDs are USB HID Usage Tables, Keyboard/Keypad page 0x07: 0x04 = 'a',
 * 0x28 = Enter, 0x29 = Esc, 0x2C = Space, 0xE0.. = modifiers-as-keys.
 *
 * The host performs key repeat, so we only send state changes.
 */
class HidKeyboard(private val writer: HidWriter) {

    private var mods = 0

    /** Insertion-ordered so the six-key rollover drops the oldest key last. */
    private val keys = LinkedHashSet<Int>()

    fun handles(keyCode: Int): Boolean =
        HidKeyMap.modifierBit(keyCode) != null || HidKeyMap.usageOf(keyCode) != null

    /** Returns true if the key was mapped and a report was queued. */
    fun keyDown(keyCode: Int): Boolean {
        val mod = HidKeyMap.modifierBit(keyCode)
        val usage = HidKeyMap.usageOf(keyCode)
        if (mod == null && usage == null) return false
        writer.submit {
            if (mod != null) {
                mods = mods or mod
            } else if (usage != null) {
                if (keys.size >= 6) keys.remove(keys.first())
                keys.add(usage)
            }
            send()
        }
        return true
    }

    fun keyUp(keyCode: Int): Boolean {
        val mod = HidKeyMap.modifierBit(keyCode)
        val usage = HidKeyMap.usageOf(keyCode)
        if (mod == null && usage == null) return false
        writer.submit {
            if (mod != null) {
                mods = mods and mod.inv()
            } else if (usage != null) {
                keys.remove(usage)
            }
            send()
        }
        return true
    }

    fun releaseAll() {
        writer.submit {
            mods = 0
            keys.clear()
            send()
        }
    }

    /**
     * Types [text] on the remote host using a US layout. Runs fully inside the
     * writer thread so the reports stay ordered; a long string therefore blocks
     * queued mouse motion (keep strings short, or split them).
     */
    fun typeText(text: String, charDelayMs: Long = 14L) {
        writer.submit {
            for (ch in text) {
                val mapped = HidKeyMap.charToUsage(ch) ?: continue
                val (usage, shift) = mapped
                mods = shift
                send(keys = intArrayOf(usage))
                Thread.sleep(charDelayMs)
                mods = 0
                send(keys = IntArray(0))
                Thread.sleep(charDelayMs)
            }
        }
    }

    /** Presses [combo] of usages together (e.g. Ctrl+C), then releases. */
    fun combo(modifier: Int, usage: Int, holdMs: Long = 30L) {
        writer.submit {
            mods = modifier
            send(keys = intArrayOf(usage))
            Thread.sleep(holdMs)
            mods = 0
            send(keys = IntArray(0))
        }
    }

    private fun send(keys: IntArray = this.keys.toIntArray()) {
        val report = ByteArray(8)
        report[0] = mods.toByte()
        report[1] = 0
        for (i in 0 until 6) {
            report[2 + i] = if (i < keys.size) keys[i].toByte() else 0
        }
        writer.write(report)
    }
}
