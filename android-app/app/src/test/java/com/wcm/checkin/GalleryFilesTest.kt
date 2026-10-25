package com.wcm.checkin

import org.junit.Assert.assertFalse
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** 图库纯函数单元测试（不触存储）：图片过滤口径与最新在前排序。 */
class GalleryFilesTest {

    // ---------- isImage 过滤 ----------

    @Test
    fun isImage_byMime() {
        assertTrue(GalleryFiles.isImage("随便", "image/jpeg"))
        assertTrue(GalleryFiles.isImage("随便", "IMAGE/GIF"))
        assertFalse(GalleryFiles.isImage("x.mov", "video/quicktime"))
        assertFalse(GalleryFiles.isImage("doc", "application/pdf"))
    }

    @Test
    fun isImage_byExtensionWhenMimeMissing() {
        assertTrue(GalleryFiles.isImage("20260101_120000_作者_01.jpg", null))
        assertTrue(GalleryFiles.isImage("a.JPEG", ""))
        assertTrue(GalleryFiles.isImage("a.GIF", "application/octet-stream"))
        assertFalse(GalleryFiles.isImage("a.mov", null))
        assertFalse(GalleryFiles.isImage("noext", "application/octet-stream"))
    }

    @Test
    fun isImage_clearNonImageMimeWinsOverExtension() {
        // 有明确非图片 mime 时不看扩展名（x.jpg 伪装的文本不算图）
        assertFalse(GalleryFiles.isImage("evil.jpg", "text/plain"))
    }

    // ---------- 排序 ----------

    @Test
    fun sortNames_newestFirst() {
        val sorted = GalleryFiles.sortNames(
            listOf(
                "20260101_090000_甲_01.jpg",
                "20261025_120000_乙_01.jpg",
                "20260303_080000_丙_01.jpg",
            )
        )
        assertEquals(
            listOf(
                "20261025_120000_乙_01.jpg",
                "20260303_080000_丙_01.jpg",
                "20260101_090000_甲_01.jpg",
            ),
            sorted,
        )
    }

    @Test
    fun sortNames_empty() {
        assertEquals(emptyList<String>(), GalleryFiles.sortNames(emptyList()))
    }
}
