package com.wcm.checkin

import org.junit.Assert.assertEquals
import org.junit.Test

/** 缩略图采样算法单元测试（官方 inSampleSize 口径，不触 framework 类）。 */
class ThumbLoaderTest {

    @Test
    fun sampleSize_smallImageNoDownsample() {
        assertEquals(1, ThumbLoader.computeInSampleSize(100, 100, 256))
        assertEquals(1, ThumbLoader.computeInSampleSize(256, 256, 256))
    }

    @Test
    fun sampleSize_powersOfTwo() {
        // 1000x1000 → 半宽 500：500/2=250 < 256 停在 2
        assertEquals(2, ThumbLoader.computeInSampleSize(1000, 1000, 256))
        // 4000x3000 → 半宽 2000、半高 1500：逐级到 8（250 < 256）
        assertEquals(8, ThumbLoader.computeInSampleSize(4000, 3000, 256))
        // 2048 目标：4000 的半宽 2000 < 2048，不再降采样
        assertEquals(1, ThumbLoader.computeInSampleSize(4000, 4000, 2048))
        // 8192：半宽 4096 → 2（2048 仍达标）→ 4（1024 不达标停）
        assertEquals(4, ThumbLoader.computeInSampleSize(8192, 8192, 2048))
    }

    @Test
    fun sampleSize_invalidInputFallsToOne() {
        assertEquals(1, ThumbLoader.computeInSampleSize(0, 100, 256))
        assertEquals(1, ThumbLoader.computeInSampleSize(100, -5, 256))
        assertEquals(1, ThumbLoader.computeInSampleSize(100, 100, 0))
    }

    @Test
    fun sampleSize_portraitAndLandscapeSame() {
        assertEquals(
            ThumbLoader.computeInSampleSize(3000, 4000, 256),
            ThumbLoader.computeInSampleSize(4000, 3000, 256),
        )
    }
}
