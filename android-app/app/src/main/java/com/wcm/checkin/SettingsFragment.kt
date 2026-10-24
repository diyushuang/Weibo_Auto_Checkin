package com.wcm.checkin

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import android.widget.Toast
import androidx.fragment.app.Fragment
import com.google.android.material.materialswitch.MaterialSwitch

/** 设置页：面板同步配置、行为开关、关于。 */
class SettingsFragment : Fragment() {

    override fun onCreateView(
        inflater: LayoutInflater,
        container: ViewGroup?,
        savedInstanceState: Bundle?,
    ): View = inflater.inflate(R.layout.fragment_settings, container, false)

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        val ctx = requireContext()

        val etUrl = view.findViewById<com.google.android.material.textfield.TextInputEditText>(R.id.etPanelUrl)
        val etToken = view.findViewById<com.google.android.material.textfield.TextInputEditText>(R.id.etPanelToken)
        etUrl.setText(AccountStore.panelUrl(ctx))
        etToken.setText(AccountStore.panelToken(ctx))

        view.findViewById<View>(R.id.btnSavePanel).setOnClickListener {
            AccountStore.savePanel(
                ctx,
                etUrl.text?.toString() ?: "",
                etToken.text?.toString() ?: "",
            )
            Toast.makeText(ctx, R.string.settings_saved, Toast.LENGTH_SHORT).show()
        }

        val switchAuto = view.findViewById<MaterialSwitch>(R.id.switchAutoOnOpen)
        switchAuto.isChecked = AccountStore.autoOnOpen(ctx)
        switchAuto.setOnCheckedChangeListener { _, checked ->
            AccountStore.setAutoOnOpen(ctx, checked)
        }

        val switchGif = view.findViewById<MaterialSwitch>(R.id.switchCrawlGif)
        switchGif.isChecked = AccountStore.crawlGif(ctx)
        switchGif.setOnCheckedChangeListener { _, checked ->
            AccountStore.setCrawlGif(ctx, checked)
        }

        val version = try {
            ctx.packageManager.getPackageInfo(ctx.packageName, 0).versionName
        } catch (e: Exception) {
            "?"
        }
        view.findViewById<TextView>(R.id.tvAbout).text =
            getString(R.string.settings_about_body, version)
    }
}
