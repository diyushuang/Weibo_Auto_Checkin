package com.wcm.checkin

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

/** 面板同步客户端：POST /api/mobile/sync（X-Panel-Token 令牌鉴权）。 */
object PanelApi {

    data class SyncResult(
        val ok: Boolean,
        val updated: Boolean,
        val name: String,
        val health: String,
        val detail: String,
    )

    fun sync(panelUrl: String, token: String, cookie: String, uid: String): SyncResult {
        val base = panelUrl.trim().trimEnd('/')
        if (base.isEmpty()) throw WeiboApi.WeiboException("未配置面板地址")
        val body = JSONObject()
            .put("cookie", cookie)
            .put("uid", uid)
            .toString()
            .toRequestBody("application/json".toMediaType())
        val request = Request.Builder()
            .url("$base/api/mobile/sync")
            .header("X-Panel-Token", token)
            .post(body)
            .build()
        WeiboApi.client.newCall(request).execute().use { resp ->
            val text = resp.body?.string() ?: "{}"
            if (resp.code == 429) {
                // 面板限流（多账号连续同步叠加保活时会触顶）：明确提示而非笼统的 HTTP 429
                val retry = resp.header("Retry-After")?.toIntOrNull()
                val detail = "同步频率超限" +
                    (if (retry != null && retry > 0) "，$retry 秒后可重试" else "（面板限流）")
                return SyncResult(false, false, "", "rate_limited", detail)
            }
            if (resp.code == 409) {
                // 面板拒绝覆盖（探活失败/账号健康）：提示去 App 重新登录
                val detail = try {
                    JSONObject(text).optString("detail", "面板拒绝本次同步")
                } catch (e: Exception) {
                    "面板拒绝本次同步"
                }
                return SyncResult(false, false, "", "invalid", detail)
            }
            if (!resp.isSuccessful) {
                throw WeiboApi.WeiboException("面板返回 HTTP ${resp.code}")
            }
            val payload = JSONObject(text)
            return SyncResult(
                ok = payload.optBoolean("ok", false),
                updated = payload.optBoolean("updated", false),
                name = payload.optString("name", ""),
                health = payload.optString("health", ""),
                detail = "",
            )
        }
    }
}
