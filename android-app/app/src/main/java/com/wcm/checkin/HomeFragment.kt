package com.wcm.checkin

import android.content.Intent
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.random.Random

/** 首页：签到 / 同步 / 账号管理 / 运行日志。 */
class HomeFragment : Fragment() {

    private var busy = false
    private val logBuffer = StringBuilder()
    private val timeFmt = SimpleDateFormat("HH:mm:ss", Locale.CHINA)

    companion object {
        /** 本次进程只自动签到一次（切 Tab 不重复触发）。 */
        private var autoHandled = false
    }

    override fun onCreateView(
        inflater: LayoutInflater,
        container: ViewGroup?,
        savedInstanceState: Bundle?,
    ): View = inflater.inflate(R.layout.fragment_home, container, false)

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        view.findViewById<View>(R.id.btnCheckin).setOnClickListener { runAll(false) }
        view.findViewById<View>(R.id.btnSync).setOnClickListener { syncOnly() }
        view.findViewById<View>(R.id.btnAdd).setOnClickListener {
            startActivity(Intent(requireContext(), LoginWebViewActivity::class.java))
        }
        render()

        val ctx = requireContext()
        if (!autoHandled) {
            autoHandled = true
            val lastAuto = AccountStore.lastAutoAt(ctx)
            if (AccountStore.autoOnOpen(ctx) && AccountStore.load(ctx).isNotEmpty()
                && System.currentTimeMillis() - lastAuto > 6 * 3600_000L
            ) {
                AccountStore.setLastAutoAt(ctx, System.currentTimeMillis())
                runAll(true)
            }
        }
    }

    override fun onResume() {
        super.onResume()
        render()
    }

    override fun onHiddenChanged(hidden: Boolean) {
        super.onHiddenChanged(hidden)
        if (!hidden) render()
    }

    /** 合并式保存：以当前库为准（运行中被删除的账号不会因快照回写而复活）。 */
    private fun mergeAndSave(
        ctx: android.content.Context,
        snapshot: List<AccountStore.Account>,
    ) {
        val current = AccountStore.load(ctx)
        current.forEach { cur ->
            snapshot.find { it.uid == cur.uid }?.let { snap ->
                cur.cookie = snap.cookie
                cur.lastResult = snap.lastResult
            }
        }
        AccountStore.save(ctx, current)
    }

    // ---------- 渲染 ----------

    private fun render() {
        val ctx = requireContext()
        val accounts = AccountStore.load(ctx)
        val panelReady = AccountStore.panelUrl(ctx).isNotEmpty() &&
            AccountStore.panelToken(ctx).isNotEmpty()
        view?.findViewById<TextView>(R.id.tvSummary)?.text = when {
            accounts.isEmpty() -> getString(R.string.home_summary_empty)
            else -> getString(
                R.string.home_summary_fmt,
                accounts.size,
                if (panelReady) getString(R.string.home_panel_ok) else getString(R.string.home_panel_off),
            )
        }

        val container = view?.findViewById<ViewGroup>(R.id.accountList) ?: return
        container.removeAllViews()
        if (accounts.isEmpty()) {
            container.addView(TextView(ctx).apply {
                text = getString(R.string.home_account_empty)
                textSize = 13f
                setPadding(0, 12, 0, 12)
                setTextColor(resources.getColor(R.color.text_secondary, null))
            })
            return
        }
        val inflater = LayoutInflater.from(ctx)
        accounts.forEach { acc ->
            val row = inflater.inflate(R.layout.item_account, container, false)
            row.findViewById<TextView>(R.id.tvName).text = acc.name.ifEmpty { "微博 ${acc.uid}" }
            row.findViewById<TextView>(R.id.tvStatus).text =
                (acc.lastResult.ifEmpty { "uid ${acc.uid}" }) +
                    "\n" + getString(R.string.home_row_hint)
            row.setOnClickListener { showTopics(acc.uid) }
            row.findViewById<View>(R.id.btnDelete).setOnClickListener {
                MaterialAlertDialogBuilder(ctx)
                    .setTitle(getString(R.string.home_delete_title, acc.name))
                    .setMessage(getString(R.string.home_delete_msg))
                    .setNegativeButton(android.R.string.cancel, null)
                    .setPositiveButton(android.R.string.ok) { _, _ ->
                        AccountStore.remove(ctx, acc.uid)
                        render()
                    }
                    .show()
            }
            container.addView(row)
        }
    }

    private fun log(line: String) {
        val host = view ?: return
        host.post {
            synchronized(logBuffer) {
                // 最新的在最上面：新行插到缓冲区头部，超限裁掉尾部最旧的
                logBuffer.insert(0, "$line\n")
                val parts = logBuffer.toString().split('\n')
                if (parts.size > 400) {
                    logBuffer.setLength(0)
                    logBuffer.append(parts.take(400).joinToString("\n"))
                }
                host.findViewById<TextView>(R.id.logView).text = logBuffer
                host.findViewById<ScrollView>(R.id.logScroll).scrollTo(0, 0)
            }
        }
    }

    private fun now(): String = timeFmt.format(Date())

    private fun setBusy(value: Boolean) {
        busy = value
        view?.findViewById<View>(R.id.btnCheckin)?.isEnabled = !value
        view?.findViewById<View>(R.id.btnSync)?.isEnabled = !value
        view?.findViewById<View>(R.id.btnAdd)?.isEnabled = !value
    }

    // ---------- 核心流程（与 v0.1.0 相同的签到/同步编排） ----------

    private fun runAll(auto: Boolean) {
        if (busy) {
            Toast.makeText(requireContext(), R.string.busy, Toast.LENGTH_SHORT).show()
            return
        }
        val ctx = requireContext()
        val accounts = AccountStore.load(ctx)
        if (accounts.isEmpty()) {
            log(getString(R.string.home_no_account))
            return
        }
        setBusy(true)
        viewLifecycleOwner.lifecycleScope.launch(Dispatchers.IO) {
            try {
                log("==== ${if (auto) "打开自动" else "手动"}签到 · ${now()} ====")
                accounts.forEachIndexed { index, acc ->
                    if (index > 0) delay(Random.nextLong(2000, 5000))
                    runAccount(acc)
                }
                log("==== 全部完成 · ${now()} ====")
                if (AccountStore.crawlAuto(ctx)) {
                    try {
                        Crawler.runIncremental(ctx, ::log, autoMode = true)
                    } catch (e: Exception) {
                        log("自动抓图中断：${e.message}")
                    }
                }
            } finally {
                mergeAndSave(ctx, accounts)
                withContext(Dispatchers.Main) {
                    setBusy(false)
                    render()
                }
            }
        }
    }

    private suspend fun runAccount(acc: AccountStore.Account) {
        val name = acc.name.ifEmpty { acc.uid }
        log("── $name ──")
        val verify = try {
            WeiboApi.verify(acc.cookie)
        } catch (e: Exception) {
            acc.lastResult = "探活失败：${e.message}"
            log("  ✗ 探活失败：${e.message}")
            return
        }
        if (!verify.login) {
            acc.lastResult = getString(R.string.home_cookie_dead)
            log("  ✗ ${getString(R.string.home_cookie_dead)}")
            return
        }
        acc.cookie = verify.cookie
        try {
            val topics = WeiboApi.listTopics(acc.cookie)
            val pending = topics.filter { !it.done }
            log("  ${topics.size} 个超话，${topics.size - pending.size} 已签，待签 ${pending.size}")
            var ok = 0
            var fail = 0
            for (topic in pending) {
                delay(Random.nextLong(3000, 8000))
                try {
                    if (WeiboApi.sign(acc.cookie, topic.scheme ?: "", verify.st)) {
                        ok++; log("  ✓ ${topic.name}")
                    } else {
                        fail++; log("  ✗ ${topic.name}")
                    }
                } catch (e: Exception) {
                    fail++; log("  ✗ ${topic.name}：${e.message}")
                }
            }
            acc.lastResult = "签到 $ok/${pending.size}" +
                (if (fail > 0) "（失败 $fail）" else "") + " · ${now()}"
            log("  小结：${acc.lastResult}")
        } catch (e: Exception) {
            acc.lastResult = "超话列表失败：${e.message}"
            log("  ✗ ${e.message}")
        }
        syncToPanel(acc)
    }

    private suspend fun syncToPanel(acc: AccountStore.Account) {
        val ctx = requireContext()
        val url = AccountStore.panelUrl(ctx)
        val token = AccountStore.panelToken(ctx)
        if (url.isEmpty() || token.isEmpty()) {
            log("  （未配置面板，跳过同步）")
            return
        }
        try {
            val r = PanelApi.sync(url, token, acc.cookie, acc.uid)
            if (r.ok) {
                log("  📲 已同步面板：${if (r.updated) "覆盖" else "新建"} ${r.name}")
            } else {
                log("  📲 面板拒绝同步：${r.detail}")
            }
        } catch (e: Exception) {
            log("  📲 同步面板失败：${e.message}")
        }
    }

    // ---------- 超话状态 / 补签（v0.4.0，只读查询 + 勾选补签，与 runAll 互斥） ----------

    private var topicBusy = false

    /** 点按账号行：拉取该账号关注的超话与今日签到状态。 */
    private fun showTopics(uid: String) {
        if (busy || topicBusy) {
            Toast.makeText(requireContext(), R.string.busy, Toast.LENGTH_SHORT).show()
            return
        }
        val ctx = requireContext()
        val acc = AccountStore.load(ctx).firstOrNull { it.uid == uid } ?: return
        topicBusy = true
        log("── 查询 ${acc.name.ifEmpty { acc.uid }} 的超话 ──")
        viewLifecycleOwner.lifecycleScope.launch(Dispatchers.IO) {
            try {
                val verify = WeiboApi.verify(acc.cookie)
                if (verify.login) saveCookie(ctx, uid, verify.cookie)
                if (!verify.login) {
                    log("  ✗ ${getString(R.string.home_cookie_dead)}")
                    withContext(Dispatchers.Main) {
                        if (isAdded) {
                            MaterialAlertDialogBuilder(requireContext())
                                .setTitle(getString(R.string.home_topic_title, acc.name.ifEmpty { acc.uid }))
                                .setMessage(R.string.home_cookie_dead)
                                .setPositiveButton(android.R.string.ok, null)
                                .show()
                        }
                    }
                    return@launch
                }
                val topics = WeiboApi.listTopics(verify.cookie)
                log("  ${topics.size} 个超话，已签 ${topics.count { it.done }}")
                withContext(Dispatchers.Main) {
                    if (isAdded) presentTopics(ctx, uid, verify.st, topics)
                }
            } catch (e: Exception) {
                log("  ✗ 超话查询失败：${e.message}")
            } finally {
                topicBusy = false
            }
        }
    }

    private fun saveCookie(ctx: android.content.Context, uid: String, cookie: String) {
        val list = AccountStore.load(ctx)
        list.firstOrNull { it.uid == uid }?.let { it.cookie = cookie }
        AccountStore.save(ctx, list)
    }

    /** 弹出超话状态：全部已签直接提示；有待签则勾选补签。 */
    private fun presentTopics(
        ctx: android.content.Context,
        uid: String,
        st: String?,
        topics: List<WeiboApi.Topic>,
    ) {
        if (!isAdded) return
        val name = AccountStore.load(ctx).firstOrNull { it.uid == uid }
            ?.name?.ifEmpty { uid } ?: uid
        when {
            topics.isEmpty() -> {
                MaterialAlertDialogBuilder(ctx)
                    .setTitle(getString(R.string.home_topic_title, name))
                    .setMessage(R.string.home_topic_none)
                    .setPositiveButton(android.R.string.ok, null)
                    .show()
            }
            topics.all { it.done } -> {
                MaterialAlertDialogBuilder(ctx)
                    .setTitle(getString(R.string.home_topic_title, name))
                    .setMessage(getString(R.string.home_topic_all_done, topics.size))
                    .setPositiveButton(android.R.string.ok, null)
                    .show()
            }
            else -> {
                val pending = topics.filter { !it.done }
                val checked = BooleanArray(pending.size)
                MaterialAlertDialogBuilder(ctx)
                    .setTitle(getString(R.string.home_topic_title, name))
                    .setMessage(
                        getString(R.string.home_topic_pick, topics.size - pending.size, topics.size)
                    )
                    .setMultiChoiceItems(
                        pending.map { it.name }.toTypedArray(), checked
                    ) { _, which, isChecked -> checked[which] = isChecked }
                    .setNegativeButton(android.R.string.cancel, null)
                    .setPositiveButton(R.string.home_topic_resign) { _, _ ->
                        resignTopics(uid, st, pending.filterIndexed { i, _ -> checked[i] })
                    }
                    .show()
            }
        }
    }

    /** 勾选补签：与主签到同节奏（3-8 秒随机间隔），完成后同步面板并更新账号状态。 */
    private fun resignTopics(uid: String, st: String?, topics: List<WeiboApi.Topic>) {
        if (topics.isEmpty()) return
        if (busy) {
            Toast.makeText(requireContext(), R.string.busy, Toast.LENGTH_SHORT).show()
            return
        }
        val ctx = requireContext()
        setBusy(true)
        log("── 补签 ${topics.size} 个超话 ──")
        viewLifecycleOwner.lifecycleScope.launch(Dispatchers.IO) {
            try {
                val acc = AccountStore.load(ctx).firstOrNull { it.uid == uid }
                if (acc == null) {
                    log("  ✗ 账号已不存在")
                    return@launch
                }
                var ok = 0
                var fail = 0
                for (topic in topics) {
                    delay(Random.nextLong(3000, 8000))
                    try {
                        if (WeiboApi.sign(acc.cookie, topic.scheme ?: "", st)) {
                            ok++; log("  ✓ ${topic.name}")
                        } else {
                            fail++; log("  ✗ ${topic.name}")
                        }
                    } catch (e: Exception) {
                        fail++; log("  ✗ ${topic.name}：${e.message}")
                    }
                }
                log("  " + getString(R.string.home_resign_done_fmt, ok, fail))
                val summary = "补签 $ok/${topics.size}" +
                    (if (fail > 0) "（失败 $fail）" else "") + " · ${now()}"
                saveLastResult(ctx, uid, summary)
                log("  小结：$summary")
                acc.lastResult = summary
                syncToPanel(acc)
            } finally {
                withContext(Dispatchers.Main) {
                    if (isAdded) {
                        setBusy(false)
                        render()
                    }
                }
            }
        }
    }

    private fun saveLastResult(ctx: android.content.Context, uid: String, result: String) {
        val list = AccountStore.load(ctx)
        list.firstOrNull { it.uid == uid }?.let { it.lastResult = result }
        AccountStore.save(ctx, list)
    }

    private fun syncOnly() {
        if (busy) {
            Toast.makeText(requireContext(), R.string.busy, Toast.LENGTH_SHORT).show()
            return
        }
        val ctx = requireContext()
        val accounts = AccountStore.load(ctx)
        if (accounts.isEmpty()) {
            log(getString(R.string.home_no_account))
            return
        }
        setBusy(true)
        viewLifecycleOwner.lifecycleScope.launch(Dispatchers.IO) {
            try {
                log("==== 仅同步面板 · ${now()} ====")
                for (acc in accounts) {
                    log("── ${acc.name.ifEmpty { acc.uid }} ──")
                    try {
                        val verify = WeiboApi.verify(acc.cookie)
                        if (!verify.login) {
                            acc.lastResult = getString(R.string.home_cookie_dead)
                            log("  ✗ ${getString(R.string.home_cookie_dead)}")
                            continue
                        }
                        acc.cookie = verify.cookie
                        syncToPanel(acc)
                    } catch (e: Exception) {
                        log("  ✗ ${e.message}")
                    }
                }
            } finally {
                mergeAndSave(ctx, accounts)
                withContext(Dispatchers.Main) {
                    setBusy(false)
                    render()
                }
            }
        }
    }
}
