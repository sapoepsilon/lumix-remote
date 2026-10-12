"use strict";
// Lumix Remote UI (iOS-camera style). Vanilla JS over the JSON API + /api/ws.

const $ = (id) => document.getElementById(id);
const LS = {
  get(k, d) { try { const v = localStorage.getItem("lumix." + k); return v === null ? d : JSON.parse(v); } catch (_) { return d; } },
  set(k, v) { try { localStorage.setItem("lumix." + k, JSON.stringify(v)); } catch (_) { /* private mode */ } },
};
const state = {
  status: null, exposure: null, settings: [], filter: "", mode: LS.get("mode", "photo"),
  auto: { on: false }, autoBusy: false, picker: null, skipConnect: false, everReady: false,
  lastBusyAt: 0, conn: null, mftap: LS.get("mftap", "off"),
};
const MODES = ["photo", "video", "timelapse"];

// ---------------------------------------------------------------- helpers
class HttpError extends Error { constructor(msg, status) { super(msg); this.status = status; } }

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch (_) { /* non-JSON */ }
  if (!r.ok) throw new HttpError(friendlyHttp(r.status, data && data.error), r.status);
  return data;
}

function friendlyHttp(code, msg) {
  if (code === 409) return msg && /not connected/.test(msg) ? "Camera isn't connected yet." : "Camera is busy with another command. Try again in a moment.";
  if (code === 502) return "Couldn't reach the camera" + (msg ? ` (${msg.split(":")[0]})` : ".");
  return msg || "Request failed (" + code + ")";
}

const RESULT_TEXT = {
  err_busy: "Camera is busy. Close any menu or playback on the camera and try again.",
  err_reject: "The camera didn't allow that in its current mode.",
  err_param: "The camera rejected that value.",
  err_critical: "The camera couldn't do that right now.",
  err_nonsupport: "This camera doesn't support that remotely.",
};
function explain(result) {
  if (!result || result === "ok") return null;
  if (result === "err_busy") state.lastBusyAt = Date.now();
  return RESULT_TEXT[result] || "Camera answered " + result;
}

let toastTimer = null;
function toast(msg, err) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast glass-strong" + (err ? " err" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    t.classList.add("leaving");
    setTimeout(() => { t.hidden = true; }, 300);
  }, err ? 4500 : 2200);
  if (err) haptic([10, 40, 10]);
}

function haptic(p) { try { if (navigator.vibrate) navigator.vibrate(p || 8); } catch (_) { /* unsupported */ } }

function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids) if (k !== null && k !== undefined && k !== false) e.append(k);
  return e;
}
function icon(name, cls) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  if (cls) s.setAttribute("class", cls);
  const u = document.createElementNS("http://www.w3.org/2000/svg", "use");
  u.setAttribute("href", "#i-" + name);
  s.append(u);
  return s;
}

function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60, s = sec % 60;
  const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}
function fmtSize(b) {
  if (!b && b !== 0) return "";
  const u = ["B", "KB", "MB", "GB"]; let i = 0; b = +b;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return (i ? b.toFixed(1) : b) + " " + u[i];
}

async function cameraCall(path, body, okMsg) {
  try {
    const r = await api(path, body);
    const hint = explain(r && r.result);
    if (hint) toast(hint, true); else if (okMsg) toast(typeof okMsg === "function" ? okMsg(r) : okMsg);
    return r;
  } catch (e) { toast(e.message, true); return null; }
}

// ---------------------------------------------------------------- settings model
function byType(type) {
  let best = null;
  for (const s of state.settings) {
    if (s.cmd_types.length === 1 && s.cmd_types[0] === type) {
      if (s.current !== null && s.enabled) return s;
      if (!best) best = s;
    }
  }
  return best;
}
function shortTitle(type, t) {
  if (!t) return "–";
  if (type === "whitebalance") {
    return { "Auto White Balance": "AWB", "AWB(Cool)": "AWBc", "AWB(Warm)": "AWBw", "Incandescent": "Tungsten" }[t] || t;
  }
  if (type === "videoquality") {
    const m = t.match(/^(\d+)x(\d+),(\d+p)/);
    if (m) {
      const res = { 3840: "4K", 4096: "C4K", 5888: "5.9K", 1920: "FHD" }[m[1]] || `${m[2]}p`;
      return `${res} ${m[3]}${/10bit/.test(t) ? " 10b" : ""}`;
    }
  }
  if (type === "iso" && t === "AUTO") return "AUTO";
  if (type === "drivemode") return { "Time Lapse/Animation": "Time Lapse", "Burst Shot 1": "Burst 1", "Burst Shot 2": "Burst 2" }[t] || t;
  return t;
}

// Quick-control tiles: [label, type, kind] kind "raw" = aperture/shutter codes.
const QC = [
  ["Aperture", "focal", "raw"], ["Shutter", "shtrspeed", "raw"], ["ISO", "iso"],
  ["EV", "exposure"], ["WB", "whitebalance"], ["Mode", "recmode"],
  ["Focus", "focusmode"], ["AF area", "afmode"], ["Drive", "drivemode"],
  ["Quality", "quality"], ["Size", "pictsize"], ["Video", "videoquality"],
];

function qcValue(type) {
  const ex = state.exposure || {};
  if (type === "focal") return { title: ex.focal_title, value: ex.focal, options: (ex.aperture_options || []).map((o) => ({ id: o.value, title: o.title, value: o.value, enabled: true })), enabled: !!(ex.aperture_options || []).length };
  if (type === "shtrspeed") return { title: ex.shtrspeed_title, value: ex.shtrspeed, options: (ex.shutter_options || []).map((o) => ({ id: o.value, title: o.title, value: o.value, enabled: true })), enabled: !!(ex.shutter_options || []).length };
  const s = byType(type);
  if (!s) return null;
  return { setting: s, title: shortTitle(type, s.current_title), value: s.current_option, options: s.options.filter((o) => o.value !== null), enabled: s.enabled };
}

function renderQC() {
  const box = $("qc");
  const scroll = box.querySelector(".picker") ? box.querySelector(".picker").scrollLeft : 0;
  box.replaceChildren();
  for (const [label, type] of QC) {
    const v = qcValue(type);
    if (!v) continue;
    const tile = el("button", {
      class: "tile" + (v.enabled ? "" : " disabled") + (state.picker === type ? " sel" : ""),
      "data-type": type, "aria-expanded": state.picker === type ? "true" : "false",
      onclick: () => { haptic(); state.picker = state.picker === type ? null : type; renderQC(); },
    }, el("small", {}, label), el("b", {}, v.title || "–"));
    box.append(tile);
    if (state.picker === type) {
      // picker goes after the last tile of this row (3 per row)
      tile.dataset.open = "1";
    }
  }
  if (state.picker) {
    const v = qcValue(state.picker);
    const tiles = [...box.children];
    const idx = tiles.findIndex((t) => t.dataset.open);
    if (v && idx >= 0) {
      const rowEnd = Math.min(tiles.length - 1, idx - (idx % 3) + 2);
      const picker = el("div", { class: "picker", role: "listbox" });
      for (const o of v.options) {
        const on = o.id === v.value || o.value === v.value;
        picker.append(el("button", {
          class: "chip" + (on ? " on" : ""), disabled: !o.enabled && !on, role: "option", "aria-selected": on ? "true" : "false",
          onclick: () => applyQC(state.picker, o),
        }, shortTitle(state.picker, o.title)));
      }
      tiles[rowEnd].after(picker);
      const on = picker.querySelector(".on");
      requestAnimationFrame(() => {
        if (on) picker.scrollLeft = on.offsetLeft - picker.clientWidth / 2 + on.offsetWidth / 2;
        else picker.scrollLeft = scroll;
      });
    }
  }
  renderReadout();
}

async function applyQC(type, o) {
  haptic();
  const tile = $("qc").querySelector(`[data-type="${type}"]`);
  if (tile) tile.classList.add("pending");
  if (type === "focal" || type === "shtrspeed") {
    const r = await cameraCall("/api/setting", { type, value: o.value });
    if (r && r.result !== "ok" && type === "shtrspeed") toast("In A / P mode the camera sets the shutter itself. Use M or S mode on the dial.", true);
    if (r && r.result !== "ok" && type === "focal") toast("In S / P mode the camera sets the aperture itself.", true);
  } else {
    const s = byType(type);
    await cameraCall(`/api/settings/${encodeURIComponent(s.id)}`, { option: o.id });
  }
  loadSettings();
}

function renderReadout() {
  const r = $("readout");
  r.replaceChildren();
  const ex = state.exposure || {};
  const parts = [];
  if (state.auto.on) parts.push(["AUTO", ""]);
  const mode = byType("recmode");
  if (mode && mode.current_title && !state.auto.on) parts.push([mode.current_title, "dim"]);
  if (ex.focal_title) parts.push([ex.focal_title, ""]);
  if (ex.shtrspeed_title) parts.push([ex.shtrspeed_title, ""]);
  const iso = byType("iso");
  if (iso && iso.current_title) parts.push(["ISO " + iso.current_title, ""]);
  const ev = byType("exposure");
  if (ev && ev.current_title) parts.push([(ev.current_title === "0" ? "±0" : ev.current_title) + " EV", ""]);
  const fm = byType("focusmode");
  if (fm && fm.current_title) parts.push([fm.current_title, "dim"]);
  for (const [t, c] of parts) r.append(el("span", { class: c }, t));
  r.hidden = !parts.length;
}

// ---------------------------------------------------------------- all settings
const SECTION_NAMES = {
  record_top: "Exposure", record_qmenu: "Quick menu", record_menu: "Recording",
  record_tab: "Display", record_tab2: "Display (2)", play_menu: "Playback", home_menu: "Home",
};

function renderAll() {
  const box = $("allsettings");
  box.replaceChildren();
  $("allcount").textContent = state.settings.length ? `${state.settings.length} settings from the camera's own menu` : "Appears once the camera is connected";
  if (!state.settings.length) { box.append(el("p", { class: "page-sub" }, "Settings appear once the camera is connected.")); return; }
  const f = state.filter.toLowerCase();
  const groups = new Map();
  for (const s of state.settings) {
    const text = (s.title + " " + s.path.join(" ") + " " + (s.current_title || "") + " " + s.cmd_types.join(" ")).toLowerCase();
    if (f && !text.includes(f)) continue;
    if (!groups.has(s.section)) groups.set(s.section, []);
    groups.get(s.section).push(s);
  }
  for (const [sec, list] of groups) {
    box.append(el("h3", { class: "group-title" }, SECTION_NAMES[sec] || sec));
    const g = el("div", { class: "group" });
    for (const s of list) {
      const usable = s.options.some((o) => o.value !== null);
      g.append(el("button", {
        class: "cell nav-cell" + (s.enabled ? "" : " disabled"), title: s.cmd_types.join(", "),
        onclick: () => openOptions(s.id),
      },
      el("div", { class: "cell-text" }, el("b", {}, s.title), s.path.length ? el("span", { class: "path" }, s.path.join(" › ")) : null),
      el("span", { class: "val" }, usable ? (s.current_title || "–") : "on camera"),
      icon("chev", "chev")));
    }
    box.append(g);
  }
  if (!groups.size) box.append(el("p", { class: "page-sub" }, "No setting matches."));
}

let optId = null;
function openOptions(id) {
  optId = id;
  renderOptions();
  push("opt");
}
function renderOptions() {
  const s = state.settings.find((x) => x.id === optId);
  if (!s) return;
  $("opttitle").textContent = s.title;
  $("optback").textContent = "All settings";
  $("optsub").textContent = [s.path.join(" › "), s.enabled ? "" : "Not available in the camera's current mode."].filter(Boolean).join(" · ");
  const list = $("optlist");
  list.replaceChildren();
  for (const o of s.options) {
    const cur = o.id === s.current_option;
    const param = o.value === null;
    list.append(el("button", {
      class: "cell nav-cell" + (o.enabled && !param ? "" : " disabled"), disabled: (!o.enabled && !cur) || param || !s.enabled,
      onclick: async () => {
        haptic();
        await cameraCall(`/api/settings/${encodeURIComponent(s.id)}`, { option: o.id }, (r) => `${r.setting.title}: ${r.setting.current_title}`);
        loadSettings();
      },
    }, el("div", { class: "cell-text" }, el("b", {}, o.title || o.value), param ? el("small", {}, "Needs a value; set it on the camera") : null),
    cur ? icon("check", "tick") : null));
  }
}

let loadTimer = null;
function loadSettings() {
  clearTimeout(loadTimer);
  loadTimer = setTimeout(async () => {
    try {
      const [all, quick] = await Promise.all([api("/api/settings"), api("/api/quick")]);
      state.settings = all.settings || [];
      state.exposure = quick.exposure;
      renderQC();
      renderAll();
      if (optId) renderOptions();
    } catch (e) { /* server restarting */ }
  }, 120);
}

// ---------------------------------------------------------------- auto preset
function renderAuto() {
  const on = !!state.auto.on;
  $("btnauto").setAttribute("aria-pressed", on ? "true" : "false");
  $("btnauto").classList.toggle("busy", state.autoBusy);
  $("autoswitch").checked = on;
  $("autoswitch").disabled = state.autoBusy;
  $("autosub").textContent = on
    ? `On (${state.auto.target || "photo"}): ${state.auto.recmode_used === "ia" ? "Intelligent Auto" : "Program auto"}, AF with face/eye detection, auto ISO and WB. Turn off to restore your settings.`
    : "Camera picks exposure, focus and white balance. Turning it off restores your settings.";
  renderReadout();
}

async function setAuto(on) {
  if (state.autoBusy) return;
  state.autoBusy = true; renderAuto(); haptic(on ? [8, 30, 8] : 8);
  const target = state.mode === "video" ? "video" : "photo";
  try {
    const r = await api("/api/presets/auto", { on, target });
    state.auto = r.auto;
    const bad = (r.steps || []).filter((s) => s.result !== "ok" && s.result !== "unchanged");
    if (on) toast(bad.length ? `Auto on (${bad.length} setting${bad.length > 1 ? "s" : ""} not allowed in this mode)` : (r.auto.recmode_used === "ia" ? "Auto on: Intelligent Auto" : "Auto on"));
    else toast(bad.length ? `Auto off; ${bad.length} value${bad.length > 1 ? "s" : ""} couldn't be restored` : "Auto off: your settings are back", bad.length > 0);
  } catch (e) { toast(e.message, true); }
  state.autoBusy = false;
  renderAuto();
  loadSettings();
}

// ---------------------------------------------------------------- status
function statusInfo(s) {
  const st = s ? s.status : "offline";
  const msg = (s && s.message) || "";
  switch (st) {
    case "ready": return { title: "Connected", msg, cls: "ok", dot: "ready" };
    case "discovering": return { title: "Looking for your camera", msg: msg || "Scanning the network…", cls: "", dot: "wait" };
    case "connecting": return { title: "Connecting…", msg, cls: "", dot: "wait" };
    case "waiting_approval": return { title: "Approve on the camera", msg: "Your camera is registering this device. If it shows a prompt, press OK on the camera.", cls: "warn", dot: "wait" };
    case "reconnecting": return { title: "Reconnecting…", msg: msg.includes("powered off") ? "The camera is off. Switch it on and it will reconnect." : "The camera dropped off (asleep or out of range). Retrying automatically.", cls: "warn", dot: "wait" };
    case "error": return { title: "Something's wrong", msg, cls: "warn", dot: "bad" };
    case "offline": return { title: "Server not reachable", msg: "Start it with ./run.sh on your Mac.", cls: "warn", dot: "bad" };
    default: return { title: "Starting…", msg, cls: "", dot: "wait" };
  }
}

function renderStatus(s) {
  const prev = state.status;
  state.status = s;
  const info = statusInfo(s);
  const ready = s.status === "ready";
  $("dot").className = "dot " + info.dot;
  const cam = s.camera || {};
  $("camname").textContent = ready && cam.friendlyName ? (cam.modelNumber || cam.friendlyName) : (ready ? "Camera" : info.title.replace("…", ""));
  if (ready && cam.friendlyName) {
    LS.set("camera", { name: cam.friendlyName, model: cam.modelNumber, ip: s.ip, ts: Date.now() });
    state.everReady = true;
  }
  // stats
  const stats = $("stats");
  stats.replaceChildren();
  if (ready) {
    const add = (label, v) => { if (v !== null && v !== undefined && v !== "") stats.append(el("span", {}, label, el("b", {}, String(v)))); };
    if (s.battery) {
      const pct = batteryPct(s.battery);
      const n = parseInt(pct, 10);
      stats.append(el("span", { class: "batt" + (n <= 20 ? " low" : "") },
        el("i", {}, el("b", { style: `width:${isNaN(n) ? 100 : n}%` })), el("b", {}, pct)));
    }
    if (state.mode === "video") { if (s.remain_video_s) add("", fmtTime(+s.remain_video_s) + " left"); }
    else add("", s.remain_photos ? s.remain_photos + " shots" : null);
    const sd = s.sd || {};
    if (sd.current) add("", sd.current.toUpperCase());
  }
  // live view
  const lv = s.liveview || {};
  const live = lv.age !== null && lv.age !== undefined && lv.age < 3;
  $("lvempty").hidden = live;
  $("lvemptytext").textContent = !ready ? info.title : "Waiting for live view…";
  // recording
  const rec = !!s.recording;
  $("app").classList.toggle("recording", rec);
  $("recbadge").hidden = !rec;
  if (rec) {
    const prog = s.rec_progress && +s.rec_progress > 0 ? +s.rec_progress : null;
    $("rectime").textContent = fmtTime(prog !== null ? prog : (s.rec_started_at ? Date.now() / 1000 - s.rec_started_at : 0));
  }
  // banner: dropped connection after first connect, or camera busy
  const busy = ready && !live && Date.now() - state.lastBusyAt < 30000;
  const banner = $("banner");
  if (!ready && (state.everReady || state.skipConnect)) {
    banner.hidden = false; $("bannerdot").className = "dot " + info.dot; $("bannertext").textContent = info.title;
  } else if (busy) {
    banner.hidden = false; $("bannerdot").className = "dot wait"; $("bannertext").textContent = "Camera busy: check its screen";
  } else banner.hidden = true;
  $("connsub").textContent = ready ? `${cam.friendlyName || ""} · ${s.ip || ""}` : info.title;
  renderConnect(s);
  if (!$("prosheet").hidden && currentPage() === "conn") renderConnInfo();
  if (ready && (!prev || prev.status !== "ready")) loadSettings();
}
function batteryPct(b) {
  const m = String(b).match(/^(\d+)\/(\d+)$/);
  return m && +m[2] > 0 ? Math.round(100 * m[1] / m[2]) + "%" : b;
}

// ---------------------------------------------------------------- connect screen
const DEFAULT_STEPS = [
  "Menu → Setup (wrench) → Wi-Fi", "Wi-Fi Function → New Connection", "Remote Shooting & View",
  "Press DISP to switch to \"Via Network\"", "Pick your home Wi-Fi (same network as this computer)",
  "Approve this device on the camera if it asks",
];
function renderChecklist(steps) {
  const done = LS.get("steps", []);
  const ol = $("checklist");
  ol.replaceChildren();
  steps.forEach((t, i) => {
    const li = el("li", { class: done.includes(i) ? "done" : "" },
      el("button", {
        onclick: () => {
          haptic();
          const d = LS.get("steps", []);
          const k = d.indexOf(i);
          if (k >= 0) d.splice(k, 1); else d.push(i);
          LS.set("steps", d);
          li.classList.toggle("done");
        },
      }, el("span", { class: "box" }, el("i", {}, String(i + 1)), icon("check")), el("span", { class: "t" }, t)));
    ol.append(li);
  });
}

let connectHideTimer = null;
function renderConnect(s) {
  const c = $("connect");
  const ready = s.status === "ready";
  const info = statusInfo(s);
  $("cstitle").textContent = info.title;
  $("csmsg").textContent = info.msg;
  $("radar").className = "radar " + info.cls;
  const rem = LS.get("camera", null);
  const remBox = $("csremember");
  if (rem && rem.name && !ready) {
    remBox.hidden = false;
    remBox.textContent = `Last camera: ${rem.model || ""} ${rem.name}${rem.ip ? " · " + rem.ip : ""}`;
  } else remBox.hidden = true;
  if (ready) {
    if (!c.hidden && !connectHideTimer) {
      connectHideTimer = setTimeout(() => {
        c.classList.add("leaving");
        setTimeout(() => { c.hidden = true; c.classList.remove("leaving"); connectHideTimer = null; }, 450);
      }, 700);
    }
  } else if (!state.skipConnect && !state.everReady) {
    clearTimeout(connectHideTimer); connectHideTimer = null;
    c.hidden = false; c.classList.remove("leaving");
  }
}

async function reconnect(ip, forget) {
  haptic();
  try {
    await api("/api/connection/reconnect", { ip: ip || null, forget: !!forget });
    toast(forget ? "Forgot the camera; searching again" : ip ? `Connecting to ${ip}…` : "Reconnecting…");
  } catch (e) { toast(e.message, true); }
}

async function loadConnection() {
  try {
    state.conn = await api("/api/connection");
    renderChecklist(state.conn.steps && state.conn.steps.length ? state.conn.steps : DEFAULT_STEPS);
  } catch (_) { renderChecklist(DEFAULT_STEPS); }
}

function renderConnInfo() {
  const s = state.status || {};
  const cam = s.camera || {};
  const rows = [
    ["Status", statusInfo(s).title], ["Camera", [cam.modelNumber, cam.friendlyName].filter(Boolean).join(" ") || "–"],
    ["Address", s.ip || "–"], ["Firmware", cam.X_FirmVersion || "–"], ["Lens", s.lens || "–"],
    ["Battery", s.battery ? batteryPct(s.battery) : "–"],
    ["Live view", s.liveview && s.liveview.fps ? s.liveview.fps + " fps" : "no frames"],
  ];
  const box = $("conninfo");
  box.replaceChildren(...rows.map(([k, v]) => el("div", { class: "cell" }, el("div", { class: "cell-text" }, el("b", {}, k)), el("span", { class: "val" }, v))));
}

// ---------------------------------------------------------------- capture
async function shoot() {
  const b = $("btnshutter");
  if (b.classList.contains("busy")) return;
  b.classList.add("busy");
  haptic(12);
  $("stage").classList.remove("flash"); void $("stage").offsetWidth; $("stage").classList.add("flash");
  const r = await cameraCall("/api/capture", {}, (x) => x.count_changed ? `Photo saved · ${x.remain_after} left` : "Shutter released");
  b.classList.remove("busy");
  return r;
}

async function toggleRec() {
  const rec = state.status && state.status.recording;
  haptic(rec ? 8 : [8, 30, 8]);
  await cameraCall(rec ? "/api/video/stop" : "/api/video/start", {}, rec ? "Recording stopped" : "Recording");
}

function shutterPressed() {
  if (state.mode === "photo") shoot();
  else if (state.mode === "video") toggleRec();
}

// ---------------------------------------------------------------- modes
function setMode(m, quiet) {
  if (!MODES.includes(m)) return;
  const changed = m !== state.mode;
  state.mode = m;
  LS.set("mode", m);
  $("app").dataset.mode = m;
  $("tlcard").hidden = m !== "timelapse";
  const btns = [...$("modestrack").children];
  btns.forEach((b) => b.classList.toggle("on", b.dataset.mode === m));
  positionModes();
  $("btnshutter").title = m === "video" ? "Start / stop video (R)" : m === "photo" ? "Take photo (Space)" : "Timelapse is coming soon";
  if (changed && !quiet) {
    haptic();
    if (state.auto.on && m !== "timelapse" && state.auto.target !== m) setAutoTarget(m);
  }
  if (state.status) renderStatus(state.status);
}
async function setAutoTarget(m) {
  state.autoBusy = true; renderAuto();
  try { const r = await api("/api/presets/auto", { on: true, target: m }); state.auto = r.auto; toast(`Auto tuned for ${m}`); }
  catch (e) { toast(e.message, true); }
  state.autoBusy = false; renderAuto(); loadSettings();
}
function positionModes() {
  const track = $("modestrack");
  const on = track.querySelector(".on");
  if (!on) return;
  const vertical = matchMedia("(min-width: 760px) and (min-aspect-ratio: 5/4)").matches;
  track.style.transform = vertical
    ? `translateY(${-(on.offsetTop + on.offsetHeight / 2)}px)`
    : `translateX(${-(on.offsetLeft + on.offsetWidth / 2)}px)`;
}
function stepMode(d) {
  const i = MODES.indexOf(state.mode) + d;
  if (i >= 0 && i < MODES.length) setMode(MODES[i]);
}

// Swipes on the viewfinder / mode strip change mode; a short tap on the viewfinder focuses.
function wireGestures() {
  let sx = 0, sy = 0, st = 0, active = false;
  const down = (e) => { active = true; sx = e.clientX; sy = e.clientY; st = Date.now(); };
  const up = (e, isLv) => {
    if (!active) return;
    active = false;
    const dx = e.clientX - sx, dy = e.clientY - sy;
    const vertical = matchMedia("(min-width: 760px) and (min-aspect-ratio: 5/4)").matches && !isLv;
    const main = vertical ? dy : dx, cross = vertical ? dx : dy;
    if (Math.abs(main) > 40 && Math.abs(main) > Math.abs(cross) * 1.3) { stepMode(main < 0 ? 1 : -1); return; }
    if (isLv && Math.abs(dx) < 10 && Math.abs(dy) < 10 && Date.now() - st < 600) touchFocus(e);
  };
  const lv = $("lv");
  lv.addEventListener("pointerdown", down);
  lv.addEventListener("pointerup", (e) => up(e, true));
  lv.addEventListener("pointercancel", () => { active = false; });
  const modes = $("modes");
  modes.addEventListener("pointerdown", (e) => { down(e); modes.setPointerCapture(e.pointerId); });
  modes.addEventListener("pointerup", (e) => {
    const dx = e.clientX - sx, dy = e.clientY - sy;
    if (Math.abs(dx) < 8 && Math.abs(dy) < 8) {
      active = false;
      const b = document.elementsFromPoint(e.clientX, e.clientY).find((x) => x.dataset && x.dataset.mode && x.tagName === "BUTTON");
      if (b) setMode(b.dataset.mode);
      return;
    }
    up(e, false);
  });
  window.addEventListener("resize", positionModes);
}

function lvPoint(ev) {
  // Map the tap to 0..1000 inside the displayed image (object-fit: contain).
  const img = $("lvimg");
  const r = img.getBoundingClientRect();
  const nw = img.naturalWidth || 16, nh = img.naturalHeight || 9;
  const scale = Math.min(r.width / nw, r.height / nh);
  const w = nw * scale, h = nh * scale;
  const ox = r.left + (r.width - w) / 2, oy = r.top + (r.height - h) / 2;
  const x = (ev.clientX - ox) / w, y = (ev.clientY - oy) / h;
  if (x < 0 || x > 1 || y < 0 || y > 1) return null;
  return { x: Math.round(x * 1000), y: Math.round(y * 1000), px: ev.clientX - r.left, py: ev.clientY - r.top };
}

async function touchFocus(ev) {
  if (state.mode === "timelapse") return;
  const p = lvPoint(ev);
  if (!p) return;
  const fm = byType("focusmode");
  if (fm && fm.current === "mf" && state.mftap === "off") {
    toast("Manual focus: tap does nothing. Turn on Auto or pick AF-S in Controls.");
    return;
  }
  const m = $("touchmark");
  m.style.left = p.px + "px";
  m.style.top = p.py + "px";
  m.classList.remove("show"); void m.offsetWidth; m.classList.add("show");
  haptic(6);
  await cameraCall("/api/touch", { x: p.x, y: p.y });
}

async function exitMfAssist() {
  const fm = byType("focusmode");
  const was = fm ? fm.current : null;
  haptic();
  const a = await cameraCall("/api/setting", { type: "focusmode", value: "afs" });
  if (a && a.result === "ok" && was && was !== "afs") {
    await new Promise((r) => setTimeout(r, 500));
    await cameraCall("/api/setting", { type: "focusmode", value: was }, "Inset closed");
  } else if (a && a.result === "ok") toast("Inset closed");
  loadSettings();
}

// ---------------------------------------------------------------- live view
function keepLiveView() {
  const img = $("lvimg");
  // Start the never-ending MJPEG response after page load so "load" fires.
  const start = () => { img.src = "/api/liveview.mjpg?t=" + Date.now(); };
  if (document.readyState === "complete") start(); else window.addEventListener("load", start);
  img.addEventListener("error", () => setTimeout(() => { img.src = "/api/liveview.mjpg?t=" + Date.now(); }, 2000));
  setInterval(() => {
    const lv = state.status && state.status.liveview;
    if (lv && lv.viewers === 0 && lv.age !== null && lv.age < 2) img.src = "/api/liveview.mjpg?t=" + Date.now();
  }, 4000);
}

// ---------------------------------------------------------------- pro sheet
const navStack = ["root"];
function currentPage() { return navStack[navStack.length - 1]; }
function showPage(name, dir) {
  for (const p of document.querySelectorAll("#pronav .page")) {
    const on = p.dataset.page === name;
    p.hidden = !on;
    p.classList.remove("in-right", "in-left");
    if (on && dir) { void p.offsetWidth; p.classList.add(dir); }
  }
  if (name === "conn") renderConnInfo();
  if (name === "all") renderAll();
}
function push(name) { navStack.push(name); showPage(name, "in-right"); }
function pop() { if (navStack.length > 1) { navStack.pop(); if (currentPage() !== "opt") optId = null; showPage(currentPage(), "in-left"); } }

function openSheet(page) {
  const s = $("prosheet");
  $("proscrim").hidden = false;
  s.setAttribute("aria-hidden", "false");
  s.hidden = false;
  requestAnimationFrame(() => s.classList.add("open"));
  if (page && currentPage() !== page) push(page);
  haptic();
}
function closeSheet() {
  const s = $("prosheet");
  s.classList.remove("open");
  s.style.transform = "";
  s.setAttribute("aria-hidden", "true");
  $("proscrim").hidden = true;
  state.picker = null;
  setTimeout(() => { if (!s.classList.contains("open")) { navStack.length = 1; optId = null; showPage("root"); renderQC(); } }, 450);
}
function sheetOpen() { return $("prosheet").classList.contains("open"); }

function wireSheet() {
  $("btnpro").addEventListener("click", () => (sheetOpen() ? closeSheet() : openSheet()));
  $("readout").addEventListener("click", () => openSheet());
  $("proscrim").addEventListener("click", closeSheet);
  $("prosheet").addEventListener("click", (e) => {
    const t = e.target.closest("[data-close],[data-pop],[data-push]");
    if (!t) return;
    if (t.hasAttribute("data-close")) closeSheet();
    else if (t.hasAttribute("data-pop")) pop();
    else push(t.dataset.push);
  });
  // drag the grabber down to dismiss
  const g = document.querySelector(".grabber"), s = $("prosheet");
  let y0 = null;
  g.addEventListener("pointerdown", (e) => { y0 = e.clientY; s.classList.add("dragging"); g.setPointerCapture(e.pointerId); });
  g.addEventListener("pointermove", (e) => { if (y0 !== null) s.style.transform = `translateY(${Math.max(0, e.clientY - y0)}px)`; });
  const end = (e) => {
    if (y0 === null) return;
    const dy = e.clientY - y0; y0 = null;
    s.classList.remove("dragging"); s.style.transform = "";
    if (dy > 90) closeSheet();
  };
  g.addEventListener("pointerup", end);
  g.addEventListener("pointercancel", end);
  $("autoswitch").addEventListener("change", (e) => setAuto(e.target.checked));
  $("filter").addEventListener("input", (e) => { state.filter = e.target.value; renderAll(); });
  $("clearlog").addEventListener("click", () => $("log").replaceChildren());
  $("btnexitassist").addEventListener("click", exitMfAssist);
  const seg = $("mftap");
  const paintSeg = () => [...seg.children].forEach((b) => b.classList.toggle("on", b.dataset.v === state.mftap));
  seg.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    state.mftap = b.dataset.v; LS.set("mftap", state.mftap); paintSeg(); haptic();
  });
  paintSeg();
  $("btnreconnect2").addEventListener("click", () => reconnect());
  $("btnsetup").addEventListener("click", () => { closeSheet(); state.skipConnect = false; $("connect").hidden = false; });
  $("btnpower").addEventListener("click", () => $("powerdlg").showModal());
  $("powerdlg").addEventListener("close", async () => {
    if ($("powerdlg").returnValue !== "ok") return;
    try { const r = await api("/api/power/off", { confirm: true }); toast("Power off: " + r.result); }
    catch (e) { toast(e.message, true); }
  });
}

// ---------------------------------------------------------------- event log
function addLog(ev) {
  const log = $("log");
  const t = new Date(ev.ts * 1000).toLocaleTimeString();
  log.prepend(el("li", { class: "k-" + ev.kind }, el("time", {}, t), `[${ev.kind}] ${ev.text}`));
  while (log.children.length > 300) log.lastChild.remove();
  if (ev.kind === "cmd" && /err_busy/.test(ev.text)) state.lastBusyAt = Date.now();
}

// ---------------------------------------------------------------- files
const files = { items: [], total: 0, kind: "", selecting: false, sel: new Set(), loading: false, available: true, job: null };
const PAGE = 60;

function openFiles() {
  $("files").hidden = false;
  $("files").classList.remove("leaving");
  haptic();
  files.items = []; files.total = 0; files.sel.clear();
  renderFiles();
  loadFiles(0);
}
async function closeFiles() {
  const f = $("files");
  if (f.hidden) return;
  f.classList.add("leaving");
  setTimeout(() => { f.hidden = true; f.classList.remove("leaving"); }, 340);
  setSelecting(false);
  if (files.available) { try { await api("/api/files/done", {}); } catch (_) { /* not there yet */ } }
}

async function loadFiles(offset) {
  if (files.loading) return;
  files.loading = true;
  $("filesempty").hidden = true;
  if (!offset) { $("grid").replaceChildren(...Array.from({ length: 12 }, () => el("div", { class: "tilef" }))); }
  try {
    const q = new URLSearchParams({ offset, limit: PAGE });
    if (files.kind) q.set("kind", files.kind);
    const r = await api("/api/files?" + q);
    files.available = true;
    files.total = r.total || 0;
    files.items = offset ? files.items.concat(r.items || []) : (r.items || []);
  } catch (e) {
    files.items = [];
    if (e.status === 404 || e.status === 405) {
      files.available = false;
      showFilesEmpty("Files not available yet", "This server doesn't have the files feature yet. Update it and try again.");
    } else showFilesEmpty("Couldn't load files", e.message);
    files.loading = false;
    $("grid").replaceChildren();
    return;
  }
  files.loading = false;
  renderFiles();
  if (!files.items.length) showFilesEmpty("No photos or videos", "Nothing on the card yet" + (files.kind ? ` for ${files.kind}s` : "") + ".");
}
function showFilesEmpty(title, sub) {
  const e = $("filesempty");
  e.replaceChildren(el("b", {}, title), sub);
  e.hidden = false;
  $("filescount").textContent = "";
}

function renderFiles() {
  const grid = $("grid");
  grid.replaceChildren();
  for (const it of files.items) {
    const img = el("img", { alt: it.name, loading: "lazy", decoding: "async", src: it.thumb_url || "" });
    img.addEventListener("load", () => img.classList.add("loaded"));
    const tile = el("button", { class: "tilef" + (files.sel.has(it.id) ? " sel" : ""), "data-id": it.id, title: it.name },
      img,
      it.kind === "video" ? el("span", { class: "badge" }, icon("play"), "VIDEO") : null,
      it.has_raw ? el("span", { class: "raw" }, "RAW") : null,
      el("span", { class: "selmark" }, icon("check")));
    tile.addEventListener("click", () => tapFile(it, tile));
    tile.addEventListener("contextmenu", (e) => { e.preventDefault(); if (!files.selecting) setSelecting(true); tapFile(it, tile); });
    grid.append(tile);
  }
  $("filescount").textContent = files.total ? `${files.total}` : "";
  $("filesmore").hidden = !(files.items.length < files.total);
  updateSelBar();
}

function tapFile(it, tile) {
  if (files.selecting) {
    haptic(5);
    if (files.sel.has(it.id)) files.sel.delete(it.id); else files.sel.add(it.id);
    tile.classList.toggle("sel", files.sel.has(it.id));
    updateSelBar();
  } else openViewer(it);
}
function setSelecting(on) {
  files.selecting = on;
  if (!on) { files.sel.clear(); for (const t of document.querySelectorAll(".tilef.sel")) t.classList.remove("sel"); }
  $("files").classList.toggle("selecting", on);
  $("filesselect").textContent = on ? "Cancel" : "Select";
  updateSelBar();
}
function updateSelBar() {
  const n = files.sel.size;
  $("filesbar").hidden = !files.selecting;
  $("selcount").textContent = n ? `${n} selected` : "Select items";
  $("btnimport").disabled = !n; $("btndl").disabled = !n;
}

function openViewer(it) {
  haptic();
  $("vname").textContent = it.name;
  const meta = [it.date ? new Date(it.date).toLocaleString() : null, fmtSize(it.size), it.has_raw ? "+ RAW" : null].filter(Boolean);
  $("vmeta").textContent = meta.join(" · ");
  const dl = $("vdownload");
  dl.href = it.download_url || "#";
  dl.setAttribute("download", it.name || "");
  const body = $("vbody");
  body.replaceChildren(el("div", { class: "spinner" }));
  const img = el("img", { alt: it.name, src: it.preview_url || it.thumb_url });
  img.addEventListener("load", () => body.replaceChildren(img, it.kind === "video" ? el("div", { class: "viewer-note glass" }, "Video preview frame. Download or import to play it.") : ""));
  img.addEventListener("error", () => body.replaceChildren(el("p", { class: "page-sub" }, "Preview not available.")));
  $("viewer").hidden = false;
}
function closeViewer() { $("viewer").hidden = true; $("vbody").replaceChildren(); }

function downloadSelected() {
  const items = files.items.filter((x) => files.sel.has(x.id) && x.download_url);
  items.forEach((it, i) => setTimeout(() => {
    const a = el("a", { href: it.download_url, download: it.name || "" });
    document.body.append(a); a.click(); a.remove();
  }, i * 400));
  toast(`Downloading ${items.length} file${items.length === 1 ? "" : "s"}`);
}

function askImport() {
  const n = files.sel.size;
  if (!n) return;
  $("importtext").textContent = `Copy ${n} item${n === 1 ? "" : "s"}${$("incraw").checked ? " (with RAW files)" : ""} from the camera to this Mac.`;
  $("importdest").value = LS.get("importdest", "");
  $("importdlg").showModal();
}
async function startImport() {
  const ids = [...files.sel];
  const dest = $("importdest").value.trim();
  LS.set("importdest", dest);
  const body = { ids, include_raw: $("incraw").checked };
  if (dest) body.dest = dest;
  try {
    const r = await api("/api/files/import", body);
    const job = r.job || r;
    const id = job.id || job.job_id;
    if (!id) throw new Error("server didn't return a job id");
    setSelecting(false);
    pollJob(id, ids.length);
  } catch (e) {
    toast(e.status === 404 ? "Import isn't available on this server yet." : e.message, true);
  }
}
async function pollJob(id, n) {
  const box = $("job");
  box.hidden = false;
  $("jobtitle").textContent = `Importing ${n} item${n === 1 ? "" : "s"}…`;
  $("jobsub").textContent = "";
  $("jobbar").style.width = "2%";
  for (;;) {
    let j;
    try { j = await api(`/api/jobs/${encodeURIComponent(id)}`); } catch (e) { $("jobsub").textContent = e.message; await sleep(1500); continue; }
    j = j.job || j;
    const total = j.files_total || j.total || n;
    const done = j.files_done !== undefined ? j.files_done : (j.done !== undefined ? j.done : null);
    let p = typeof j.progress === "number" ? j.progress : (done !== null && total ? done / total : 0);
    if (p > 1) p /= 100;
    $("jobbar").style.width = Math.max(2, Math.round(p * 100)) + "%";
    const bytes = j.bytes_done ? ` · ${fmtSize(j.bytes_done)}${j.bytes_total ? " of " + fmtSize(j.bytes_total) : ""}` : "";
    const skipped = j.files_skipped ? ` · ${j.files_skipped} already there` : "";
    $("jobsub").textContent = (done !== null ? `${done} / ${total}` : `${Math.round(p * 100)}%`) + bytes + skipped;
    if (j.current) $("jobtitle").textContent = `Importing ${j.current}`;
    const st = String(j.state || j.status || "").toLowerCase();
    if (st === "done" || st === "done_with_errors") {
      const failed = j.files_failed || 0;
      $("jobtitle").textContent = failed ? `Imported with ${failed} error${failed === 1 ? "" : "s"}` : "Imported";
      $("jobbar").style.width = "100%";
      toast(`Imported ${done !== null ? done : n} of ${total}${j.dest ? " to " + j.dest : ""}${failed ? `, ${failed} failed` : ""}`, failed > 0);
      haptic([8, 30, 8]);
      break;
    }
    if (["error", "failed", "cancelled", "canceled"].includes(st)) {
      $("jobtitle").textContent = st.startsWith("cancel") ? "Import cancelled" : "Import failed";
      const err = Array.isArray(j.errors) && j.errors.length ? (j.errors[0].error || j.errors[0]) : j.error;
      toast("Import " + st + (err ? ": " + err : ""), true);
      break;
    }
    await sleep(700);
  }
  setTimeout(() => { box.hidden = true; }, 2500);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function wireFiles() {
  $("btnfiles").addEventListener("click", openFiles);
  $("filesclose").addEventListener("click", closeFiles);
  $("filesselect").addEventListener("click", () => { haptic(); setSelecting(!files.selecting); });
  $("filesmore").addEventListener("click", () => loadFiles(files.items.length));
  $("fkind").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    files.kind = b.dataset.v;
    [...$("fkind").children].forEach((x) => x.classList.toggle("on", x === b));
    haptic(); loadFiles(0);
  });
  $("btnimport").addEventListener("click", askImport);
  $("btndl").addEventListener("click", downloadSelected);
  $("importdlg").addEventListener("close", () => { if ($("importdlg").returnValue === "ok") startImport(); });
  $("viewerclose").addEventListener("click", closeViewer);
  // infinite scroll
  $("files").addEventListener("scroll", () => {
    const f = $("files");
    if (!files.loading && files.items.length < files.total && f.scrollTop + f.clientHeight > f.scrollHeight - 400) loadFiles(files.items.length);
  });
}

// ---------------------------------------------------------------- websocket
function connectWs() {
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/api/ws");
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.type === "status") renderStatus(msg.data);
    else if (msg.type === "exposure") { state.exposure = msg.data; renderQC(); }
    else if (msg.type === "settings") loadSettings();
    else if (msg.type === "preset") { state.auto = msg.data; renderAuto(); }
    else if (msg.type === "event") addLog(msg.data);
    else if (msg.type === "events") { $("log").replaceChildren(); for (const ev of msg.data) addLog(ev); }
  };
  ws.onclose = () => {
    renderStatus({ status: "offline", message: "server unreachable", liveview: {} });
    setTimeout(connectWs, 2000);
  };
}

// ---------------------------------------------------------------- wiring
function init() {
  $("btnshutter").addEventListener("click", shutterPressed);
  $("btnaf").addEventListener("click", () => { haptic(); cameraCall("/api/af", {}); });
  $("btnauto").addEventListener("click", () => setAuto(!state.auto.on));
  $("btnconn").addEventListener("click", () => (state.status && state.status.status === "ready" ? openSheet("conn") : ($("connect").hidden = false, state.skipConnect = false)));
  $("bannerbtn").addEventListener("click", () => reconnect());
  $("btnreconnect").addEventListener("click", () => reconnect());
  $("btnskip").addEventListener("click", () => { state.skipConnect = true; $("connect").hidden = true; if (state.status) renderStatus(state.status); });
  $("btnforget").addEventListener("click", () => { LS.set("camera", null); reconnect(null, true); });
  $("ipform").addEventListener("submit", (e) => { e.preventDefault(); const ip = $("ipinput").value.trim(); if (ip) reconnect(ip); });
  for (const b of $("modestrack").children) b.addEventListener("click", (e) => e.preventDefault());
  wireGestures();
  wireSheet();
  wireFiles();
  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, select, textarea") || e.metaKey || e.ctrlKey || e.altKey) return;
    if (document.querySelector("dialog[open]")) return;
    const k = e.key;
    if (k === "Escape") {
      if (!$("viewer").hidden) closeViewer();
      else if (!$("files").hidden) { if (files.selecting) setSelecting(false); else closeFiles(); }
      else if (sheetOpen()) { if (navStack.length > 1) pop(); else closeSheet(); }
      return;
    }
    if (!$("files").hidden || !$("viewer").hidden) return;
    if (e.code === "Space") { e.preventDefault(); shoot(); }
    else if (k === "r" || k === "R") toggleRec();
    else if (k === "p" || k === "P") (sheetOpen() ? closeSheet() : openSheet());
    else if (k === "a" || k === "A") setAuto(!state.auto.on);
    else if (k === "f" || k === "F") { haptic(); cameraCall("/api/af", {}); }
    else if (k === "g" || k === "G") openFiles();
    else if (k === "1" || k === "2" || k === "3") setMode(MODES[+k - 1]);
    else if (k === "ArrowLeft") stepMode(-1);
    else if (k === "ArrowRight") stepMode(1);
  });
  setMode(state.mode, true);
  renderChecklist(DEFAULT_STEPS);
  renderConnect({ status: "starting" });
  keepLiveView();
  connectWs();
  loadSettings();
  loadConnection();
  api("/api/presets").then((r) => { state.auto = r.auto || { on: false }; renderAuto(); }).catch(() => {});
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(positionModes);
}
init();
