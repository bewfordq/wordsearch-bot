package com.wordbot.app

import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.widget.RemoteViews

/** Home-screen widget: the bot's status line with Start and Stop. */
class BotWidget : AppWidgetProvider() {
    override fun onUpdate(context: Context, manager: AppWidgetManager, ids: IntArray) {
        update(context, BotService.instance?.summary() ?: "Wordbot · stopped", BotService.instance != null)
    }

    companion object {
        fun update(context: Context, text: String, running: Boolean) {
            val views = RemoteViews(context.packageName, R.layout.widget)
            views.setTextViewText(R.id.status, text)
            views.setOnClickPendingIntent(R.id.start, BotService.startIntent(context))
            views.setOnClickPendingIntent(R.id.stop, BotService.controlIntent(context, BotService.ACTION_STOP))
            AppWidgetManager.getInstance(context).updateAppWidget(ComponentName(context, BotWidget::class.java), views)
        }
    }
}
