package com.kvmlite.app.hid

import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Drives the real HidMouse / HidKeyboard code against a temp file and asserts
 * the exact byte layout of the reports that would hit /dev/hidg0.
 */
class HidReportFormatTest {

    private lateinit var file: File
    private lateinit var writer: HidWriter
    private lateinit var mouse: HidMouse
    private lateinit var keyboard: HidKeyboard

    @Before
    fun setUp() {
        file = File.createTempFile("hidg-test", ".bin")
        writer = HidWriter(file.absolutePath, append = true)
        assertTrue("writer should open the temp node: ${writer.lastError}", writer.open())
        mouse = HidMouse(writer)
        mouse.reportLength = 4
        keyboard = HidKeyboard(writer)
    }

    @After
    fun tearDown() {
        writer.shutdown()
        file.delete()
    }

    /** Blocks until the writer thread has drained everything queued so far. */
    private fun drain(): ByteArray {
        val latch = CountDownLatch(1)
        writer.submit { latch.countDown() }
        assertTrue("writer did not drain", latch.await(3, TimeUnit.SECONDS))
        return file.readBytes()
    }

    @Test
    fun fourByteMouseReport() {
        mouse.moveRelative(3f, -2f)
        val b = drain()
        assertEquals("one report", 4, b.size)
        assertEquals(0, b[0].toInt())     // no buttons
        assertEquals(3, b[1].toInt())     // dx right
        assertEquals(-2, b[2].toInt())    // dy up
        assertEquals(0, b[3].toInt())     // wheel
    }

    @Test
    fun threeByteBootReport() {
        mouse.reportLength = 3
        mouse.moveRelative(1f, 1f)
        val b = drain()
        assertEquals(3, b.size)
        assertArrayEquals(byteArrayOf(0, 1, 1), b)
    }

    @Test
    fun eightByteExtendedReport() {
        mouse.reportLength = 8
        mouse.moveRelative(-5f, 7f)
        val b = drain()
        assertEquals(8, b.size)
        assertEquals(-5, b[1].toInt())
        assertEquals(7, b[2].toInt())
        assertArrayEquals(byteArrayOf(0, 0, 0, 0), b.copyOfRange(4, 8))
    }

    @Test
    fun wheelUsesByteThree() {
        mouse.wheel(-1)
        val b = drain()
        assertArrayEquals(byteArrayOf(0, 0, 0, -1), b)
    }

    @Test
    fun leftClickIsPressThenRelease() {
        mouse.leftClick()
        val b = drain()
        assertEquals(8, b.size)
        assertArrayEquals(byteArrayOf(1, 0, 0, 0), b.copyOfRange(0, 4))
        assertArrayEquals(byteArrayOf(0, 0, 0, 0), b.copyOfRange(4, 8))
    }

    @Test
    fun rightClickSetsBit1() {
        mouse.rightClick()
        val b = drain()
        assertArrayEquals(byteArrayOf(2, 0, 0, 0), b.copyOfRange(0, 4))
    }

    @Test
    fun subPixelMotionIsCarried() {
        mouse.moveRelative(0.6f, 0f)
        mouse.moveRelative(0.6f, 0f)
        val b = drain()
        assertEquals(4, b.size)
        assertEquals(1, b[1].toInt())
    }

    @Test
    fun largeDeltaIsChunkedTo127() {
        mouse.moveRelative(300f, 0f)
        val b = drain()
        assertEquals("127 + 127 + 46", 12, b.size)
        assertEquals(127, b[1].toInt())
        assertEquals(127, b[5].toInt())
        assertEquals(46, b[9].toInt())
    }

    @Test
    fun keyboardReportIsEightBytesModifierReservedSixKeys() {
        keyboard.typeText("hi", charDelayMs = 0L)
        val b = drain()
        assertEquals("press+release per char", 4 * 8, b.size)
        assertArrayEquals(byteArrayOf(0, 0, 0x0B, 0, 0, 0, 0, 0), b.copyOfRange(0, 8))   // 'h'
        assertArrayEquals(ByteArray(8), b.copyOfRange(8, 16))                            // released
        assertArrayEquals(byteArrayOf(0, 0, 0x0C, 0, 0, 0, 0, 0), b.copyOfRange(16, 24)) // 'i'
        assertArrayEquals(ByteArray(8), b.copyOfRange(24, 32))
    }

    @Test
    fun shiftedCharUsesLeftShiftModifier() {
        keyboard.typeText("A", charDelayMs = 0L)
        val b = drain()
        assertEquals(16, b.size)
        assertEquals(0x02, b[0].toInt())    // LShift
        assertEquals(0x04, b[2].toInt())    // 'a'
    }
}
