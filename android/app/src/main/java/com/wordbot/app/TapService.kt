package com.wordbot.app

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Intent
import android.graphics.Path
import android.graphics.PixelFormat
import android.os.Handler
import android.os.HandlerThread
import android.os.Looper
import android.view.View
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * The bot's fingers: taps and drags as accessibility gestures, the back button, and which app is in front.
 * While the bot plays it also keeps the screen on (a 1-pixel overlay), as scrcpy's stay_awake did.
 *
 * Android can drop the service while the bot plays (see A11y): BotService switches it back on, and a new connection
 * puts the screen overlay back and, in an app Android restarted mid-game, carries on (Recovery).
 */
class TapService : AccessibilityService() {
    companion object {
        @Volatile var instance: TapService? = null
            private set

        /** Keep the screen on (the bot plays). Kept here so a new connection of the service puts it back. */
        @Volatile var screenOn = false
            set(on) {
                field = on
                instance?.applyScreenOn()
            }
    }

    @Volatile private var lastPackage: String? = null
    @Volatile private var lastActivity: Pair<String, String>? = null  // (package, activity class)
    private val main = Handler(Looper.getMainLooper())
    private var keepOn: View? = null
    // Gesture results come on a thread of their own: on the main thread they waited behind the app's screen,
    // the widget and the notification, and the bot waits for every one before looking at the screen again.
    private val gestureThread = HandlerThread("gestures").apply { start() }
    private val gestures = Handler(gestureThread.looper)

    override fun onServiceConnected() {
        instance = this
        applyScreenOn()
        Recovery.serviceConnected(this)
    }

    // Only this connection's own: if Android connects the service again (MIUI restarting it, say), a late unbind or
    // destroy of the old connection mustn't make the bot think the service is off while the new one works.
    override fun onUnbind(intent: Intent?): Boolean {
        if (instance === this) instance = null
        main.post { removeOverlay() }
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        removeOverlay()
        gestureThread.quitSafely()
        super.onDestroy()
    }

    override fun onInterrupt() {}

    override fun onAccessibilityEvent(event: AccessibilityEvent) {
        if (event.eventType == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) {
            val pkg = event.packageName?.toString() ?: return
            lastPackage = pkg
            // Dialogs and popups report view classes; only activities say which screen is in front.
            val cls = event.className?.toString() ?: return
            if (cls.endsWith("Activity")) lastActivity = pkg to cls
        }
    }

    fun foreground(): String? =
        try { rootInActiveWindow?.packageName?.toString() } catch (_: Exception) { null } ?: lastPackage

    fun activity(): String? {
        val a = lastActivity ?: return null
        return if (a.first == foreground()) a.second else null
    }

    private fun send(gesture: GestureDescription, timeoutMs: Long): Boolean {
        val done = CountDownLatch(1)
        var ok = false
        val sent = dispatchGesture(gesture, object : GestureResultCallback() {
            override fun onCompleted(g: GestureDescription?) { ok = true; done.countDown() }
            override fun onCancelled(g: GestureDescription?) { done.countDown() }
        }, gestures)
        if (!sent) return false
        done.await(timeoutMs, TimeUnit.MILLISECONDS)
        return ok
    }

    fun tap(x: Int, y: Int, holdMs: Long): Boolean {
        val path = Path().apply { moveTo(x.toFloat(), y.toFloat()) }
        val stroke = GestureDescription.StrokeDescription(path, 0, holdMs.coerceAtLeast(1))
        return send(GestureDescription.Builder().addStroke(stroke).build(), holdMs + 2000)
    }

    /** Move at an even pace, then hold still before lifting (no fling), like the stream's drag. */
    fun drag(x1: Int, y1: Int, x2: Int, y2: Int, moveMs: Long, holdMs: Long): Boolean {
        val path = Path().apply { moveTo(x1.toFloat(), y1.toFloat()); lineTo(x2.toFloat(), y2.toFloat()) }
        val move = GestureDescription.StrokeDescription(path, 0, moveMs.coerceAtLeast(1), holdMs > 0)
        if (!send(GestureDescription.Builder().addStroke(move).build(), moveMs + 2000)) return false
        if (holdMs <= 0) return true
        val rest = Path().apply { moveTo(x2.toFloat(), y2.toFloat()); lineTo(x2.toFloat(), y2 + 1f) }
        val hold = move.continueStroke(rest, 0, holdMs, false)
        return send(GestureDescription.Builder().addStroke(hold).build(), holdMs + 2000)
    }

    // A drag in two halves, so the bot can look at the screen while the finger rests on the last letter (does the
    // game draw its selection?): beginDrag moves and stays down, endDrag holds still and lifts.
    @Volatile private var held: GestureDescription.StrokeDescription? = null
    @Volatile private var heldAt: Pair<Int, Int>? = null

    fun beginDrag(x1: Int, y1: Int, x2: Int, y2: Int, moveMs: Long): Boolean {
        val path = Path().apply { moveTo(x1.toFloat(), y1.toFloat()); lineTo(x2.toFloat(), y2.toFloat()) }
        val move = GestureDescription.StrokeDescription(path, 0, moveMs.coerceAtLeast(1), true)
        held = null
        if (!send(GestureDescription.Builder().addStroke(move).build(), moveMs + 2000)) return false
        held = move
        heldAt = x2 to y2
        return true
    }

    fun endDrag(holdMs: Long): Boolean {
        val move = held ?: return false
        val (x, y) = heldAt ?: return false
        held = null
        val rest = Path().apply { moveTo(x.toFloat(), y.toFloat()); lineTo(x.toFloat(), y + 1f) }
        val lift = move.continueStroke(rest, 0, holdMs.coerceAtLeast(1), false)
        return send(GestureDescription.Builder().addStroke(lift).build(), holdMs + 2000)
    }

    fun back(): Boolean = performGlobalAction(GLOBAL_ACTION_BACK)

    fun home(): Boolean = performGlobalAction(GLOBAL_ACTION_HOME)

    private fun applyScreenOn() = main.post {
        val wm = getSystemService(WindowManager::class.java)
        val on = screenOn && instance === this
        if (on && keepOn == null) {
            val v = View(this)
            val lp = WindowManager.LayoutParams(
                1, 1, WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
                    WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON,
                PixelFormat.TRANSLUCENT,
            )
            try { wm.addView(v, lp); keepOn = v } catch (_: Exception) {}
        } else if (!on) {
            removeOverlay()
        }
    }

    private fun removeOverlay() {
        val v = keepOn ?: return
        keepOn = null
        try { getSystemService(WindowManager::class.java).removeView(v) } catch (_: Exception) {}
    }
}
