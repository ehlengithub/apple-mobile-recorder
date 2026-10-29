// Control page: live phone, bilingual live log, and a wall of step screenshots.
const $ = (id) => document.getElementById(id);

// ---- language ------------------------------------------------------------------------

let lang = "zh";
try { lang = localStorage.getItem("lang") || "zh"; } catch { /* private mode */ }
const t = (key) => I18N[lang][key] ?? I18N.en[key] ?? key;

function applyLang() {
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll("[data-i18n-ph]").forEach((el) => { el.placeholder = t(el.dataset.i18nPh); });
  document.querySelectorAll("#lang button").forEach((b) => b.classList.toggle("on", b.dataset.lang === lang));
  $("examples").replaceChildren(...t("examples").map((text) => {
    const b = document.createElement("button");
    b.textContent = text;
    b.addEventListener("click", () => { $("task").value = text; start(text); });  // one click runs the demo
    return b;
  }));
  shown = "";  // re-render steps in the new language
  if (typeof applyMotion === "function") applyMotion();
  updateViewerControls();
  renderFrameState();
}
document.querySelectorAll("#lang button").forEach((b) => b.addEventListener("click", () => {
  lang = b.dataset.lang;
  try { localStorage.setItem("lang", lang); } catch { /* private mode */ }
  applyLang();
}));

// ---- motion (tap ripples, flowing swipe paths); remembered per browser -------------------

let motion = !matchMedia("(prefers-reduced-motion: reduce)").matches;
try { motion = (localStorage.getItem("motion") || (motion ? "on" : "off")) === "on"; } catch { /* private mode */ }
function applyMotion() {
  document.body.classList.toggle("still", !motion);
  $("motion").textContent = t(motion ? "motionOn" : "motionOff");
}
$("motion").addEventListener("click", () => {
  motion = !motion;
  try { localStorage.setItem("motion", motion ? "on" : "off"); } catch { /* private mode */ }
  applyMotion();
});

const say = (key) => { $("msg").textContent = key ? t(key) : ""; };
async function post(path, body) {
  try {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    const data = await r.json().catch(() => ({}));
    const message = r.ok ? "" : data.detail || r.statusText;
    $("msg").textContent = $("viewer-msg").textContent = message;
    $("viewer-msg").hidden = !message;
    return r.ok ? data : null;
  } catch {
    $("msg").textContent = $("viewer-msg").textContent = t("networkError");
    $("viewer-msg").hidden = false;
    return null;
  }
}

// ---- step helpers --------------------------------------------------------------------

const fmt = (sec) => (sec == null ? "–" : sec < 10 ? `${sec.toFixed(1)}s` : `${Math.round(sec)}s`);
const isPlan = (s) => s.action.includes("plan");
const isBad = (s) => ["hiccup", "error", "fail"].includes(s.action);
const what = (s) => describeStep(s)[lang === "zh" ? 1 : 0];
// the model answers in English; a Chinese version is shown when it gives one
const why = (s) => (lang === "zh" && s.thought_zh) || s.thought || s.answer || s.reason || "";
const planText = (s) => (lang === "zh" && s.plan_zh) || s.plan || "";
const times = (s) => [s.think_s != null && `${t("thinking")} ${fmt(s.think_s)}`, s.act_s != null && `${t("doing")} ${fmt(s.act_s)}`].filter(Boolean).join(" · ");
const where = (s) => ({
  tap: () => ` (${s.x}, ${s.y})${s.precise ? " · precise" : ""}`, swipe: () => ` (${s.x1}, ${s.y1}) → (${s.x2}, ${s.y2})`,
  path: () => ` · ${s.points.length} pts`, circle: () => ` (${s.x}, ${s.y}) r=${s.r}`,
}[s.action] || (() => ""))();

// Draw what the AI did on its screenshot (coordinates are 0-1000): yellow on black, plus a hand for taps.
// .ripple and .flow shapes are animated in CSS; body.still turns the motion off.
let aspect = .75;  // Decoded frame dimensions take precedence over the status fallback.
function overlay(s) {
  const svg = (inner) => `<svg viewBox="0 0 1000 1000" preserveAspectRatio="none">${inner}</svg>`;
  const stroke = (shape) =>  // black casing under a yellow line, so it shows on any background
    `${shape.replace("/>", ` style="stroke:#000;fill:none" stroke-width="9" vector-effect="non-scaling-stroke" stroke-linecap="round" stroke-linejoin="round"/>`)}
     ${shape.replace("/>", ` style="stroke:var(--hl);fill:none" stroke-width="5" vector-effect="non-scaling-stroke" stroke-linecap="round" stroke-linejoin="round"/>`)}`;
  const dot = (x, y, r) => `<ellipse cx="${x}" cy="${y}" rx="${r}" ry="${r * aspect}" style="fill:var(--hl);stroke:#000" stroke-width="2" vector-effect="non-scaling-stroke"/>`;
  const pts = (list) => list.map(([x, y]) => `${x},${y}`).join(" ");
  const flow = (list) =>  // dashes that travel along the stroke, in its direction
    `<polyline class="flow" points="${pts(list)}" style="stroke:#000;fill:none" stroke-width="2.5" vector-effect="non-scaling-stroke" stroke-linecap="round"/>`;
  switch (s.action) {
    case "tap": {  // red, so the tap point stands out from the yellow swipe paths
      const ring = (cls, r, w, color) => `<ellipse class="${cls}" cx="${s.x}" cy="${s.y}" rx="${r}" ry="${r * aspect}" style="stroke:${color};fill:none" stroke-width="${w}" vector-effect="non-scaling-stroke"/>`;
      return svg(ring("ripple", 100, 7, "var(--tap)") + ring("", 100, 11, "#000") + ring("", 100, 6, "var(--tap)") +
        `<ellipse cx="${s.x}" cy="${s.y}" rx="22" ry="${22 * aspect}" style="fill:var(--tap);stroke:#fff" stroke-width="3" vector-effect="non-scaling-stroke"/>`);
    }
    case "swipe": {
      const a = Math.atan2((s.y2 - s.y1) / aspect, s.x2 - s.x1), l = 45;
      const head = (d) => [s.x2 - l * Math.cos(a + d), s.y2 - l * aspect * Math.sin(a + d)];
      return svg(stroke(`<polyline points="${pts([[s.x1, s.y1], [s.x2, s.y2]])}"/>`) + flow([[s.x1, s.y1], [s.x2, s.y2]]) +
        stroke(`<polyline points="${pts([head(0.5), [s.x2, s.y2], head(-0.5)])}"/>`) + dot(s.x1, s.y1, 14));
    }
    case "path":
      return svg(stroke(`<polyline points="${pts(s.points)}"/>`) + flow(s.points) + dot(...s.points[0], 12));
    case "circle":
      return svg(stroke(`<ellipse cx="${s.x}" cy="${s.y}" rx="${s.r}" ry="${s.r * aspect}"/>`));
    default:
      return "";
  }
}
// the 👆 sits on top of the svg as text, so it is not stretched
function hand(s) {
  if (s.action !== "tap") return "";
  return `<span class="hand" style="left:${s.x / 10}%;top:${s.y / 10}%">👆</span>`;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
}

// Log line in the chosen language, newest at the bottom.
function logLine(s) {
  const li = el("li", isBad(s) ? "bad" : "");
  const head = el("div");
  head.append(el("span", "time", new Date(s.t * 1000).toLocaleTimeString()), el("span", "what", what(s) + where(s)));
  li.append(head);
  const text = planText(s) || why(s);
  if (text) li.append(el("div", "why", text));
  if (times(s)) li.append(el("div", "meta", times(s)));
  return li;
}

// Wall card: screenshot with the action drawn on it.
function card(s, index, number, scale, runId) {
  const c = el("div", "card" + (isBad(s) ? " bad" : ""));
  if (s.shot) {
    const shot = el("div", "shot");
    const img = el("img");
    img.loading = "lazy";
    img.addEventListener("load", () => { shot.style.aspectRatio = `${img.naturalWidth} / ${img.naturalHeight}`; });
    img.src = `/api/steps/${index}.jpg?run=${runId}`;
    const ov = el("div", "overlay");
    ov.innerHTML = overlay(s) + hand(s);
    shot.append(img, ov);
    shot.addEventListener("click", () => zoom(img.src, s));
    c.append(shot);
  }
  const body = el("div", "body");
  body.append(el("div", "no", isPlan(s) ? t("planner") : lang === "zh" ? `第 ${number} 步` : `${t("step")} ${number}`),
    el("div", "what", what(s)), el("div", "why", isPlan(s) ? "" : why(s)));
  if (s.think_s != null) {
    const bar = el("div", "bar");
    const a = el("div", "think"), b = el("div", "act");
    a.style.width = `${(s.think_s / scale) * 100}%`;
    b.style.width = `${((s.act_s || 0) / scale) * 100}%`;
    bar.append(a, b);
    body.append(bar, el("div", "times", times(s)));
  }
  c.append(body);
  return c;
}

function zoom(src, s) {
  $("zoom-img").src = src;
  $("zoom-overlay").innerHTML = overlay(s) + hand(s);
  $("zoom-text").textContent = `${what(s)} — ${why(s)}`;
  $("zoom").showModal();
}
$("zoom").addEventListener("click", () => $("zoom").close());

// ---- live phone: click = tap, drag = swipe, wheel = scroll --------------------------

const live = $("live");
const viewer = $("viewer"), stage = $("viewer-stage");
let down = null, calibrating = false;
let screenOnline = false, statusAvailable = false, frameUsable = false;
let statusAspect = .75, decodedAspect = null;
let frameSeq = null, frameEpoch = 0, frameController = null, frameUrl = "";

function fitScreen() {
  aspect = decodedAspect || statusAspect;
  document.documentElement.style.setProperty("--screen-aspect", aspect);
  const width = Math.min(stage.clientWidth, stage.clientHeight * aspect);
  $("screen").style.width = `${Math.max(0, width)}px`;
  $("screen").style.height = `${Math.max(0, width / aspect)}px`;
}
new ResizeObserver(fitScreen).observe(stage);

function updateViewerControls() {
  const focused = $("workspace").classList.contains("screen-focus");
  $("focus-screen").textContent = t(focused ? "screenRestore" : "screenFocus");
  $("focus-screen").setAttribute("aria-pressed", String(focused));
  const fullscreen = document.fullscreenElement === viewer;
  $("fullscreen").textContent = t(fullscreen ? "fullscreenExit" : "fullscreen");
  $("fullscreen").setAttribute("aria-pressed", String(fullscreen));
}
$("focus-screen").addEventListener("click", () => {
  $("workspace").classList.toggle("screen-focus");
  updateViewerControls();
});
$("fullscreen").addEventListener("click", async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await viewer.requestFullscreen();
  } catch {
    $("viewer-msg").textContent = t("fullscreenUnavailable");
    $("viewer-msg").hidden = false;
  }
});
document.addEventListener("fullscreenchange", () => { down = null; updateViewerControls(); fitScreen(); });

function renderFrameState() {
  const paused = document.hidden;
  const usable = statusAvailable && screenOnline && frameUsable && !paused;
  const key = paused ? "streamPaused" : !statusAvailable || (screenOnline && !frameUsable && frameUrl)
    ? "streamRetry" : usable ? "streamLive" : "streamWaiting";
  $("frame-state").textContent = t(key);
  $("frame-state").dataset.state = usable ? "live" : key === "streamRetry" ? "error" : "waiting";
  $("screen").classList.toggle("stale", !usable);
  $("hint").hidden = usable;
  $("hint").textContent = t(key === "streamWaiting" ? "waiting" : key);
}

function clearFrame() {
  live.removeAttribute("src");
  if (frameUrl) URL.revokeObjectURL(frameUrl);
  frameUrl = "";
  frameSeq = decodedAspect = null;
  frameUsable = false;
  down = null;
  $("frame-info").textContent = "";
  fitScreen();
}

function setScreenOnline(online) {
  if (online !== screenOnline) {
    frameEpoch++;
    frameController?.abort();
    screenOnline = online;
    if (!online) clearFrame();
  }
  renderFrameState();
}

function canPoint() {
  return statusAvailable && screenOnline && frameUsable && !document.hidden;
}

function frac(e) {
  const r = live.getBoundingClientRect(), ratio = live.naturalWidth / live.naturalHeight || r.width / r.height;
  const w = Math.min(r.width, r.height * ratio), h = w / ratio;  // object-fit: contain box
  if (!(w > 0 && h > 0)) return null;
  const x = (e.clientX - r.left - (r.width - w) / 2) / w, y = (e.clientY - r.top - (r.height - h) / 2) / h;
  // Letterbox/outside drags are never clamped into a tap at the screen's edge.
  return x >= 0 && x <= 1 && y >= 0 && y <= 1 ? { x, y } : null;
}
live.addEventListener("pointerdown", (e) => {
  if (e.button !== 0 || !e.isPrimary || !canPoint()) return;
  const point = frac(e);
  if (point) { down = { ...point, pointerId: e.pointerId }; live.setPointerCapture(e.pointerId); }
});
live.addEventListener("pointercancel", () => { down = null; });
live.addEventListener("lostpointercapture", () => { down = null; });
live.addEventListener("pointerup", async (e) => {
  if (!down || e.pointerId !== down.pointerId) return;
  const up = frac(e), from = down;
  down = null;
  if (!up || !canPoint()) return;
  if (calibrating) {
    calibrating = false;
    const ok = await post("/api/calibrate/finish", up);
    calibrationHint(ok ? "calOk" : null);
  } else if (Math.hypot(up.x - from.x, up.y - from.y) < 0.02) {
    post("/api/tap", { ...up, precise: true });
  } else {
    post("/api/swipe", { x1: from.x, y1: from.y, x2: up.x, y2: up.y, precise: true });
  }
});
let wheelAt = 0;
live.addEventListener("wheel", (e) => {
  e.preventDefault();
  const point = frac(e);
  if (!canPoint() || !point || !e.deltaY) return;
  if (Date.now() - wheelAt < 250) return;
  wheelAt = Date.now();
  post("/api/scroll", { ...point, lines: e.deltaY > 0 ? -3 : 3, precise: true });
}, { passive: false });

async function installFrame(blob, epoch) {
  const url = URL.createObjectURL(blob);
  try {
    const next = new Image();
    next.src = url;
    await next.decode();  // One request/decode at a time, never queue old frames.
    if (epoch !== frameEpoch || !screenOnline || document.hidden) return false;
    if (!next.naturalWidth || !next.naturalHeight) throw new Error("Empty frame");
    const old = frameUrl;
    live.src = frameUrl = url;
    decodedAspect = next.naturalWidth / next.naturalHeight;
    $("frame-info").textContent = `${next.naturalWidth} × ${next.naturalHeight}`;
    frameUsable = true;
    fitScreen();
    renderFrameState();
    if (old) URL.revokeObjectURL(old);
    return true;
  } finally {
    if (url !== frameUrl) URL.revokeObjectURL(url);
  }
}

// Long-poll new versions; the 30 fps ceiling includes transfer and decode time.
// Older servers without X-Frame-Seq fall back to sequential, uncached polling.
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function refreshLive() {
  let failures = 0;
  while (true) {
    if (!screenOnline || document.hidden) { await pause(200); continue; }
    const started = performance.now(), epoch = frameEpoch;
    const controller = new AbortController();
    frameController = controller;
    const timeout = setTimeout(() => controller.abort(), 4000);
    try {
      const query = new URLSearchParams({ wait: "0.5" });
      if (frameSeq !== null) query.set("after", String(frameSeq));
      const r = await fetch(`/api/frame.jpg?${query}`, { cache: "no-store", signal: controller.signal });
      if (epoch !== frameEpoch || !screenOnline) continue;
      if (r.status === 204) {
        // A still page is healthy. Keep the last decoded picture on screen.
        frameUsable = Boolean(frameUrl);
        renderFrameState();
      } else {
        if (!r.ok) throw new Error(`Frame HTTP ${r.status}`);
        const header = r.headers.get("X-Frame-Seq");
        const seq = header !== null && /^\d+$/.test(header) && Number.isSafeInteger(Number(header)) ? Number(header) : null;
        if (await installFrame(await r.blob(), epoch)) frameSeq = seq;
      }
      failures = 0;
    } catch {
      if (epoch === frameEpoch && screenOnline && !document.hidden) {
        failures++;
        frameUsable = false;  // Mark a retained picture stale and block gestures.
        down = null;
        renderFrameState();
      }
    } finally {
      clearTimeout(timeout);
      if (frameController === controller) frameController = null;
    }
    await pause(Math.max(1000 / 30 - (performance.now() - started), failures ? Math.min(1500, 100 * 2 ** Math.min(failures, 4)) : 0));
  }
}
document.addEventListener("visibilitychange", () => {
  frameEpoch++;
  frameController?.abort();
  down = null;
  renderFrameState();
});

// ---- controls ------------------------------------------------------------------------

document.querySelectorAll("[data-post]").forEach((b) => b.addEventListener("click", () => post(b.dataset.post)));
// Calibration instructions show right under the phone, where the user is looking.
function calibrationHint(key) {
  $("caption").textContent = t(key || "liveCaption");
  $("caption").classList.toggle("callout", Boolean(key));
  live.parentElement.classList.toggle("calibrating", key === "calNow");
  if (key === "calOk") setTimeout(() => calibrationHint(null), 4000);
}
$("cal").addEventListener("click", async () => {
  calibrationHint("calFinding");
  const r = await post("/api/calibrate/start");
  if (!r) return calibrationHint(null);
  if (r.auto) return calibrationHint("calOk");  // found the pointer by itself
  calibrating = true;
  calibrationHint("calNow");
});
$("type").addEventListener("click", async () => { if (await post("/api/type", { text: $("text").value })) $("text").value = ""; });
$("press").addEventListener("click", () => post("/api/key", { text: $("key").value }));
function start(task) {
  if (!task) return say("needTask");
  post("/api/agent", { text: task });
}
$("run").addEventListener("click", () => start($("task").value.trim()));
$("stop").addEventListener("click", () => post("/api/agent/stop"));
$("clear").addEventListener("click", async () => {
  if (await post("/api/agent/clear")) { $("task").value = ""; say(null); }
});
$("estop").addEventListener("click", () => post("/api/agent/stop"));
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("zoom").open) post("/api/agent/stop"); });

let macroState = {};
$("rec").addEventListener("click", () => post(macroState.recording ? "/api/macro/record/stop" : "/api/macro/record/start"));
$("mplay").addEventListener("click", () => {
  const loops = Number($("loops").value);
  if (!Number.isInteger(loops) || loops < 0 || loops > 10000) return;
  post(macroState.playing ? "/api/macro/stop" : "/api/macro/play", { loops });
});
$("mclear").addEventListener("click", () => post("/api/macro/clear"));
$("list-mode").addEventListener("change", async (e) => {
  const ok = await post("/api/macro/list-mode", { enabled: e.target.checked });
  if (!ok) e.target.checked = Boolean(macroState.list_mode);
});
function renderMacro(s) {
  macroState = s.macro || {};
  const m = macroState, busy = s.agent.running;
  const ready = s.device && s.screen && s.precision_calibrated;
  $("rec").textContent = t(m.recording ? "macroRecordStop" : "macroRecord");
  $("mplay").textContent = t(m.playing ? "macroStop" : "macroPlay");
  $("rec").disabled = busy || m.playing || (!m.recording && !ready);
  $("mplay").disabled = busy || m.recording || (!m.playing && (!ready || !m.steps));
  $("loops").disabled = m.playing || m.recording;
  $("list-mode").checked = Boolean(m.list_mode);
  $("list-mode").disabled = busy || m.playing || m.recording || !m.steps;
  $("mclear").disabled = busy || m.playing || m.recording || !m.steps;
  $("cal").disabled = busy || m.playing || m.recording || s.pointer_mode === "absolute";
  $("macro-msg").textContent = m.last_error || (m.playing
    ? `${t("macroRunning")} ${m.loop}/${m.loops || "∞"} · ${m.steps} ${t("macroSteps")}`
    : m.recording ? `${t("macroRecording")} ${m.steps} ${t("macroSteps")}`
      : `${m.steps || 0} ${t("macroSteps")} · ${s.pointer_mode === "absolute" ? t("macroAbsoluteReady") : s.precision_calibrated ? t("macroReady") : t("macroNeedCalibration")}`);
}

// ---- polling -------------------------------------------------------------------------

let shown = "";
async function poll() {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 4000);
  try {
    const response = await fetch("/api/status", { cache: "no-store", signal: controller.signal });
    if (!response.ok) throw new Error(`Status HTTP ${response.status}`);
    const s = await response.json();
    statusAvailable = true;
    if (Number.isFinite(s.aspect) && s.aspect > 0) statusAspect = s.aspect;
    // A receiver restart can reset the counter. Never seed it from status:
    // only a successfully decoded frame counts as a displayed version.
    if (Number.isSafeInteger(s.frame_seq) && frameSeq !== null && s.frame_seq < frameSeq) frameSeq = null;
    setScreenOnline(Boolean(s.screen));
    fitScreen();
    let ready = true;
    $("setup").querySelectorAll("li").forEach((li) => { const ok = s[li.dataset.key]; li.classList.toggle("done", ok); ready &&= ok; });
    $("setup").classList.toggle("all-done", ready);

    const a = s.agent, steps = a.steps;
    const macroBusy = s.macro?.playing || s.macro?.recording;
    $("run").disabled = a.running || macroBusy || !ready;
    $("examples").querySelectorAll("button").forEach((b) => { b.disabled = a.running || macroBusy || !ready; });
    $("stop").hidden = !a.running;
    $("clear").hidden = a.running || !steps.length;
    $("live-dot").classList.toggle("on", a.running);

    renderMacro(s);
    const avg = (xs) => (xs.length ? fmt(xs.reduce((n, x) => n + x, 0) / xs.length) : "–");
    const planned = steps.filter((x) => isPlan(x) && x.think_s != null).map((x) => x.think_s);
    const acted = steps.filter((x) => !isPlan(x) && x.think_s != null).map((x) => x.think_s);
    $("m-planner").textContent = s.models.planner;
    $("m-actor").textContent = s.models.actor;
    $("m-planner-s").textContent = `${planned.length}× · ${t("avg")} ${avg(planned)}`;
    $("m-actor-s").textContent = `${acted.length}× · ${t("avg")} ${avg(acted)}`;

    $("summary").hidden = !steps.length;
    const actions = steps.filter((x) => x.act_s != null);
    const end = a.running ? Date.now() / 1000 : steps.at(-1)?.t;
    const state = { done: "done", fail: "gaveUp", stopped: "stopped", error: "errorState" }[steps.at(-1)?.action];
    $("st-state").textContent = t(a.running ? "working" : state || "idle");
    $("st-steps").textContent = actions.length;
    $("st-time").textContent = steps.length ? fmt(end - steps[0].t) : "0s";
    $("st-step").textContent = actions.length ? fmt(actions.reduce((n, x) => n + (x.think_s || 0) + x.act_s, 0) / actions.length) : "–";

    const key = lang + JSON.stringify(steps);
    if (key !== shown) {
      shown = key;
      const plan = [...steps].reverse().find((x) => x.plan);
      $("plan").hidden = !plan;
      if (plan) $("plan-list").replaceChildren(...planText(plan).split("\n").map((l) => l.replace(/^\s*\d+[.)]\s*/, "").trim())
        .filter(Boolean).map((l) => el("li", "", l)));

      const logBox = $("log").parentElement, stick = logBox.scrollTop + logBox.clientHeight >= logBox.scrollHeight - 30;
      $("log").replaceChildren(...steps.map(logLine));
      if (stick) logBox.scrollTop = logBox.scrollHeight;  // follow the newest line

      const scale = Math.max(1, ...steps.map((x) => (x.think_s || 0) + (x.act_s || 0)));
      const runId = steps.length ? steps[0].t : "";
      let n = 0;
      const cards = steps.map((x, i) => card(x, i, isPlan(x) ? n : ++n, scale, runId));
      if (a.running && cards.length) cards.at(-1).classList.add("current");
      $("wall").replaceChildren(...cards.reverse());  // newest first
    }
  } catch {
    statusAvailable = false;
    down = null;
    renderFrameState();
  } finally { clearTimeout(timeout); }
  setTimeout(poll, 500);
}

applyLang();
applyMotion();
refreshLive();
poll();
