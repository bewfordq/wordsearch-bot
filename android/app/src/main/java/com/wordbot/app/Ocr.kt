package com.wordbot.app

import android.content.Context
import com.googlecode.tesseract.android.TessBaseAPI
import java.io.File
import java.util.Locale

/**
 * Tesseract for the bot (android/python/pytesseract): the same engine and eng.traineddata as on the PC. tsv()
 * builds the TSV `tesseract ... tsv` prints (TessBaseAPI::GetTSVText), so the bot parses it the same way.
 *
 * Two engines, each used by one call at a time: the level complete screen's stats are read in the background
 * (a big picture: seconds on the phone) while the bot already looks for the next screen's START. With one
 * engine the second call waited for the first; on the PC each call is a process of its own.
 */
object Ocr {
    private const val ENGINES = 2
    private val lock = Object()
    private val idle = ArrayDeque<TessBaseAPI>()
    private var dir: File? = null

    fun init(context: Context) {
        synchronized(lock) {
            if (dir != null) return
            val d = File(context.filesDir, "tesseract")
            val tessdata = File(d, "tessdata").apply { mkdirs() }
            val model = File(tessdata, "eng.traineddata")
            if (!model.exists() || model.length() == 0L) {
                context.assets.open("tessdata/eng.traineddata").use { input -> model.outputStream().use { input.copyTo(it) } }
            }
            repeat(ENGINES) { idle.addLast(engine(d)) }
            dir = d
        }
    }

    private fun engine(d: File): TessBaseAPI {
        val a = TessBaseAPI()
        if (!a.init(d.absolutePath, "eng")) {
            a.recycle()
            throw IllegalStateException("Tesseract didn't start")
        }
        return a
    }

    private fun <T> withEngine(gray: ByteArray, w: Int, h: Int, psm: Int, whitelist: String, read: (TessBaseAPI) -> T): T {
        val a = synchronized(lock) {
            if (dir == null) throw IllegalStateException("Tesseract not started")
            while (idle.isEmpty()) lock.wait()
            idle.removeFirst()
        }
        try {
            a.setPageSegMode(psm)
            a.setVariable(TessBaseAPI.VAR_CHAR_WHITELIST, whitelist)
            a.setImage(gray, w, h, 1, w)
            return read(a)
        } finally {
            a.clear()
            synchronized(lock) {
                idle.addLast(a)
                lock.notify()
            }
        }
    }

    fun text(gray: ByteArray, w: Int, h: Int, psm: Int, whitelist: String): String =
        withEngine(gray, w, h, psm, whitelist) { it.getUTF8Text() ?: "" }

    fun tsv(gray: ByteArray, w: Int, h: Int, psm: Int, whitelist: String): String =
        withEngine(gray, w, h, psm, whitelist) { a ->
            a.getUTF8Text()  // recognise
            val sb = StringBuilder("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n")
            sb.append("1\t1\t0\t0\t0\t0\t0\t0\t$w\t$h\t-1\t\n")
            val it = a.getResultIterator()
            if (it != null) {
                var block = 0; var par = 0; var line = 0; var word = 0
                fun row(level: Int, ril: Int) {
                    val r = it.getBoundingRect(ril)
                    sb.append("$level\t1\t$block\t$par\t$line\t$word\t${r.left}\t${r.top}\t${r.width()}\t${r.height()}\t-1\t\n")
                }
                it.begin()
                do {
                    val text = it.getUTF8Text(TessBaseAPI.PageIteratorLevel.RIL_WORD) ?: continue
                    if (it.isAtBeginningOf(TessBaseAPI.PageIteratorLevel.RIL_BLOCK)) {
                        block++; par = 0; line = 0; word = 0
                        row(2, TessBaseAPI.PageIteratorLevel.RIL_BLOCK)
                    }
                    if (it.isAtBeginningOf(TessBaseAPI.PageIteratorLevel.RIL_PARA)) {
                        par++; line = 0; word = 0
                        row(3, TessBaseAPI.PageIteratorLevel.RIL_PARA)
                    }
                    if (it.isAtBeginningOf(TessBaseAPI.PageIteratorLevel.RIL_TEXTLINE)) {
                        line++; word = 0
                        row(4, TessBaseAPI.PageIteratorLevel.RIL_TEXTLINE)
                    }
                    word++
                    val r = it.getBoundingRect(TessBaseAPI.PageIteratorLevel.RIL_WORD)
                    val conf = String.format(Locale.US, "%.6f", it.confidence(TessBaseAPI.PageIteratorLevel.RIL_WORD))
                    sb.append("5\t1\t$block\t$par\t$line\t$word\t${r.left}\t${r.top}\t${r.width()}\t${r.height()}\t$conf\t$text\n")
                } while (it.next(TessBaseAPI.PageIteratorLevel.RIL_WORD))
                it.delete()
            }
            sb.toString()
        }
}
