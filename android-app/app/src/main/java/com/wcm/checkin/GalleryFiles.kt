package com.wcm.checkin

import android.content.ContentResolver
import android.net.Uri
import android.provider.DocumentsContract

/**
 * 图库数据源（v0.4.0）：枚举 SAF 相册目录里的图片文件。
 *
 * - 过滤口径：mime 为 image/* 直接收；mime 缺失或是八进制流时按扩展名兜底
 *   （SAF 创建的 jpg/gif 都带正确 mime，外部文件靠扩展名兜住）；
 * - 排序口径：文件名降序 = 最新在前（抓取文件名 yyyyMMdd_HHmmss_作者_序号）。
 * 过滤与排序是纯函数，随 CI 做 JVM 单测。
 */
object GalleryFiles {

    data class GalleryItem(val uri: Uri, val name: String, val size: Long)

    private val IMAGE_EXTS = setOf("jpg", "jpeg", "png", "gif", "webp", "bmp")

    fun isImage(name: String, mime: String?): Boolean {
        val m = mime?.trim()?.lowercase().orEmpty()
        if (m.startsWith("image/")) return true
        // 有明确非图片 mime 时不看扩展名；未识别（含 octet-stream / 空）时按扩展名兜底
        if (m.isNotEmpty() && m != "application/octet-stream") return false
        val ext = name.substringAfterLast('.', "").lowercase()
        return ext in IMAGE_EXTS
    }

    fun sortNames(names: List<String>): List<String> = names.sortedByDescending { it }

    /** 列出目录内全部图片（最新在前）。目录不可读时抛出的异常由调用方兜底。 */
    fun listImages(resolver: ContentResolver, treeUri: Uri): List<GalleryItem> {
        val rootId = DocumentsContract.getTreeDocumentId(treeUri)
        val childrenUri = DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, rootId)
        val names = ArrayList<GalleryItem>()
        resolver.query(
            childrenUri,
            arrayOf(
                DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                DocumentsContract.Document.COLUMN_DISPLAY_NAME,
                DocumentsContract.Document.COLUMN_MIME_TYPE,
                DocumentsContract.Document.COLUMN_SIZE,
            ),
            null, null, null,
        )?.use { cursor ->
            val idCol = cursor.getColumnIndex(DocumentsContract.Document.COLUMN_DOCUMENT_ID)
            val nameCol = cursor.getColumnIndex(DocumentsContract.Document.COLUMN_DISPLAY_NAME)
            val mimeCol = cursor.getColumnIndex(DocumentsContract.Document.COLUMN_MIME_TYPE)
            val sizeCol = cursor.getColumnIndex(DocumentsContract.Document.COLUMN_SIZE)
            while (cursor.moveToNext()) {
                val name = cursor.getString(nameCol) ?: continue
                if (name.isEmpty()) continue
                val mime = cursor.getString(mimeCol)
                if (!isImage(name, mime)) continue
                val docId = cursor.getString(idCol) ?: continue
                names.add(
                    GalleryItem(
                        uri = DocumentsContract.buildDocumentUriUsingTree(treeUri, docId),
                        name = name,
                        size = cursor.getLong(sizeCol),
                    )
                )
            }
        }
        return names.sortedByDescending { it.name }
    }
}
