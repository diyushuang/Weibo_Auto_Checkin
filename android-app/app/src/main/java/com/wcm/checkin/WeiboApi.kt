package com.wcm.checkin

import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import org.json.JSONObject
import java.net.URI
import java.util.concurrent.TimeUnit

/**
 * m.weibo.cn 客户端：与面板 app/weibo_client.py 同一套接口与判定逻辑的 Kotlin 移植。
 *
 * - verify: /api/config 探活并回收 Set-Cookie（登录态轮换的入口）
 * - listTopics: 关注超话列表（分页 + 已签检测）
 * - sign: 超话签到（执行按钮 scheme + st）
 * 所有出站 URL 只允许微博自家域名（对齐服务端 validate_weibo_url 语义）。
 */
object WeiboApi {

    class WeiboException(message: String) : Exception(message)

    data class Topic(val name: String, val scheme: String?, val done: Boolean)
    data class Verify(val login: Boolean, val st: String?, val cookie: String)

    private const val BASE = "https://m.weibo.cn"
    private const val UA =
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) " +
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
    private val WEIBO_HOSTS = listOf("weibo.cn", "weibo.com", "weibocdn.com", "sinaimg.cn")
    private val DONE_BUTTONS = setOf("已签", "已簽", "已签到", "已簽到")

    val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .callTimeout(25, TimeUnit.SECONDS)
        .build()

    // ---------- cookie 工具 ----------

    fun parseCookieString(raw: String): LinkedHashMap<String, String> {
        val map = LinkedHashMap<String, String>()
        raw.split(";").forEach { part ->
            val kv = part.trim()
            if (kv.isEmpty()) return@forEach
            val key = kv.substringBefore("=").trim()
            val value = kv.substringAfter("=", "").trim()
            if (key.isNotEmpty() && value.isNotEmpty()) map[key] = value
        }
        return map
    }

    fun cookieToString(map: Map<String, String>): String =
        map.entries.joinToString("; ") { "${it.key}=${it.value}" }

    /** 合并响应里的 Set-Cookie（登录态轮换：换发的新 SUB 就在这里进来）。 */
    private fun mergeSetCookies(base: LinkedHashMap<String, String>, response: Response) {
        for (header in response.headers("set-cookie")) {
            val kv = header.substringBefore(';')
            val key = kv.substringBefore('=').trim()
            val value = kv.substringAfter('=', "").trim()
            if (key.isNotEmpty() && value.isNotEmpty()) base[key] = value
        }
    }

    private fun request(url: String, cookie: String): Request =
        Request.Builder().url(url)
            .header("User-Agent", UA)
            .header("Referer", "$BASE/")
            .header("X-Requested-With", "XMLHttpRequest")
            .header("Accept", "application/json, text/plain, */*")
            .header("Cookie", cookie)
            .build()

    /** 出站白名单：只允许微博自家域，防 cookie 被发往任意外部主机。 */
    fun weiboUrl(raw: String): String {
        val url = raw.trim()
        val uri = URI(url)
        val scheme = uri.scheme?.lowercase() ?: ""
        if (scheme !in listOf("http", "https")) {
            throw WeiboException("仅允许 http/https 链接")
        }
        val host = (uri.host ?: "").lowercase().trimEnd('.')
        if (host.isEmpty() || WEIBO_HOSTS.none { host == it || host.endsWith(".$it") }) {
            throw WeiboException("链接域名不在微博白名单内：$host")
        }
        return url
    }

    // ---------- 接口 ----------

    /** 探活 + 轮换：返回是否登录、st 与合并轮换后的完整 cookie。 */
    fun verify(cookie: String): Verify {
        val jar = parseCookieString(cookie)
        client.newCall(request("$BASE/api/config", cookie)).execute().use { resp ->
            if (!resp.isSuccessful) {
                throw WeiboException("探活请求失败：HTTP ${resp.code}")
            }
            mergeSetCookies(jar, resp)
            val payload = JSONObject(resp.body?.string() ?: "{}")
            val data = payload.optJSONObject("data") ?: JSONObject()
            val stRaw = data.optString("st", "")
            return Verify(
                login = data.optBoolean("login", false),
                st = stRaw.ifEmpty { null },
                cookie = cookieToString(jar),
            )
        }
    }

    /** 取当前账号 uid 与昵称（登录成功后调用）。 */
    fun fetchProfile(cookie: String): Pair<String, String> {
        try {
            client.newCall(request("$BASE/api/config", cookie)).execute().use { resp ->
                if (!resp.isSuccessful) return "" to ""
                val data = JSONObject(resp.body?.string() ?: "{}").optJSONObject("data")
                    ?: return "" to ""
                val uid = (data.optString("uid", "").ifEmpty { data.optString("user_id", "") }).trim()
                var name = data.optString("screen_name", "")
                if (uid.isNotEmpty() && name.isEmpty()) {
                    client.newCall(
                        request("$BASE/api/container/getIndex?type=uid&value=$uid", cookie)
                    ).execute().use { r2 ->
                        if (r2.isSuccessful) {
                            val p = JSONObject(r2.body?.string() ?: "{}")
                                .optJSONObject("data")?.optJSONObject("userinfo") ?: return "" to uid
                            name = p.optString("screen_name", "")
                        }
                    }
                }
                return name.trim() to uid
            }
        } catch (e: Exception) {
            return "" to ""
        }
    }

    /** 关注超话列表（对齐 weibo_client.get_followed_topics 的解析与翻页）。 */
    fun listTopics(cookie: String): List<Topic> {
        val topics = ArrayList<Topic>()
        var sinceId = ""
        val seenPages = HashSet<String>()
        var page = 0
        while (page < 6) {
            page++
            var url = "$BASE/api/container/getIndex?containerid=100803_-_followsuper"
            if (sinceId.isNotEmpty()) url += "&since_id=$sinceId"
            client.newCall(request(url, cookie)).execute().use { resp ->
                if (!resp.isSuccessful) {
                    throw WeiboException("超话列表请求失败：HTTP ${resp.code}")
                }
                val payload = JSONObject(resp.body?.string() ?: "{}")
                val ok = payload.opt("ok")
                if (ok != 1 && ok.toString() != "1") {
                    val detail = payload.optString("msg", ok?.toString() ?: "?")
                    if (ok.toString() == "-100") {
                        throw WeiboException("Cookie 已失效（微博要求重新登录）")
                    }
                    if (topics.isEmpty()) {
                        throw WeiboException("微博未返回超话列表：$detail")
                    }
                    return topics
                }
                val data = payload.optJSONObject("data") ?: JSONObject()
                val cards = data.optJSONArray("cards") ?: return topics
                for (i in 0 until cards.length()) {
                    val card = cards.optJSONObject(i) ?: continue
                    val groups = card.optJSONArray("card_group")
                    val items = if (groups != null) {
                        (0 until groups.length()).mapNotNull { groups.optJSONObject(it) }
                    } else {
                        listOf(card)
                    }
                    for (item in items) {
                        if (item.optString("card_type") != "8") continue
                        val buttons = item.optJSONArray("buttons") ?: continue
                        val name = item.optString("title_sub", "").trim()
                        if (name.isEmpty()) continue
                        val button = buttons.optJSONObject(0) ?: continue
                        val buttonName = button.optString("name", "")
                        val buttonScheme = button.optString("scheme", "")
                        val done = buttonName in DONE_BUTTONS || buttonScheme.isEmpty()
                        topics.add(
                            Topic(
                                name = name,
                                scheme = if (done) null else buttonScheme,
                                done = done,
                            )
                        )
                    }
                }
                sinceId = data.optJSONObject("cardlistInfo")?.optString("since_id", "")
                    ?: ""
            }
            if (sinceId.isEmpty() || !seenPages.add(sinceId)) break
            Thread.sleep(500)
        }
        return topics
    }

    /** 签到一个超话：执行按钮 scheme（自动补 st）。返回是否成功。 */
    fun sign(cookie: String, scheme: String, st: String?): Boolean {
        if (scheme.isEmpty()) throw WeiboException("缺少签到链接")
        var url = if (scheme.startsWith("http")) scheme else BASE + scheme
        url = weiboUrl(url)
        if (!st.isNullOrEmpty()) {
            url += (if (url.contains('?')) "&" else "?") + "st=" + st
        }
        client.newCall(request(url, cookie)).execute().use { resp ->
            if (!resp.isSuccessful) {
                throw WeiboException("HTTP ${resp.code}")
            }
            val body = resp.body?.string() ?: ""
            return try {
                JSONObject(body).optInt("ok", 0) == 1
            } catch (e: Exception) {
                false
            }
        }
    }

    // ========================= 原图抓取（v0.2.0，口径对齐抓取流程交接文档） =========================

    /** 列表接口的一页微博（只保留抓图需要的字段）。 */
    data class RawPost(
        val id: String,
        val bid: String,
        val createdAt: String,      // 原始串，如 "Thu Oct 22 15:27:12 +0800 2026"
        val pinned: Boolean,
        val isRetweet: Boolean,
        val picNum: Int,            // 不可信：Live 会重复计数，仅当粗筛用
    )

    private val MONTHS = mapOf(
        "Jan" to "01", "Feb" to "02", "Mar" to "03", "Apr" to "04",
        "May" to "05", "Jun" to "06", "Jul" to "07", "Aug" to "08",
        "Sep" to "09", "Oct" to "10", "Nov" to "11", "Dec" to "12",
    )

    private val TIME_RE = Regex(
        "^([A-Za-z]{3})\\s+([A-Za-z]{3})\\s+(\\d{1,2})\\s+([\\d:]{8})\\s+([+-]\\d{4})\\s+(\\d{4})$"
    )

    /** 微博时间串 → (yyyy-MM-dd, HH:mm:ss, 年)。解析失败返回 null（老帖边界判定依赖年份）。 */
    fun parseWeiboTime(raw: String): Triple<String, String, Int>? {
        val m = TIME_RE.find(raw.trim()) ?: return null
        val month = MONTHS[m.groupValues[2]] ?: return null
        val day = m.groupValues[3].padStart(2, '0')
        return Triple(
            "${m.groupValues[6]}-$month-$day",
            m.groupValues[4],
            m.groupValues[6].toInt(),
        )
    }

    /** 用户微博列表（每页 10 条）。返回 (lifetime 总数, 本页帖列表)。 */
    fun fetchUserPosts(cookie: String, uid: String, page: Int): Pair<Int, List<RawPost>> {
        val url = "$BASE/api/container/getIndex?containerid=107603$uid&page=$page"
        val req = Request.Builder().url(url)
            .header("User-Agent", UA)
            .header("Referer", "$BASE/u/$uid")
            .header("X-Requested-With", "XMLHttpRequest")
            .header("MWeibo-Pwa", "1")
            .header("Cookie", cookie)
            .build()
        client.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) throw WeiboException("列表请求失败：HTTP ${resp.code}")
            val payload = JSONObject(resp.body?.string() ?: "{}")
            if (payload.optInt("ok", 0) != 1) {
                throw WeiboException("列表接口 ok=${payload.opt("ok")}")
            }
            val data = payload.optJSONObject("data") ?: JSONObject()
            val total = data.optJSONObject("cardlistInfo")?.optInt("total", 0) ?: 0
            val posts = ArrayList<RawPost>()
            val cards = data.optJSONArray("cards") ?: return total to posts
            for (i in 0 until cards.length()) {
                val card = cards.optJSONObject(i) ?: continue
                if (card.optInt("card_type") != 9) continue   // 156 等运营位跳过
                val mb = card.optJSONObject("mblog") ?: continue
                posts.add(
                    RawPost(
                        id = mb.optString("id", ""),
                        bid = mb.optString("bid", ""),
                        createdAt = mb.optString("created_at", ""),
                        pinned = mb.optInt("mblogtype", 0) == 1,
                        isRetweet = mb.optJSONObject("retweeted_status") != null,
                        picNum = mb.optInt("pic_num", 0),
                    )
                )
            }
            return total to posts
        }
    }

    /** 微博详情（含完整 pic_ids 与帖级 live_photo，列表接口拿不到）。 */
    fun fetchPostDetail(cookie: String, bid: String): JSONObject {
        val url = "$BASE/statuses/show?id=$bid"
        val req = Request.Builder().url(url)
            .header("User-Agent", UA)
            .header("Referer", "$BASE/detail/$bid")
            .header("X-Requested-With", "XMLHttpRequest")
            .header("MWeibo-Pwa", "1")
            .header("Cookie", cookie)
            .build()
        client.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) throw WeiboException("详情请求失败：HTTP ${resp.code}")
            val payload = JSONObject(resp.body?.string() ?: "{}")
            if (payload.optInt("ok", 0) != 1) {
                throw WeiboException("详情接口 ok=${payload.opt("ok")}")
            }
            return payload.optJSONObject("data") ?: JSONObject()
        }
    }

    /** 任意 uid 的昵称（抓图目标显示用）。 */
    fun fetchUserName(cookie: String, uid: String): String {
        return try {
            val url = "$BASE/api/container/getIndex?type=uid&value=$uid"
            client.newCall(request(url, cookie)).execute().use { resp ->
                if (!resp.isSuccessful) return ""
                JSONObject(resp.body?.string() ?: "{}")
                    .optJSONObject("data")?.optJSONObject("userinfo")
                    ?.optString("screen_name", "") ?: ""
            }
        } catch (e: Exception) {
            ""
        }
    }

    /** 原图 URL：/large/ 才是原始分辨率（接口给的 mw2000 是缩图）。host 按 pid 轮转。 */
    fun originalImageUrl(pid: String): String {
        val crc = java.util.zip.CRC32().apply { update(pid.toByteArray()) }.value.toInt()
        val host = 1 + (Math.floorMod(crc, 4))
        return "https://wx$host.sinaimg.cn/large/$pid.jpg"
    }

    /** 从用户输入解析 UID：纯数字 / 含 /u/ 或 profile/ 的链接 / 个性域名（302 跳转）。 */
    fun resolveUid(input: String): String {
        val text = input.trim()
        if (text.matches(Regex("^\\d+$"))) return text
        Regex("/u/(\\d+)").find(text)?.let { return it.groupValues[1] }
        Regex("profile/(\\d+)").find(text)?.let { return it.groupValues[1] }
        Regex("containerid=107603(\\d+)").find(text)?.let { return it.groupValues[1] }
        // 个性域名：m.weibo.cn/n/<name> 会 302 到 /u/<uid>
        val domain = Regex("([A-Za-z][A-Za-z0-9_-]*)/?$").find(text.trimEnd('/'))
            ?.groupValues?.get(1) ?: return ""
        return try {
            client.newCall(
                Request.Builder().url("$BASE/n/$domain")
                    .header("User-Agent", UA).build()
            ).execute().use { resp ->
                resp.request.url.toString().let { finalUrl ->
                    Regex("/u/(\\d+)").find(finalUrl)?.groupValues?.get(1) ?: ""
                }
            }
        } catch (e: Exception) {
            ""
        }
    }
}
