package com.wcm.checkin

import java.io.ByteArrayOutputStream
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt

/**
 * 纯 Kotlin GIF89a 编码器（零依赖，替代已退役的 ffmpeg-kit）。
 *
 * 口径对齐《抓取流程交接文档》的 ffmpeg 滤镜链：
 *   fps=12 + 长边 640（由调用方缩放）+ 256 色调色板（中值切分）+ Bayer 抖动。
 *
 * 性能：最近色查找用 15bit 量化缓存（32K 项），单帧 640x480 索引约 30 万次
 * 数组访问；调色板从全部帧抽样 ~3 万像素构建，单次几十毫秒级。
 */
object GifEncoder {

    /** frames 需同尺寸；delayCs 为 1/100 秒单位的帧间隔。 */
    fun encode(frames: List<android.graphics.Bitmap>, delayCs: Int): ByteArray {
        require(frames.isNotEmpty()) { "没有帧" }
        val w = frames[0].width
        val h = frames[0].height
        val palette = buildPalette(frames)
        val out = ByteArrayOutputStream(64 * 1024)

        // ---- 头部：GIF89a + 全局颜色表 ----
        writeAscii(out, "GIF89a")
        writeShortLE(out, w)
        writeShortLE(out, h)
        out.write(0xF7)                       // GCT=1, 8bit 色彩分辨率, 256 色
        out.write(0)                          // 背景色索引
        out.write(0)                          // 像素宽高比
        for (c in palette) {
            out.write(c shr 16 and 0xFF)
            out.write(c shr 8 and 0xFF)
            out.write(c and 0xFF)
        }
        for (i in palette.size until 256) {   // 调色板不足 256 时补黑
            out.write(0); out.write(0); out.write(0)
        }

        // ---- 无限循环扩展（NETSCAPE2.0）----
        out.write(0x21); out.write(0xFF); out.write(11)
        writeAscii(out, "NETSCAPE2.0")
        out.write(3); out.write(1)
        writeShortLE(out, 0)
        out.write(0)

        val indices = IntArray(w * h)
        for (frame in frames) {
            // 图形控制扩展：不处置（整帧覆盖），帧延时
            out.write(0x21); out.write(0xF9); out.write(4)
            out.write(0x04)
            writeShortLE(out, delayCs)
            out.write(0); out.write(0)
            // 图像描述符：整幅、无局部色表、不隔行
            out.write(0x2C)
            writeShortLE(out, 0); writeShortLE(out, 0)
            writeShortLE(out, w); writeShortLE(out, h)
            out.write(0)
            indexFrame(frame, palette, indices)
            lzwEncode(out, indices)
        }
        out.write(0x3B)                       // 结束
        return out.toByteArray()
    }

    private fun writeShortLE(out: ByteArrayOutputStream, v: Int) {
        out.write(v and 0xFF)
        out.write(v shr 8 and 0xFF)
    }

    /** Android 低版本没有 ByteArrayOutputStream.writeBytes（Java 11 API）。 */
    private fun writeAscii(out: ByteArrayOutputStream, s: String) {
        val b = s.toByteArray(Charsets.US_ASCII)
        out.write(b, 0, b.size)
    }

    // ---------------- 调色板：中值切分 ----------------

    private fun buildPalette(frames: List<android.graphics.Bitmap>): IntArray {
        val samples = ArrayList<Int>(1 shl 15)
        for (f in frames) {
            val px = IntArray(f.width * f.height)
            f.getPixels(px, 0, f.width, 0, 0, f.width, f.height)
            val step = max(1, px.size / 6144)     // 每帧抽 ~6K 像素
            var i = 0
            while (i < px.size) {
                samples.add(px[i])
                i += step
            }
        }
        if (samples.isEmpty()) return intArrayOf(0)
        var boxes = listOf(samples)
        while (boxes.size < 256) {
            var bestIdx = -1
            var bestScore = -1.0
            for (i in boxes.indices) {
                val box = boxes[i]
                if (box.size < 2) continue
                val score = rangeVolume(box) * sqrt(box.size.toDouble())
                if (score > bestScore) { bestScore = score; bestIdx = i }
            }
            if (bestIdx < 0) break
            val box = boxes[bestIdx]
            val channel = widestChannel(box)
            val sorted = box.sortedBy { it shr (16 - channel * 8) and 0xFF }
            val mid = sorted.size / 2
            val left = ArrayList(sorted.subList(0, mid))
            val right = ArrayList(sorted.subList(mid, sorted.size))
            boxes = boxes.toMutableList().also {
                it.removeAt(bestIdx); it.add(left); it.add(right)
            }
        }
        val palette = IntArray(boxes.size)
        for (i in boxes.indices) {
            var r = 0L; var g = 0L; var b = 0L
            for (c in boxes[i]) {
                r += c shr 16 and 0xFF; g += c shr 8 and 0xFF; b += c and 0xFF
            }
            val n = boxes[i].size
            palette[i] = ((r / n).toInt() shl 16) or ((g / n).toInt() shl 8) or (b / n).toInt()
        }
        return palette
    }

    /** channel: 0=R 1=G 2=B。返回该通道的极差。 */
    private fun widestChannel(box: List<Int>): Int {
        var rmin = 255; var rmax = 0; var gmin = 255; var gmax = 0; var bmin = 255; var bmax = 0
        for (c in box) {
            val r = c shr 16 and 0xFF; val g = c shr 8 and 0xFF; val b = c and 0xFF
            if (r < rmin) rmin = r; if (r > rmax) rmax = r
            if (g < gmin) gmin = g; if (g > gmax) gmax = g
            if (b < bmin) bmin = b; if (b > bmax) bmax = b
        }
        val dr = rmax - rmin; val dg = gmax - gmin; val db = bmax - bmin
        return when (max(dr, max(dg, db))) {
            dr -> 0
            dg -> 1
            else -> 2
        }
    }

    private fun rangeVolume(box: List<Int>): Double {
        var rmin = 255; var rmax = 0; var gmin = 255; var gmax = 0; var bmin = 255; var bmax = 0
        for (c in box) {
            val r = c shr 16 and 0xFF; val g = c shr 8 and 0xFF; val b = c and 0xFF
            if (r < rmin) rmin = r; if (r > rmax) rmax = r
            if (g < gmin) gmin = g; if (g > gmax) gmax = g
            if (b < bmin) bmin = b; if (b > bmax) bmax = b
        }
        return ((rmax - rmin + 1) * (gmax - gmin + 1) * (bmax - bmin + 1)).toDouble()
    }

    // ---------------- 帧索引：最近色 + Bayer 8x8 抖动 ----------------

    // 标准 Bayer 8x8 有序抖动矩阵（0..63），映射到 ±3 色阶偏移
    private val BAYER = intArrayOf(
        0, 32, 8, 40, 2, 34, 10, 42,
        48, 16, 56, 24, 50, 18, 58, 26,
        12, 44, 4, 36, 14, 46, 6, 38,
        60, 28, 52, 20, 62, 30, 54, 22,
        3, 35, 11, 43, 1, 33, 9, 41,
        51, 19, 59, 27, 49, 17, 57, 25,
        15, 47, 7, 39, 13, 45, 5, 37,
        63, 31, 55, 23, 61, 29, 53, 21,
    )

    private fun indexFrame(
        frame: android.graphics.Bitmap,
        palette: IntArray,
        out: IntArray,
    ) {
        val w = frame.width
        val h = frame.height
        val px = IntArray(w * h)
        frame.getPixels(px, 0, w, 0, 0, w, h)
        // 15bit (5R5G5B) 量化缓存：同一量化色 + 同一抖动档 → 同一索引
        val cache = IntArray(32768) { -1 }
        for (i in px.indices) {
            val x = i % w
            val y = i / w
            val d = (BAYER[(y and 7) * 8 + (x and 7)] - 32) * 6 / 64   // -3..3
            var r = (px[i] shr 16 and 0xFF) + d
            var g = (px[i] shr 8 and 0xFF) + d
            var b = (px[i] and 0xFF) + d
            r = min(255, max(0, r)); g = min(255, max(0, g)); b = min(255, max(0, b))
            val key = (r shr 3 shl 10) or (g shr 3 shl 5) or (b shr 3)
            val hit = cache[key]
            out[i] = if (hit >= 0) hit else nearest(palette, r, g, b).also { cache[key] = it }
        }
    }

    private fun nearest(palette: IntArray, r: Int, g: Int, b: Int): Int {
        var best = 0
        var bestDist = Int.MAX_VALUE
        for (i in palette.indices) {
            val c = palette[i]
            val dr = r - (c shr 16 and 0xFF)
            val dg = g - (c shr 8 and 0xFF)
            val db = b - (c and 0xFF)
            val dist = dr * dr + dg * dg + db * db
            if (dist < bestDist) { bestDist = dist; best = i }
            if (bestDist == 0) break
        }
        return best
    }

    // ---------------- LZW ----------------

    private fun lzwEncode(out: ByteArrayOutputStream, indices: IntArray) {
        val minCodeSize = 8
        out.write(minCodeSize)
        val raw = ByteArrayOutputStream(indices.size * 2)
        val dict = HashMap<Int, Int>(1 shl 13)

        var codeSize = 9
        var dictSize = 258
        var bitBuf = 0
        var bitCnt = 0

        fun emit(code: Int) {
            bitBuf = bitBuf or (code shl bitCnt)
            bitCnt += codeSize
            while (bitCnt >= 8) {
                raw.write(bitBuf and 0xFF)
                bitBuf = bitBuf ushr 8
                bitCnt -= 8
            }
        }

        emit(256)                                    // clear
        var prefix = indices[0]
        for (i in 1 until indices.size) {
            val k = indices[i]
            val key = prefix * 256 + k
            val found = dict[key]
            if (found != null) {
                prefix = found
            } else {
                emit(prefix)
                if (dictSize < 4096) {
                    dict[key] = dictSize
                    dictSize++
                    if (dictSize == (1 shl codeSize) && codeSize < 12) codeSize++
                } else {
                    emit(256)                        // 字典满：清码重置
                    dict.clear()
                    dictSize = 258
                    codeSize = 9
                }
                prefix = k
            }
        }
        emit(prefix)
        emit(257)                                    // end of information
        if (bitCnt > 0) raw.write(bitBuf and 0xFF)

        // 打包成 ≤255 字节的子块
        val data = raw.toByteArray()
        var i = 0
        while (i < data.size) {
            val n = min(255, data.size - i)
            out.write(n)
            out.write(data, i, n)
            i += n
        }
        out.write(0)                                 // 块终止符
    }
}
