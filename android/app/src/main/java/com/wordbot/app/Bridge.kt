package com.wordbot.app

import android.content.Context
import android.content.Intent
import android.os.BatteryManager
import android.os.PowerManager
import android.view.WindowManager

/** What the Python bot calls (wordbot/android_device.py): the phone's screen, taps, apps, OCR and battery. */
class Bridge(private val context: Context) {
    fun copyFrame(): ByteArray = Capture.copyFrame()
    fun newBuffer(size: Int): ByteArray = ByteArray(size)
    fun copyInto(dst: ByteArray, x: Int, y: Int, w: Int, h: Int): Int = Capture.copyInto(dst, x, y, w, h)
    fun frameAgeMs(): Long = Capture.frameAgeMs()
    fun frameSeq(): Long = Capture.seq
    fun waitFrame(after: Long, ms: Long): Long = Capture.waitFrame(after, ms)
    fun captureRunning(): Boolean = Capture.running
    fun accessibilityRunning(): Boolean = TapService.instance != null

    fun screenWidth(): Int = context.getSystemService(WindowManager::class.java).maximumWindowMetrics.bounds.width()
    fun screenHeight(): Int = context.getSystemService(WindowManager::class.java).maximumWindowMetrics.bounds.height()

    fun tap(x: Int, y: Int, holdMs: Long): Boolean = TapService.instance?.tap(x, y, holdMs) ?: false
    fun drag(x1: Int, y1: Int, x2: Int, y2: Int, moveMs: Long, holdMs: Long): Boolean =
        TapService.instance?.drag(x1, y1, x2, y2, moveMs, holdMs) ?: false
    fun beginDrag(x1: Int, y1: Int, x2: Int, y2: Int, moveMs: Long): Boolean =
        TapService.instance?.beginDrag(x1, y1, x2, y2, moveMs) ?: false
    fun endDrag(holdMs: Long): Boolean = TapService.instance?.endDrag(holdMs) ?: false
    fun back(): Boolean = TapService.instance?.back() ?: false
    fun home(): Boolean = TapService.instance?.home() ?: false
    fun foregroundPackage(): String? = TapService.instance?.foreground()
    /** The activity in front, when it belongs to the app in front (an ad shows as another activity of the game). */
    fun foregroundActivity(): String? = TapService.instance?.activity()

    /** Start (or bring back) an app. Allowed from the background because the accessibility service is bound. */
    fun launchApp(pkg: String): Boolean {
        val launch = context.packageManager.getLaunchIntentForPackage(pkg) ?: return false
        launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED)
        return try { (TapService.instance ?: context).startActivity(launch); true } catch (_: Exception) { false }
    }

    fun ocrTsv(gray: ByteArray, w: Int, h: Int, psm: Int, whitelist: String): String = Ocr.tsv(gray, w, h, psm, whitelist)
    fun ocrText(gray: ByteArray, w: Int, h: Int, psm: Int, whitelist: String): String = Ocr.text(gray, w, h, psm, whitelist)

    fun batteryPercent(): Int =
        context.getSystemService(BatteryManager::class.java).getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
    fun charging(): Boolean = context.getSystemService(BatteryManager::class.java).isCharging
    /** PowerManager.THERMAL_STATUS_*: 0 none, 1 light, 2 moderate, 3 severe (the CPU is held back from here on)... */
    fun thermalStatus(): Int = context.getSystemService(PowerManager::class.java).currentThermalStatus
}
