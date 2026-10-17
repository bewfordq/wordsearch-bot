package `in`.playsimple.wordsearch

import android.app.Activity
import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Rect
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.MotionEvent
import android.view.View
import android.view.WindowInsets
import android.view.WindowInsetsController
import android.view.WindowManager
import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.hypot

/**
 * Stands in for Word Search on the emulator: shows levels the bot saved (assets/levels, made by
 * tools/standin_levels.py). A swipe from a word's first letter to its last (either way) marks it found, as the game
 * does: a coloured capsule over the letters and the word greyed in the list. Once every word is found, the level
 * complete screen; its Next Level button opens the next level. Every swipe is logged (tag FakeGame).
 */
class MainActivity : Activity() {
    class Word(
        val text: String, val fx: Float, val fy: Float, val tx: Float, val ty: Float, val tol: Float,
        val overlay: Bitmap, val ox: Int, val oy: Int, val box: Rect, var found: Boolean = false,
    )

    class Level(val image: Bitmap, val words: List<Word>)

    private lateinit var levels: List<Level>
    private lateinit var complete: Bitmap
    private lateinit var next: Rect
    private var index = 0
    private var done = false
    private val main = Handler(Looper.getMainLooper())

    private fun load(name: String): Bitmap = assets.open("levels/$name").use {
        BitmapFactory.decodeStream(it, null, BitmapFactory.Options().apply { inScaled = false })!!
    }

    private fun rect(a: JSONArray) = Rect(a.getInt(0), a.getInt(1), a.getInt(0) + a.getInt(2), a.getInt(1) + a.getInt(3))

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        window.attributes.layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_ALWAYS
        val spec = JSONObject(assets.open("levels/levels.json").use { it.readBytes().toString(Charsets.UTF_8) })
        val ls = spec.getJSONArray("levels")
        levels = (0 until ls.length()).map { i ->
            val l = ls.getJSONObject(i)
            val ws = l.getJSONArray("words")
            Level(load(l.getString("image")), (0 until ws.length()).map { j ->
                val w = ws.getJSONObject(j)
                val from = w.getJSONArray("from")
                val to = w.getJSONArray("to")
                Word(w.getString("word"), from.getDouble(0).toFloat(), from.getDouble(1).toFloat(),
                    to.getDouble(0).toFloat(), to.getDouble(1).toFloat(), w.getDouble("tol").toFloat(),
                    load(w.getString("overlay")), w.getInt("ox"), w.getInt("oy"), rect(w.getJSONArray("box")))
            })
        }
        complete = load(spec.getString("complete"))
        next = rect(spec.getJSONArray("next"))
        setContentView(Board(this))
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) {
            window.setDecorFitsSystemWindows(false)
            window.insetsController?.let {
                it.hide(WindowInsets.Type.systemBars())
                it.systemBarsBehavior = WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            }
        }
    }

    inner class Board(context: Context) : View(context) {
        private var downX = 0f
        private var downY = 0f
        private val grey = Paint().apply { color = Color.argb(200, 255, 255, 255) }  // black text -> ~200 grey

        private fun scale() = width / 1080f

        override fun onDraw(canvas: Canvas) {
            canvas.save()
            canvas.scale(scale(), scale())
            if (done) {
                canvas.drawBitmap(complete, 0f, 0f, null)
            } else {
                val l = levels[index]
                canvas.drawBitmap(l.image, 0f, 0f, null)
                for (w in l.words.filter { it.found }) {
                    canvas.drawBitmap(w.overlay, w.ox.toFloat(), w.oy.toFloat(), null)
                    canvas.drawRect(w.box, grey)
                }
            }
            canvas.restore()
        }

        override fun onTouchEvent(e: MotionEvent): Boolean {
            val x = e.x / scale()
            val y = e.y / scale()
            when (e.actionMasked) {
                MotionEvent.ACTION_DOWN -> { downX = x; downY = y }
                MotionEvent.ACTION_UP -> {
                    if (done) {
                        if (next.contains(x.toInt(), y.toInt())) {
                            index = (index + 1) % levels.size
                            levels[index].words.forEach { it.found = false }
                            done = false
                            Log.i("FakeGame", "next level -> ${index + 1}")
                            invalidate()
                        }
                        return true
                    }
                    val l = levels[index]
                    val hit = l.words.firstOrNull { w ->
                        !w.found && (
                            (hypot(downX - w.fx, downY - w.fy) < w.tol && hypot(x - w.tx, y - w.ty) < w.tol) ||
                                (hypot(downX - w.tx, downY - w.ty) < w.tol && hypot(x - w.fx, y - w.fy) < w.tol))
                    }
                    Log.i("FakeGame", "swipe (${downX.toInt()},${downY.toInt()}) -> (${x.toInt()},${y.toInt()}) in " +
                        "${e.eventTime - e.downTime} ms: ${hit?.let { "found " + it.text } ?: "no word"}")
                    if (hit != null) {
                        hit.found = true
                        invalidate()
                        if (l.words.all { it.found }) main.postDelayed({
                            done = true
                            Log.i("FakeGame", "level ${index + 1} complete")
                            invalidate()
                        }, 1500)
                    }
                }
            }
            return true
        }
    }
}
