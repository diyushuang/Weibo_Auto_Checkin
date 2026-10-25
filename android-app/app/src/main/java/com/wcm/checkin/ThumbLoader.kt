package com.wcm.checkin

import android.content.ContentResolver
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.ImageDecoder
import android.graphics.drawable.BitmapDrawable
import android.graphics.drawable.Drawable
import android.net.Uri
import android.os.Build
import android.util.LruCache

/**
 * 图片解码（v0.4.0）：网格缩略图（采样 + LruCache）与全屏预览大图。
 *
 * 预览优先走 ImageDecoder（API 28+）：GIF 返回 AnimatedImageDrawable 端内直接动起来，
 * 大图按 inSampleSize 降到长边 2048 防 OOM；旧系统 / 解码失败回退 BitmapFactory 静态帧。
 */
object ThumbLoader {

    private const val THUMB_SIZE = 256
    private const val FULL_SIZE = 2048

    // lazy：JVM 单元测试只测 computeInSampleSize 等纯函数，不触发 framework LruCache
    private val cache: LruCache<String, Bitmap> by lazy {
        object : LruCache<String, Bitmap>(cacheKb()) {
            override fun sizeOf(key: String, value: Bitmap): Int = value.byteCount / 1024
        }
    }

    private fun cacheKb(): Int =
        ((Runtime.getRuntime().maxMemory() / 1024) / 8).toInt().coerceIn(8 * 1024, 64 * 1024)

    /** 官方 inSampleSize 算法：半宽高逐级减半，缩到刚好覆盖 reqSize。 */
    fun computeInSampleSize(width: Int, height: Int, reqSize: Int): Int {
        if (width <= 0 || height <= 0 || reqSize <= 0) return 1
        var sample = 1
        if (width > reqSize || height > reqSize) {
            val halfWidth = width / 2
            val halfHeight = height / 2
            while (halfWidth / sample >= reqSize && halfHeight / sample >= reqSize) {
                sample *= 2
            }
        }
        return sample
    }

    /** 缩略图（带缓存；GIF 取首帧）。失败返回 null。 */
    fun thumbnail(resolver: ContentResolver, uri: Uri): Bitmap? {
        val key = uri.toString()
        cache.get(key)?.let { return it }
        val bmp = decodeSampled(resolver, uri, THUMB_SIZE) ?: return null
        cache.put(key, bmp)
        return bmp
    }

    /** 两次流式解码（先边界再采样），任何异常吞为 null 由调用方占位。 */
    fun decodeSampled(resolver: ContentResolver, uri: Uri, reqSize: Int): Bitmap? {
        return try {
            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            resolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, bounds) }
            if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null
            val opts = BitmapFactory.Options().apply {
                inSampleSize = computeInSampleSize(bounds.outWidth, bounds.outHeight, reqSize)
            }
            resolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, opts) }
        } catch (e: Exception) {
            null
        }
    }

    /** 预览大图：优先 ImageDecoder（GIF 动画 + 采样），回退 BitmapFactory。 */
    fun fullDrawable(resolver: ContentResolver, uri: Uri): Drawable? {
        if (Build.VERSION.SDK_INT >= 28) {
            try {
                val source = ImageDecoder.createSource(resolver, uri)
                return ImageDecoder.decodeDrawable(source) { decoder, info, _ ->
                    val sample = computeInSampleSize(info.size.width, info.size.height, FULL_SIZE)
                    if (sample > 1) decoder.setTargetSampleSize(sample)
                }
            } catch (e: Exception) {
                // 回退静态帧
            }
        }
        return decodeSampled(resolver, uri, FULL_SIZE)?.let { BitmapDrawable(null, it) }
    }
}
