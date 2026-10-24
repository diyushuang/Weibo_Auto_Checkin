package com.wcm.checkin

import android.content.Context
import android.graphics.Bitmap
import android.media.MediaMetadataRetriever
import java.io.File
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

/**
 * Live Photo（mov）→ GIF 转换：帧提取用系统 MediaMetadataRetriever，
 * 编码用自研 GifEncoder。口径对齐交接文档：12fps、长边 640、256 色调色板。
 *
 * mov 落在 cacheDir 临时文件（应用私有目录，随时可回收），成功转出 GIF 后即删，
 * 相册目录里只留 GIF；转换失败时由调用方回退保存原始 mov，内容不丢。
 */
object LivePhotoConverter {

    /**
     * @return GIF 字节；视频太短/解码失败返回 null（调用方回退存 mov）。
     */
    fun movBytesToGif(
        context: Context,
        mov: ByteArray,
        fps: Int = 12,
        maxEdge: Int = 640,
    ): ByteArray? {
        if (mov.isEmpty()) return null
        val tmp = File(context.cacheDir, "live_${System.currentTimeMillis()}.mov")
        val retriever = MediaMetadataRetriever()
        val frames = ArrayList<Bitmap>()
        try {
            tmp.writeBytes(mov)
            retriever.setDataSource(tmp.absolutePath)
            val durMs = retriever.extractMetadata(
                MediaMetadataRetriever.METADATA_KEY_DURATION
            )?.toLongOrNull() ?: 0L
            if (durMs < 300) return null

            val stepMs = 1000.0 / fps
            var t = 0.0
            while (t < durMs) {
                var frame: Bitmap? = null
                try {
                    frame = retriever.getFrameAtTime(
                        (t * 1000).toLong(),
                        MediaMetadataRetriever.OPTION_CLOSEST,
                    )
                } catch (e: Exception) {
                }
                if (frame == null) {
                    try {
                        frame = retriever.getFrameAtTime(
                            (t * 1000).toLong(),
                            MediaMetadataRetriever.OPTION_CLOSEST_SYNC,
                        )
                    } catch (e: Exception) {
                    }
                }
                if (frame != null) {
                    frames.add(scaleDown(frame, maxEdge))
                }
                t += stepMs
            }
            if (frames.size < 2) return null
            val delayCs = (100.0 / fps).roundToInt()
            return GifEncoder.encode(frames, delayCs)
        } catch (e: Exception) {
            return null
        } finally {
            frames.forEach { it.recycle() }
            try {
                retriever.release()
            } catch (e: Exception) {
            }
            tmp.delete()
        }
    }

    /** 长边压到 maxEdge 以内（等比）；小图原样返回。原图立即回收省内存。 */
    private fun scaleDown(src: Bitmap, maxEdge: Int): Bitmap {
        val longEdge = max(src.width, src.height)
        if (longEdge <= maxEdge) return src
        val ratio = maxEdge.toFloat() / longEdge
        val w = max(1, (src.width * ratio).roundToInt())
        val h = max(1, (src.height * ratio).roundToInt())
        val scaled = Bitmap.createScaledBitmap(src, w, h, true)
        if (scaled != src) src.recycle()
        return scaled
    }
}
