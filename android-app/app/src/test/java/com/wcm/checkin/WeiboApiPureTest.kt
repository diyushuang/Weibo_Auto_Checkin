package com.wcm.checkin

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Test

/** WeiboApi 纯函数单元测试（不触网）：时间解析 / 摘要 / bid 解析 / cookie 与 URL 白名单。 */
class WeiboApiPureTest {

    // ---------- 时间解析 ----------

    @Test
    fun parseWeiboTime_valid() {
        val t = WeiboApi.parseWeiboTime("Thu Oct 22 15:27:12 +0800 2026")
        assertNotNull(t)
        assertEquals("2026-10-22", t!!.first)
        assertEquals("15:27:12", t.second)
        assertEquals(2026, t.third)
    }

    @Test
    fun parseWeiboTime_invalid() {
        assertNull(WeiboApi.parseWeiboTime("2026-10-22 15:27:12"))
        assertNull(WeiboApi.parseWeiboTime("Thu Oct 22 2026"))
    }

    // ---------- 正文摘要 ----------

    @Test
    fun summarize_stripsHtmlAndEntities() {
        assertEquals("hello a&b", WeiboApi.summarize("hello&nbsp;<a>a&amp;b</a>"))
        assertEquals("纯文本", WeiboApi.summarize("  纯\t文本  "))
    }

    @Test
    fun summarize_truncatesTo90() {
        assertEquals(91, WeiboApi.summarize("x".repeat(200)).length)   // 90 字 + 省略号
    }

    // ---------- 单条微博标识解析 ----------

    @Test
    fun parseBidFromInput_variants() {
        assertEquals("RnQI2A5sU", WeiboApi.parseBidFromInput("https://m.weibo.cn/detail/RnQI2A5sU"))
        assertEquals("RnQI2A5sU", WeiboApi.parseBidFromInput("https://m.weibo.cn/status/RnQI2A5sU?f=1"))
        assertEquals("RnQI2A5sU", WeiboApi.parseBidFromInput("https://weibo.com/1234567/RnQI2A5sU"))
        assertEquals("AbCd12345", WeiboApi.parseBidFromInput("AbCd12345"))
        assertEquals("5356841628600820", WeiboApi.parseBidFromInput("5356841628600820"))
        assertEquals("", WeiboApi.parseBidFromInput("https://m.weibo.cn/u/123456"))
        assertEquals("", WeiboApi.parseBidFromInput("不是链接也不是ID"))
    }

    // ---------- cookie 工具 ----------

    @Test
    fun cookie_roundtrip() {
        val raw = "SUB=abc123; SUBP=def; SCF=x"
        assertEquals(raw, WeiboApi.cookieToString(WeiboApi.parseCookieString(raw)))
        assertEquals(emptyMap<String, String>(), WeiboApi.parseCookieString("垃圾串无等号"))
    }

    // ---------- 出站 URL 白名单（安全约束：仅微博域，拒绝其余 host 与私网 IP） ----------

    @Test
    fun weiboUrl_acceptsWeiboHosts() {
        assertEquals(
            "https://wx1.sinaimg.cn/large/a.jpg",
            WeiboApi.weiboUrl("https://wx1.sinaimg.cn/large/a.jpg"),
        )
        assertEquals(
            "https://video.weibo.com/media/play?livephoto=x",
            WeiboApi.weiboUrl("https://video.weibo.com/media/play?livephoto=x"),
        )
    }

    @Test
    fun weiboUrl_rejectsForeignAndPrivateHosts() {
        assertThrows(WeiboApi.WeiboException::class.java) {
            WeiboApi.weiboUrl("https://evil.example.com/steal.jpg")
        }
        assertThrows(WeiboApi.WeiboException::class.java) {
            WeiboApi.weiboUrl("http://192.168.1.1/internal")
        }
        assertThrows(WeiboApi.WeiboException::class.java) {
            WeiboApi.weiboUrl("http://127.0.0.1:8000/")
        }
        assertThrows(WeiboApi.WeiboException::class.java) {
            WeiboApi.weiboUrl("ftp://m.weibo.cn/a")
        }
    }
}
