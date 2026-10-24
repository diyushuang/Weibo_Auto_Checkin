package com.wcm.checkin

import android.content.ContentResolver
import android.content.Context
import android.net.Uri
import android.provider.DocumentsContract
import org.json.JSONObject
import java.io.IOException

/**
 * 原图抓取引擎（v0.2.0）。口径对齐《抓取流程交接文档》：
 *
 * 1. 列表 /api/container/getIndex?containerid=107603<UID>&page=N（每页 10 条，card_type=9）；
 * 2. 详情 /statuses/show?id=<BID> 才有完整 pic_ids 与帖级 live_photo；
 * 3. pics 数组会截断到 9 且可能是数字键对象 —— 图清单以 pic_ids 为准，pics 只用于
 *    判定哪个 pid 是 Live（配对 videoSrc）；
 * 4. 原图 = wx{1..4}.sinaimg.cn/large/<PID>.jpg（接口给的 mw2000 是缩图）；
 * 5. Live 的 mov 内层直链 403，必须走 video.weibo.com/media/play?livephoto= 包装地址；
 * 6. 置顶老帖永远在第 1 页，不参与"翻到底"判定；连续 10 条已完成的帖才停止翻页；
 * 7. 节流：下载间 600-900ms、详情间 ≥620ms、翻页间 ≥700ms。
 *
 * 下载阶段对 CDN 完全匿名（不带 Cookie）；所有出站 URL 过微博域名白名单。
 */
object Crawler {

    class CrawlStopped(message: String) : Exception(message)

    /** SAF 相册目录：DocumentsContract 单次查询缓存全部文件名，创建即写入。 */
    class TreeStore(resolver: ContentResolver, treeUri: Uri) {
        private val rootDocId = DocumentsContract.getTreeDocumentId(treeUri)
        private val rootUri = DocumentsContract.buildDocumentUriUsingTree(treeUri, rootDocId)
        private val childrenUri =
            DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, rootDocId)
        private val names = HashSet<String>()

        fun loadExisting() {
            names.clear()
            resolver.query(
                childrenUri,
                arrayOf(DocumentsContract.Document.COLUMN_DISPLAY_NAME),
                null, null, null,
            )?.use { cursor ->
                val idx = cursor.getColumnIndex(DocumentsContract.Document.COLUMN_DISPLAY_NAME)
                while (cursor.moveToNext()) names.add(cursor.getString(idx))
            }
        }

        fun count(): Int = names.size

        fun hasFile(name: String): Boolean = names.contains(name)

        /** 幂等写入：同名文件已存在视为成功（断点续跑不产生 "(1)" 副本）。 */
        fun save(resolver: ContentResolver, name: String, mime: String, bytes: ByteArray): Boolean {
            if (names.contains(name)) return true
            if (bytes.isEmpty()) return false
            return try {
                val docUri = DocumentsContract.createDocument(resolver, rootUri, mime, name)
                    ?: return false
                resolver.openOutputStream(docUri)?.use { it.write(bytes) } ?: return false
                names.add(name)
                true
            } catch (e: IOException) {
                false
            }
        }
    }

    private const val MAX_PAGES = 200          // 单账号安全上限（每页 10 条，2000 帖）
    private const val STOP_AFTER_DONE = 10     // 连续 N 条已完成帖 → 判定增量结束
    private val LIVE_WRAPPER = "https://video.weibo.com/media/play?livephoto="

    private fun safeName(raw: String): String =
        raw.replace(Regex("[\\\\/:*?\"<>|\\s]+"), "").ifEmpty { "微博" }

    /** pics 归一化：数组或数字键对象 → 列表。 */
    private fun normPics(o: Any?): List<JSONObject> {
        val arr = when (o) {
            is org.json.JSONArray ->
                (0 until o.length()).mapNotNull { o.optJSONObject(it) }
            is JSONObject -> o.keys().asSequence().mapNotNull { o.optJSONObject(it) }.toList()
            else -> emptyList()
        }
        return arr
    }

    /** 从 large.url 反推 pid（比 p.pid 稳：Live 条目两处 pid 不总一致）。 */
    private fun pidOf(pic: JSONObject): String {
        val url = pic.optJSONObject("large")?.optString("url", "")
            ?: pic.optString("url", "")
        if (url.isNotEmpty()) {
            return url.substringAfterLast('/')
                .replace(Regex("\\.(jpg|jpeg|png|gif|webp)$", RegexOption.IGNORE_CASE), "")
        }
        return pic.optString("pid", "")
    }

    /**
     * 对全部抓取目标跑一轮增量。cookie 取第一个账号；无配置（目标/目录）时直接返回。
     * 每张图下载后回调 log；状态随帖持久化，任意时刻取消都可续跑。
     */
    suspend fun runIncremental(
        ctx: Context,
        log: (String) -> Unit,
        autoMode: Boolean = false,
    ) {
        val targets = AccountStore.crawlTargets(ctx)
        val treeUriRaw = AccountStore.crawlTreeUri(ctx)
        val accounts = AccountStore.load(ctx)
        if (accounts.isEmpty()) {
            log("✗ 没有登录账号，先在首页添加账号")
            return
        }
        if (targets.isEmpty() || treeUriRaw.isEmpty()) {
            if (!autoMode) log("✗ 请先添加抓取目标并选择相册目录")
            return
        }
        val cookie = accounts.first().cookie
        val treeUri = Uri.parse(treeUriRaw)
        val store = TreeStore(ctx.contentResolver, treeUri)
        store.loadExisting()
        log("相册目录已有 ${store.count()} 个文件")

        val done = AccountStore.crawlDone(ctx)
        var downloaded = 0
        for (target in targets) {
            val label = target.name.ifEmpty { target.uid }
            log("── $label（${target.uid}）──")
            try {
                downloaded += crawlTarget(ctx, cookie, target, store, done, log)
            } catch (e: kotlinx.coroutines.CancellationException) {
                AccountStore.saveCrawlDone(ctx, done)
                throw e
            } catch (e: Exception) {
                log("  ✗ 目标中断：${e.message}")
            }
            AccountStore.saveCrawlDone(ctx, done)
        }
        log("本轮共下载 $downloaded 个新文件" + if (store.count() > 0) "，目录累计 ${store.count()}" else "")
    }

    private suspend fun crawlTarget(
        ctx: Context,
        cookie: String,
        target: AccountStore.CrawlTarget,
        store: TreeStore,
        done: MutableMap<String, MutableMap<String, Int>>,
        log: (String) -> Unit,
    ): Int {
        val label = safeName(target.name.ifEmpty { target.uid })
        val donePosts = done.getOrPut(target.uid) { HashMap() }
        var downloaded = 0
        var consecutiveDone = 0
        var emptyPages = 0

        for (page in 1..MAX_PAGES) {
            val (_, posts) = try {
                WeiboApi.fetchUserPosts(cookie, target.uid, page)
            } catch (e: Exception) {
                log("  列表第 $page 页失败：${e.message}，跳过该页")
                emptyPages++
                if (emptyPages >= 3) break
                continue
            }
            if (posts.isEmpty()) break
            var newInPage = 0

            for (post in posts) {
                if (post.id.isEmpty() || post.bid.isEmpty()) continue
                if (!post.pinned) {
                    if (donePosts.containsKey(post.id)) {
                        consecutiveDone++
                        if (consecutiveDone >= STOP_AFTER_DONE) {
                            log("  连续 $STOP_AFTER_DONE 条已完成，增量结束（第 $page 页）")
                            return downloaded
                        }
                        continue
                    }
                    consecutiveDone = 0
                }
                if (post.isRetweet) continue          // 只要原创
                if (post.picNum <= 0) continue         // 无图帖

                val files = downloadPost(ctx, cookie, post, label, store, donePosts, log)
                downloaded += files
                newInPage += files
                AccountStore.saveCrawlDone(ctx, done)
                kotlinx.coroutines.delay(620)
            }
            if (newInPage == 0 && posts.none { !it.pinned && !donePosts.containsKey(it.id) && !it.isRetweet && it.picNum > 0 }) {
                // 整页都没有可做的新帖：再确认一页，仍空则收工
                emptyPages++
                if (emptyPages >= 2) {
                    log("  第 $page 页无新内容，抓取完成")
                    return downloaded
                }
            } else {
                emptyPages = 0
            }
            kotlinx.coroutines.delay(700)
        }
        return downloaded
    }

    /** 下载一条微博的全部原图与 Live（mov）。返回新落盘文件数；进度记入 donePosts。 */
    private suspend fun downloadPost(
        ctx: Context,
        cookie: String,
        post: WeiboApi.RawPost,
        label: String,
        store: TreeStore,
        donePosts: MutableMap<String, Int>,
        log: (String) -> Unit,
    ): Int {
        val detail = try {
            WeiboApi.fetchPostDetail(cookie, post.bid)
        } catch (e: Exception) {
            log("  ✗ ${post.bid} 详情失败：${e.message}")
            return 0
        }
        val time = WeiboApi.parseWeiboTime(post.createdAt)
        val date = time?.first ?: "unknown"
        val hhmm = time?.second?.replace(":", "") ?: "000000"
        val prefix = "${date}_${hhmm}_$label"

        // 图清单：详情 pic_ids 完整且保序；为空再退化到 pics
        val picIds = ArrayList<String>()
        val idsArr = detail.optJSONArray("pic_ids")
        if (idsArr != null) {
            for (i in 0 until idsArr.length()) picIds.add(idsArr.optString(i, ""))
        }
        val pics = normPics(detail.opt("pics"))
        if (picIds.isEmpty()) pics.forEach { picIds.add(pidOf(it)) }
        if (picIds.isEmpty()) {
            donePosts[post.id] = 0   // 视频帖（pic_num 是封面计数）：标记完成避免反复请求
            return 0
        }

        // Live 配对：pics 里 type==livephoto 的条目（≤9 条，超出部分无法配对 → 独立存放）
        val liveByIndex = HashMap<Int, String>()
        val usedLiveUrls = HashSet<String>()
        for (pic in pics) {
            val videoSrc = pic.optString("videoSrc", "")
            if (pic.optString("type") == "livephoto" && videoSrc.isNotEmpty()) {
                val idx = picIds.indexOf(pidOf(pic))
                if (idx >= 0) {
                    liveByIndex[idx] = videoSrc
                    usedLiveUrls.add(videoSrc)
                }
            }
        }
        val liveExtras = ArrayList<String>()
        val postLive = detail.optJSONArray("live_photo")
        if (postLive != null) {
            for (i in 0 until postLive.length()) {
                val enc = postLive.optString(i, "")
                if (enc.isNotEmpty() && !usedLiveUrls.contains(LIVE_WRAPPER + enc)) {
                    liveExtras.add(LIVE_WRAPPER + enc)
                }
            }
        }

        val totalFiles = picIds.size + liveByIndex.size + liveExtras.size
        val already = donePosts[post.id] ?: 0
        if (already >= totalFiles) return 0

        var seq = 0
        var saved = 0
        // 逐图：序号与交接文档一致（两位序号，Live 与其静图同号加 _live）
        for (i in picIds.indices) {
            val stillName = String.format("%s_%02d.jpg", prefix, i + 1)
            if (seq++ < already || store.hasFile(stillName)) {
                // 已下载过（断点续跑 / 上一轮已存在）
            } else {
                val bytes = fetchBytes(WeiboApi.originalImageUrl(picIds[i]))
                if (bytes != null && store.save(ctx.contentResolver, stillName, "image/jpeg", bytes)) {
                    saved++
                } else {
                    log("  ✗ $stillName 下载失败")
                }
                kotlinx.coroutines.delay(kotlin.random.Random.nextLong(600, 900))
            }
            liveByIndex[i]?.let { wrapper ->
                val liveBase = String.format("%s_%02d_live", prefix, i + 1)
                if (seq++ < already) {
                } else if (saveLive(ctx, store, liveBase, wrapper)) {
                    saved++
                } else {
                    log("  ✗ ${liveBase}.mov/.gif 保存失败")
                }
            }
        }
        liveExtras.forEachIndexed { n, wrapper ->
            val liveBase = String.format("%s_live%02d", prefix, n + 1)
            if (seq++ < already) {
            } else if (saveLive(ctx, store, liveBase, wrapper)) {
                saved++
            } else {
                log("  ✗ ${liveBase}.mov/.gif 保存失败")
            }
        }

        donePosts[post.id] = if (saved == 0 && already == 0 && picIds.isNotEmpty()) 0 else minOf(seq, totalFiles)
        if (saved > 0) log("  ✓ $prefix…（+${saved}）")
        return saved
    }

    /**
     * 保存一个 Live Photo：优先转 GIF（设置开启且转换成功），失败或关闭时保存原始 mov，
     * 内容不丢。转换用 cacheDir 临时文件，相册目录不留 mov 残留。
     */
    private suspend fun saveLive(
        ctx: Context,
        store: TreeStore,
        baseName: String,
        wrapperUrl: String,
    ): Boolean {
        val mov = fetchBytes(wrapperUrl)
        if (mov == null) {
            kotlinx.coroutines.delay(kotlin.random.Random.nextLong(600, 900))
            return false
        }
        if (AccountStore.crawlGif(ctx)) {
            val gif = LivePhotoConverter.movBytesToGif(ctx, mov)
            if (gif != null && store.save(ctx.contentResolver, "$baseName.gif", "image/gif", gif)) {
                kotlinx.coroutines.delay(kotlin.random.Random.nextLong(600, 900))
                return true
            }
        }
        val ok = store.save(ctx.contentResolver, "$baseName.mov", "video/quicktime", mov)
        kotlinx.coroutines.delay(kotlin.random.Random.nextLong(600, 900))
        return ok
    }

    /** 匿名下载（CDN 不需要登录态）。URL 必须过微博域名白名单；上限 64MB。 */
    private fun fetchBytes(rawUrl: String): ByteArray? {
        return try {
            val url = WeiboApi.weiboUrl(rawUrl)
            val request = okhttp3.Request.Builder()
                .url(url)
                .header("User-Agent", "okhttp")
                .build()
            WeiboApi.client.newCall(request).execute().use { resp ->
                if (!resp.isSuccessful) return null
                val body = resp.body ?: return null
                val bytes = body.bytes()
                if (bytes.size > 64 * 1024 * 1024) return null
                bytes
            }
        } catch (e: Exception) {
            null
        }
    }
}
