package com.wcm.checkin

import android.annotation.SuppressLint
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.webkit.CookieManager
import android.webkit.WebView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity

/**
 * 微博登录页：账密 / 验证码全部在微博官方页面完成，本应用只读取登录后的
 * Cookie（CookieManager 持久化，关闭 App 不丢）。
 *
 * 判定登录成功的标准与服务端一致：SUB + (SCF|SSOLoginState|ALF|MLOGIN) 之一，
 * 且 m.weibo.cn/api/config 明确返回 login=true（裸 SUB 只是访客假登录）。
 */
class LoginWebViewActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var statusView: TextView
    private val handler = Handler(Looper.getMainLooper())
    private var finished = false

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_login)
        webView = findViewById(R.id.webView)
        statusView = findViewById(R.id.loginStatus)

        val cm = CookieManager.getInstance()
        // 每次进入都是干净会话：添加第二个账号时不继承第一个的登录
        cm.removeAllCookies {
            cm.setAcceptCookie(true)
            cm.setAcceptThirdPartyCookies(webView, true)
        }

        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            userAgentString = webView.settings.userAgentString
        }
        webView.webViewClient = android.webkit.WebViewClient()

        // 与面板扫码登录同源的 wapsso 入口，成功后 302 回 m.weibo.cn
        webView.loadUrl(
            "https://passport.weibo.com/sso/signin?entry=wapsso&source=wapsso" +
                "&url=https%3A%2F%2Fm.weibo.cn%2F"
        )
        handler.postDelayed(::pollLogin, 1500)
    }

    private fun cookieString(): String {
        val cm = CookieManager.getInstance()
        val merged = LinkedHashMap<String, String>()
        // weibo.cn 域最后合并（同名 key 以 m 域为准，对齐服务端聚合语义）
        for (url in listOf(
            "https://passport.weibo.com", "https://weibo.com", "https://m.weibo.cn"
        )) {
            val raw = cm.getCookie(url) ?: continue
            WeiboApi.parseCookieString(raw).forEach { (k, v) -> merged[k] = v }
        }
        return WeiboApi.cookieToString(merged)
    }

    private fun looksLoggedIn(jar: Map<String, String>): Boolean {
        if (!jar.containsKey("SUB")) return false
        val realKeys = listOf("SCF", "SSOLoginState", "ALF", "MLOGIN")
        return realKeys.any { jar.containsKey(it) }
    }

    private fun pollLogin() {
        if (finished || isFinishing) return
        val cookie = cookieString()
        val jar = WeiboApi.parseCookieString(cookie)
        if (looksLoggedIn(jar)) {
            statusView.text = "已检测到登录，正在验证…"
            Thread {
                val verify = try {
                    WeiboApi.verify(cookie)
                } catch (e: Exception) {
                    null
                }
                if (verify != null && verify.login) {
                    val (name, uid) = WeiboApi.fetchProfile(verify.cookie)
                    runOnUiThread {
                        finished = true
                        if (uid.isNotEmpty()) {
                            AccountStore.upsert(
                                this,
                                AccountStore.Account(
                                    uid = uid,
                                    name = name.ifEmpty { "微博 $uid" },
                                    cookie = verify.cookie,
                                )
                            )
                            Toast.makeText(this, "已添加账号：$name", Toast.LENGTH_SHORT).show()
                        } else {
                            Toast.makeText(this, "登录验证成功但未取到 uid，请重试", Toast.LENGTH_SHORT).show()
                        }
                        finish()
                    }
                } else {
                    runOnUiThread {
                        // 服务端不认：多半还在跳转中，继续轮询
                        statusView.text = "已检测到 Cookie，等待微博确认登录态…"
                        handler.postDelayed(::pollLogin, 1500)
                    }
                }
            }.start()
        } else {
            handler.postDelayed(::pollLogin, 1500)
        }
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        super.onDestroy()
    }
}
