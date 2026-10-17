package com.wordbot.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONObject
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Runs the bot: owns the screen capture, starts the Python bot and its dashboard (android_app.py), and shows its
 * status in a pinned notification with Start / Pause / Resume / Stop, refreshed every second (the widget too).
 * While the bot plays it watches the accessibility service and switches it back on when Android drops it (A11y),
 * and leaves a heartbeat so an app Android killed mid-game carries on once restarted (Recovery).
 */
class BotService : Service() {
    companion object {
        const val ACTION_INIT = "com.wordbot.app.INIT"      // just load Python, the bot and the dashboard
        const val ACTION_START = "com.wordbot.app.START"
        const val ACTION_PAUSE = "com.wordbot.app.PAUSE"
        const val ACTION_RESUME = "com.wordbot.app.RESUME"
        const val ACTION_STOP = "com.wordbot.app.STOP"
        const val ACTION_QUIT = "com.wordbot.app.QUIT"
        const val ACTION_DRY_RUN_ON = "com.wordbot.app.DRY_RUN_ON"
        const val ACTION_DRY_RUN_OFF = "com.wordbot.app.DRY_RUN_OFF"
        const val EXTRA_CODE = "code"
        const val EXTRA_DATA = "data"
        private const val CHANNEL = "bot"
        private const val NOTIFICATION = 1

        @Volatile var instance: BotService? = null
            private set
        @Volatile var state: JSONObject = JSONObject().put("status", "stopped").put("phase", "Not started")
            private set
        @Volatile var ready = false   // the dashboard is up
            private set

        fun startIntent(ctx: Context): PendingIntent = PendingIntent.getActivity(
            ctx, 0, Intent(ctx, ConsentActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )

        fun controlIntent(ctx: Context, action: String): PendingIntent = PendingIntent.getBroadcast(
            ctx, action.hashCode(), Intent(ctx, ControlReceiver::class.java).setAction(action),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
    }

    private val python = Executors.newSingleThreadExecutor()      // calls that may take a while (start, init)
    private val status = Executors.newSingleThreadScheduledExecutor()
    private var app: PyObject? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var wasRunning = false
    private var wasPlaying = false
    private var beatAt = 0L
    private var a11yLostAt = 0L      // when the bot, playing, lost the accessibility service
    private var a11yRepairAt = 0L
    private var shown = ""      // what the notification and the widget show now
    private var shownAt = 0L

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Bot status", NotificationManager.IMPORTANCE_LOW))
        status.scheduleWithFixedDelay({ refresh() }, 1, 1, TimeUnit.SECONDS)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // A foreground service of the screen capture type may only start once the user has allowed the capture.
        // Before that (the app's screen loading the dashboard) the service runs as a plain one, alongside the app.
        val capture = intent?.action == ACTION_START && intent.hasExtra(EXTRA_DATA) || Capture.running
        if (capture) startForeground(NOTIFICATION, notification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION)
        when (val action = intent?.action) {
            ACTION_START -> {
                @Suppress("DEPRECATION")
                val data: Intent? = intent.getParcelableExtra(EXTRA_DATA)
                if (data != null) Capture.start(this, intent.getIntExtra(EXTRA_CODE, 0), data)
                Recovery.started(this)
                python.execute {
                    if (ensurePython().isEmpty()) {
                        ensureAccessibility()
                        app?.callAttr("start")
                    }
                }
            }
            ACTION_INIT, null -> python.execute { ensurePython() }
            else -> control(action)
        }
        return START_NOT_STICKY
    }

    fun control(action: String) {
        if (action == ACTION_PAUSE || action == ACTION_STOP || action == ACTION_QUIT) Recovery.heartbeat(this, false)
        when (action) {
            ACTION_QUIT -> {
                python.execute { app?.callAttr("stop") }
                Capture.stop()
                awake(false)
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
            }
            else -> python.execute {
                when (action) {
                    ACTION_PAUSE -> app?.callAttr("pause")
                    ACTION_RESUME -> app?.callAttr("resume")
                    ACTION_STOP -> app?.callAttr("stop")
                    ACTION_DRY_RUN_ON -> app?.callAttr("set_dry_run", true)
                    ACTION_DRY_RUN_OFF -> app?.callAttr("set_dry_run", false)
                }
            }
        }
    }

    /** Python, Tesseract, the bot and its dashboard, once per process. Returns "" or what went wrong. */
    private fun ensurePython(): String {
        if (app != null) return ""
        return try {
            state = JSONObject().put("status", "starting").put("phase", "Loading the bot")
            if (!Python.isStarted()) Python.start(AndroidPlatform(this))
            Ocr.init(this)
            val module = Python.getInstance().getModule("android_app")
            val home = getExternalFilesDir(null)!!.absolutePath
            val err = module.callAttr("init", Bridge(applicationContext), home).toString()
            if (err.isEmpty()) {
                app = module
                ready = true
                for ((level, msg) in Recovery.exits(this)) note(level, msg)
            } else state = JSONObject().put("status", "error").put("phase", err.lineSequence().first())
            err
        } catch (e: Exception) {
            state = JSONObject().put("status", "error").put("phase", e.toString())
            e.toString()
        }
    }

    /** A line in the bot's log (the dashboard's), from here. */
    private fun note(level: String, message: String) {
        try { app?.callAttr("note", level, message) } catch (_: Exception) {}
    }

    /**
     * Start: the accessibility service switched back on if Android switched it off (a force stop does). It may be
     * connecting right now (the app has just started), so that gets a moment first. Without the permission the
     * bot says what to turn on.
     */
    private fun ensureAccessibility() {
        if (TapService.instance != null || A11y.waitConnected(1500) || !A11y.canRepair(this)) return
        if (A11y.repair(this) && A11y.waitConnected(5000)) {
            note("info", "Wordbot's accessibility service was off: switched it back on")
        } else {
            note("warn", "Wordbot's accessibility service didn't come back on: Settings > Accessibility > Wordbot")
        }
    }

    /**
     * Playing and the accessibility service gone (Android dropped it, see A11y): every swipe would fail, so after a
     * few seconds (MIUI sometimes reconnects it by itself) it's switched back on. On the status thread.
     */
    private fun watchAccessibility(running: Boolean, now: Long) {
        if (!running || TapService.instance != null) {
            a11yLostAt = 0L
            return
        }
        if (a11yLostAt == 0L) {
            a11yLostAt = now
            return
        }
        // Without the permission the bot reports it (every swipe fails until it's turned on in Settings).
        if (now - a11yLostAt < 3000 || now - a11yRepairAt < 20_000 || !A11y.canRepair(this)) return
        a11yRepairAt = now
        if (A11y.repair(this) && A11y.waitConnected(5000)) {
            note("warn", "Android dropped Wordbot's accessibility service: switched it back on")
        } else {
            note("error", "Android dropped Wordbot's accessibility service and it didn't come back on")
        }
    }

    private fun refresh() {
        val a = app
        if (a != null) {
            try { state = JSONObject(a.callAttr("state").toString()) } catch (_: Exception) {}
        }
        val running = state.optBoolean("running")
        if (running != wasRunning) {
            awake(running)
            wasRunning = running
        }
        val now = SystemClock.elapsedRealtime()
        val playing = running && !state.optBoolean("paused")
        if (playing != wasPlaying || playing && now - beatAt > 30_000) {
            Recovery.heartbeat(this, playing)
            wasPlaying = playing
            beatAt = now
        }
        watchAccessibility(running, now)
        // Only when what they show has changed (or once a minute): each update makes the system UI and the
        // launcher redraw, on the CPU the game and the bot share.
        val showing = "${summary()}|${contentText()}|$running|${state.optBoolean("paused")}"
        if (showing != shown || now - shownAt > 60_000) {
            shown = showing
            shownAt = now
            getSystemService(NotificationManager::class.java).notify(NOTIFICATION, notification())
            BotWidget.update(this, summary(), running)
        }
    }

    private fun contentText(): String {
        val err = state.optString("device_error", "")
        return "${state.optInt("session_levels")} levels · ${state.optInt("session_words")} words this session" +
            if (err.isNotEmpty() && err != "null" && state.optBoolean("running")) " · $err" else ""
    }

    private fun awake(on: Boolean) {
        TapService.screenOn = on
        if (on && wakeLock == null) {
            wakeLock = getSystemService(PowerManager::class.java)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "wordbot:bot").apply { acquire() }
        } else if (!on) {
            wakeLock?.release()
            wakeLock = null
        }
    }

    fun summary(): String {
        val level = state.opt("level")
        val head = if (level != null && level != JSONObject.NULL) "Level $level" else "Wordbot"
        val what = when {
            state.optBoolean("paused") -> "paused"
            state.optBoolean("running") -> state.optString("phase")
            else -> state.optString("status")
        }
        return "$head · $what"
    }

    private fun notification(): Notification {
        val running = state.optBoolean("running")
        val paused = state.optBoolean("paused")
        val b = Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setContentTitle(summary())
            .setContentText(contentText())
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(PendingIntent.getActivity(this, 1, Intent(this, MainActivity::class.java),
                PendingIntent.FLAG_IMMUTABLE))
        fun action(label: String, pi: PendingIntent) =
            b.addAction(Notification.Action.Builder(null, label, pi).build())
        when {
            running && paused -> { action("Resume", controlIntent(this, ACTION_RESUME)); action("Stop", controlIntent(this, ACTION_STOP)) }
            running -> { action("Pause", controlIntent(this, ACTION_PAUSE)); action("Stop", controlIntent(this, ACTION_STOP)) }
            else -> { action("Start", startIntent(this)); action("Quit", controlIntent(this, ACTION_QUIT)) }
        }
        return b.build()
    }

    override fun onDestroy() {
        instance = null
        ready = false
        status.shutdownNow()
        awake(false)
        state = JSONObject().put("status", "stopped").put("phase", "Not started")
        BotWidget.update(this, summary(), false)
        super.onDestroy()
    }
}
