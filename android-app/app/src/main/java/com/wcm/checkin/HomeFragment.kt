package com.wcm.checkin

import android.content.Intent
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
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
                acc.lastResult.ifEmpty { "uid ${acc.uid}" }
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
                logBuffer.append(line).append('\n')
                val parts = logBuffer.toString().split('\n')
                if (parts.size > 400) {
                    logBuffer.setLength(0)
                    logBuffer.append(parts.takeLast(400).joinToString("\n"))
                }
                host.findViewById<TextView>(R.id.logView).text = logBuffer
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
