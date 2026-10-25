package com.wcm.checkin

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.ImageView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.updatePadding
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.GridLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.appbar.MaterialToolbar
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 图库（v0.4.0）：相册目录内图片的三列网格浏览，缩略图后台采样解码 + LruCache。
 * 点按进全屏预览（ImagePreviewActivity）；顶部工具栏可刷新。
 */
class GalleryBrowseActivity : AppCompatActivity() {

    private lateinit var treeUriRaw: String
    private lateinit var treeUri: Uri
    private lateinit var toolbar: MaterialToolbar
    private lateinit var recycler: RecyclerView
    private lateinit var emptyView: TextView
    private val gridAdapter = GridAdapter()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_gallery_browse)

        treeUriRaw = intent.getStringExtra(EXTRA_TREE) ?: ""
        if (treeUriRaw.isEmpty()) {
            finish()
            return
        }
        treeUri = Uri.parse(treeUriRaw)

        toolbar = findViewById(R.id.galleryToolbar)
        recycler = findViewById(R.id.galleryGrid)
        emptyView = findViewById(R.id.galleryEmpty)

        toolbar.setNavigationOnClickListener { finish() }
        toolbar.inflateMenu(R.menu.gallery_browser)
        toolbar.setOnMenuItemClickListener { item ->
            if (item.itemId == R.id.action_refresh) {
                load()
                true
            } else {
                false
            }
        }
        recycler.layoutManager = GridLayoutManager(this, 3)
        recycler.adapter = gridAdapter

        applyInsets()
        load()
    }

    private fun applyInsets() {
        val root = findViewById<View>(R.id.galleryRoot)
        ViewCompat.setOnApplyWindowInsetsListener(root) { _, insets ->
            val bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout()
            )
            root.updatePadding(top = bars.top)
            recycler.updatePadding(bottom = bars.bottom)
            WindowInsetsCompat.CONSUMED
        }
    }

    private fun load() {
        val resolver = contentResolver
        lifecycleScope.launch(Dispatchers.IO) {
            val items = try {
                GalleryFiles.listImages(resolver, treeUri)
            } catch (e: Exception) {
                emptyList()
            }
            withContext(Dispatchers.Main) {
                if (isFinishing || isDestroyed) return@withContext
                gridAdapter.submit(items)
                toolbar.subtitle = getString(R.string.gallery_count_fmt, items.size)
                emptyView.visibility = if (items.isEmpty()) View.VISIBLE else View.GONE
                recycler.visibility = if (items.isEmpty()) View.GONE else View.VISIBLE
            }
        }
    }

    private fun openPreview(position: Int) {
        ImagePreviewActivity.start(this, treeUriRaw, position)
    }

    private inner class GridAdapter : RecyclerView.Adapter<GridAdapter.Holder>() {

        private val items = ArrayList<GalleryFiles.GalleryItem>()

        fun submit(list: List<GalleryFiles.GalleryItem>) {
            items.clear()
            items.addAll(list)
            notifyDataSetChanged()
        }

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Holder =
            Holder(
                LayoutInflater.from(parent.context)
                    .inflate(R.layout.item_gallery_grid, parent, false)
            )

        override fun getItemCount(): Int = items.size

        override fun onBindViewHolder(holder: Holder, position: Int) {
            holder.bind(items[position])
        }

        inner class Holder(view: View) : RecyclerView.ViewHolder(view) {
            private val image: ImageView = view.findViewById(R.id.gridImage)
            private val badge: TextView = view.findViewById(R.id.gifBadge)

            fun bind(item: GalleryFiles.GalleryItem) {
                badge.visibility =
                    if (item.name.endsWith(".gif", ignoreCase = true)) View.VISIBLE else View.GONE
                image.setImageDrawable(null)
                val key = item.uri.toString()
                image.tag = key
                itemView.setOnClickListener {
                    openPreview(bindingAdapterPosition)
                }
                val resolver = contentResolver
                lifecycleScope.launch(Dispatchers.IO) {
                    val bmp = try {
                        ThumbLoader.thumbnail(resolver, item.uri)
                    } catch (e: Exception) {
                        null
                    }
                    withContext(Dispatchers.Main) {
                        // 复用错位防护：仅当此视图仍绑定同一文件时上屏
                        if (image.tag == key && bmp != null) image.setImageBitmap(bmp)
                    }
                }
            }
        }
    }

    companion object {
        const val EXTRA_TREE = "tree"

        fun start(ctx: Context, treeUriRaw: String) {
            ctx.startActivity(
                Intent(ctx, GalleryBrowseActivity::class.java)
                    .putExtra(EXTRA_TREE, treeUriRaw)
            )
        }
    }
}
