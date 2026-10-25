package com.wcm.checkin

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Matrix
import android.graphics.drawable.AnimatedImageDrawable
import android.graphics.drawable.Drawable
import android.os.Build
import android.util.AttributeSet
import android.view.GestureDetector
import android.view.MotionEvent
import android.view.ScaleGestureDetector
import androidx.appcompat.widget.AppCompatImageView
import kotlin.math.min

/**
 * 手势缩放大图查看（v0.4.0，矩阵实现零依赖）：单指平移、双指捏合缩放（焦点跟随）、
 * 双击在 1x / 2.5x 间切换。
 *
 * 放大态通过 [onZoomChanged] 通知宿主：ViewPager2 据此停用横向翻页，
 * 平移/缩放才不会被翻页手势抢走；回到 1x 自动恢复翻页。
 */
class ZoomableImageView(context: Context, attrs: AttributeSet? = null) :
    AppCompatImageView(context, attrs) {

    var onZoomChanged: ((Boolean) -> Unit)? = null

    private val m = Matrix()
    private var baseScale = 1f
    private var zoom = 1f
    private var zoomedNotified = false
    private val maxZoom = 8f

    private val gesture = GestureDetector(
        context,
        object : GestureDetector.SimpleOnGestureListener() {
            override fun onDown(e: MotionEvent): Boolean = true

            override fun onScroll(
                e1: MotionEvent?,
                e2: MotionEvent,
                distanceX: Float,
                distanceY: Float,
            ): Boolean {
                m.postTranslate(-distanceX, -distanceY)
                applyMatrix(clamp = true)
                return true
            }

            override fun onDoubleTap(e: MotionEvent): Boolean {
                if (zoom > 1.01f) reset() else zoomTo(2.5f, e.x, e.y)
                return true
            }
        },
    )

    private val scaleDetector = ScaleGestureDetector(
        context,
        object : ScaleGestureDetector.SimpleOnScaleGestureListener() {
            override fun onScale(detector: ScaleGestureDetector): Boolean {
                zoomTo(zoom * detector.scaleFactor, detector.focusX, detector.focusY)
                return true
            }
        },
    )

    init {
        scaleType = ScaleType.MATRIX
    }

    override fun setImageDrawable(drawable: Drawable?) {
        super.setImageDrawable(drawable)
        reset()
        if (Build.VERSION.SDK_INT >= 28) {
            (drawable as? AnimatedImageDrawable)?.start()
        }
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        if (w > 0 && h > 0) reset()
    }

    /** 复位到 fit-center（1x）。 */
    fun reset() {
        val d = drawable
        if (d == null || d.intrinsicWidth <= 0 || d.intrinsicHeight <= 0 || width <= 0 || height <= 0) {
            zoom = 1f
            notifyZoom()
            return
        }
        baseScale = min(width / d.intrinsicWidth.toFloat(), height / d.intrinsicHeight.toFloat())
        m.reset()
        m.postScale(baseScale, baseScale)
        m.postTranslate(
            (width - d.intrinsicWidth * baseScale) / 2f,
            (height - d.intrinsicHeight * baseScale) / 2f,
        )
        zoom = 1f
        applyMatrix(clamp = false)
    }

    private fun zoomTo(targetZoom: Float, focusX: Float, focusY: Float) {
        val target = targetZoom.coerceIn(1f, maxZoom)
        val factor = target / zoom
        if (factor == 1f) return
        m.postScale(factor, factor, focusX, focusY)
        zoom = target
        applyMatrix(clamp = true)
    }

    private fun applyMatrix(clamp: Boolean) {
        if (clamp) clampTranslation()
        imageMatrix = m
        notifyZoom()
    }

    /** 平移收界：图边不拖出可视区；图小于视图时居中固定。 */
    private fun clampTranslation() {
        val d = drawable ?: return
        val dw = d.intrinsicWidth * baseScale * zoom
        val dh = d.intrinsicHeight * baseScale * zoom
        if (dw <= 0f || dh <= 0f) return
        val values = FloatArray(9)
        m.getValues(values)
        val left = values[Matrix.MTRANS_X]
        val top = values[Matrix.MTRANS_Y]
        val tx = if (dw <= width) (width - dw) / 2f else left.coerceIn(width - dw, 0f)
        val ty = if (dh <= height) (height - dh) / 2f else top.coerceIn(height - dh, 0f)
        if (tx != left || ty != top) m.postTranslate(tx - left, ty - top)
    }

    private fun notifyZoom() {
        val zoomed = zoom > 1.01f
        if (zoomed != zoomedNotified) {
            zoomedNotified = zoomed
            onZoomChanged?.invoke(zoomed)
        }
    }

    @SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(event: MotionEvent): Boolean {
        scaleDetector.onTouchEvent(event)
        if (!scaleDetector.isInProgress) gesture.onTouchEvent(event)
        // 放大 / 捏合中要独占手势，禁止 ViewPager2 拦截横向滑动；1x 时放行翻页
        if (zoom > 1.01f || scaleDetector.isInProgress) {
            parent?.requestDisallowInterceptTouchEvent(true)
        }
        return true
    }
}
