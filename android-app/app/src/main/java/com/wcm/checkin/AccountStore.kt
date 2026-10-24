package com.wcm.checkin

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/** 账号与配置存储：全部放应用私有 SharedPreferences（无任何存储权限）。 */
object AccountStore {

    data class Account(
        val uid: String,
        val name: String,
        var cookie: String,
        var lastResult: String = "",
    )

    private const val PREFS = "wcm"
    private const val KEY_ACCOUNTS = "accounts"
    private const val KEY_PANEL_URL = "panel_url"
    private const val KEY_PANEL_TOKEN = "panel_token"
    private const val KEY_AUTO_ON_OPEN = "auto_on_open"
    private const val KEY_LAST_AUTO_AT = "last_auto_at"

    // ---- 图片抓取（v0.2.0）----
    private const val KEY_CRAWL_TARGETS = "crawl_targets"   // [{"uid","name"}]
    private const val KEY_CRAWL_TREE = "crawl_tree_uri"     // SAF 相册目录持久授权
    private const val KEY_CRAWL_DONE = "crawl_done"         // {uid: {post_id: 已下序号}}
    private const val KEY_CRAWL_AUTO = "crawl_auto"         // 签到后自动增量抓图
    private const val KEY_CRAWL_GIF = "crawl_gif"           // Live Photo 转 GIF（默认开）

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun load(ctx: Context): MutableList<Account> {
        val raw = prefs(ctx).getString(KEY_ACCOUNTS, "[]") ?: "[]"
        val list = ArrayList<Account>()
        try {
            val arr = JSONArray(raw)
            for (i in 0 until arr.length()) {
                val o = arr.getJSONObject(i)
                list.add(
                    Account(
                        uid = o.optString("uid", ""),
                        name = o.optString("name", "未命名账号"),
                        cookie = o.optString("cookie", ""),
                        lastResult = o.optString("lastResult", ""),
                    )
                )
            }
        } catch (e: Exception) {
            // 存储损坏时静默重置，宁可丢账号也不能让 App 起不来
        }
        return list
    }

    fun save(ctx: Context, accounts: List<Account>) {
        val arr = JSONArray()
        accounts.forEach {
            arr.put(
                JSONObject()
                    .put("uid", it.uid)
                    .put("name", it.name)
                    .put("cookie", it.cookie)
                    .put("lastResult", it.lastResult)
            )
        }
        prefs(ctx).edit().putString(KEY_ACCOUNTS, arr.toString()).apply()
    }

    /** 按 uid upsert（同微博号换 cookie 只更新一行）。 */
    fun upsert(ctx: Context, account: Account) {
        val list = load(ctx)
        val idx = list.indexOfFirst { it.uid.isNotEmpty() && it.uid == account.uid }
        if (idx >= 0) {
            list[idx] = account.copy(lastResult = list[idx].lastResult)
        } else {
            list.add(account)
        }
        save(ctx, list)
    }

    fun remove(ctx: Context, uid: String) {
        val list = load(ctx)
        list.removeAll { it.uid == uid }
        save(ctx, list)
    }

    fun panelUrl(ctx: Context): String = prefs(ctx).getString(KEY_PANEL_URL, "") ?: ""
    fun panelToken(ctx: Context): String = prefs(ctx).getString(KEY_PANEL_TOKEN, "") ?: ""

    fun savePanel(ctx: Context, url: String, token: String) {
        prefs(ctx).edit()
            .putString(KEY_PANEL_URL, url.trim())
            .putString(KEY_PANEL_TOKEN, token.trim())
            .apply()
    }

    fun autoOnOpen(ctx: Context): Boolean = prefs(ctx).getBoolean(KEY_AUTO_ON_OPEN, true)
    fun setAutoOnOpen(ctx: Context, value: Boolean) {
        prefs(ctx).edit().putBoolean(KEY_AUTO_ON_OPEN, value).apply()
    }

    fun lastAutoAt(ctx: Context): Long = prefs(ctx).getLong(KEY_LAST_AUTO_AT, 0L)
    fun setLastAutoAt(ctx: Context, at: Long) {
        prefs(ctx).edit().putLong(KEY_LAST_AUTO_AT, at).apply()
    }

    // ---- 图片抓取 ----

    data class CrawlTarget(val uid: String, val name: String)

    fun crawlTargets(ctx: Context): MutableList<CrawlTarget> {
        val raw = prefs(ctx).getString(KEY_CRAWL_TARGETS, "[]") ?: "[]"
        val list = ArrayList<CrawlTarget>()
        try {
            val arr = JSONArray(raw)
            for (i in 0 until arr.length()) {
                val o = arr.getJSONObject(i)
                list.add(CrawlTarget(o.optString("uid", ""), o.optString("name", "")))
            }
        } catch (e: Exception) {
        }
        return list
    }

    fun addCrawlTarget(ctx: Context, target: CrawlTarget) {
        val list = crawlTargets(ctx)
        if (list.none { it.uid == target.uid }) list.add(target)
        val arr = JSONArray()
        list.forEach { arr.put(JSONObject().put("uid", it.uid).put("name", it.name)) }
        prefs(ctx).edit().putString(KEY_CRAWL_TARGETS, arr.toString()).apply()
    }

    fun removeCrawlTarget(ctx: Context, uid: String) {
        val list = crawlTargets(ctx)
        list.removeAll { it.uid == uid }
        val arr = JSONArray()
        list.forEach { arr.put(JSONObject().put("uid", it.uid).put("name", it.name)) }
        prefs(ctx).edit().putString(KEY_CRAWL_TARGETS, arr.toString()).apply()
    }

    fun crawlTreeUri(ctx: Context): String = prefs(ctx).getString(KEY_CRAWL_TREE, "") ?: ""
    fun setCrawlTreeUri(ctx: Context, uri: String) {
        prefs(ctx).edit().putString(KEY_CRAWL_TREE, uri).apply()
    }

    /** 已完成进度：uid -> (post_id -> 帖内已下载张数，支持中断续跑)。 */
    fun crawlDone(ctx: Context): MutableMap<String, MutableMap<String, Int>> {
        val raw = prefs(ctx).getString(KEY_CRAWL_DONE, "{}") ?: "{}"
        val result = HashMap<String, MutableMap<String, Int>>()
        try {
            val root = JSONObject(raw)
            root.keys().forEach { uid ->
                val inner = HashMap<String, Int>()
                val posts = root.optJSONObject(uid) ?: JSONObject()
                posts.keys().forEach { pid -> inner[pid] = posts.optInt(pid, 0) }
                result[uid] = inner
            }
        } catch (e: Exception) {
        }
        return result
    }

    fun saveCrawlDone(ctx: Context, done: Map<String, Map<String, Int>>) {
        val root = JSONObject()
        done.forEach { (uid, posts) ->
            val inner = JSONObject()
            posts.forEach { (pid, n) -> inner.put(pid, n) }
            root.put(uid, inner)
        }
        prefs(ctx).edit().putString(KEY_CRAWL_DONE, root.toString()).apply()
    }

    fun crawlAuto(ctx: Context): Boolean = prefs(ctx).getBoolean(KEY_CRAWL_AUTO, false)
    fun setCrawlAuto(ctx: Context, value: Boolean) {
        prefs(ctx).edit().putBoolean(KEY_CRAWL_AUTO, value).apply()
    }

    fun crawlGif(ctx: Context): Boolean = prefs(ctx).getBoolean(KEY_CRAWL_GIF, true)
    fun setCrawlGif(ctx: Context, value: Boolean) {
        prefs(ctx).edit().putBoolean(KEY_CRAWL_GIF, value).apply()
    }
}
