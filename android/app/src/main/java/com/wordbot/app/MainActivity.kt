package com.wordbot.app

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.NotificationManager
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.view.View
import android.webkit.JavascriptInterface
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView

/**
 * The app's screen: Start / Pause / Resume / Stop, what's still to set up, and below them the bot's full
 * dashboard (the same page as on the PC, served by the bot inside the app) with its settings, log and stats.
 */
class MainActivity : Activity() {
    private val main = Handler(Looper.getMainLooper())
    private lateinit var status: TextView
    private lateinit var setup: LinearLayout
    private lateinit var web: WebView
    private lateinit var loading: TextView
    private var shownChecklist: String? = null
    private var webLoaded = false

    private val tick = object : Runnable {
        override fun run() {
            refresh()
            main.postDelayed(this, 1000)
        }
    }

    /** Called from the dashboard page: its Start button asks for screen capture through the app. */
    inner class AppBridge {
        @JavascriptInterface
        fun start() = main.post { startBot() }
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val pad = (12 * resources.displayMetrics.density).toInt()
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        val top = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(pad, pad, pad, pad / 2) }
        status = TextView(this).apply { textSize = 16f; setTypeface(typeface, Typeface.BOLD) }
        top.addView(status)

        val buttons = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        fun button(label: String, onClick: () -> Unit) = buttons.addView(Button(this).apply {
            text = label
            isAllCaps = false
            textSize = 13f       // five across a phone screen: "Resume" fits on one line
            maxLines = 1
            setPadding(0, paddingTop, 0, paddingBottom)
            setOnClickListener { onClick() }
            layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
        })
        button("Start") { startBot() }
        button("Pause") { BotService.instance?.control(BotService.ACTION_PAUSE) }
        button("Resume") { BotService.instance?.control(BotService.ACTION_RESUME) }
        button("Stop") { BotService.instance?.control(BotService.ACTION_STOP) }
        button("Quit") { BotService.instance?.control(BotService.ACTION_QUIT) }
        top.addView(buttons)
        setup = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        top.addView(setup)
        col.addView(top)

        loading = TextView(this).apply {
            text = "Loading the bot (the first start takes a few seconds)…"
            setPadding(pad, pad, pad, pad)
        }
        col.addView(loading)
        web = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            addJavascriptInterface(AppBridge(), "WordbotApp")
            webViewClient = object : WebViewClient() {
                override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                    if (request.isForMainFrame) {
                        webLoaded = false  // the bot's server isn't up yet: try again on the next tick
                    }
                }
            }
            visibility = View.GONE
            layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f)
        }
        col.addView(web)
        setContentView(col)

        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
    }

    private fun startBot() {
        startActivity(Intent(this, ConsentActivity::class.java))
    }

    override fun onResume() {
        super.onResume()
        // Load Python, the bot and its dashboard (no playing yet).
        startService(Intent(this, BotService::class.java).setAction(BotService.ACTION_INIT))
        main.post(tick)
    }

    override fun onPause() {
        main.removeCallbacks(tick)
        // The live picture keeps streaming otherwise, taking CPU from the game while the bot plays.
        web.loadUrl("about:blank")
        webLoaded = false
        super.onPause()
    }

    /** Sets a TextView's text only when it differs: every setText lays the screen out again. */
    private fun TextView.show(text: String) {
        if (this.text.toString() != text) this.text = text
    }

    private fun refresh() {
        val s = BotService.state
        status.show(BotService.instance?.summary() ?: "Wordbot · ${s.optString("phase")}")

        if (BotService.ready && !webLoaded) {
            webLoaded = true
            web.loadUrl("http://127.0.0.1:8765/")
            web.visibility = View.VISIBLE
            loading.visibility = View.GONE
        } else if (!BotService.ready && s.optString("status") == "error") {
            loading.show("The bot couldn't start: ${s.optString("phase")}")
        }

        // The checklist is built again only when something on it changed.
        val accessibility = TapService.instance != null
        val notifications = getSystemService(NotificationManager::class.java).areNotificationsEnabled()
        val battery = getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(packageName)
        val repair = A11y.canRepair(this)
        val silent = Recovery.silentCapture(this)
        val checklist = "$accessibility $notifications $battery $repair $silent"
        if (checklist != shownChecklist) {
            shownChecklist = checklist
            setup.removeAllViews()
            if (!accessibility) item("Accessibility service (swipes and taps)", "Turn on") {
                if (repair) Thread { A11y.repair(applicationContext) }.start()
                else startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
            }
            if (!notifications) item("Notifications (controls while the game is in front)", "Allow") {
                requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
            }
            if (!battery) item("Battery: no restrictions", "Allow") {
                startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
            }
            if (!accessibility || !battery) setup.addView(TextView(this).apply {
                textSize = 12f
                text = "Xiaomi: also Settings > Apps > Wordbot > Autostart on, and lock Wordbot in recents (hold its " +
                    "card): Clear all and MIUI's cleaners force stop apps, and Android then switches the " +
                    "accessibility service off."
            })
            // Both only from a PC (adb): the app can't grant them to itself.
            if (!repair || !silent) setup.addView(TextView(this).apply {
                textSize = 12f
                setTextIsSelectable(true)
                text = "So Wordbot can switch its accessibility service back on and carry on by itself when Android " +
                    "stops it, run once with the phone plugged into the PC:" +
                    (if (!repair) "\nadb shell pm grant $packageName android.permission.WRITE_SECURE_SETTINGS" else "") +
                    (if (!silent) "\nadb shell appops set $packageName PROJECT_MEDIA allow" else "")
            })
        }
    }

    private fun item(label: String, fix: String, onFix: () -> Unit) {
        val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        row.addView(TextView(this).apply {
            text = "✗  $label"
            setTextColor(Color.rgb(190, 40, 40))
            textSize = 14f
            layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
        })
        row.addView(Button(this).apply { text = fix; isAllCaps = false; setOnClickListener { onFix() } })
        setup.addView(row)
    }
}
