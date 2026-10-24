package com.wcm.checkin

import android.content.ContentResolver
import android.content.Context
import android.net.Uri
import android.provider.DocumentsContract
import org.json.JSONObject
import java.io.IOException

/**
 * 原图抓取引擎（v0.3.1）。口径对齐《抓取流程交接文档》：
 *
 * 1. 列表 /api/container/getIndex?containerid=107603<UID>&page=N（每页 10 条，card_type=9）；
 * 2. 详情 /statuses/show?id=<BID> 才有完整 pic_ids 与帖级 live_photo；
 * 3. pics 数组会截断到 9 且可能是数字键对象 —— 图清单以 pic_ids 为准，pics 只用于
 *    判定哪个 pid 是 Live（配对 videoSrc）；
 * 4. 原图 = wx{1..4}.sinaimg.cn/large/<PID>.jpg（接口给的 mw2000 是缩图）；
 * 5. Live 的 mov 内层直链 403，必须走 video.weibo.com/media/play?livephoto= 包装地址；
 * 6. 置顶老帖永远在第 1 页，不参与"翻到底"判定；连续 10 条已完成的帖才停止翻页；
 * 7. 节流：下载间 600-900ms、详情间 ≥620ms、翻页间 ≥700ms；
 * 8. 单文件下载失败自动重试 3 次（间隔递增），重试与失败原因写入日志；
 *    相册目录已有同名文件时跳过下载并在日志注明排除原因。
 *
 * 下载阶段对 CDN 完全匿名（不带 Cookie）；所有出站 URL 过微博域名白名单。
 * v0.3.1：支持单条微博抓取（crawlSinglePost），幂等靠文件名，不占用增量进度。
 */
object Crawler {

    /** SAF 相册目录：DocumentsContract 单次查询缓存全部文件名，创建即写入。 */
    class TreeStore(private val resolver: ContentResolver, treeUri: Uri) {
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
    private const val DOWNLOAD_ATTEMPTS = 3    // 单文件下载尝试次数（CDN 偶发 403/超时自动重试）
    private val LIVE_WRAPPER = "https://video.weibo.com/media/play?livephoto="

    /** 大文件下载专用 client：Live mov 可达 64MB，放宽全局 client 的 25s callTimeout。 */
    private val downloadClient = WeiboApi.client.newBuilder()
        .callTimeout(120, java.util.concurrent.TimeUnit.SECONDS)
        .readTimeout(60, java.util.concurrent.TimeUnit.SECONDS)
        .build()

    private fun safeName(raw: String): String =
        raw.replace(Regex("[\\\\/:*?\"<>|\\s]+"), "").ifEmpty { "微博" }

    /** pics 归一化：数组或数字键对象 → 列表。 */
    private fun normPics(o: Any?): List<JSONObject> {
        return when (o) {
            is org.json.JSONArray ->
                (0 until o.length()).mapNotNull { o.optJSONObject(it) }
            is JSONObject -> o.keys().asSequence().mapNotNull { o.optJSONObject(it) }.toList()
            else -> emptyList()
        }
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

    /** 一条微博的全部图片资产（解析自详情接口）。 */
    class PostAssets(
        val picIds: List<String>,
        val liveByIndex: Map<Int, String>,
        val liveExtras: List<String>,
    ) {
        val totalFiles: Int get() = picIds.size + liveByIndex.size + liveExtras.size
    }

    /** 详情 → 图清单（pic_ids 完整保序）+ Live 配对（pics 前 9 条内）+ 独立 Live。 */
    private fun assetsFromDetail(detail: JSONObject): PostAssets {
        val picIds = ArrayList<String>()
        val idsArr = detail.optJSONArray("pic_ids")
        if (idsArr != null) {
            for (i in 0 until idsArr.length()) picIds.add(idsArr.optString(i, ""))
        }
        val pics = normPics(detail.opt("pics"))
        if (picIds.isEmpty()) pics.forEach { picIds.add(pidOf(it)) }

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
        return PostAssets(picIds, liveByIndex, liveExtras)
    }

    /**
     * 按序下载一帖全部资产。already = 该帖已完成的文件序号（断点续跑跳过前 N 个）。
     * 下载前查重：相册目录已有同名文件则跳过并写明原因；下载失败自动重试。
     * 返回 (新落盘数, 完成序号)。
     */
    private suspend fun saveAssetSequence(
        ctx: Context,
        store: TreeStore,
        prefix: String,
        assets: PostAssets,
        already: Int,
        log: (String) -> Unit,
    ): Pair<Int, Int> {
        var seq = 0
        var saved = 0
        for (i in assets.picIds.indices) {
            val stillName = String.format("%s_%02d.jpg", prefix, i + 1)
            when {
                seq++ < already -> {}   // 断点续跑：此序号此前已完成
                store.hasFile(stillName) ->
                    log("  ↷ 排除重复 $stillName（相册目录已存在同名文件）")
                else -> {
                    val bytes = fetchBytes(WeiboApi.originalImageUrl(assets.picIds[i]), stillName, log)
                    if (bytes != null) {
                        if (store.save(ctx.contentResolver, stillName, "image/jpeg", bytes)) saved++
                        else log("  ✗ $stillName 写入相册目录失败")
                    }
                    kotlinx.coroutines.delay(kotlin.random.Random.nextLong(600, 900))
                }
            }
            assets.liveByIndex[i]?.let { wrapper ->
                val liveBase = String.format("%s_%02d_live", prefix, i + 1)
                when {
                    seq++ < already -> {}
                    store.hasFile("$liveBase.gif") || store.hasFile("$liveBase.mov") ->
                        log("  ↷ 排除重复 $liveBase.gif/.mov（相册目录已存在同名文件）")
                    saveLive(ctx, store, liveBase, wrapper, log) -> saved++
                    else -> {}   // 下载失败：saveLive 内部已写日志
                }
            }
        }
        assets.liveExtras.forEachIndexed { n, wrapper ->
            val liveBase = String.format("%s_live%02d", prefix, n + 1)
            when {
                seq++ < already -> {}
                store.hasFile("$liveBase.gif") || store.hasFile("$liveBase.mov") ->
                    log("  ↷ 排除重复 $liveBase.gif/.mov（相册目录已存在同名文件）")
                saveLive(ctx, store, liveBase, wrapper, log) -> saved++
                else -> {}   // 下载失败：saveLive 内部已写日志
            }
        }
        return saved to seq
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
        log: (String) -> Unit,
    ): Boolean {
        val mov = fetchBytes(wrapperUrl, "$baseName.mov", log)
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
            if (gif == null) log("  ↷ $baseName GIF 转换失败，回退保存原始 mov")
        }
        val ok = store.save(ctx.contentResolver, "$baseName.mov", "video/quicktime", mov)
        if (!ok) log("  ✗ $baseName.mov 写入相册目录失败")
        kotlinx.coroutines.delay(kotlin.random.Random.nextLong(600, 900))
        return ok
    }

    // ========================= 单条抓取（v0.3.1） =========================

    /** 抓取一条微博的全部原图与 Live。幂等：同名文件跳过，不占用增量进度。 */
    suspend fun crawlSinglePost(ctx: Context, bid: String, log: (String) -> Unit) {
        val accounts = AccountStore.load(ctx)
        if (accounts.isEmpty()) {
            log("✗ 没有登录账号，先在首页添加账号")
            return
        }
        val treeUriRaw = AccountStore.crawlTreeUri(ctx)
        if (treeUriRaw.isEmpty()) {
            log("✗ 请先选择相册目录")
            return
        }
        val cookie = accounts.first().cookie
        val store = TreeStore(ctx.contentResolver, Uri.parse(treeUriRaw))
        store.loadExisting()

        val detail = try {
            WeiboApi.fetchPostDetail(cookie, bid)
        } catch (e: Exception) {
            log("✗ 详情获取失败：${e.message}")
            return
        }
        val assets = assetsFromDetail(detail)
        if (assets.picIds.isEmpty()) {
            log("✗ 该微博没有图片（或为视频微博）")
            return
        }
        val t = WeiboApi.parseWeiboTime(detail.optString("created_at", ""))
        val date = t?.first ?: "unknown"
        val hhmm = t?.second?.replace(":", "") ?: "000000"
        val screenName = detail.optJSONObject("user")?.optString("screen_name", "") ?: ""
        val prefix = "${date}_${hhmm}_${safeName(screenName.ifEmpty { "微博" })}"
        val liveCount = assets.liveByIndex.size + assets.liveExtras.size
        log("── 单条抓取 $prefix（${assets.picIds.size} 图 / $liveCount Live）──")
        val (saved, _) = saveAssetSequence(ctx, store, prefix, assets, 0, log)
        log(
            if (saved > 0) "✓ 完成，新增 $saved 个文件（目录累计 ${store.count()}）"
            else "✓ 完成（文件均已存在，无新增）"
        )
    }

    // ========================= 全量增量抓取 =========================

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
        val store = TreeStore(ctx.contentResolver, Uri.parse(treeUriRaw))
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

        val assets = assetsFromDetail(detail)
        if (assets.picIds.isEmpty()) {
            donePosts[post.id] = 0   // 视频帖（pic_num 是封面计数）：标记完成避免反复请求
            return 0
        }
        val already = donePosts[post.id] ?: 0
        if (already >= assets.totalFiles) return 0

        val (saved, completed) = saveAssetSequence(ctx, store, prefix, assets, already, log)
        donePosts[post.id] =
            if (saved == 0 && already == 0 && assets.picIds.isNotEmpty()) 0 else completed
        if (saved > 0) log("  ✓ $prefix…（+$saved）")
        return saved
    }

    /** 匿名下载（CDN 不需要登录态）。URL 必须过微博域名白名单；上限 64MB。
     *  失败自动重试 DOWNLOAD_ATTEMPTS 次（间隔递增），重试过程与原因都写入日志。 */
    private suspend fun fetchBytes(rawUrl: String, label: String, log: (String) -> Unit): ByteArray? {
        var reason = "未知错误"
        for (attempt in 1..DOWNLOAD_ATTEMPTS) {
            if (attempt > 1) {
                kotlinx.coroutines.delay(1200L * (attempt - 1))
            }
            val (bytes, why) = tryDownload(rawUrl)
            if (bytes != null) return bytes
            reason = why
            if (attempt < DOWNLOAD_ATTEMPTS) {
                log("  ↻ $label 下载失败（第 $attempt 次：$reason），重试…")
            }
        }
        log("  ✗ $label 下载失败（$reason，已尝试 $DOWNLOAD_ATTEMPTS 次）")
        return null
    }

    /** 单次下载尝试：返回 (字节, 失败原因)。成功时原因为空串。 */
    private fun tryDownload(rawUrl: String): Pair<ByteArray?, String> {
        return try {
            val url = WeiboApi.weiboUrl(rawUrl)
            val request = okhttp3.Request.Builder()
                .url(url)
                .header("User-Agent", "okhttp")
                .build()
            downloadClient.newCall(request).execute().use { resp ->
                if (!resp.isSuccessful) return null to "HTTP ${resp.code}"
                val body = resp.body ?: return null to "空响应"
                val bytes = body.bytes()
                when {
                    bytes.isEmpty() -> null to "空文件"
                    bytes.size > 64 * 1024 * 1024 -> null to "超过 64MB 上限"
                    else -> bytes to ""
                }
            }
        } catch (e: Exception) {
            null to (e.message ?: e.javaClass.simpleName)
        }
    }
}
