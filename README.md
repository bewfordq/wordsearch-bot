# This project is fully ai generated!!!!!!!!!!!

# Wordbot

A bot that plays PlaySimple's **Word Search** Android game (`in.playsimple.wordsearch`) for you. It reads
the board from the screen, finds the words and swipes them. It also handles the level types without a word
list (Picture Puzzles, hidden words, quotes, opposite words), taps through Next Level and Collect, closes
popups and backs out of ads.

You can run it in two ways:

| | **Phone app** | **PC + USB cable** |
|---|---|---|
| Runs on | the phone itself | a Linux PC, with the phone plugged in |
| Install | [`Wordbot.apk`](Wordbot.apk) | Python, adb, Tesseract |
| Controls | the app, its notification, a home-screen widget | a dashboard in the browser |
| Good for | leaving it playing on its own | watching it closely, fixing misreads, tweaking settings |

Both run the same bot code and the same dashboard. Everything runs on your own devices: the screen is read
with OpenCV and Tesseract, and nothing is sent anywhere.

> **Unofficial.** Wordbot has no connection to PlaySimple. Botting probably breaks the game's terms of
> service, so use it at your own risk. It never taps anything that looks like a purchase (see
> [Safety](#safety)).

---

## Phone app

If you want the most simple way to download it, go to releases and download the most recent .apk file.

### What you need

- A **64-bit ARM** Android phone (nearly every phone from the last few years) on **Android 11 or newer**.
- Word Search installed, in **English**.

### Install

1. Copy **Wordbot.apk** to the phone and open it. Allow installing from that app when Android asks. Because
   the APK isn't from the Play Store, Play Protect may warn you about it.
2. Open **Wordbot** and allow notifications. The top of the app lists any setup still missing, each with a
   button to fix it.
3. **Accessibility service**: this is how the bot swipes and taps. Tap **Turn on**, then switch on
   **Wordbot** in the list.
   - If Android says **"Restricted setting"** (Android 13 and newer, for apps installed from a file), go to
     Settings > Apps > Wordbot, tap **⋮** (top right) > **Allow restricted settings**, then try again.
4. **Battery: no restrictions**: tap **Allow**.
5. **Xiaomi phones** also need: Settings > Apps > Wordbot > **Autostart** on, and Wordbot locked in
   recents (hold its card and tap the lock). Otherwise MIUI's cleaners stop it.

### Play

1. Press **Start** in the app (or on the widget, or in the notification).
2. Android asks to record the screen. Choose **Start now**. It asks again each time the app starts fresh.
3. The bot brings Word Search to the front and plays.

**Pause / Resume / Stop** are in the notification while the game is on screen. Open the Wordbot app to see
the full dashboard: the live screen, the log, stats and settings.

When the battery drops below 15% and the phone isn't charging, the bot rests. It also rests at 7% or lower
while charging, because a PC's USB port gives less power than the bot uses. It carries on by itself once
the battery is 20% higher.

### Optional: let it recover by itself (needs a PC once)

Android sometimes stops background apps: battery savers, *Clear all*, crashes, low memory. When that
happens, the accessibility service switches off and the bot stops. Two extra permissions let Wordbot
switch the service back on and pick up where it left off, without you tapping anything. An app can't give
them to itself, so run these once, with USB debugging on and the phone plugged into a computer that has
`adb`:

```
adb shell pm grant com.wordbot.app android.permission.WRITE_SECURE_SETTINGS
adb shell appops set com.wordbot.app PROJECT_MEDIA allow
```

They stay through updates but not through an uninstall. On Xiaomi, the first one also needs
*USB debugging (Security settings)* on in Developer options.

What the app does with them:
- If the service drops mid-game, Wordbot switches it back on within a few seconds and carries on.
- If Android killed and restarted the app mid-game, the bot carries on by itself. Without the second
  permission, a notification asks you to tap it instead. After three restarts in half an hour it stays
  stopped.
- After a force stop, nothing restarts until you open the app.
- The next time it starts, the log says why Android ended the app.

The logs and settings are in `/sdcard/Android/data/com.wordbot.app/files/`. Three settings exist only in the
app, not in the dashboard. They're in that folder's `config.json` under `"android"`:
`battery_stop_pct` (15), `dashboard_fps` (4), and `dashboard_lan` (off; on serves the dashboard to other
devices on your Wi-Fi at port 8765).

---

## PC version

Tested on Ubuntu.

### Setup (once)

```
sudo apt install adb tesseract-ocr scrcpy
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

If `python3 -m venv` complains that `ensurepip` is missing, create the venv with
`python3 -m venv --without-pip .venv` and install the packages with another Python's pip:
`<other-python> -m pip --python .venv/bin/python install -r requirements.txt`.

**Phone:** turn on Developer options → **USB debugging**. On Xiaomi phones also turn on **USB debugging
(Security settings)**, or the taps are ignored. Plug the phone in, accept the prompt and open Word Search.

### Run

```
python3 wordbot.py
```

This opens the dashboard at <http://127.0.0.1:8765>. `wordbot.py` switches to `.venv` by itself. The bot
waits until you press **Start**.

```
python3 wordbot.py                 dashboard only; press Start
python3 wordbot.py --start         start playing straight away
python3 wordbot.py --levels 10     stop after 10 levels
python3 wordbot.py --dry-run       read and solve, never touch the phone
python3 wordbot.py --no-browser    don't open a browser tab
python3 wordbot.py --host 0.0.0.0  reach the dashboard from another device on your network
python3 wordbot.py --port 9000
```

`--levels` and `--dry-run` are saved, just like the matching dashboard settings. Press Ctrl+C to quit.

---

## The dashboard

**Phone (left)**
- The live phone screen, with the grid the bot read and its swipes drawn on top: planned ones orange, done
  ones green, refused ones red.
- **View / Tap / Drag = swipe** switches between watching and using the picture to tap or swipe the phone.
- **Connect, Open game, Back, Home, Save screenshot, Disconnect.**

**Bot (middle)**
- **Start / Pause / Step / Stop.** **Step** does one action (one word, one button), then pauses again.
- Quick switches: dry run, go to the next level by itself, check and redo missed words.
- **Read screen** reads the board and plans the swipes without swiping. **Solve this level** plays the
  current level once. **What screen is this?** says what the bot thinks is on screen. **Tap green button**
  taps the biggest green button.
- The current level: the grid as read, and each word with its state (planned, swiped, found, not taken).
  - Click a word to swipe it now.
  - Click two letters in a straight line to swipe between them.
  - Double-click a letter to fix a misread. The bot plans again with your letter.
  - Letters with an orange border were read by Tesseract. Red ones are unsure.
- Stats for this session and overall, and the last 30 levels with their times.

**Side (right)**
- **Log**: everything the bot does, with warnings and errors in colour.
- **Settings**: every setting below. Changes apply straight away and are saved to `config.json`.
- **Letters**: the letter shapes the bot has learned, and how many pictures it remembers.

## Settings

| Group | Setting | What it does |
|---|---|---|
| Phone | Live stream (scrcpy) | Live video and fast touches. Off: adb screenshots and `input` commands (slower) |
| | Stream fps, bit rate, phone serial | The serial only matters with several phones plugged in |
| Swiping | Swipe time, hold at end, gap between words | Speed of each swipe |
| | Position / timing jitter | Random variation, so the swipes look less mechanical |
| | Check each swipe | Watch the game draw its selection, and swipe again if it didn't |
| Reading and solving | Word order | list, longest, shortest, top, random |
| | Allowed misread letters | A word may disagree with the grid as read in this many letters |
| | Learn letter shapes | From solved words; reading gets faster and more reliable over time |
| Checking | Check words afterwards / after every word, delay, redo attempts | |
| Picture Puzzles / word hunt | Search the grid, hidden words, minimum word frequency, words per check, max guesses | For levels without a word list (see below) |
| | Use hints, max hints per level, paid hints | Paid hints (100 coins) are **off** by default |
| Flow | Auto next level, stop after N levels, pause before each level, rest every N levels | |
| | Restart a stuck level, pause when stuck, pause on errors | |
| | Unknown screen wait / action | What to do on a screen the bot doesn't know: back, tap_center, wait or pause |
| | Close popups, tap unfamiliar green buttons, reopen the game | Unfamiliar green buttons are **off** by default |
| Ads | Press back after, tap ad corners | |
| Logging | Save level screenshots, save unknown screens | `logs/levels`, `logs/unknown` |

## How it plays

Each pass of the loop, the bot looks at the screen and decides what it is:

- **A puzzle.** It finds the title bar, the word list and the letter grid.
  - Grid letters are matched against letter shapes learned on earlier levels. Tesseract reads only the
    letters it doesn't know yet.
  - The word list is read with Tesseract. Found words turn light grey, so it knows what's left.
  - Each word is looked up along the 8 straight directions, then swiped from its first letter to its last.
  - Afterwards it checks which words turned grey and redoes the rest. It learns letter shapes from the
    words the game accepted, so a misread never becomes a template.
- **Levels without a word list.** Hidden words (rows of gold coins, one per letter), Picture Puzzles,
  quotes with blanks, "Solve the sequence!" and "Find the opposite words!". For these, the bot tries real
  English words found in the grid, most common first, and keeps the ones the game marks. A wrong guess
  costs nothing in this game.
  - Coin rows and blanks give each word's length, which narrows the guesses.
  - It remembers each picture's word (`data/pictures/`), so a picture it has seen before is solved at once.
  - If every guess fails, it uses the game's hint (free ones first), then restarts the level once for a
    fresh board, then keeps trying. A level only counts as done when the game moves on.
- **Every swipe is checked.** The game ignores touches while it plays a found word's animation. The bot
  notices that and swipes the word again once the animation is over.
- **A green button** it knows (Next Level, Continue, Collect, Claim, Got it, ...): it taps it. On a popup
  that offers something else, like a duel rematch, it taps "Play Later".
- **A tutorial** (dimmed screen with a white arrow): it taps where the arrow points.
- **A popup with a round X**: it closes it.
- **An ad**: it presses back after a few seconds, then tries the usual close-button corners. If another
  app comes to the front, it reopens Word Search.
  **It is reccomened to use an adblocker on your phone like adguard**
- **Anything else**: it waits, looks for a text button, then does the unknown-screen action. It saves a
  screenshot to `logs/unknown/` so you can see what it was.

## Safety

- It never taps a button that looks like a purchase (BUY, PAY, SHOP, OFFER, SUBSCRIBE, REMOVE ADS, a price,
  ...). A screen that shows a price counts as a shop: the bot only presses back there.
- If Google Play, Google Play services or Wallet comes to the front (a purchase sheet, say), it never
  touches it and presses back to cancel.
- Hints that cost in-game coins are off unless you turn them on.
- **Nothing leaves your devices. The dashboard listens on `127.0.0.1` only, unless you pass
  `--host 0.0.0.0` (PC) or set `dashboard_lan` (app).**

---

## Building the APK

The app is the same Python bot (`wordbot/`) running inside an Android app:
- **Phone side (Kotlin, `android/app`):** screen capture with MediaProjection instead of scrcpy, swipes and
  taps through an accessibility service instead of adb, and Tesseract through Tesseract4Android (same
  engine, same `eng.traineddata`).
- **Python side:** Chaquopy runs the bot (Python 3.10, numpy 1.26, OpenCV 4.5).
  `wordbot/android_device.py` stands in for `device.py`. `android/python/` holds the app's entry points and
  a pytesseract stand-in.
- **Bundled data:** the learned letter shapes (`templates/`), remembered pictures and the word list. The
  app keeps learning in its own files folder.

You need JDK 17, the Android SDK (platform 34) with `ANDROID_HOME` set, and Python 3.10 on the PC (on the
PATH as `python3.10`, or set `wordbot.buildPython=/path/to/python3.10` in
`$GRADLE_USER_HOME/gradle.properties`).

```
cd android
./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

## Tests

```
.venv/bin/python -m pytest tests
```

The tests run on real screenshots in `tests/screens/`. `tests/test_android.py` runs the app's Python side on
the PC against a fake phone.

**On the Android emulator**, with a stand-in for the game (`android/standin`: real level screenshots that
react to swipes like the game, built by `tools/standin_levels.py`):

```
$ANDROID_HOME/emulator/emulator -avd <an Android 13 x86_64 AVD> -no-window -no-audio -no-snapshot -gpu swiftshader_indirect &
cd android
./gradlew -p standin assembleDebug && ./gradlew -Pwordbot.emulator assembleDebug
adb -e install -r standin/app/build/outputs/apk/debug/app-debug.apk
adb -e install -r app/build/outputs/apk/debug/app-debug.apk
adb -e shell pm grant com.wordbot.app android.permission.POST_NOTIFICATIONS
adb -e shell appops set com.wordbot.app PROJECT_MEDIA allow
adb -e shell settings put secure enabled_accessibility_services com.wordbot.app/com.wordbot.app.TapService
adb -e shell am start -n com.wordbot.app/.MainActivity          # then press Start in the app
adb -e logcat -s python.stdout FakeGame                          # the bot's log and the stand-in's swipes
```

Afterwards run `adb -e emu kill`, then build again without `-Pwordbot.emulator` for a real phone.

## Files

```
wordbot.py              PC entry point
wordbot/bot.py          the loop, level playing, word hunting, dashboard commands
wordbot/vision.py       reading the puzzle screen
wordbot/screens.py      buttons, tutorials, popups
wordbot/glyphs.py       letter templates + Tesseract
wordbot/solver.py       finding listed words in the grid
wordbot/dictionary.py   English words in the grid, for hidden words / pictures / quotes
wordbot/pictures.py     remembered pictures
wordbot/device.py       phone over USB: scrcpy stream or adb
wordbot/android_device.py  phone in the app: screen capture + accessibility service
wordbot/stream.py       scrcpy client
wordbot/dashboard.py    web server; static/ holds the page
android/                the Android app, and the stand-in game for the emulator
templates/              learned letter shapes (delete to relearn)
data/                   scrcpy server, word frequencies, remembered pictures
tests/                  tests on real screenshots
config.json             your settings (created on first change, not in the repo)
logs/                   events, stats, level / unknown / manual screenshots (not in the repo)
```

## Troubleshooting

- **"Restricted setting" when turning on the accessibility service**: Settings > Apps > Wordbot > **⋮** >
  **Allow restricted settings**, then turn it on again.
- **The app keeps stopping, or the accessibility service keeps switching off**: Autostart, battery
  "No restrictions", lock it in recents, and the [two adb permissions](#optional-let-it-recover-by-itself-needs-a-pc-once).
  The log line "Android ended Wordbot at …" says what stopped it.
- **"No Android device found" (PC)**: check the cable (some only charge), USB debugging, and the prompt on
  the phone. `adb devices` should list the phone as `device`.
- **Taps do nothing (PC)**: turn on *USB debugging (Security settings)*.
- **The bot is stuck on a screen**: look at the newest file in `logs/unknown/`. Use the dashboard's Tap mode
  to get past it, or change Flow → unknown screen action.
- **A word is misread**: double-click the letter in the dashboard's grid and type the right one. If letter
  shapes were learned wrong, use Letters → Forget all letter shapes.
- **"Live stream unavailable" in the log (PC)**: the bot falls back to adb screenshots, which are slower but
  fine. Check that `av` is installed in `.venv`.

## Credits

- [scrcpy](https://github.com/Genymobile/scrcpy) server (Apache 2.0), in `data/`
- [wordfreq](https://github.com/rspeer/wordfreq) word frequencies (data CC BY-SA 4.0), in `data/words_en.txt.gz`
- [Tesseract](https://github.com/tesseract-ocr/tesseract) and
  [Tesseract4Android](https://github.com/adaptech-cz/Tesseract4Android) (Apache 2.0)
- [Chaquopy](https://chaquo.com/chaquopy/) (MIT), [OpenCV](https://opencv.org/) (Apache 2.0),
  [NumPy](https://numpy.org/) (BSD)

Word Search and its artwork belong to PlaySimple.
