package com.wordbot.app

import android.content.Context
import android.content.Intent
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.Image
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Handler
import android.os.HandlerThread
import android.view.WindowManager
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * The phone's screen, frame by frame (MediaProjection into an ImageReader): what the scrcpy stream gives the bot
 * on the PC. Android only produces a frame when something on screen changes; the newest one is kept.
 *
 * The bot copies frames out with copyInto, into a buffer of its own that it reuses: a new 10 MB array per frame
 * (copyFrame) kept the garbage collector busy, on the same CPU as the game. While it waits for a letter to land
 * it copies only the tapped cell (~30 KB) out of each frame.
 */
object Capture {
    const val HEADER = 32  // copyInto: int32 frame width, height, x, y, w, h, int64 frame number (little endian)

    private val lock = Object()
    private var projection: MediaProjection? = null
    private var display: VirtualDisplay? = null
    private var reader: ImageReader? = null
    private var thread: HandlerThread? = null
    private var latest: Image? = null
    @Volatile private var latestAt = 0L  // System.nanoTime() when the newest frame arrived

    @Volatile var seq: Long = 0
        private set
    @Volatile var width = 0
        private set
    @Volatile var height = 0
        private set

    val running: Boolean get() = projection != null && display != null

    fun start(context: Context, resultCode: Int, data: Intent) {
        stop()
        val mpm = context.getSystemService(MediaProjectionManager::class.java)
        val p = mpm.getMediaProjection(resultCode, data) ?: return
        val bounds = context.getSystemService(WindowManager::class.java).maximumWindowMetrics.bounds
        width = bounds.width()
        height = bounds.height()
        val t = HandlerThread("capture").apply { start() }
        val handler = Handler(t.looper)
        val r = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 3)
        r.setOnImageAvailableListener({ rd ->
            val img = try { rd.acquireLatestImage() } catch (e: IllegalStateException) { null }
            if (img != null) synchronized(lock) {
                latest?.close()
                latest = img
                latestAt = System.nanoTime()
                seq++
                lock.notifyAll()
            }
        }, handler)
        p.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() { stop() }
        }, handler)
        projection = p
        reader = r
        thread = t
        display = p.createVirtualDisplay(
            "wordbot", width, height, context.resources.displayMetrics.densityDpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, r.surface, null, handler,
        )
    }

    fun stop() {
        val p = projection
        projection = null
        synchronized(lock) {
            latest?.close()
            latest = null
            lock.notifyAll()
        }
        display?.release()
        display = null
        reader?.close()
        reader = null
        thread?.quitSafely()
        thread = null
        try { p?.stop() } catch (_: Exception) {}
    }

    /** int32 width, int32 height, int64 frame number (little endian), then width*height RGBA pixels. */
    fun copyFrame(): ByteArray = synchronized(lock) {
        val img = latest ?: return ByteArray(0)
        val w = img.width
        val h = img.height
        val out = ByteArray(16 + w * h * 4)
        ByteBuffer.wrap(out, 0, 16).order(ByteOrder.LITTLE_ENDIAN).putInt(w).putInt(h).putLong(seq)
        copyRows(img, out, 16, 0, 0, w, h)
        out
    }

    /**
     * Copies the rectangle (x, y, w, h) of the newest frame, clipped to it (w or h <= 0: to the frame's edge),
     * into dst: the HEADER, then its RGBA pixels row by row. Returns the bytes written, 0 when there's no frame
     * yet, or minus the bytes needed when dst is too small.
     */
    fun copyInto(dst: ByteArray, x: Int, y: Int, w: Int, h: Int): Int = synchronized(lock) {
        val img = latest ?: return 0
        val fw = img.width
        val fh = img.height
        val x1 = x.coerceIn(0, fw)
        val y1 = y.coerceIn(0, fh)
        val x2 = if (w <= 0) fw else (x + w).coerceIn(x1, fw)
        val y2 = if (h <= 0) fh else (y + h).coerceIn(y1, fh)
        val n = HEADER + (x2 - x1) * (y2 - y1) * 4
        if (dst.size < n) return -n
        ByteBuffer.wrap(dst, 0, HEADER).order(ByteOrder.LITTLE_ENDIAN)
            .putInt(fw).putInt(fh).putInt(x1).putInt(y1).putInt(x2 - x1).putInt(y2 - y1).putLong(seq)
        copyRows(img, dst, HEADER, x1, y1, x2 - x1, y2 - y1)
        n
    }

    private fun copyRows(img: Image, dst: ByteArray, offset: Int, x: Int, y: Int, w: Int, h: Int) {
        val plane = img.planes[0]
        val buf = plane.buffer
        val row = w * 4
        if (row == 0 || h == 0) return
        if (x == 0 && plane.rowStride == row) {
            buf.position(y * plane.rowStride)
            buf.get(dst, offset, row * h)
        } else {
            for (r in 0 until h) {
                buf.position((y + r) * plane.rowStride + x * plane.pixelStride)
                buf.get(dst, offset + r * row, row)
            }
        }
        buf.rewind()
    }

    /** How long ago the newest frame arrived, in ms; -1 before the first. */
    fun frameAgeMs(): Long = if (seq == 0L) -1 else (System.nanoTime() - latestAt) / 1_000_000

    /** Waits up to ms for a frame newer than `after`; returns the newest frame number. */
    fun waitFrame(after: Long, ms: Long): Long = synchronized(lock) {
        val end = System.currentTimeMillis() + ms
        while (seq <= after && running) {
            val left = end - System.currentTimeMillis()
            if (left <= 0) break
            lock.wait(left)
        }
        seq
    }
}
