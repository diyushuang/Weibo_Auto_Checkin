package com.wcm.checkin

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs

/**
 * GIF 编码器 JVM 单元测试。
 *
 * Android 单元测试编译目标是 android.jar，没有 javax.imageio，所以这里自带一个
 * 迷你 GIF 解析器（结构解析 + LZW 解码），完整回验自己编码出的字节流：
 * 头部/逻辑屏幕/调色板/帧数/帧延时/像素逐点还原。
 */
class GifEncoderTest {

    // ---------------- 迷你 GIF 解码器（测试专用） ----------------

    private class Frame(val delayCs: Int, val rgb: IntArray)

    private var pendingDelay = 0

    private fun decode(gif: ByteArray): Pair<Pair<Int, Int>, List<Frame>> {
        assertEquals("GIF89a", String(gif.copyOfRange(0, 6), Charsets.US_ASCII))
        var p = 6
        fun u8(): Int = gif[p++].toInt() and 0xFF
        fun u16(): Int = u8() or (u8() shl 8)

        val width = u16()
        val height = u16()
        val packed = u8()
        u8(); u8()                                    // bg / aspect
        var palette = IntArray(0)
        if (packed and 0x80 != 0) {
            val n = 2 shl (packed and 7)
            palette = IntArray(n) { (u8() shl 16) or (u8() shl 8) or u8() }
        }
        val frames = ArrayList<Frame>()
        loop@ while (p < gif.size) {
            when (u8()) {
                0x21 -> {                            // 扩展块
                    when (u8()) {
                        0xF9 -> {                    // 图形控制：记录帧延时
                            u8()                     // block size(4)
                            u8()                     // flags
                            pendingDelay = u16()
                            u8(); u8()               // 透明索引 / 终止符
                        }
                        else -> {                    // 其它扩展（如 NETSCAPE 循环）：跳子块
                            while (true) {
                                val n = u8()
                                if (n == 0) break
                                p += n
                            }
                        }
                    }
                }
                0x2C -> {                            // 图像
                    u16(); u16()                     // left / top
                    val iw = u16()
                    val ih = u16()
                    val ipacked = u8()
                    if (ipacked and 0x80 != 0) {     // 局部色表（本编码器不产生，防御性跳过）
                        val n = 2 shl (ipacked and 7)
                        repeat(n) { u8(); u8(); u8() }
                    }
                    val minCodeSize = u8()
                    var data = ByteArray(0)
                    while (true) {
                        val n = u8()
                        if (n == 0) break
                        data += gif.copyOfRange(p, p + n)
                        p += n
                    }
                    val indices = lzwDecode(minCodeSize, data, iw * ih)
                    val rgb = IntArray(iw * ih) { palette[indices[it]] }
                    frames.add(Frame(pendingDelay, rgb))
                }
                0x3B -> break@loop                    // 结束符
            }
        }
        assertEquals("结束符应为最后一个字节", gif.size, p)
        return (width to height) to frames
    }

    /** 标准 GIF LZW 解码（LSB-first 位序，与编码器互为镜像）。 */
    private fun lzwDecode(minCodeSize: Int, data: ByteArray, expected: Int): IntArray {
        val clear = 1 shl minCodeSize
        val eoi = clear + 1
        var codeSize = minCodeSize + 1
        val seqs = Array(4096) { IntArray(0) }
        fun resetDict() {
            for (i in 0 until clear) seqs[i] = intArrayOf(i)
            for (i in clear until 4096) seqs[i] = IntArray(0)
        }
        resetDict()
        var next = eoi + 1

        var bitPos = 0
        fun nextCode(): Int {
            var code = 0
            for (i in 0 until codeSize) {
                val bit = (data[bitPos shr 3].toInt() ushr (bitPos and 7)) and 1
                code = code or (bit shl i)
                bitPos++
            }
            return code
        }

        val out = IntArray(expected)
        var written = 0
        var prev = -1
        while (written < expected) {
            val code = nextCode()
            when {
                code == clear -> {
                    resetDict()
                    next = eoi + 1
                    codeSize = minCodeSize + 1
                    prev = -1
                }
                code == eoi -> return out.copyOf(written)
                else -> {
                    val entry: IntArray = when {
                        prev < 0 -> seqs[code]
                        code < next -> seqs[code]
                        else -> seqs[prev] + seqs[prev][0]   // KwKwK 情形
                    }
                    assertTrue("解码出非法条目（LZW 实现有误）", entry.isNotEmpty())
                    for (v in entry) {
                        if (written < expected) out[written++] = v
                    }
                    if (prev >= 0 && next < 4096) {
                        seqs[next] = seqs[prev] + entry[0]
                        next++
                        if (next == (1 shl codeSize) && codeSize < 12) codeSize++
                    }
                    prev = code
                }
            }
        }
        return out
    }

    // ---------------- 用例 ----------------

    private fun solidFrame(w: Int, h: Int, argb: Int): GifEncoder.GifFrame {
        val px = IntArray(w * h) { argb }
        return GifEncoder.GifFrame(w, h, px)
    }

    @Test
    fun encodesTwoSolidFramesWithCorrectStructure() {
        val gif = GifEncoder.encode(
            listOf(
                solidFrame(8, 8, 0xFFCC0000.toInt()),
                solidFrame(8, 8, 0xFF0000CC.toInt()),
            ),
            delayCs = 8,
        )
        assertEquals(0x3B, gif[gif.size - 1].toInt() and 0xFF)

        val ((w, h), frames) = decode(gif)
        assertEquals(8, w)
        assertEquals(8, h)
        assertEquals("帧数应为 2", 2, frames.size)
        assertEquals("帧延时应为 8（1/100s）", 8, frames[0].delayCs)
        // 纯色帧：调色板均值精确，最近色映射应逐像素还原
        assertEquals(0xCC0000, frames[0].rgb[4 * 8 + 4])
        assertEquals(0x0000CC, frames[1].rgb[4 * 8 + 4])
    }

    @Test
    fun gradientFramesDecodeWithinPaletteTolerance() {
        val w = 64
        val h = 64
        fun gradientFrame(shift: Int): GifEncoder.GifFrame {
            val px = IntArray(w * h) { i ->
                val x = i % w
                0xFF000000.toInt() or
                    ((x * 4 + shift).coerceAtMost(255) shl 16) or
                    (x * 2 shl 8) or (x * 3 shr 2)
            }
            return GifEncoder.GifFrame(w, h, px)
        }
        val gif = GifEncoder.encode(listOf(gradientFrame(0), gradientFrame(20)), delayCs = 8)
        val ((dw, dh), frames) = decode(gif)
        assertEquals(64, dw)
        assertEquals(64, dh)
        assertEquals(2, frames.size)
        // 抽样 3 个点：256 色调色板 + ±3 抖动下误差应 ≤ 24/通道
        for (x in intArrayOf(8, 32, 56)) {
            val c = frames[1].rgb[32 * w + x]
            val expectR = x * 4 + 20
            val expectG = x * 2
            val expectB = x * 3 / 4
            assertTrue("R@x=$x 偏差过大: ${c shr 16 and 0xFF} vs $expectR",
                abs((c shr 16 and 0xFF) - expectR) <= 24)
            assertTrue(abs((c shr 8 and 0xFF) - expectG) <= 24)
            assertTrue(abs((c and 0xFF) - expectB) <= 24)
        }
    }
}
