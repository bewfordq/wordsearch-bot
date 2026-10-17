package com.wordbot.app

import android.Manifest
import android.content.ComponentName
import android.content.Context
import android.content.pm.PackageManager
import android.os.SystemClock
import android.provider.Settings

/**
 * Wordbot's accessibility service (TapService) in Android's settings, and switching it back on.
 *
 * Android drops it in two ways (tried on the Android 13 emulator, 2026-10-16):
 * - a force stop switches it off in Settings for good. MIUI force stops apps for "Clear all" in recents, swiping
 *   the app away, and in its memory and battery cleaners.
 * - a crash or a kill leaves it on in Settings but disconnected until Android restarts the app (MIUI only does that
 *   with Autostart on). Switching it off and on again is what makes Android try again.
 *
 * Writing the setting needs WRITE_SECURE_SETTINGS, granted once over USB:
 *   adb shell pm grant com.wordbot.app android.permission.WRITE_SECURE_SETTINGS
 */
object A11y {
    fun canRepair(ctx: Context): Boolean =
        ctx.checkSelfPermission(Manifest.permission.WRITE_SECURE_SETTINGS) == PackageManager.PERMISSION_GRANTED

    private fun ours(ctx: Context) = ComponentName(ctx, TapService::class.java)

    private fun enabled(ctx: Context): List<String> =
        (Settings.Secure.getString(ctx.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES) ?: "")
            .split(':').filter { it.isNotBlank() }

    /**
     * Switches the service on; off first when Settings still lists it (a crashed service Android gave up on).
     * Waits for Android to take the first change in, so not on the main thread. False without the permission.
     */
    @Synchronized
    fun repair(ctx: Context): Boolean {
        if (!canRepair(ctx)) return false
        val cr = ctx.contentResolver
        val all = enabled(ctx)
        val others = all.filter { ComponentName.unflattenFromString(it) != ours(ctx) }
        return try {
            if (others.size != all.size) {
                Settings.Secure.putString(cr, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, others.joinToString(":"))
                Thread.sleep(800)
            }
            val on = others + ours(ctx).flattenToString()
            Settings.Secure.putString(cr, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, on.joinToString(":"))
            Settings.Secure.putInt(cr, Settings.Secure.ACCESSIBILITY_ENABLED, 1)
            true
        } catch (_: SecurityException) {
            false
        }
    }

    /** Waits up to ms for the service to connect. */
    fun waitConnected(ms: Long): Boolean {
        val end = SystemClock.elapsedRealtime() + ms
        while (TapService.instance == null && SystemClock.elapsedRealtime() < end) Thread.sleep(100)
        return TapService.instance != null
    }
}
