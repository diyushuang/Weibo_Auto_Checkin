package com.wcm.checkin

import android.content.ClipData
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.updatePadding
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.RecyclerView
import androidx.viewpager2.widget.ViewPager2
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 全屏预览（v0.4.0）：ViewPager2 左右翻页，页内 ZoomableImageView 双指/双击缩放
 * （放大态自动停用翻页手势）。GIF 经 ImageDecoder 在端内直接播放；
 * 支持分享、用系统应用打开、从相册目录删除。
 *
 * 列表与图库页同源（同一 treeUri 重查、同名降序），只传初始下标，避免大 Intent。
 */
class ImagePreviewActivity : AppCompatActivity() {

    private val items = ArrayList<GalleryFiles.GalleryItem>()
    private val pagerAdapter = PagerAdapter()
    private lateinit var pager: ViewPager2
    private lateinit var tvIndex: TextView
    private lateinit var tvName: TextView

    private val pageCallback = object : ViewPager2.OnPageChangeCallback() {
        override fun onPageSelected(position: Int) {
            updateInfo()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_image_preview)

        pager = findViewById(R.id.previewPager)
        tvIndex = findViewById(R.id.tvPreviewIndex)
        tvName = findViewById(R.id.tvPreviewName)
        pager.adapter = pagerAdapter
        pager.registerOnPageChangeCallback(pageCallback)

        findViewById<View>(R.id.previewRoot)?.let { root ->
            ViewCompat.setOnApplyWindowInsetsListener(root) { _, insets ->
                val bars = insets.getInsets(
                    WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout()
                )
                root.updatePadding(top = bars.top, bottom = bars.bottom)
                WindowInsetsCompat.CONSUMED
            }
        }

        findViewById<View>(R.id.btnShare).setOnClickListener { shareCurrent() }
        findViewById<View>(R.id.btnOpen).setOnClickListener { openCurrent() }
        findViewById<View>(R.id.btnDelete).setOnClickListener { confirmDelete() }

        val treeUriRaw = intent.getStringExtra(EXTRA_TREE) ?: ""
        val startIndex = intent.getIntExtra(EXTRA_INDEX, 0)
        if (treeUriRaw.isEmpty()) {
            finish()
            return
        }
        val treeUri = Uri.parse(treeUriRaw)
        val resolver = contentResolver
        lifecycleScope.launch(Dispatchers.IO) {
            items.addAll(
                try {
                    GalleryFiles.listImages(resolver, treeUri)
                } catch (e: Exception) {
                    emptyList()
                }
            )
            withContext(Dispatchers.Main) {
                if (isFinishing || isDestroyed) return@withContext
                if (items.isEmpty()) {
                    Toast.makeText(this@ImagePreviewActivity, R.string.preview_load_fail, Toast.LENGTH_SHORT).show()
                    finish()
                    return@withContext
                }
                pagerAdapter.notifyDataSetChanged()
                pager.setCurrentItem(startIndex.coerceIn(0, items.size - 1), false)
                updateInfo()
            }
        }
    }

    override fun onDestroy() {
        pager.unregisterOnPageChangeCallback(pageCallback)
        super.onDestroy()
    }

    private fun currentItem(): GalleryFiles.GalleryItem? {
        val pos = pager.currentItem
        return if (pos in items.indices) items[pos] else null
    }

    private fun updateInfo() {
        val pos = pager.currentItem
        if (pos !in items.indices) return
        tvIndex.text = getString(R.string.preview_index_fmt, pos + 1, items.size)
        tvName.text = items[pos].name
    }

    private fun mimeOf(name: String): String = when (name.substringAfterLast('.', "").lowercase()) {
        "jpg", "jpeg" -> "image/jpeg"
        "png" -> "image/png"
        "gif" -> "image/gif"
        "webp" -> "image/webp"
        "bmp" -> "image/bmp"
        else -> "image/*"
    }

    private fun shareCurrent() {
        val item = currentItem() ?: return
        val send = Intent(Intent.ACTION_SEND).apply {
            type = mimeOf(item.name)
            putExtra(Intent.EXTRA_STREAM, item.uri)
            // content URI 授权给接收方：flag + clipData 双保险（chooser 会重建 intent）
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            clipData = ClipData.newRawUri("image", item.uri)
        }
        try {
            startActivity(Intent.createChooser(send, getString(R.string.preview_share)))
        } catch (e: Exception) {
            Toast.makeText(this, R.string.preview_share_fail, Toast.LENGTH_SHORT).show()
        }
    }

    private fun openCurrent() {
        val item = currentItem() ?: return
        try {
            startActivity(
                Intent(Intent.ACTION_VIEW).apply {
                    setDataAndType(item.uri, mimeOf(item.name))
                    addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                }
            )
        } catch (e: Exception) {
            Toast.makeText(this, R.string.preview_open_fail, Toast.LENGTH_SHORT).show()
        }
    }

    private fun confirmDelete() {
        val item = currentItem() ?: return
        MaterialAlertDialogBuilder(this)
            .setTitle(R.string.preview_delete_title)
            .setMessage(item.name + "\n" + getString(R.string.preview_delete_msg))
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(R.string.preview_delete) { _, _ -> deleteCurrent() }
            .show()
    }

    private fun deleteCurrent() {
        val pos = pager.currentItem
        if (pos !in items.indices) return
        val item = items[pos]
        val resolver = contentResolver
        lifecycleScope.launch(Dispatchers.IO) {
            val ok = try {
                android.provider.DocumentsContract.deleteDocument(resolver, item.uri)
            } catch (e: Exception) {
                false
            }
            withContext(Dispatchers.Main) {
                if (isFinishing || isDestroyed) return@withContext
                if (!ok) {
                    Toast.makeText(this@ImagePreviewActivity, R.string.preview_delete_fail, Toast.LENGTH_SHORT).show()
                    return@withContext
                }
                items.removeAt(pos)
                pagerAdapter.notifyDataSetChanged()
                if (items.isEmpty()) {
                    finish()
                } else {
                    pager.setCurrentItem(pos.coerceIn(0, items.size - 1), false)
                    updateInfo()
                }
            }
        }
    }

    private inner class PagerAdapter : RecyclerView.Adapter<PagerAdapter.Holder>() {

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Holder {
            val view = ZoomableImageView(parent.context).apply {
                layoutParams = RecyclerView.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.MATCH_PARENT,
                )
            }
            return Holder(view)
        }

        override fun getItemCount(): Int = items.size

        override fun onBindViewHolder(holder: Holder, position: Int) {
            holder.bind(items[position])
        }

        inner class Holder(val zoomView: ZoomableImageView) : RecyclerView.ViewHolder(zoomView) {
            fun bind(item: GalleryFiles.GalleryItem) {
                // 放大态停用翻页手势，回到 1x 恢复
                zoomView.onZoomChanged = { zoomed -> pager.isUserInputEnabled = !zoomed }
                zoomView.setImageDrawable(null)
                val key = item.uri.toString()
                zoomView.tag = key
                val resolver = contentResolver
                lifecycleScope.launch(Dispatchers.IO) {
                    val drawable = try {
                        ThumbLoader.fullDrawable(resolver, item.uri)
                    } catch (e: Exception) {
                        null
                    }
                    withContext(Dispatchers.Main) {
                        if (zoomView.tag == key && drawable != null) {
                            zoomView.setImageDrawable(drawable)
                        }
                    }
                }
            }
        }
    }

    companion object {
        const val EXTRA_TREE = "tree"
        const val EXTRA_INDEX = "index"

        fun start(ctx: Context, treeUriRaw: String, index: Int) {
            ctx.startActivity(
                Intent(ctx, ImagePreviewActivity::class.java)
                    .putExtra(EXTRA_TREE, treeUriRaw)
                    .putExtra(EXTRA_INDEX, index)
            )
        }
    }
}
