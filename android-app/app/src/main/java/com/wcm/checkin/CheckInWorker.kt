package com.wcm.checkin

import android.content.Context
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import java.util.concurrent.TimeUnit

/**
 * 后台保活：每 6 小时对每个账号发一次探活请求（顺带收下微博换发的新 Cookie），
 * 并把最新 Cookie 推送到面板。WorkManager 自动跨重启持久化，无需任何额外权限。
 *
 * 注意：这里只做轻量保活，不做签到 —— 签到由用户打开 App 触发（前台、真机、
 * 看得见进度），避免后台签到被系统省电策略掐断造成半途状态。
 */
class CheckInWorker(context: Context, params: WorkerParameters) :
    CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val ctx = applicationContext
        val accounts = AccountStore.load(ctx)
        val panelUrl = AccountStore.panelUrl(ctx)
        val panelToken = AccountStore.panelToken(ctx)
        var alive = 0

        for (acc in accounts) {
            try {
                val verify = WeiboApi.verify(acc.cookie)
                if (verify.login) {
                    acc.cookie = verify.cookie
                    acc.lastResult = acc.lastResult.ifEmpty { "后台保活 ✓" }
                    alive++
                    if (panelUrl.isNotEmpty() && panelToken.isNotEmpty()) {
                        try {
                            PanelApi.sync(panelUrl, panelToken, acc.cookie, acc.uid)
                        } catch (e: Exception) {
                            // 面板同步失败不影响本地保活
                        }
                    }
                }
            } catch (e: Exception) {
                // 网络抖动/风控：下一轮再说
            }
        }
        // 合并式写回：整轮跑下来要几分钟，期间用户新增/删除的账号不被旧快照覆盖
        AccountStore.mergeSave(ctx, accounts)
        return Result.success()
    }

    companion object {
        private const val WORK_NAME = "wcm-keepalive"

        fun schedule(ctx: Context) {
            // 没网/低电不空跑：省电省流量，条件满足后 WorkManager 自动补跑
            val request = PeriodicWorkRequestBuilder<CheckInWorker>(
                6, TimeUnit.HOURS, 1, TimeUnit.HOURS
            )
                .setConstraints(
                    Constraints.Builder()
                        .setRequiredNetworkType(NetworkType.CONNECTED)
                        .setRequiresBatteryNotLow(true)
                        .build()
                )
                .build()
            WorkManager.getInstance(ctx).enqueueUniquePeriodicWork(
                WORK_NAME, ExistingPeriodicWorkPolicy.KEEP, request
            )
        }
    }
}
