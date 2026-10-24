package com.wcm.checkin

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.materialswitch.MaterialSwitch
import com.google.android.material.textfield.TextInputEditText
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** 图片页：相册目录 / 抓取目标 / 一键增量抓取（含 Live→GIF）。 */
class GalleryFragment : Fragment() {

    private var crawlJob: Job? = null
    private var busy = false
    private val logBuffer = StringBuilder()
    private val timeFmt = SimpleDateFormat("HH:mm:ss", Locale.CHINA)

    private val pickDir =
        registerForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri: Uri? ->
            if (uri != null) {
                requireContext().contentResolver.takePersistableUriPermission(
                    uri,
                    Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
                )
                AccountStore.setCrawlTreeUri(requireContext(), uri.toString())
                renderDir()
                toast(getString(R.string.crawl_dir_saved))
            }
        }

    override fun onCreateView(
        inflater: LayoutInflater,
        container: ViewGroup?,
        savedInstanceState: Bundle?,
    ): View = inflater.inflate(R.layout.fragment_gallery, container, false)

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        view.findViewById<View>(R.id.btnPickDir).setOnClickListener { pickDir.launch(null) }
        view.findViewById<View>(R.id.btnAddTarget).setOnClickListener { addTarget() }
        view.findViewById<View>(R.id.btnSinglePost).setOnClickListener { pickSinglePost() }
        view.findViewById<View>(R.id.btnCrawlStart).setOnClickListener { start() }
        view.findViewById<View>(R.id.btnCrawlStop).setOnClickListener { crawlJob?.cancel() }

        val switchAuto = view.findViewById<MaterialSwitch>(R.id.switchCrawlAuto)
        switchAuto.isChecked = AccountStore.crawlAuto(requireContext())
        switchAuto.setOnCheckedChangeListener { _, checked ->
            AccountStore.setCrawlAuto(requireContext(), checked)
        }

        renderDir()
        renderTargets()
        view.findViewById<TextView>(R.id.crawlLog).text = logBuffer
    }

    override fun onHiddenChanged(hidden: Boolean) {
        super.onHiddenChanged(hidden)
        if (!hidden && isAdded) {
            renderDir()
            renderTargets()
        }
    }

    private fun toast(text: String) = Toast.makeText(requireContext(), text, Toast.LENGTH_SHORT).show()

    private fun renderDir() {
        val ctx = requireContext()
        val raw = AccountStore.crawlTreeUri(ctx)
        val display = if (raw.isEmpty()) getString(R.string.crawl_dir_empty) else try {
            val segments = Uri.parse(raw).pathSegments
            segments.lastOrNull { it.contains(':') }?.substringAfter(':')
                ?: segments.lastOrNull() ?: raw
        } catch (e: Exception) {
            raw
        }
        view?.findViewById<TextView>(R.id.tvDirPath)?.text = display
    }

    private fun renderTargets() {
        val ctx = requireContext()
        val container = view?.findViewById<ViewGroup>(R.id.crawlTargetList) ?: return
        container.removeAllViews()
        val targets = AccountStore.crawlTargets(ctx)
        if (targets.isEmpty()) {
            val empty = TextView(ctx).apply {
                text = getString(R.string.crawl_target_empty)
                textSize = 13f
                setPadding(0, 12, 0, 12)
                setTextColor(resources.getColor(R.color.text_secondary, null))
            }
            container.addView(empty)
            return
        }
        val inflater = LayoutInflater.from(ctx)
        targets.forEach { t ->
            val row = inflater.inflate(R.layout.item_crawl_target, container, false)
            row.findViewById<TextView>(R.id.tvTargetName).text =
                t.name.ifEmpty { "UID ${t.uid}" }
            row.findViewById<TextView>(R.id.tvTargetUid).text = t.uid
            row.findViewById<View>(R.id.btnTargetDelete).setOnClickListener {
                MaterialAlertDialogBuilder(ctx)
                    .setTitle(getString(R.string.crawl_remove_title, t.name))
                    .setMessage(getString(R.string.crawl_remove_msg))
                    .setNegativeButton(android.R.string.cancel, null)
                    .setPositiveButton(android.R.string.ok) { _, _ ->
                        AccountStore.removeCrawlTarget(ctx, t.uid)
                        renderTargets()
                    }
                    .show()
            }
            container.addView(row)
        }
    }

    private fun addTarget() {
        val ctx = requireContext()
        val dialogView = layoutInflater.inflate(R.layout.dialog_add_target, null)
        val et = dialogView.findViewById<TextInputEditText>(R.id.etTargetInput)
        MaterialAlertDialogBuilder(ctx)
            .setTitle(getString(R.string.crawl_add_title))
            .setView(dialogView)
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(android.R.string.ok) { _, _ ->
                val input = et.text?.toString() ?: ""
                viewLifecycleOwner.lifecycleScope.launch(Dispatchers.IO) {
                    val uid = WeiboApi.resolveUid(input)
                    if (uid.isEmpty()) {
                        log("✗ 无法解析 UID：$input")
                        return@launch
                    }
                    val cookie = AccountStore.load(ctx).firstOrNull()?.cookie ?: ""
                    val name = if (cookie.isEmpty()) "" else WeiboApi.fetchUserName(cookie, uid)
                    AccountStore.addCrawlTarget(ctx, AccountStore.CrawlTarget(uid, name))
                    withContext(Dispatchers.Main) { renderTargets() }
                    log("✓ 已添加：${name.ifEmpty { uid }}（$uid）")
                }
            }
            .show()
    }

    private fun log(line: String) {
        val host = view ?: return
        host.post {
            synchronized(logBuffer) {
                logBuffer.append("[").append(timeFmt.format(Date())).append("] ")
                    .append(line).append('\n')
                val parts = logBuffer.toString().split('\n')
                if (parts.size > 400) {
                    logBuffer.setLength(0)
                    logBuffer.append(parts.takeLast(400).joinToString("\n"))
                }
                host.findViewById<TextView>(R.id.crawlLog).text = logBuffer
            }
        }
    }

    private fun start() {
        if (busy) {
            toast(getString(R.string.busy))
            return
        }
        val ctx = requireContext()
        if (AccountStore.crawlTargets(ctx).isEmpty()) {
            toast(getString(R.string.crawl_need_target))
            return
        }
        if (AccountStore.crawlTreeUri(ctx).isEmpty()) {
            toast(getString(R.string.crawl_need_dir))
            return
        }
        busy = true
        view?.findViewById<View>(R.id.btnCrawlStart)?.isEnabled = false
        view?.findViewById<View>(R.id.btnAddTarget)?.isEnabled = false
        crawlJob = viewLifecycleOwner.lifecycleScope.launch(Dispatchers.IO) {
            try {
                log("==== 抓取开始（Live→${if (AccountStore.crawlGif(ctx)) "GIF" else "MOV"}） ====")
                Crawler.runIncremental(ctx, ::log)
                log("==== 抓取结束 ====")
            } catch (e: CancellationException) {
                log("⏹ 已停止（进度已保存，可续跑）")
            } catch (e: Exception) {
                log("✗ 抓取异常：${e.message}")
            } finally {
                withContext(Dispatchers.Main) {
                    busy = false
                    view?.findViewById<View>(R.id.btnCrawlStart)?.isEnabled = true
                    view?.findViewById<View>(R.id.btnAddTarget)?.isEnabled = true
                }
            }
        }
    }

    // ---------- 单条抓取（v0.3.1） ----------

    private fun pickSinglePost() {
        if (busy) {
            toast(getString(R.string.busy))
            return
        }
        val ctx = requireContext()
        val dialogView = layoutInflater.inflate(R.layout.dialog_single_post, null)
        val et = dialogView.findViewById<TextInputEditText>(R.id.etSingleInput)
        MaterialAlertDialogBuilder(ctx)
            .setTitle(getString(R.string.single_title))
            .setView(dialogView)
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(android.R.string.ok) { _, _ ->
                val input = et.text?.toString() ?: ""
                viewLifecycleOwner.lifecycleScope.launch(Dispatchers.IO) {
                    val bid = WeiboApi.parseBidFromInput(input)
                    if (bid.isNotEmpty()) {
                        startSingle(bid)
                        return@launch
                    }
                    // 不是微博链接：当博主 UID 处理，拉最近微博供选择
                    val uid = WeiboApi.resolveUid(input)
                    if (uid.isEmpty()) {
                        log("✗ 无法识别输入：$input")
                        return@launch
                    }
                    val cookie = AccountStore.load(ctx).firstOrNull()?.cookie
                    if (cookie.isNullOrEmpty()) {
                        log("✗ 没有登录账号，先在首页添加账号")
                        return@launch
                    }
                    log("正在拉取博主 $uid 的最近微博…")
                    val posts = ArrayList<WeiboApi.RawPost>()
                    for (page in 1..2) {
                        try {
                            val (_, list) = WeiboApi.fetchUserPosts(cookie, uid, page)
                            posts.addAll(list)
                            if (list.isEmpty()) break
                        } catch (e: Exception) {
                            break
                        }
                    }
                    if (posts.isEmpty()) {
                        log("✗ 没有拉到微博（UID 是否正确？）")
                        return@launch
                    }
                    withContext(Dispatchers.Main) { showPostPicker(posts) }
                }
            }
            .show()
    }

    /** 简单的微博选择器：时间 + 图数 + 置顶/转发标记 + 正文摘要，点选即抓。 */
    private fun showPostPicker(posts: List<WeiboApi.RawPost>) {
        val items = posts.map { p ->
            val t = WeiboApi.parseWeiboTime(p.createdAt)
            val whenStr = if (t != null) {
                "${t.first.substring(5)} ${t.second.substring(0, 5)}"
            } else {
                "未知时间"
            }
            val tags = buildString {
                if (p.pinned) append(" · 置顶")
                if (p.isRetweet) append(" · 转发")
                else if (p.picNum > 0) append(" · ${p.picNum}图")
                else append(" · 无图")
            }
            "$whenStr$tags\n${p.text.ifEmpty { "（无文字）" }}"
        }.toTypedArray()
        MaterialAlertDialogBuilder(requireContext())
            .setTitle(getString(R.string.single_pick_title, posts.size))
            .setNegativeButton(android.R.string.cancel, null)
            .setSingleChoiceItems(items, -1) { dialog, which ->
                dialog.dismiss()
                val bid = posts[which].bid
                viewLifecycleOwner.lifecycleScope.launch(Dispatchers.IO) { startSingle(bid) }
            }
            .show()
    }

    private suspend fun startSingle(bid: String) {
        // busy 只在主线程读写，避免并发点击下双重启动
        val gate = withContext(Dispatchers.Main) {
            if (busy) return@withContext false
            busy = true
            view?.findViewById<View>(R.id.btnCrawlStart)?.isEnabled = false
            view?.findViewById<View>(R.id.btnSinglePost)?.isEnabled = false
            true
        }
        if (!gate) return
        try {
            Crawler.crawlSinglePost(requireContext(), bid, ::log)
        } catch (e: CancellationException) {
            log("⏹ 单条抓取被取消")
        } catch (e: Exception) {
            log("✗ 单条抓取异常：${e.message}")
        } finally {
            withContext(Dispatchers.Main) {
                busy = false
                view?.findViewById<View>(R.id.btnCrawlStart)?.isEnabled = true
                view?.findViewById<View>(R.id.btnSinglePost)?.isEnabled = true
            }
        }
    }
}
