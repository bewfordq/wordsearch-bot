package com.wordbot.app

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Pause / Resume / Stop / Quit from the notification and the widget, passed to the running service. */
class ControlReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action ?: return
        BotService.instance?.control(action)
    }
}
