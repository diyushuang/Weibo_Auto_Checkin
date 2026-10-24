package com.wcm.checkin

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import javax.imageio.ImageIO
import kotlin.math.abs

/**
 * GIF 编码器 JVM 单元测试：用 JDK 自带的 ImageIO 解码器验证我们编码的字节流
 * 结构正确、可被标准 GIF 解码器还原（头部/尺寸/帧数/像素精度）。
 */
class GifEncoderTest {

    private fun solidFrame(w: Int, h: Int, argb: Int): GifEncoder.GifFrame {
        val px = IntArray(w * h) { argb }
        return GifEncoder.GifFrame(w, h, px)
    }

    private fun readerOf(gif: ByteArray): javax.imageio.ImageReader {
        val readers = ImageIO.getImageReadersByFormatName("gif")
        assertTrue("JVM 没有 GIF 解码器", readers.hasNext())
        val reader = readers.next()
        reader.input = ImageIO.createImageInputStream(ByteArrayInputStream(gif))
        return reader
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
        // 头部与结束符
        assertEquals("GIF89a", String(gif.copyOfRange(0, 6), Charsets.US_ASCII))
        assertEquals(0x3B, gif[gif.size - 1].toInt() and 0xFF)
        // 逻辑屏幕尺寸（小端）
        assertEquals(8, gif[6].toInt() and 0xFF)
        assertEquals(0, gif[7].toInt() and 0xFF)
        assertEquals(8, gif[8].toInt() and 0xFF)
        assertEquals(0, gif[9].toInt() and 0xFF)

        val reader = readerOf(gif)
        assertEquals("帧数应为 2", 2, reader.getNumImages(true))
        val f0 = reader.read(0)
        val f1 = reader.read(1)
        assertEquals(8, f0.width)
        assertEquals(8, f0.height)
        // 纯色帧的调色板均值精确，最近色映射应逐像素还原
        assertEquals(0xFFCC0000.toInt(), f0.getRGB(4, 4))
        assertEquals(0xFF0000CC.toInt(), f1.getRGB(4, 4))
    }

    @Test
    fun gradientFramesDecodeWithinPaletteTolerance() {
        val w = 64
        val h = 64
        fun gradientFrame(shift: Int): GifEncoder.GifFrame {
            val px = IntArray(w * h) { i ->
                val x = i % w
                0xFF000000.toInt() or ((x * 4 + shift).coerceAtMost(255) shl 16) or
                    (x * 2 shl 8) or (x * 3 shr 2)
            }
            return GifEncoder.GifFrame(w, h, px)
        }
        val gif = GifEncoder.encode(
            listOf(gradientFrame(0), gradientFrame(20)),
            delayCs = 8,
        )
        val reader = readerOf(gif)
        assertEquals(2, reader.getNumImages(true))
        val f1 = reader.read(1)
        assertEquals(64, f1.width)
        // 抽样 3 个点：256 色调色板 + ±3 抖动下误差应 ≤ 24/通道
        for (x in intArrayOf(8, 32, 56)) {
            val expectR = x * 4 + 20
            val expectG = x * 2
            val expectB = x * 3 / 4
            val c = f1.getRGB(x, 32)
            assertTrue(
                "R@x=$x 偏差过大: ${(c shr 16 and 0xFF)} vs $expectR",
                abs((c shr 16 and 0xFF) - expectR) <= 24,
            )
            assertTrue(abs((c shr 8 and 0xFF) - expectG) <= 24)
            assertTrue(abs((c and 0xFF) - expectB) <= 24)
        }
    }
}
