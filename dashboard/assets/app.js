(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  const REFRESH_MS = 1000;
  const FRAMES_MS = 2500;
  const RESULTS_MS = 5000;
  const DEMO_MS = 3600;
  const MAX_FRAMES = 200;

  const MODE_MAP = {
    "74be": "制冷",
    "6cbe": "抽湿",
    "64be": "送风",
    "5c7e": "制热",
    "747e": "自动",
  };
  const RUN_BYTES = new Set([0x02, 0x06, 0x0a, 0xf2]);
  const IDLE_BYTES = new Set([0xee, 0xfe, 0xea]);
  const OFFSETS = ["+0", "+1", "+2", "+3", "+4", "+5", "+6", "+7", "+8", "+9", "+10", "+11"];
  const BYTE_LABELS = ["ADDR", "ADDR", "ADDR", "ADDR", "F2", "MODE", "TEMP", "RUN", "FAN", "FLAG", "END", "END"];
  const BYTE_CLASSES = ["", "", "", "", "", "is-mode", "is-temp", "is-run", "is-fan", "is-flag", "", ""];
  const OPS = [
    ["power", "开关机", "按面板 On/Off 后记录"],
    ["temp_up", "升温 +1°C", "按面板 ▲ 后记录"],
    ["temp_dn", "降温 -1°C", "按面板 ▼ 后记录"],
    ["fan", "风速", "按面板 Fan 后记录"],
    ["mode", "模式", "按面板 Mode 后记录"],
    ["swing", "摆风", "按面板 Swing 后记录"],
    ["sleep", "睡眠", "按面板 Sleep 后记录"],
    ["turbo", "强力", "按面板 Turbo 后记录"],
  ];

  const state = {
    mode: "live",
    snapshot: null,
    decoded: null,
    frames: [],
    history: [],
    lastKey: "",
    lastSuccess: 0,
    errors: 0,
    tab: "monitor",
    framesPaused: false,
    demo: null,
    demoIndex: 0,
    demoPaused: false,
    demoMeta: null,
    themeMode: "auto",
    timers: {},
    results: [],
    toastTimer: null,
    frameSignature: "",
    resultsSignature: "",
  };

  const els = {};

  function cacheElements() {
    for (const id of [
      "conn-badge", "conn-text", "theme-toggle", "theme-icon", "demo-bar", "demo-label", "demo-toggle",
      "demo-icon", "empty", "empty-title", "empty-message", "retry", "monitor", "setpoint-value",
      "hero-mode", "hero-fan", "range-fill", "range-marker", "tile-mode", "tile-fan", "tile-run",
      "tile-xfan", "tile-xfan-hint", "device-age", "stat-port", "stat-frames", "stat-last",
      "stat-source", "timeline", "byte-strip", "raw-status", "raw-e9", "copy-status", "dec-temp",
      "dec-mode", "dec-fan", "dec-run", "dec-xfan", "dec-room", "frame-filter", "frame-pause",
      "frame-clear", "frame-body", "frame-count", "frame-wrap", "ops", "op-status", "results", "toast",
    ]) {
      els[id] = document.getElementById(id);
    }
    els.tabs = $$(".tab");
    els.panels = $$(".panel");
  }

  function hex(value, width = 2) {
    return Number.isFinite(value) ? value.toString(16).toUpperCase().padStart(width, "0") : "—";
  }

  function compactHex(value) {
    if (!value) return "";
    return String(value).replace(/[^0-9a-fA-F]/g, "").toUpperCase();
  }

  function spacedHex(value) {
    const compact = compactHex(value);
    if (!compact) return "—";
    return compact.match(/.{1,2}/g)?.join(" ") || compact;
  }

  function frameType(value) {
    const compact = compactHex(value);
    if (compact.includes("BAAACE3E")) return "STATUS";
    if (compact.includes("E9A6CE3E")) return "E9";
    if (compact.startsWith("72")) return "72";
    return "OTHER";
  }

  function normalizeFrame(frame) {
    if (Array.isArray(frame)) {
      const ts = Number(frame[0]);
      const raw = spacedHex(frame[1]);
      return { ts, hex: raw, type: frameType(raw), len: raw.split(" ").length };
    }
    const raw = spacedHex(frame?.hex || "");
    return {
      ts: Number(frame?.ts) || 0,
      hex: raw,
      type: frame?.tag && frame.tag !== "其他" ? frame.tag : frameType(raw),
      len: Number(frame?.len) || raw.split(" ").length,
      ascii: frame?.ascii || "",
    };
  }

  function clamp(value, low, high) {
    return Math.min(high, Math.max(low, value));
  }

  function formatAge(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "—";
    if (seconds < 2) return "just now";
    if (seconds < 60) return `${Math.round(seconds)}s ago`;
    return `${Math.round(seconds / 60)}m ago`;
  }

  function formatClock(ts) {
    if (typeof ts === "string") return ts;
    if (!Number.isFinite(ts)) return "—";
    const date = new Date(ts * 1000);
    return date.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function toast(message) {
    if (!els.toast) return;
    els.toast.textContent = message;
    els.toast.classList.add("is-visible");
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => els.toast.classList.remove("is-visible"), 2200);
  }

  function applyTheme() {
    const prefersDark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
    const resolved = state.themeMode === "auto" ? (prefersDark ? "dark" : "light") : state.themeMode;
    document.documentElement.dataset.theme = resolved;
    if (els.themeIcon) els.themeIcon.setAttribute("href", resolved === "dark" ? "#i-sun" : "#i-moon");
    if (els["theme-toggle"]) {
      const label = state.themeMode === "auto" ? "自动" : state.themeMode === "light" ? "浅色" : "深色";
      els["theme-toggle"].title = `主题：${label}（点击切换）`;
      els["theme-toggle"].setAttribute("aria-label", `主题：${label}`);
    }
  }

  function initTheme() {
    try {
      const queryTheme = new URLSearchParams(location.search).get("theme");
      state.themeMode = queryTheme === "light" || queryTheme === "dark"
        ? queryTheme
        : (localStorage.getItem("gree-theme") || "auto");
    } catch (_) {
      state.themeMode = "auto";
    }
    applyTheme();
    els["theme-toggle"]?.addEventListener("click", () => {
      state.themeMode = state.themeMode === "auto" ? "light" : state.themeMode === "light" ? "dark" : "auto";
      try {
        localStorage.setItem("gree-theme", state.themeMode);
      } catch (_) {
        /* private mode */
      }
      applyTheme();
    });
    window.matchMedia?.("(prefers-color-scheme: dark)").addEventListener?.("change", () => {
      if (state.themeMode === "auto") applyTheme();
    });
  }

  function setTab(tab) {
    state.tab = tab;
    els.tabs.forEach((button) => {
      const active = button.dataset.tab === tab;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", String(active));
    });
    els.panels.forEach((panel) => {
      const active = panel.id === `panel-${tab}`;
      panel.hidden = !active;
      panel.classList.toggle("is-active", active);
    });
    if (tab === "lab") renderLab();
  }

  async function fetchJSON(url, timeout = 3000, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(url, {
        cache: "no-store",
        signal: controller.signal,
        ...options,
      });
      if (!response.ok) throw new Error(`${url}: ${response.status}`);
      return await response.json();
    } finally {
      clearTimeout(timer);
    }
  }

  function decode(data) {
    const status = data?.status || {};
    const bytes = Array.isArray(status.sbytes) ? status.sbytes : [];
    const modeByte = bytes[5];
    const tempByte = bytes[6];
    const runByte = bytes[7];
    const fanByte = bytes[8];
    const flagByte = bytes[9];
    const pair = Number.isFinite(modeByte) && Number.isFinite(flagByte) ? hex(modeByte) + hex(flagByte) : "";
    const mode = modeByte === 0xf4 ? "关机" : MODE_MAP[pair] || status.mode || "未知";
    // Prefer the byte in the frame itself so the big number always matches
    // the byte strip and does not inherit the old server's 10s mode filter.
    let setpoint = null;
    if (Number.isFinite(tempByte)) {
      const decoded = Math.floor((0xfe - tempByte) / 4);
      if (decoded >= 16 && decoded <= 30) setpoint = decoded;
    }
    if (setpoint === null && Number.isFinite(status.setpoint)) setpoint = status.setpoint;
    const fan = fanByte === 0xfa ? "1 档" : fanByte === 0xf6 ? "2 档" : fanByte === 0xf2 ? "3 档" : null;
    const run = modeByte === 0xf4
      ? "关机"
      : RUN_BYTES.has(runByte)
        ? "运行中"
        : IDLE_BYTES.has(runByte)
          ? "待机"
          : "未知";
    const xfanByte = data?.blow?.xfan_byte;
    const xfanKnown = Number.isFinite(xfanByte);
    const xfanRunning = xfanByte === 0xf4;
    return {
      mode,
      setpoint,
      fan,
      run,
      xfan: !xfanKnown ? "未知" : xfanRunning ? "正在吹干" : "未在吹干",
      xfanKnown,
      xfanRunning,
      modeByte,
      tempByte,
      runByte,
      fanByte,
      flagByte,
      xfanByte,
      rawStatus: status.raw || "—",
      rawE9: data?.e9?.raw || "—",
      e9b5: data?.e9?.b5,
      e9b6: data?.e9?.b6,
    };
  }

  function describeChange(previous, next) {
    if (!previous) return "已连接，读到第一帧";
    const changes = [];
    if (previous.setpoint !== next.setpoint) {
      changes.push(`设定 ${previous.setpoint ?? "—"}→${next.setpoint ?? "—"}°C`);
    }
    if (previous.mode !== next.mode) changes.push(`模式 ${previous.mode}→${next.mode}`);
    if (previous.fan !== next.fan) changes.push(`风速 ${previous.fan ?? "—"}→${next.fan ?? "—"}`);
    if (previous.run !== next.run) changes.push(`运行 ${previous.run}→${next.run}`);
    if (previous.xfan !== next.xfan) changes.push(`X-FAN ${previous.xfan}→${next.xfan}`);
    return changes.join(" · ") || "状态更新";
  }

  function applySnapshot(snapshot, meta = null) {
    const previous = state.decoded;
    const next = decode(snapshot);
    state.snapshot = snapshot;
    state.decoded = next;
    state.demoMeta = meta;
    state.frames = (snapshot?.frames || []).map(normalizeFrame).slice(-MAX_FRAMES);
    if (state.mode === "live") {
      state.lastSuccess = Date.now();
      state.errors = 0;
    }
    const key = [next.mode, next.setpoint, next.fan, next.run, next.xfan].join("|");
    if (key !== state.lastKey) {
      const time = meta?.at || formatClock(snapshot?.status?.ts || snapshot?.last_update);
      state.history.unshift({ time, text: describeChange(previous, next) });
      state.history = state.history.slice(0, 7);
      state.lastKey = key;
    }
    renderAll();
  }

  function startLive() {
    clearTimers();
    state.timers.live = setInterval(refreshLive, REFRESH_MS);
    state.timers.frames = setInterval(refreshFrames, FRAMES_MS);
    state.timers.results = setInterval(refreshResults, RESULTS_MS);
    refreshFrames();
    refreshResults();
  }

  function clearTimers() {
    Object.values(state.timers).forEach(clearInterval);
    state.timers = {};
  }

  async function refreshLive() {
    try {
      const data = await fetchJSON("/data", 2500);
      if (!data || !data.status) throw new Error("no status");
      applySnapshot(data);
    } catch (_) {
      state.errors += 1;
      renderHeader();
      if (state.errors === 3) toast("数据接口暂时不可用，保留最后已知状态");
    }
  }

  async function refreshFrames() {
    try {
      const data = await fetchJSON("/buslog?n=250", 2500);
      if (Array.isArray(data?.frames)) {
        state.frames = data.frames.map(normalizeFrame).slice(-MAX_FRAMES);
        if (state.tab === "lab") renderFrames();
      }
    } catch (_) {
      /* keep last frames */
    }
  }

  async function refreshResults() {
    try {
      const data = await fetchJSON("/results", 2500);
      if (Array.isArray(data?.results)) {
        state.results = data.results;
        if (state.tab === "lab") renderResults();
      }
    } catch (_) {
      /* keep last results */
    }
  }

  async function loadInitial() {
    if (new URLSearchParams(location.search).has("demo") || location.protocol === "file:") {
      await startDemo();
      return;
    }
    try {
      const data = await fetchJSON("/data", 2500);
      if (!data || !data.status) throw new Error("no status");
      state.mode = "live";
      applySnapshot(data);
      startLive();
    } catch (_) {
      await startDemo();
    }
  }

  async function startDemo() {
    try {
      const demo = await fetchJSON("/demo.json", 3000);
      if (!Array.isArray(demo?.timeline) || !demo.timeline.length) throw new Error("bad demo");
      state.mode = "demo";
      state.demo = demo;
      state.demoIndex = 0;
      state.demoPaused = false;
      els["demo-bar"].hidden = false;
      els["demo-icon"].setAttribute("href", "#i-pause");
      applyDemoStep();
      state.timers.demo = setInterval(() => {
        if (!state.demoPaused) {
          state.demoIndex = (state.demoIndex + 1) % state.demo.timeline.length;
          applyDemoStep();
        }
      }, DEMO_MS);
    } catch (_) {
      showEmpty(true, "无法加载 demo 数据", "请通过 HTTP 打开页面，例如 python3 serve_demo.py，然后访问 http://localhost:8765/?demo=1。");
    }
  }

  function applyDemoStep() {
    const item = state.demo?.timeline?.[state.demoIndex];
    if (!item) return;
    els["demo-label"].textContent = `${state.demoIndex + 1}/${state.demo.timeline.length} · ${item.label}`;
    applySnapshot(item.data, item);
  }

  function showEmpty(visible, title, message) {
    els.empty.hidden = !visible;
    els.monitor.hidden = visible;
    if (title) els["empty-title"].textContent = title;
    if (message) els["empty-message"].textContent = message;
  }

  function renderAll() {
    renderHeader();
    renderMonitor();
    if (state.tab === "lab") renderLab();
  }

  function renderHeader() {
    const live = state.mode === "live";
    const ageSeconds = live ? (Date.now() - state.lastSuccess) / 1000 : null;
    let text = "Connecting";
    let cls = "badge badge-muted";
    if (!live) {
      text = "demo";
      cls = "badge badge-demo";
    } else if (ageSeconds < 8 && state.errors === 0) {
      text = "Online";
      cls = "badge badge-ok";
    } else if (ageSeconds < 30) {
      text = "Stale";
      cls = "badge badge-warn";
    } else if (state.lastSuccess) {
      text = "Offline";
      cls = "badge badge-error";
    }
    els["conn-badge"].className = cls;
    els["conn-text"].textContent = text;
    els["device-age"].textContent = live ? formatAge(ageSeconds) : (state.demoMeta?.at || "demo");
  }

  function renderMonitor() {
    const d = state.decoded;
    if (!d) return;
    const sp = d.setpoint;
    els["setpoint-value"].textContent = sp ?? "—";
    els["setpoint-value"].classList.toggle("is-empty", sp === null);
    const percent = sp === null ? 0 : clamp(((sp - 16) / 14) * 100, 0, 100);
    els["range-fill"].style.width = `${percent}%`;
    els["range-marker"].style.left = `${percent}%`;
    els["hero-mode"].textContent = d.mode;
    els["hero-mode"].className = `badge ${d.mode === "关机" ? "badge-muted" : "badge-teal"}`;
    els["hero-fan"].textContent = d.fan || "风速未知";
    els["hero-fan"].className = `badge ${d.fan ? "badge-outline" : "badge-muted"}`;

    els["tile-mode"].textContent = d.mode;
    els["tile-fan"].textContent = d.fan || "未知";
    els["tile-run"].textContent = d.run;
    els["tile-xfan"].textContent = d.xfan;
    els["tile-xfan-hint"].textContent = !d.xfanKnown
      ? "0xEE frame not seen"
      : d.xfanRunning
        ? "After shutdown"
        : "Setting not readable";
    const xfanTile = els["tile-xfan"].closest(".tile");
    if (xfanTile) xfanTile.dataset.tone = !d.xfanKnown ? "muted" : d.xfanRunning ? "teal" : "amber";

    els["stat-port"].textContent = state.snapshot?.port || "/dev/ttyUSB0";
    els["stat-frames"].textContent = String(state.snapshot?.frame_count ?? state.frames.length ?? 0);
    els["stat-last"].textContent = state.mode === "live"
      ? formatAge((Date.now() - state.lastSuccess) / 1000)
      : (state.demoMeta?.at || "demo");
    els["stat-source"].textContent = state.mode === "live" ? "live" : "demo";

    renderTimeline();
    renderByteStrip(Array.isArray(state.snapshot?.status?.sbytes) ? state.snapshot.status.sbytes : []);
  }

  function renderTimeline() {
    els.timeline.replaceChildren();
    if (!state.history.length) {
      const li = document.createElement("li");
      li.className = "timeline-empty";
      li.textContent = "Waiting for data…";
      els.timeline.append(li);
      return;
    }
    for (const item of state.history) {
      const li = document.createElement("li");
      const time = document.createElement("time");
      time.textContent = item.time;
      const text = document.createElement("span");
      text.textContent = item.text;
      li.append(time, text);
      els.timeline.append(li);
    }
  }

  function renderByteStrip(bytes) {
    els["byte-strip"].replaceChildren();
    if (!bytes.length) {
      const empty = document.createElement("span");
      empty.className = "muted small";
      empty.textContent = "尚未捕获状态帧";
      els["byte-strip"].append(empty);
      return;
    }
    bytes.forEach((value, index) => {
      const chip = document.createElement("div");
      chip.className = `byte ${BYTE_CLASSES[index] || ""}`.trim();
      let label = BYTE_LABELS[index] || "?";
      if (index === 5) label = `MODE ${state.decoded?.mode || ""}`.trim();
      if (index === 6) label = `TEMP ${state.decoded?.setpoint ?? "?"}°`;
      if (index === 7) label = `RUN ${state.decoded?.run || ""}`.trim();
      if (index === 8) label = `FAN ${state.decoded?.fan?.replace(" 档", "") || "?"}`;
      const off = document.createElement("span");
      off.className = "off";
      off.textContent = OFFSETS[index] || "";
      const hexValue = document.createElement("span");
      hexValue.className = "hex";
      hexValue.textContent = hex(value);
      const lbl = document.createElement("span");
      lbl.className = "lbl";
      lbl.textContent = label;
      chip.append(off, hexValue, lbl);
      els["byte-strip"].append(chip);
    });
  }

  function renderLab() {
    const d = state.decoded;
    if (!d) return;
    els["raw-status"].textContent = spacedHex(d.rawStatus);
    els["raw-e9"].textContent = spacedHex(d.rawE9);
    els["dec-temp"].textContent = d.setpoint === null ? "—" : `${d.setpoint}°C`;
    els["dec-mode"].textContent = d.mode;
    els["dec-fan"].textContent = d.fan || "—";
    els["dec-run"].textContent = d.run;
    els["dec-xfan"].textContent = d.xfan;
    renderFrames();
    renderOps();
    renderResults();
  }

  function renderFrames() {
    if (state.framesPaused) return;
    const filter = els["frame-filter"].value;
    const rows = state.frames.filter((frame) => {
      if (filter === "all") return true;
      if (filter === "status") return frame.type === "STATUS";
      if (filter === "e9") return frame.type === "E9";
      return frame.type !== "STATUS" && frame.type !== "E9";
    }).slice(-MAX_FRAMES).reverse();
    const firstTs = state.frames[0]?.ts || 0;
    const lastTs = state.frames[state.frames.length - 1]?.ts || 0;
    const signature = `${filter}:${state.frames.length}:${firstTs}:${lastTs}`;
    if (signature === state.frameSignature) return;
    state.frameSignature = signature;
    const wrap = els["frame-wrap"];
    const scrollTop = wrap.scrollTop;
    const fragment = document.createDocumentFragment();
    if (!rows.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 4;
      td.className = "empty-cell";
      td.textContent = "Waiting for frames…";
      tr.append(td);
      fragment.append(tr);
    } else {
      for (const frame of rows) {
        const tr = document.createElement("tr");
        const time = document.createElement("td");
        time.textContent = formatClock(frame.ts);
        const type = document.createElement("td");
        type.textContent = frame.type;
        const len = document.createElement("td");
        len.textContent = String(frame.len || "");
        const raw = document.createElement("td");
        raw.textContent = frame.hex;
        raw.title = frame.hex;
        tr.append(time, type, len, raw);
        fragment.append(tr);
      }
    }
    els["frame-body"].replaceChildren(fragment);
    wrap.scrollTop = scrollTop;
    els["frame-count"].textContent = `${rows.length} frames`;
  }

  function renderOps() {
    if (els.ops.dataset.mode === state.mode && els.ops.children.length) return;
    els.ops.dataset.mode = state.mode;
    els.ops.replaceChildren();
    for (const [op, label, hint] of OPS) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "op";
      button.dataset.op = op;
      button.disabled = state.mode === "demo";
      const title = document.createElement("b");
      title.textContent = label;
      const sub = document.createElement("span");
      sub.textContent = state.mode === "demo" ? "Live mode only" : hint;
      button.append(title, sub);
      button.addEventListener("click", () => recordOperation(op));
      els.ops.append(button);
    }
    const reset = document.createElement("button");
    reset.type = "button";
    reset.className = "op";
    reset.disabled = state.mode === "demo";
    const resetTitle = document.createElement("b");
    resetTitle.textContent = "重置基准";
    const resetHint = document.createElement("span");
    resetHint.textContent = state.mode === "demo" ? "Live mode only" : "把下一次快照作为新基准";
    reset.append(resetTitle, resetHint);
    reset.addEventListener("click", resetBaseline);
    els.ops.append(reset);
  }

  async function recordOperation(op) {
    if (state.mode === "demo") {
      toast("demo 模式不能录制；连接树莓派后再用");
      return;
    }
    $$(".op", els.ops).forEach((button) => { button.disabled = true; });
    els["op-status"].textContent = "Recording 15s. Press the panel button now.";
    try {
      const result = await fetchJSON(`/record?op=${encodeURIComponent(op)}`, 22000, { method: "POST" });
      els["op-status"].textContent = `Recorded ${op} at ${formatClock(result.ts)}.`;
      await refreshResults();
      toast("快照已记录");
    } catch (error) {
      els["op-status"].textContent = `录制失败：${error.message || error}`;
      toast("录制失败");
    } finally {
      $$(".op", els.ops).forEach((button) => { button.disabled = state.mode === "demo"; });
    }
  }

  async function resetBaseline() {
    if (state.mode === "demo") return;
    try {
      await fetchJSON("/reset", 5000, { method: "POST" });
      state.results = [];
      renderResults();
      els["op-status"].textContent = "基准已重置。";
      toast("基准已重置");
    } catch (_) {
      toast("重置失败");
    }
  }

  function renderResults() {
    const lastResultTs = state.results[state.results.length - 1]?.ts || 0;
    const signature = `${state.mode}:${state.results.length}:${lastResultTs}`;
    if (signature === state.resultsSignature && els.results.children.length) return;
    state.resultsSignature = signature;
    els.results.replaceChildren();
    if (state.mode === "demo") {
      const note = document.createElement("div");
      note.className = "result-note";
      note.textContent = "demo 数据不能录制；连接真实总线后再用。";
      els.results.append(note);
      return;
    }
    if (!state.results.length) {
      const note = document.createElement("div");
      note.className = "result-note";
      note.textContent = "还没有快照。按面板按键后点一个录制按钮。";
      els.results.append(note);
      return;
    }
    for (const result of state.results.slice().reverse().slice(0, 8)) {
      const card = document.createElement("div");
      card.className = "result";
      const heading = document.createElement("h3");
      const time = document.createElement("span");
      time.className = "muted";
      time.textContent = `${formatClock(result.ts)} · `;
      heading.append(result.op || "snapshot", time);
      if (Number.isFinite(result.setpoint)) {
        const value = document.createElement("span");
        value.className = "badge badge-teal";
        value.textContent = `${result.setpoint}°C`;
        heading.append(" ", value);
      }
      card.append(heading);
      const diff = result.diff || {};
      if (diff.note) {
        const row = document.createElement("div");
        row.className = "diff-row";
        row.textContent = diff.note;
        card.append(row);
      }
      if (Array.isArray(diff.byte_diffs) && diff.byte_diffs.length) {
        for (const item of diff.byte_diffs.slice(0, 6)) {
          const before = spacedHex(item.base);
          const after = spacedHex(item.changed);
          const row = document.createElement("div");
          row.className = "diff-row";
          const offsets = Array.isArray(item.bytes) ? item.bytes.map((n) => `+${n}`).join(",") : "?";
          const label = document.createElement("span");
          label.textContent = `${item.type || "frame"} ${offsets}: `;
          const oldValue = document.createElement("span");
          oldValue.className = "before";
          oldValue.textContent = before;
          const arrow = document.createElement("span");
          arrow.textContent = " → ";
          const newValue = document.createElement("span");
          newValue.className = "after";
          newValue.textContent = after;
          row.append(label, oldValue, arrow, newValue);
          card.append(row);
        }
      }
      if (Array.isArray(diff.frame_count_changes) && diff.frame_count_changes.length) {
        for (const change of diff.frame_count_changes.slice(0, 3)) {
          const row = document.createElement("div");
          row.className = "diff-row";
          row.textContent = `frame count ${change.frame}: ${change.base} → ${change.changed}`;
          card.append(row);
        }
      }
      const file = result.snap ? String(result.snap).split("/").pop() : "";
      if (file) {
        const foot = document.createElement("div");
        foot.className = "result-note";
        foot.textContent = `snapshot: ${file}`;
        card.append(foot);
      }
      els.results.append(card);
    }
  }

  async function copyText(value) {
    const text = String(value || "");
    if (!text || text === "—") {
      toast("没有可复制的内容");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      toast("已复制");
    } catch (_) {
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.append(area);
      area.select();
      document.execCommand("copy");
      area.remove();
      toast("已复制");
    }
  }

  function bindEvents() {
    els.tabs.forEach((button) => button.addEventListener("click", () => setTab(button.dataset.tab)));
    els.retry?.addEventListener("click", () => {
      showEmpty(false);
      loadInitial();
    });
    els["demo-toggle"]?.addEventListener("click", () => {
      state.demoPaused = !state.demoPaused;
      els["demo-icon"].setAttribute("href", state.demoPaused ? "#i-play" : "#i-pause");
      els["demo-toggle"].setAttribute("aria-label", state.demoPaused ? "继续回放" : "暂停回放");
    });
    els["frame-filter"]?.addEventListener("change", renderFrames);
    els["frame-pause"]?.addEventListener("click", () => {
      state.framesPaused = !state.framesPaused;
      els["frame-pause"].textContent = state.framesPaused ? "Resume" : "Pause";
      if (!state.framesPaused) renderFrames();
    });
    els["frame-clear"]?.addEventListener("click", () => {
      state.frames = [];
      renderFrames();
      els["frame-count"].textContent = "0 frames";
    });
    els["copy-status"]?.addEventListener("click", () => copyText(els["raw-status"].textContent));
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        clearTimers();
      } else if (state.mode === "live") {
        startLive();
      } else if (state.mode === "demo" && state.demo) {
        state.timers.demo = setInterval(() => {
          if (!state.demoPaused) {
            state.demoIndex = (state.demoIndex + 1) % state.demo.timeline.length;
            applyDemoStep();
          }
        }, DEMO_MS);
      }
    });
  }

  function init() {
    cacheElements();
    initTheme();
    bindEvents();
    if (new URLSearchParams(location.search).get("tab") === "lab") setTab("lab");
    loadInitial();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
