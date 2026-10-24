package com.wcm.checkin

import android.os.Bundle
import android.view.View
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.updatePadding
import androidx.fragment.app.Fragment
import com.google.android.material.bottomnavigation.BottomNavigationView

/**
 * 单 Activity 壳：底部导航（首页 / 图片 / 设置），Material 3。
 *
 * 三个页面用 add + show/hide 常驻（不 replace）：切换 Tab 不会销毁视图与
 * 进行中的签到/抓取协程，滚动位置与日志也得以保留。
 */
class MainActivity : AppCompatActivity() {

    private lateinit var home: HomeFragment
    private lateinit var gallery: GalleryFragment
    private lateinit var settings: SettingsFragment

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        CheckInWorker.schedule(this)

        if (savedInstanceState == null) {
            home = HomeFragment()
            gallery = GalleryFragment()
            settings = SettingsFragment()
            supportFragmentManager.beginTransaction()
                .add(R.id.fragmentContainer, settings, "settings").hide(settings)
                .add(R.id.fragmentContainer, gallery, "gallery").hide(gallery)
                .add(R.id.fragmentContainer, home, "home")
                .commit()
        } else {
            val fm = supportFragmentManager
            home = fm.findFragmentByTag("home") as HomeFragment
            gallery = fm.findFragmentByTag("gallery") as GalleryFragment
            settings = fm.findFragmentByTag("settings") as SettingsFragment
        }

        val nav = findViewById<BottomNavigationView>(R.id.bottomNav)
        nav.setOnItemSelectedListener { item ->
            when (item.itemId) {
                R.id.nav_gallery -> show(gallery)
                R.id.nav_settings -> show(settings)
                else -> show(home)
            }
            true
        }
        if (savedInstanceState == null) {
            nav.selectedItemId = R.id.nav_home
        }

        applyEdgeToEdgeInsets()
    }

    private fun show(fragment: Fragment) {
        supportFragmentManager.beginTransaction()
            .hide(home).hide(gallery).hide(settings)
            .show(fragment)
            .commit()
    }

    /** targetSdk 35 起系统强制边到边：给内容区让出状态栏/挖孔（含横屏侧挖孔）
     *  与底部导航栏的内边距。 */
    private fun applyEdgeToEdgeInsets() {
        val nav = findViewById<BottomNavigationView>(R.id.bottomNav)
        val root = findViewById<View>(R.id.rootLayout)
        ViewCompat.setOnApplyWindowInsetsListener(root) { _, insets ->
            val bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout()
            )
            // 横屏时挖孔/手势条在左右两侧，同样要让位（全面屏中间挖孔在竖屏时
            // 计入 bars.top，横屏时计入 left/right）
            root.updatePadding(top = bars.top, left = bars.left, right = bars.right)
            nav.updatePadding(bottom = bars.bottom)
            WindowInsetsCompat.CONSUMED
        }
    }
}
