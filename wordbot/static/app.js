"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
let S = null;            // newest state from the bot
let lastEvent = 0;
let touchMode = "off";
let selCell = null;
let gridKey = "";
let cfgKey = "";

// ---------- server calls ----------
async function post(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  return r.json().catch(() => ({}));
}
const control = (action) => post("/api/control", { action }).then(poll);
const cmd = (name, args) => post("/api/cmd", { name, args });
function setCfg(path, value) {
  const changes = {};
  let cur = changes;
  const parts = path.split(".");
  parts.slice(0, -1).forEach((p) => { cur = cur[p] = {}; });
  cur[parts[parts.length - 1]] = value;
  return post("/api/config", { changes }).then(poll);
}
const getCfg = (path) => path.split(".").reduce((o, k) => (o ? o[k] : undefined), S && S.config);

// ---------- polling ----------
async function poll() {
  try {
    const r = await fetch(`/api/state?since=${lastEvent}`);
    S = await r.json();
    render();
  } catch (e) {
    $("#status").textContent = "offline";
    $("#status").className = "pill";
    $("#phase").textContent = "The dashboard lost the bot. Is `python wordbot.py` still running?";
  }
}
setInterval(poll, 400);

// ---------- render ----------
function fmtSecs(s) {
  if (s == null) return "–";
  s = Math.round(s);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), h = Math.floor(m / 60);
  return h ? `${h}h ${m % 60}m` : `${m}m ${s % 60}s`;
}

function render() {
  const st = S.status;
  const pill = $("#status");
  pill.textContent = st + (S.dry_run ? " · dry run" : "");
  pill.className = "pill " + st;
  $("#phase").textContent = S.phase || "";
  const d = S.device;
  $("#device").textContent = d.serial
    ? `phone ${d.serial} · ${d.mode}${d.battery != null ? ` · battery ${d.battery}%` : ""}`
    : "phone: not connected";
  $("#device").title = d.error || "";

  $("#btnStart").disabled = st === "running";
  $("#btnStart").textContent = st === "paused" ? "Resume" : "Start";
  $("#btnPause").disabled = st !== "running" && st !== "resting";
  $("#btnStop").disabled = st === "idle";

  $$("[data-cfg]").forEach((el) => {
    const v = getCfg(el.dataset.cfg);
    if (el.type === "checkbox" && document.activeElement !== el) el.checked = !!v;
  });

  renderLevel();
  renderStats();
  renderLog();
  renderLetters();
  const ck = JSON.stringify(S.config);
  if (ck !== cfgKey) { cfgKey = ck; renderSettings(); }
  drawOverlay();
}

function renderLevel() {
  const lv = S.level || {};
  const p = S.puzzle;
  $("#levelTitle").textContent = lv.number ? `Level ${lv.number}` : "Level –";
  const plan = S.plan || [];
  const found = plan.filter((i) => i.status === "found").length;
  const bits = [];
  if (lv.title) bits.push(lv.title);
  if (lv.kind === "pictures" || lv.kind === "quote")
    bits.push(`${lv.kind === "quote" ? "Quote" : "Picture Puzzle"}: ${found}/${lv.words} found, ${lv.tries || 0} guesses`);
  else if (plan.length) bits.push(`${found}/${plan.length} words`);
  if (lv.elapsed != null) bits.push(fmtSecs(lv.elapsed));
  if (lv.done) bits.push("done");
  $("#levelInfo").textContent = bits.join(" · ");

  const key = JSON.stringify([p && p.grid && p.grid.letters, p && p.grid && p.grid.source, plan.map((i) => [i.word, i.status]), selCell]);
  if (key === gridKey) return;
  gridKey = key;

  const g = $("#grid");
  g.innerHTML = "";
  if (p && p.grid) {
    const hits = new Set();
    plan.forEach((i) => { if (i.status === "found" && i.cells) i.cells.forEach(([r, c]) => hits.add(`${r},${c}`)); });
    g.style.gridTemplateColumns = `repeat(${p.grid.cols}, 30px)`;
    p.grid.letters.forEach((row, r) => row.forEach((ch, c) => {
      const el = document.createElement("div");
      const src = p.grid.source[r][c];
      el.className = "cell" + (src === "ocr" ? " ocr" : src === "guess" ? " guess" : src === "user" ? " user" : "")
        + (hits.has(`${r},${c}`) ? " hit" : "") + (selCell && selCell[0] === r && selCell[1] === c ? " sel" : "");
      el.textContent = ch;
      el.title = `row ${r + 1}, col ${c + 1} · ${src} · confidence ${p.grid.conf[r][c]}`;
      el.onclick = () => cellClick(r, c);
      el.ondblclick = () => {
        const v = prompt(`Letter at row ${r + 1}, column ${c + 1}:`, ch);
        if (v && /^[a-z]$/i.test(v.trim())) cmd("set_cell", { r, c, letter: v.trim().toUpperCase() });
        selCell = null;
      };
      g.appendChild(el);
    }));
  } else {
    g.innerHTML = '<div class="muted">No puzzle read yet.</div>';
  }

  const w = $("#words");
  w.innerHTML = "";
  plan.forEach((i) => {
    const el = document.createElement("div");
    el.className = "word";
    const bad = i.status === "not in grid" || i.status === "not taken";
    el.innerHTML = `<b></b><span class="tag ${bad ? "bad" : i.status}"></span>`;
    el.querySelector("b").textContent = i.word;
    el.querySelector("span").textContent = i.status + (i.mismatches ? ` (${i.mismatches} misread)` : "");
    el.title = i.from ? "Click to swipe this word now" : "Not found in the grid as read";
    el.onclick = () => i.from && cmd("swipe_word", { word: i.word });
    w.appendChild(el);
  });
}

function cellClick(r, c) {
  if (!selCell) { selCell = [r, c]; gridKey = ""; renderLevel(); return; }
  const [r1, c1] = selCell;
  selCell = null;
  gridKey = "";
  if (r1 === r && c1 === c) { renderLevel(); return; }
  const dr = r - r1, dc = c - c1;
  if (dr && dc && Math.abs(dr) !== Math.abs(dc)) { alert("Pick two letters in a straight line (across, down or diagonal)."); renderLevel(); return; }
  cmd("swipe_cells", { r1, c1, r2: r, c2: c });
  renderLevel();
}

function renderStats() {
  const s = S.stats;
  const items = [
    ["Levels (session)", s.session_levels],
    ["Words (session)", s.session_words],
    ["Session time", fmtSecs(s.session_secs)],
    ["Levels (all)", s.total_levels],
    ["Words (all)", s.total_words],
    ["Avg level", s.avg_time != null ? `${s.avg_time.toFixed(1)}s` : "–"],
    ["Best level", s.best_time != null ? `${s.best_time.toFixed(1)}s` : "–"],
    ["Errors", s.errors || 0],
  ];
  $("#stats").innerHTML = items.map(([k, v]) => `<div class="stat"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("");
  $("#history").innerHTML = (s.history || []).slice().reverse().map((h) =>
    `<tr><td>${new Date(h.t * 1000).toLocaleTimeString()}</td><td>${h.level ?? "?"}</td><td>${esc(h.title || "")}</td><td>${h.words}</td><td>${h.secs}s</td></tr>`).join("");
}

function esc(t) { return String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

function renderLog() {
  const box = $("#log");
  const showInfo = $("#logInfo").checked;
  for (const e of S.events) {
    lastEvent = Math.max(lastEvent, e.id);
    const el = document.createElement("div");
    el.className = `ev ${e.level}`;
    el.dataset.level = e.level;
    if ((!showInfo && e.level === "info") || (!$("#logDebug").checked && e.level === "debug")) el.classList.add("hidden");
    const t = new Date(e.t * 1000).toLocaleTimeString();
    el.innerHTML = `<span class="t">${t}</span>`;
    el.appendChild(document.createTextNode(e.msg));
    if (e.data && e.data.trace) el.title = e.data.trace;
    box.appendChild(el);
  }
  while (box.children.length > 600) box.removeChild(box.firstChild);
  if (S.events.length && $("#logAuto").checked) box.scrollTop = box.scrollHeight;
}
function filterLog() {
  $$("#log .ev").forEach((el) => el.classList.toggle("hidden",
    (!$("#logInfo").checked && el.dataset.level === "info") || (!$("#logDebug").checked && el.dataset.level === "debug")));
}
$("#logInfo").onchange = filterLog;
$("#logDebug").onchange = filterLog;
$("#logClear").onclick = () => { $("#log").innerHTML = ""; };

function renderLetters() {
  const t = S.templates || {};
  const html = "ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("").map((ch) =>
    `<div class="letter ${t[ch] ? "" : "none"}"><b>${ch}</b><span>${t[ch] || 0}</span></div>`).join("");
  if ($("#letters").innerHTML !== html) $("#letters").innerHTML = html;
  $("#tessCalls").textContent = `Tesseract calls this session: ${S.tesseract_calls} · pictures remembered: ${S.pictures_known}`;
}

// ---------- settings ----------
const META = {
  "device.use_scrcpy": ["Live stream (scrcpy)", "Live video and fast touches. Off: adb screenshots (slower)."],
  "device.max_fps": ["Stream fps", "Frames per second from the phone."],
  "device.bit_rate": ["Stream bit rate", "Bits per second; higher is sharper."],
  "device.serial": ["Phone serial", "Only needed with several phones plugged in."],
  "swipe.move_ms": ["Swipe time (ms)", "Finger travel from first to last letter."],
  "swipe.hold_ms": ["Hold at end (ms)", "Rest on the last letter before lifting."],
  "swipe.gap_ms": ["Gap between words (ms)", ""],
  "swipe.jitter_px": ["Position jitter (px)", "Random offset on each touch point."],
  "swipe.jitter_ms": ["Timing jitter (ms)", "Random extra time on each timing."],
  "swipe.check_touch": ["Check each swipe", "Watch the game draw its selection; swipe again if it didn't (it ignores swipes during animations)."],
  "solve.order": ["Word order", "Order the words are swiped in.", ["list", "longest", "shortest", "top", "random"]],
  "solve.fuzzy": ["Allowed misread letters", "A word may disagree with the read grid in this many letters."],
  "solve.learn_templates": ["Learn letter shapes", "From solved words, so fewer Tesseract calls."],
  "solve.max_read_attempts": ["Read attempts", "Re-read a blurry or half-drawn screen this many times."],
  "verify.enabled": ["Check words afterwards", "Redo words the game didn't take."],
  "verify.each_word": ["Check after every word", "Slower; finds a problem sooner."],
  "verify.delay_ms": ["Check delay (ms)", "Wait before checking the word list."],
  "verify.retries": ["Redo attempts", ""],
  "flow.auto_next": ["Auto next level", "Tap Next Level, Continue, Collect, ..."],
  "flow.max_levels": ["Stop after N levels", "0 = keep going."],
  "flow.level_pause_s": ["Pause before each level (s)", ""],
  "flow.rest_every": ["Rest every N levels", "0 = never."],
  "flow.rest_s": ["Rest length (s)", ""],
  "flow.unknown_wait_s": ["Unknown screen wait (s)", "How long a screen the bot doesn't know may stay before it acts."],
  "flow.unknown_action": ["Unknown screen action", "After the wait and no text button found.", ["back", "tap_center", "wait", "pause"]],
  "flow.tap_unknown_green": ["Tap unfamiliar green buttons", "After the wait. Never taps BUY, $, SUBSCRIBE, ..."],
  "flow.relaunch_game": ["Reopen the game", "If another app comes to the front."],
  "flow.stop_on_error": ["Pause on errors", "Pause when a level can't be read or solved."],
  "flow.restart_when_stuck": ["Restart a stuck level", "Once per level, for a fresh board, when every guess has failed."],
  "flow.pause_when_stuck": ["Pause when stuck", "Off: keep trying a level that can't be finished instead of stopping."],
  "hunt.enabled": ["Search the grid for unread words", "Picture Puzzles, and words the list reading missed."],
  "hunt.hidden_words": ["Find hidden words", "Rows of coins in the list: extra coins."],
  "hunt.min_zipf": ["Minimum word frequency", "Zipf scale: 3 = common, 2 = rare. Lower tries more words."],
  "hunt.batch": ["Words per check", "Swipe this many guesses, then check which were accepted."],
  "hunt.check_delay_ms": ["Check delay (ms)", "Wait before checking which guesses were accepted."],
  "hunt.max_tries": ["Max guesses per level", ""],
  "hunt.use_hints": ["Use hints when stuck", "The game's hint marks a word's first letter. Free hints first, then coins."],
  "hunt.max_hints": ["Max hints per level", ""],
  "hunt.paid_hints": ["Allow paid hints", "Spend coins (100 each) on hints once the free ones are gone."],
  "hunt.paid_hints_when_stuck": ["Paid hint when stuck", "Only when every guess has failed: 100 coins rather than a stuck bot."],
  "flow.close_popups": ["Close popups", "Tap the round X on popups."],
  "ads.back_after_s": ["Press back on ads after (s)", "Ads are other screens inside the game app."],
  "ads.tap_corners": ["Tap ad corners", "If back doesn't close an ad, tap where close buttons usually are."],
  "dry_run": ["Dry run", "Read and solve, but don't touch the phone."],
  "logging.save_levels": ["Save level screenshots", "Kept in logs/levels (last 300)."],
  "logging.save_unknown": ["Save unknown screens", "Kept in logs/unknown."],
  "dashboard.adb_refresh_s": ["Picture refresh without stream (s)", "0 = only when the bot looks."],
};
const GROUPS = { device: "Phone connection", swipe: "Swiping", solve: "Reading and solving", verify: "Checking", hunt: "Picture Puzzles / word hunt", flow: "Flow", ads: "Ads", logging: "Logging", dashboard: "Dashboard" };

function renderSettings() {
  const root = $("#settings");
  if (root.contains(document.activeElement)) return;  // don't redraw under the cursor
  root.innerHTML = "";
  const add = (group, title, entries) => {
    const box = document.createElement("div");
    box.className = "set-group";
    box.innerHTML = `<h3>${title}</h3>`;
    entries.forEach(([path, val]) => box.appendChild(settingRow(path, val)));
    root.appendChild(box);
  };
  add("general", "General", [["dry_run", S.config.dry_run]]);
  Object.entries(GROUPS).forEach(([k, title]) => add(k, title, Object.entries(S.config[k] || {}).map(([kk, v]) => [`${k}.${kk}`, v])));
}

function settingRow(path, val) {
  const [label, help, options] = META[path] || [path, ""];
  const row = document.createElement("div");
  row.className = "set";
  const id = "cfg_" + path.replace(/\./g, "_");
  row.innerHTML = `<label for="${id}">${label}<small>${help}</small></label>`;
  let input;
  if (options) {
    input = document.createElement("select");
    options.forEach((o) => { const op = document.createElement("option"); op.value = op.textContent = o; input.appendChild(op); });
    input.value = val;
    input.onchange = () => setCfg(path, input.value);
  } else if (typeof val === "boolean") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = val;
    input.onchange = () => setCfg(path, input.checked);
  } else if (typeof val === "number") {
    input = document.createElement("input");
    input.type = "number";
    input.step = Number.isInteger(val) ? "1" : "0.1";
    input.value = val;
    input.onchange = () => setCfg(path, Number(input.value));
  } else {
    input = document.createElement("input");
    input.type = "text";
    input.value = val;
    input.onchange = () => setCfg(path, input.value);
  }
  input.id = id;
  row.appendChild(input);
  return row;
}
$("#cfgReset").onclick = () => { if (confirm("Reset every setting to its default?")) post("/api/config/reset").then(() => { cfgKey = ""; poll(); }); };

// ---------- live picture ----------
const img = $("#screen");
const canvas = $("#overlay");
const wrap = $("#screenWrap");
let mjpegFailed = false;
img.onload = () => $("#noframe").classList.add("hidden");
img.onerror = () => {
  if (!mjpegFailed) { mjpegFailed = true; }
  setTimeout(() => { img.src = `/frame.jpg?t=${Date.now()}`; }, 700);
};
img.src = "/stream.mjpg?fps=12";
setInterval(() => { if (mjpegFailed) img.src = `/frame.jpg?t=${Date.now()}`; }, 1000);

function devSize() { return (S && S.device && S.device.size) || [1080, 2400]; }
function toDevice(ev) {
  const r = canvas.getBoundingClientRect();
  const [w, h] = devSize();
  return [Math.round((ev.clientX - r.left) / r.width * w), Math.round((ev.clientY - r.top) / r.height * h)];
}

function drawOverlay() {
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth, H = canvas.clientHeight;
  if (canvas.width !== Math.round(W * dpr)) { canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr); }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  if (!S || !$("#showOverlay").checked || !S.puzzle) return;
  if (S.screen && S.screen.kind !== "puzzle") return;
  if (S.level && S.level.board_gone) return;   // the old level's board, not the one on screen
  const [dw, dh] = devSize();
  const sx = W / dw, sy = H / dh;
  const g = S.puzzle.grid;
  ctx.fillStyle = "rgba(20,163,127,.9)";
  g.centers.forEach((row) => row.forEach(([x, y]) => { ctx.beginPath(); ctx.arc(x * sx, y * sy, 2, 0, 7); ctx.fill(); }));
  const colors = { found: "rgba(31,157,85,.55)", swiped: "rgba(20,163,127,.8)", planned: "rgba(60,120,255,.8)", pending: "rgba(255,170,0,.85)", "not taken": "rgba(214,69,69,.9)" };
  ctx.lineCap = "round";
  (S.plan || []).forEach((i) => {
    if (!i.from) return;
    ctx.strokeStyle = colors[i.status] || "rgba(255,255,255,.7)";
    ctx.lineWidth = i.status === "found" ? 3 : 5;
    ctx.beginPath();
    ctx.moveTo(i.from[0] * sx, i.from[1] * sy);
    ctx.lineTo(i.to[0] * sx, i.to[1] * sy);
    ctx.stroke();
  });
}
window.addEventListener("resize", drawOverlay);

let dragFrom = null;
canvas.addEventListener("pointerdown", (ev) => {
  if (touchMode === "off") return;
  dragFrom = toDevice(ev);
  canvas.setPointerCapture(ev.pointerId);
});
canvas.addEventListener("pointerup", (ev) => {
  if (touchMode === "off" || !dragFrom) return;
  const to = toDevice(ev);
  const [x1, y1] = dragFrom;
  dragFrom = null;
  const moved = Math.hypot(to[0] - x1, to[1] - y1);
  if (touchMode === "swipe" && moved > 25) cmd("swipe", { x1, y1, x2: to[0], y2: to[1] });
  else cmd("tap", { x: x1, y: y1 });
});
$$("#touchMode button").forEach((b) => b.onclick = () => {
  touchMode = b.dataset.mode;
  $$("#touchMode button").forEach((x) => x.classList.toggle("on", x === b));
  wrap.classList.remove("tap", "swipe");
  if (touchMode !== "off") wrap.classList.add(touchMode);
});

// ---------- buttons ----------
// Inside the Android app, Start goes through the app: it asks Android for screen capture first.
$("#btnStart").onclick = () => (window.WordbotApp && (!S || S.status === "idle"))
  ? window.WordbotApp.start() : control(S && S.status !== "idle" ? "resume" : "start");
$("#btnPause").onclick = () => control("pause");
$("#btnStep").onclick = () => control("step");
$("#btnStop").onclick = () => control("stop");
$$("[data-cmd]").forEach((b) => b.onclick = () => {
  if (b.dataset.cmd === "reset_stats" && !confirm("Reset all stats?")) return;
  if (b.dataset.cmd === "clear_templates" && !confirm("Forget every learned letter shape?")) return;
  cmd(b.dataset.cmd).then(poll);
});
$$("[data-cfg]").forEach((el) => el.onchange = () => setCfg(el.dataset.cfg, el.checked));
$$(".tabs button").forEach((b) => b.onclick = () => {
  $$(".tabs button").forEach((x) => x.classList.toggle("on", x === b));
  $$(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== `tab-${b.dataset.tab}`));
});

if (window.WordbotApp) document.body.classList.add("in-app");
poll();
