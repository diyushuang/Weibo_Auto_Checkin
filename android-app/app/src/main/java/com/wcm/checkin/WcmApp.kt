package com.wcm.checkin

import android.app.Application
import com.google.android.material.color.DynamicColors

/** Android 12+ 自动跟随系统壁纸取色（Material You），低版本回退品牌色。 */
class WcmApp : Application() {
    override fun onCreate() {
        super.onCreate()
        DynamicColors.applyToActivitiesIfAvailable(this)
    }
}
