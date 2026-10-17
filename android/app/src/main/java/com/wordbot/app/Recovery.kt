package com.wordbot.app

import android.app.ActivityManager
import android.app.AppOpsManager
import android.app.ApplicationExitInfo
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.os.Process
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * When Android kills the app while the bot plays, the bot goes with it (and so does the accessibility service, see
 * A11y). After a crash or a kill Android starts the app again to connect the accessibility service (on MIUI only
 * with Autostart on); then the bot carries on:
 * - by itself, when screen capture can start without its "Start now" prompt. That needs, once over USB:
 *     adb shell appops set com.wordbot.app PROJECT_MEDIA allow
 * - else from a notification: tap it and "Start now".
 * A force stop starts nothing again, so after one the bot waits for Start.
 *
 * At the next start the bot's log says why Android ended the app (ActivityManager's exit history).
 */
object Recovery {
    const val NOTIFICATION = 2
    private const val CHANNEL = "recovery"
    private const val PREFS = "recovery"
    private const val FRESH_MS = 3 * 60_000L      // a heartbeat older than this: it wasn't killed mid-game
    private const val MAX_RESUMES = 3             // per RESUME_WINDOW_MS: a bot that keeps crashing stays stopped
    private const val RESUME_WINDOW_MS = 30 * 60_000L

    @Volatile private var checked = false  // once per process

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    /** While the bot plays (not paused), BotService calls this every half minute; false once it stops or pauses. */
    fun heartbeat(ctx: Context, playing: Boolean) {
        prefs(ctx).edit().putLong("playing_at", if (playing) System.currentTimeMillis() else 0L).apply()
    }

    /** The user started the bot: nothing left to carry on in this process. */
    fun started(ctx: Context) {
        checked = true
        ctx.getSystemService(NotificationManager::class.java).cancel(NOTIFICATION)
    }

    /** The accessibility service connected (TapService, on the main thread). */
    fun serviceConnected(service: Context) {
        if (checked) return
        checked = true
        val p = prefs(service)
        val now = System.currentTimeMillis()
        val playingAt = p.getLong("playing_at", 0L)
        // Not a kill mid-game, or the user's opening the app (they have Start).
        if (playingAt == 0L || now - playingAt > FRESH_MS || BotService.instance != null) return
        val recent = (p.getString("resumes", "") ?: "").split(',').mapNotNull { it.toLongOrNull() }
            .filter { now - it < RESUME_WINDOW_MS }
        if (recent.size >= MAX_RESUMES) {
            notify(service, "Wordbot keeps getting stopped", "Android ended it ${recent.size + 1} times in half an hour: " +
                "it's left stopped. Tap to start it again.")
            return
        }
        p.edit().putString("resumes", (recent + now).joinToString(",")).apply()
        if (silentCapture(service)) {
            // Allowed from the background: the accessibility service is bound.
            service.startActivity(Intent(service, ConsentActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } else {
            notify(service, "Android stopped Wordbot while it played", "Tap to carry on (then Start now).")
        }
    }

    /** Screen capture starts without asking ("adb shell appops set com.wordbot.app PROJECT_MEDIA allow"). */
    fun silentCapture(ctx: Context): Boolean = try {
        ctx.getSystemService(AppOpsManager::class.java)
            .unsafeCheckOpNoThrow("android:project_media", Process.myUid(), ctx.packageName) == AppOpsManager.MODE_ALLOWED
    } catch (_: Exception) {
        false
    }

    private fun notify(ctx: Context, title: String, text: String) {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Bot stopped by Android", NotificationManager.IMPORTANCE_DEFAULT))
        nm.notify(NOTIFICATION, Notification.Builder(ctx, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_dialog_alert)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setContentIntent(BotService.startIntent(ctx))
            .setAutoCancel(true)
            .build())
    }

    /**
     * How the app's process ended since the last call, oldest first, as (log level, message) for the bot's log:
     * a warning when the bot was playing then.
     */
    fun exits(ctx: Context): List<Pair<String, String>> {
        val p = prefs(ctx)
        val seen = p.getLong("exit_seen", 0L)
        val playingAt = p.getLong("playing_at", 0L)
        val list = try {
            ctx.getSystemService(ActivityManager::class.java).getHistoricalProcessExitReasons(ctx.packageName, 0, 10)
        } catch (_: Exception) {
            return emptyList()
        }.filter { it.processName == ctx.packageName && it.timestamp > seen }.sortedBy { it.timestamp }
        if (list.isEmpty()) return emptyList()
        p.edit().putLong("exit_seen", list.last().timestamp).apply()
        val clock = SimpleDateFormat("MMM d HH:mm:ss", Locale.ENGLISH)
        return list.map { e ->
            val playing = playingAt != 0L && e.timestamp - playingAt in 0..FRESH_MS
            val mb = if (e.pss > 0) ", using ${e.pss / 1024} MB" else ""
            val desc = e.description?.takeIf { it.isNotBlank() }?.let { " [$it]" } ?: ""
            val level = if (playing && e.reason != ApplicationExitInfo.REASON_EXIT_SELF) "warn" else "info"
            level to "Android ended Wordbot at ${clock.format(Date(e.timestamp))}" +
                (if (playing) " while the bot played" else "") + ": ${why(e)}$desc$mb"
        }
    }

    private fun why(e: ApplicationExitInfo): String = when (e.reason) {
        ApplicationExitInfo.REASON_USER_REQUESTED ->
            "force stopped (Clear all or swiped out of recents, MIUI's cleaner or battery saver, or Settings), " +
                "which also switches the accessibility service off"
        ApplicationExitInfo.REASON_SIGNALED -> "killed (signal ${e.status}): a low-memory kill or MIUI's power/memory manager"
        ApplicationExitInfo.REASON_LOW_MEMORY -> "killed: the phone ran out of memory"
        ApplicationExitInfo.REASON_CRASH -> "crashed (Kotlin/Java)"
        ApplicationExitInfo.REASON_CRASH_NATIVE -> "crashed in native code (Python, OpenCV or Tesseract)"
        ApplicationExitInfo.REASON_ANR -> "stopped responding (ANR)"
        ApplicationExitInfo.REASON_EXCESSIVE_RESOURCE_USAGE -> "killed for using too much CPU or battery in the background"
        ApplicationExitInfo.REASON_EXIT_SELF -> "quit"
        ApplicationExitInfo.REASON_PERMISSION_CHANGE -> "a permission was changed"
        ApplicationExitInfo.REASON_INITIALIZATION_FAILURE -> "failed to start"
        ApplicationExitInfo.REASON_DEPENDENCY_DIED -> "something it depends on died"
        ApplicationExitInfo.REASON_OTHER -> "killed by the system"
        else -> "ended (reason ${e.reason})"
    }
}
