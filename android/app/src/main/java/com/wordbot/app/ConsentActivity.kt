package com.wordbot.app

import android.app.Activity
import android.content.Intent
import android.media.projection.MediaProjectionManager
import android.os.Bundle

/**
 * Start: asks Android for screen capture the first time (once per app start: "Start now"), then starts the bot.
 * Invisible otherwise.
 */
class ConsentActivity : Activity() {
    companion object {
        const val EXTRA_THEN = "then"  // what to do once capture runs (BotService.ACTION_START by default)
    }

    private val then: String get() = intent.getStringExtra(EXTRA_THEN) ?: BotService.ACTION_START

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Capture.running) {
            startForegroundService(Intent(this, BotService::class.java).setAction(then))
            finish()
            return
        }
        @Suppress("DEPRECATION")
        startActivityForResult(getSystemService(MediaProjectionManager::class.java).createScreenCaptureIntent(), 1)
    }

    @Deprecated("startActivityForResult is enough here")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (resultCode == RESULT_OK && data != null) {
            startForegroundService(
                Intent(this, BotService::class.java).setAction(then)
                    .putExtra(BotService.EXTRA_CODE, resultCode).putExtra(BotService.EXTRA_DATA, data),
            )
        }
        finish()
    }
}
