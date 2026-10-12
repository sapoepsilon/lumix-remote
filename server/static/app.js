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
  camId: LS.get("camId", null), cameras: [], models: [],
};
const MODES = ["photo", "video", "timelapse"];

// ---------------------------------------------------------------- helpers
class HttpError extends Error { constructor(msg, status) { super(msg); this.status = status; } }

// Camera routes go to /api/cameras/<id>/... for the camera this tab shows;
// /api/cameras and /api/models are global.
const GLOBAL_API = /^\/api\/(cameras|models)(\/|\?|$)/;
function cp(path) {
  if (!state.camId || !path || !path.startsWith("/api/") || GLOBAL_API.test(path)) return path;
  return "/api/cameras/" + encodeURIComponent(state.camId) + "/" + path.slice(5);
}

async function api(path, body, method) {
  const opt = body === undefined && !method ? {} : {
    method: method || "POST", headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  };
  const r = await fetch(cp(path), opt);
  let data = null;
  try { data = await r.json(); } catch (_) { /* non-JSON */ }
  if (!r.ok) throw new HttpError(friendlyHttp(r.status, data && data.error), r.status);
  return data;
}

function friendlyHttp(code, msg) {
  if (code === 409) {
    if (!msg || /^camera busy \(/.test(msg)) return "Camera is busy with another command. Try again in a moment.";
    if (/not connected/.test(msg)) return "Camera isn't connected yet.";
    // the server's own explanation (wedged, operated on the body, interval run, Auto refused, ...)
    return msg.charAt(0).toUpperCase() + msg.slice(1) + (/[.!?]$/.test(msg) ? "" : ".");
  }
  if (code === 502) return "Couldn't reach the camera" + (msg ? ` (${msg.split(":")[0]})` : ".");
  if (code === 404 && msg && /^no camera (set up|')/.test(msg)) return msg.startsWith("no camera set up") ? "No camera set up yet. Add one first." : "That camera isn't registered any more.";
  return msg || "Request failed (" + code + ")";
}

const RESULT_TEXT = {
  err_busy: "Camera is busy. Close any menu or playback on the camera and try again.",
  err_reject: "The camera didn't allow that in its current mode.",
  err_param: "The camera rejected that value.",
  err_critical: "The camera couldn't do that right now.",
  err_nonsupport: "This camera doesn't support that remotely.",
  err_local_recmode_guard: "Held back a mode switch to protect the camera (too many in 20 s). Try again in a few seconds.",
};
const WEDGED_TEXT = "The camera is stuck (it answers \"busy\" to every command). Switch it off and on again.";
function explain(result) {
  if (!result || result === "ok") return null;
  if (result === "err_busy" && state.status && state.status.wedged) return WEDGED_TEXT;
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
    case "reconnecting":
      if ((s && s.powered_off) || msg.includes("powered off")) {
        return { title: "Camera is off", cls: "warn", dot: "bad", msg: blePaired()
          ? "Tap Turn on over Bluetooth, or switch it on by hand. It reconnects by itself."
          : "Switch it on and it reconnects by itself (Wi-Fi can't wake it; pair Bluetooth in Cameras › Run setup again… to turn it on from here)." };
      }
      return { title: "Reconnecting…", msg: "The camera dropped off (asleep, off or out of range). Retrying automatically." + (msg ? ` (${msg})` : ""), cls: "warn", dot: "wait" };
    case "error": return { title: "Something's wrong", msg, cls: "warn", dot: "bad" };
    case "offline": return { title: "Server not reachable", msg: "Start it with ./run.sh on your Mac.", cls: "warn", dot: "bad" };
    case "no_camera": return { title: "No camera yet", msg: "Add a camera to get started.", cls: "", dot: "" };
    case "paused": return { title: "Not connecting", msg: "This camera is switched off in Cameras (Connect automatically).", cls: "", dot: "" };
    default: return { title: "Starting…", msg, cls: "", dot: "wait" };
  }
}

function renderStatus(s) {
  const prev = state.status;
  state.status = s;
  const info = statusInfo(s);
  const ready = s.status === "ready";
  $("dot").className = "dot " + (ready && s.wedged ? "bad" : ready && s.body_busy ? "wait" : info.dot);
  const cam = s.camera || {};
  const me = currentCamera();
  const nick = me && me.nickname && !me.provisional ? me.nickname : null;
  $("camname").textContent = ready ? (nick || cam.modelNumber || cam.friendlyName || "Camera")
    : (state.cameras.length > 1 && nick ? `${nick} · ${info.title.replace("…", "")}` : info.title.replace("…", ""));
  if (s.status === "no_camera" && !wz.open && !state.skipConnect) openWizard({});
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
  $("lvemptytext").textContent = !ready ? info.title : s.wedged ? "Live view stopped: the camera is stuck"
    : s.body_busy ? "Live view paused while the camera is in use" : s.cammode === "play" ? "Camera is in playback" : "Waiting for live view…";
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
  $("bannerbtn").hidden = false;
  if (!ready && (state.everReady || state.skipConnect)) {
    banner.hidden = false; $("bannerdot").className = "dot " + info.dot; $("bannertext").textContent = info.title;
  } else if (ready && s.wedged) {
    // only a power cycle clears it; reconnecting would not help
    banner.hidden = false; $("bannerdot").className = "dot bad"; $("bannerbtn").hidden = true;
    $("bannertext").textContent = "Camera stuck: switch it off and on again";
  } else if (ready && s.body_busy) {
    banner.hidden = false; $("bannerdot").className = "dot wait"; $("bannerbtn").hidden = true;
    $("bannertext").textContent = "Someone is using the camera";
  } else if (busy) {
    banner.hidden = false; $("bannerdot").className = "dot wait"; $("bannertext").textContent = "Camera busy: check its screen";
  } else banner.hidden = true;
  $("bannerwake").hidden = banner.hidden || !canWake(s) || !(blePaired() || wake.running);
  $("connsub").textContent = ready ? `${cam.friendlyName || ""} · ${s.ip || ""}` : info.title;
  renderConnect(s);
  if (!$("prosheet").hidden && currentPage() === "conn") renderConnInfo();
  if (me && me.status !== s.status && s.status !== "offline") { me.status = s.status; if (currentPage() === "cams") renderCameras(); }
  if (ready && (!prev || prev.status !== "ready")) loadSettings();
  renderWake();
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
  const none = s.status === "no_camera";
  $("btnreconnect").hidden = none;
  $("btnwizard").textContent = none ? "Add a camera" : "Set up step by step";
  $("btnwizard").classList.toggle("primary", none);
  for (const d of c.querySelectorAll(".cs-more")) d.hidden = none;
  $("cswake").hidden = !canWake(s) && !wake.running;
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
    if (state.conn.model && state.conn.model.menu_assist && LS.get("mftap", null) === null) {
      state.mftap = state.conn.model.menu_assist.ui_default_mf_tap || "off";
    }
    renderChecklist(state.conn.steps && state.conn.steps.length ? state.conn.steps : DEFAULT_STEPS);
  } catch (_) { renderChecklist(DEFAULT_STEPS); }
}

function renderConnInfo() {
  const s = state.status || {};
  const cam = s.camera || {};
  const me = currentCamera();
  const rows = [
    ["Status", statusInfo(s).title], ["Camera", [cam.modelNumber, cam.friendlyName].filter(Boolean).join(" ") || "–"],
    ["Profile", s.model_name || "–"], ["Name", me ? me.nickname : "–"],
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
  else if (state.mode === "timelapse") tlShutter();
}

// ---------------------------------------------------------------- modes
function setMode(m, quiet) {
  if (!MODES.includes(m)) return;
  const changed = m !== state.mode;
  state.mode = m;
  LS.set("mode", m);
  $("app").dataset.mode = m;
  $("tlpanel").hidden = m !== "timelapse";
  const btns = [...$("modestrack").children];
  btns.forEach((b) => b.classList.toggle("on", b.dataset.mode === m));
  positionModes();
  $("btnshutter").title = m === "video" ? "Start / stop video (R)" : m === "photo" ? "Take photo (Space)" : "Start / stop timelapse (Space)";
  if (m === "timelapse") loadTimelapse();
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
  const start = () => { img.src = cp("/api/liveview.mjpg") + "?t=" + Date.now(); };
  if (document.readyState === "complete") start(); else window.addEventListener("load", start);
  const noSource = () => !state.status || ["no_camera", "offline"].includes(state.status.status);
  img.addEventListener("error", () => setTimeout(function retry() {
    if (noSource()) { setTimeout(retry, 3000); return; }   // nothing to stream: don't hammer the server
    img.src = cp("/api/liveview.mjpg") + "?t=" + Date.now();
  }, 2000));
  setInterval(() => {
    const lv = state.status && state.status.liveview;
    if (lv && lv.viewers === 0 && lv.age !== null && lv.age < 2) img.src = cp("/api/liveview.mjpg") + "?t=" + Date.now();
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
  s.inert = false;
  s.hidden = false;
  requestAnimationFrame(() => s.classList.add("open"));
  if (page && currentPage() !== page) push(page);
  haptic();
}
function closeSheet() {
  const s = $("prosheet");
  s.inert = true;            // off-screen: keep its controls out of the Tab order
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
  $("btnpower").addEventListener("click", openPowerSheet);
  $("btnwake").addEventListener("click", bleWake);
  $("powerdlg").addEventListener("close", async () => {
    if ($("powerdlg").returnValue !== "ok") return;
    try {
      const r = await api("/api/power/off", { confirm: true });
      const q = r.quiesce || {};
      const paused = (q.paused || []).map((x) => x.label);
      toast(r.result === "ok" ? "Camera powering off" + (paused.length ? ` · paused ${paused.join(", ")}` : "") : "Power off: " + r.result, r.result !== "ok");
    } catch (e) { toast(e.message, true); }
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
  loadFiles(0).then(watchRunningImport);
}
// An import paused when you left Files continues on the next visit: show its progress.
async function watchRunningImport() {
  if (files.job) return;
  try {
    const jobs = await api("/api/jobs");
    const j = (jobs || []).find((x) => ["queued", "running"].includes(x.state));
    if (j) pollJob(j.id, j.files_total || (j.ids || []).length);
  } catch (_) { /* older server */ }
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
    if (e.status === 404 && /^No camera|isn't registered/.test(e.message)) {
      showFilesEmpty("No camera yet", "Add a camera to browse its card.");
    } else if (e.status === 404 || e.status === 405) {
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
    const img = el("img", { alt: it.name, loading: "lazy", decoding: "async", src: it.thumb_url ? cp(it.thumb_url) : "" });
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
  dl.href = it.download_url ? cp(it.download_url) : "#";
  dl.setAttribute("download", it.name || "");
  const body = $("vbody");
  body.replaceChildren(el("div", { class: "spinner" }));
  const img = el("img", { alt: it.name, src: cp(it.preview_url || it.thumb_url) });
  img.addEventListener("load", () => body.replaceChildren(img, it.kind === "video" ? el("div", { class: "viewer-note glass" }, "Video preview frame. Download or import to play it.") : ""));
  img.addEventListener("error", () => body.replaceChildren(el("p", { class: "page-sub" }, "Preview not available.")));
  $("viewer").hidden = false;
}
function closeViewer() { $("viewer").hidden = true; $("vbody").replaceChildren(); }

function downloadSelected() {
  const items = files.items.filter((x) => files.sel.has(x.id) && x.download_url);
  items.forEach((it, i) => setTimeout(() => {
    const a = el("a", { href: cp(it.download_url), download: it.name || "" });
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
  api("/api/files/status").then((st) => {
    if (st && st.default_dest) $("importdest").placeholder = st.default_dest.replace(/^\/Users\/[^/]+/, "~") + " (default)";
  }).catch(() => {});
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
  if (files.job === id) return;
  files.job = id;
  try { await pollJobLoop(id, n); } finally { if (files.job === id) files.job = null; }
}
async function pollJobLoop(id, n) {
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
    $("jobsub").textContent = (done !== null ? `${done + (j.files_skipped || 0)} / ${total}` : `${Math.round(p * 100)}%`) + bytes + skipped;
    if (j.current) $("jobtitle").textContent = `Importing ${j.current}`;
    const st = String(j.state || j.status || "").toLowerCase();
    if (st === "paused") {
      $("jobtitle").textContent = "Import paused";
      $("jobsub").textContent = `${done !== null ? done : 0} / ${total} saved · continues next time you open Files`;
      break;
    }
    if (st === "done" || st === "done_with_errors") {
      const failed = j.files_failed || 0;
      $("jobtitle").textContent = failed ? `Imported with ${failed} error${failed === 1 ? "" : "s"}` : "Imported";
      $("jobbar").style.width = "100%";
      const got = done !== null ? done : n, skip = j.files_skipped || 0;
      const where = j.dest ? " in " + j.dest.replace(/^\/Users\/[^/]+/, "~") : "";
      if (!got && skip && !failed) {
        $("jobtitle").textContent = "Already imported";
        toast(`Already on this Mac: ${skip} file${skip === 1 ? "" : "s"}${where}`);
      } else {
        toast(`Imported ${got} file${got === 1 ? "" : "s"}${where}${skip ? ` (${skip} already there)` : ""}${failed ? `, ${failed} failed` : ""}`, failed > 0);
      }
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


// ---------------------------------------------------------------- cameras
const enc = encodeURIComponent;
function currentCamera() { return state.cameras.find((c) => c.id === state.camId) || null; }

async function loadCameras() {
  try {
    const r = await api("/api/cameras");
    state.models = r.models || [];
    setCameras(r);
  } catch (_) { /* older server without /api/cameras: stay on the alias routes */ }
}

function setCameras(d) {
  state.cameras = d.cameras || [];
  const ids = state.cameras.map((c) => c.id);
  if (!state.camId || !ids.includes(state.camId)) {
    const next = d.active && ids.includes(d.active) ? d.active : ids[0] || null;
    if (next !== state.camId) {
      const had = state.camId !== null;
      state.camId = next; LS.set("camId", next);
      if (had) resetForCamera();
    }
  }
  renderCameras();
}

function dotFor(st) {
  if (st === "ready") return "ready";
  if (st === "error") return "bad";
  if (st === "paused" || st === "no_camera") return "";
  return "wait";
}

function camModelText(c) {
  const cam = c.camera || {};
  return cam.modelNumber ? (c.profile && c.profile.id !== "generic" ? c.profile.name : cam.modelNumber)
    : c.model && c.model !== "auto" ? (c.profile ? c.profile.name : c.model) : "Model detected on connect";
}

function renderCameras() {
  const cams = state.cameras;
  const me = currentCamera();
  $("connchev").toggleAttribute("hidden", cams.length < 2);   // SVG: no .hidden property
  $("camssub").textContent = !cams.length ? "Add a camera" : cams.length === 1 ? `${me ? me.nickname : ""} · add another camera`
    : `${me ? me.nickname : "–"} · ${cams.length} cameras`;
  const box = $("camlist");
  box.replaceChildren();
  for (const c of cams) {
    const info = statusInfo({ status: c.status, message: c.message });
    const row = el("div", { class: "cell nav-cell cam-row", role: "button", tabindex: "0",
      onclick: (e) => { if (!e.target.closest(".info")) switchCamera(c.id, true); },
      onkeydown: (e) => { if (e.key === "Enter") switchCamera(c.id, true); } },
    el("span", { class: "dot " + dotFor(c.status) }),
    el("div", { class: "cell-text" }, el("b", {}, c.nickname || c.id),
      el("small", {}, [camModelText(c), c.status === "ready" ? c.ip : info.title.replace("…", "")].filter(Boolean).join(" · "))),
    c.id === state.camId ? icon("check", "tick") : null,
    el("button", { class: "info", "aria-label": `${c.nickname} details`, onclick: () => openCamDetail(c.id) }, icon("info")));
    box.append(row);
  }
  if (!cams.length) box.append(el("div", { class: "cell" }, el("div", { class: "cell-text" }, el("b", {}, "No cameras yet"), el("small", {}, "Add one to get started."))));
  if (camDetailId && currentPage() === "cam") renderCamDetail();
  renderWake();
}

function resetForCamera() {
  state.settings = []; state.exposure = null; state.auto = { on: false }; state.status = null;
  state.everReady = false; state.picker = null; state.conn = null;
  $("log").replaceChildren();
  renderQC(); renderAll(); renderAuto();
  const img = $("lvimg");
  img.removeAttribute("src");
  img.src = cp("/api/liveview.mjpg") + "?t=" + Date.now();
  connectWs();
  loadSettings(); loadConnection(); loadPresets(); tl.status = null; loadTimelapse();
}

async function switchCamera(id, fromList) {
  if (!id) return;
  haptic();
  const changed = id !== state.camId;
  state.camId = id; LS.set("camId", id);
  try { await api(`/api/cameras/${enc(id)}/activate`, {}); } catch (_) { /* still show it */ }
  if (changed) {
    resetForCamera();
    const c = currentCamera();
    toast(`Now using ${c ? c.nickname : id}`);
  }
  renderCameras();
  if (fromList && changed) setTimeout(closeSheet, 250);
}

let camDetailId = null;
async function openCamDetail(id) {
  camDetailId = id;
  push("cam");
  renderCamDetail();
  try {
    const d = await api(`/api/cameras/${enc(id)}`);
    camDetail = d;
    renderCamDetail();
  } catch (e) { toast(e.message, true); }
}
let camDetail = null;
function renderCamDetail() {
  const c = state.cameras.find((x) => x.id === camDetailId);
  if (!c) return;
  const d = camDetail && camDetail.id === c.id ? camDetail : null;
  $("camtitle").textContent = c.nickname || c.id;
  if (document.activeElement !== $("camnick")) $("camnick").value = c.nickname || "";
  const sel = $("cammodel");
  sel.replaceChildren(el("option", { value: "auto" }, "Detect automatically"),
    ...state.models.map((m) => el("option", { value: m.id }, m.name + (m.verified ? "" : " (untested)"))));
  sel.value = c.model || "auto";
  const cam = c.camera || {};
  const ports = c.ports || {};
  const rows = [
    ["Status", statusInfo({ status: c.status }).title], ["Detected", [cam.modelNumber, cam.X_FirmVersion ? "fw " + cam.X_FirmVersion : null].filter(Boolean).join(" · ") || "not yet"],
    ["Profile", c.profile ? c.profile.name + (c.profile.verified ? "" : " (untested)") : "–"],
    ["Address", c.ip || "found automatically"], ["Bluetooth", c.ble && c.ble.registered ? "paired" + (c.ble.name ? " · " + c.ble.name : "") : "not paired"],
    ["Live view port", ports.liveview || "–"], ["ID (CLI)", c.id],
  ];
  if (d && d.entry && (ports.http !== 80 || ports.upnp !== 60606)) rows.push(["Camera ports", `${ports.http} / ${ports.upnp} / ${ports.files}`]);
  $("caminfo").replaceChildren(...rows.map(([k, v]) => el("div", { class: "cell" }, el("div", { class: "cell-text" }, el("b", {}, k)), el("span", { class: "val" }, String(v)))));
  $("camauto").checked = !(c.settings && c.settings.autoconnect === false);
  $("camwd").checked = !!(c.settings && c.settings.watchdog);
  $("camuse").hidden = c.id === state.camId;
}

async function patchCamera(id, body, okMsg) {
  try {
    const r = await api(`/api/cameras/${enc(id)}`, body, "PATCH");
    await loadCameras();
    if (okMsg) toast(okMsg);
    return r;
  } catch (e) { toast(e.message, true); await loadCameras(); return null; }
}

function wireCameras() {
  const add = () => { closeSheet(); openWizard({}); };
  $("btnaddcam").addEventListener("click", add);
  $("btnaddcam2").addEventListener("click", add);
  const saveNick = () => {
    const v = $("camnick").value.trim();
    const c = state.cameras.find((x) => x.id === camDetailId);
    if (c && v && v !== c.nickname) patchCamera(c.id, { nickname: v }, "Renamed");
  };
  $("camnick").addEventListener("change", saveNick);
  $("camnick").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("camnick").blur(); } });
  $("cammodel").addEventListener("change", (e) => patchCamera(camDetailId, { model: e.target.value }, "Model saved; reconnecting"));
  $("camauto").addEventListener("change", (e) => patchCamera(camDetailId, { settings: { autoconnect: e.target.checked } }));
  $("camwd").addEventListener("change", (e) => patchCamera(camDetailId, { settings: { watchdog: e.target.checked } }));
  $("camuse").addEventListener("click", () => switchCamera(camDetailId, true));
  $("camsetup").addEventListener("click", () => { const id = camDetailId; closeSheet(); openWizard({ camId: id, reuse: true, step: "wifi" }); });
  $("camremove").addEventListener("click", () => {
    const c = state.cameras.find((x) => x.id === camDetailId);
    $("removetitle").textContent = `Remove ${c ? c.nickname : "camera"}?`;
    $("removedlg").showModal();
  });
  $("removedlg").addEventListener("close", async () => {
    if ($("removedlg").returnValue !== "ok") return;
    const id = camDetailId;
    try {
      await api(`/api/cameras/${enc(id)}`, undefined, "DELETE");
      toast("Camera removed");
      pop();
      if (id === state.camId) state.camId = null;
      await loadCameras();
      if (!state.camId) resetForCamera();
    } catch (e) { toast(e.message, true); }
  });
}

// ---------------------------------------------------------------- add-camera wizard
const WZ = ["model", "wifi", "ble", "name", "done"];
const wz = { open: false, step: "model", camId: null, model: "auto", created: false, poll: null, profiles: {}, ble: "idle", bleMsg: "", status: null, reuse: false };

async function profileFor(id) {
  if (!wz.profiles[id]) {
    try { wz.profiles[id] = await api(`/api/models/${enc(id)}`); } catch (_) { return null; }
  }
  return wz.profiles[id];
}
// Steps to show: the chosen model's, or the detected one's, or the S5's (verified) for auto.
async function wzProfile() {
  const c = state.cameras.find((x) => x.id === wz.camId);
  const detected = c && c.camera && c.camera.modelNumber && c.profile ? c.profile.id : null;
  const pick = wz.model !== "auto" ? wz.model : detected && detected !== "generic" ? detected : "dc_s5";
  return profileFor(pick);
}

async function openWizard(opts) {
  if (wz.open && !opts.step) return;
  Object.assign(wz, { open: true, camId: opts.camId || null, model: "auto", created: false, ble: "idle", bleMsg: "", status: null, reuse: !!opts.reuse });
  if (!state.models.length) await loadCameras();
  const c = state.cameras.find((x) => x.id === wz.camId);
  if (c) wz.model = c.model || "auto";
  $("wizard").hidden = false;
  $("wizard").classList.remove("leaving");
  renderWzModels();
  await wzGo(WZ.includes(opts.step) ? opts.step : "model");
}

async function closeWizard(cancelled) {
  stopWzPoll();
  const created = wz.created && wz.camId;
  wz.open = false;
  const w = $("wizard");
  w.classList.add("leaving");
  setTimeout(() => { w.hidden = true; w.classList.remove("leaving"); }, 380);
  if (location.hash.startsWith("#wizard")) history.replaceState(null, "", location.pathname + location.search);
  if (cancelled && created) {
    // a camera added by this wizard that never connected: don't leave it behind
    const c = state.cameras.find((x) => x.id === wz.camId);
    if (c && c.status !== "ready" && !c.udn_bound) {
      try { await api(`/api/cameras/${enc(wz.camId)}`, undefined, "DELETE"); } catch (_) { /* keep it */ }
      await loadCameras();
    }
  }
}

async function wzGo(step, back) {
  const prev = wz.step;
  wz.step = step;
  for (const sec of document.querySelectorAll(".wz-step")) {
    const on = sec.dataset.step === step;
    sec.hidden = !on;
    sec.classList.toggle("back", !!back);
    if (on) { sec.style.animation = "none"; void sec.offsetWidth; sec.style.animation = ""; }
  }
  const i = WZ.indexOf(step);
  [...$("wzdots").children].forEach((d, k) => { d.className = k === i ? "on" : k < i ? "done" : ""; });
  $("wzback").hidden = i === 0 || step === "done";
  $("wzcancel").textContent = step === "done" ? "Close" : "Cancel";
  $("wizard").scrollTop = 0;
  if (step === "wifi") { await renderWzWifi(); startWzPoll(); } else stopWzPoll();
  if (step === "ble") await renderWzBle();
  if (step === "name") renderWzName();
  if (step === "done") renderWzDone();
  if (prev !== step) haptic();
}

function renderWzModels() {
  const box = $("wzmodels");
  const opts = [{ id: "auto", name: "Detect automatically", sub: "Recommended. Recognised from the camera when it connects." },
    ...state.models.map((m) => ({ id: m.id, name: m.name, sub: m.model_numbers && m.model_numbers.length ? m.model_numbers.join(", ") : "Safe defaults for any LUMIX Sync body", verified: m.verified }))];
  box.replaceChildren(...opts.map((o) => el("button", {
    class: "cell nav-cell", role: "radio", "aria-checked": wz.model === o.id ? "true" : "false",
    onclick: () => { wz.model = o.id; haptic(); renderWzModels(); },
  }, el("div", { class: "cell-text" }, el("b", {}, o.name, o.verified ? el("span", { class: "badge" }, "tested") : null), el("small", {}, o.sub)), icon("check", "tick"))));
}

async function wzEnsureCamera() {
  // Zero-friction path: the camera the server made on first run (provisional, never
  // connected) is the one we set up, instead of adding a second entry.
  if (!wz.camId && !wz.reuse) {
    const prov = state.cameras.find((c) => c.provisional && !c.udn_bound && c.status !== "ready");
    if (prov) wz.camId = prov.id;
  }
  if (wz.camId) {
    const c = state.cameras.find((x) => x.id === wz.camId);
    if (c && (c.model || "auto") !== wz.model) await patchCamera(wz.camId, { model: wz.model });
    return true;
  }
  try {
    const n = state.cameras.length + 1;
    const r = await api("/api/cameras", { nickname: `Camera ${n}`, model: wz.model });
    wz.camId = r.id; wz.created = true;
    await loadCameras();
    return true;
  } catch (e) { toast(e.message, true); return false; }
}

function stepItem(i, s) {
  return el("li", {}, el("button", {
    onclick: (e) => { haptic(); e.currentTarget.parentElement.classList.toggle("done"); },
  }, el("span", { class: "box" }, el("i", {}, String(i + 1)), icon("check")),
  el("span", { class: "t" }, el("b", {}, s.title), s.detail ? el("small", {}, s.detail) : null)));
}

async function renderWzWifi() {
  const p = await wzProfile();
  const steps = (p && p.setup_wifi) || [];
  $("wzwifisub").textContent = p && p.id !== "generic"
    ? `On your ${p.name}, put it on your home Wi-Fi in remote-shooting mode:`
    : "On the camera, put it on your home Wi-Fi in remote-shooting mode:";
  $("wzwifisteps").replaceChildren(...steps.map((s, i) => stepItem(i, s)));
  renderWzStatus();
}

function startWzPoll() {
  stopWzPoll();
  const tick = async () => {
    if (!wz.open || wz.step !== "wifi" || !wz.camId) return;
    try {
      const d = await api(`/api/cameras/${enc(wz.camId)}`);
      wz.status = d.status_detail || { status: d.status, message: d.message };
      wz.detail = d;
    } catch (_) { wz.status = { status: "offline" }; }
    renderWzStatus();
  };
  tick();
  wz.poll = setInterval(tick, 1000);
}
function stopWzPoll() { if (wz.poll) { clearInterval(wz.poll); wz.poll = null; } }

function renderWzStatus() {
  const s = wz.status || { status: "starting" };
  const st = s.status;
  const order = { discover: 0, approve: 1, ready: 2 };
  const at = st === "ready" ? 3 : ["connecting", "waiting_approval"].includes(st) ? 1 : 0;
  for (const row of document.querySelectorAll(".wz-stage")) {
    const k = order[row.dataset.stage];
    row.classList.toggle("done", k < at);
    row.classList.toggle("active", k === at);
    row.classList.toggle("warn", k === at && ["waiting_approval", "error", "reconnecting"].includes(st));
    const small = row.querySelector("small");
    small.textContent = "";
    if (k === at) {
      if (row.dataset.stage === "discover") small.textContent = st === "paused" ? "This camera is set not to connect automatically." : s.message || "Scanning for a Lumix in remote-shooting mode…";
      else if (row.dataset.stage === "approve") small.textContent = st === "waiting_approval" ? "If the camera shows a prompt, press OK on it." : s.message || "Registering this computer on the camera…";
    }
    if (row.dataset.stage === "discover" && k < at && s.ip) small.textContent = `Found at ${s.ip}`;
    if (row.dataset.stage === "ready" && st === "ready") {
      const cam = s.camera || {};
      small.textContent = [cam.modelNumber, s.model_name && s.model !== "generic" ? s.model_name + " profile" : "generic profile", s.ip].filter(Boolean).join(" · ");
    }
  }
  $("wzwifinext").disabled = st !== "ready";
  $("wzwifinext").textContent = st === "ready" ? "Continue" : "Waiting for the camera…";
}

async function renderWzBle() {
  const p = await wzProfile();
  const c = state.cameras.find((x) => x.id === wz.camId);
  const can = !!(p && p.ble);
  $("wzblesteps").replaceChildren(...((p && p.setup_ble) || []).map((s, i) => stepItem(i, s)));
  $("wzblesteps").hidden = !can;
  $("wzblewarn").hidden = !can || wz.ble === "ok";
  $("wzblesub").textContent = can
    ? "Pairing lets this computer switch the camera's Wi-Fi on and set its clock. You can skip it and pair later."
    : "Bluetooth control isn't known for this model yet, so there is nothing to pair. You can still use it over Wi-Fi.";
  const box = $("wzblestate");
  const pair = $("wzblepair");
  pair.hidden = !can || wz.ble === "ok";
  pair.disabled = wz.ble === "running";
  pair.textContent = wz.ble === "failed" ? "Try again" : "Pair over Bluetooth";
  $("wzbleskip").textContent = wz.ble === "ok" || !can ? "Continue" : "Skip";
  box.hidden = wz.ble === "idle" && !(c && c.ble && c.ble.registered);
  box.className = "wz-blestate callout " + (wz.ble === "running" ? "" : wz.ble === "failed" ? "bad" : "ok");
  if (wz.ble === "running") box.replaceChildren(el("span", { class: "spinner" }), el("div", {}, el("b", {}, "Pairing…"), el("span", {}, " Keep the camera on the Pairing screen. This takes up to a few minutes.")));
  else if (wz.ble === "failed") box.replaceChildren(el("div", {}, el("b", {}, "Pairing didn't finish"), el("span", {}, " " + (wz.bleMsg || "Check that the camera shows Pairing, then try again."))));
  else if (wz.ble === "ok" || (c && c.ble && c.ble.registered)) box.replaceChildren(el("div", {}, el("b", {}, "Paired"), el("span", {}, " This computer can wake the camera's Wi-Fi over Bluetooth.")));
}

async function wzPair() {
  wz.ble = "running"; wz.bleMsg = ""; await renderWzBle();
  try {
    const r = await fetch(`/api/cameras/${enc(wz.camId)}/ble/register`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    let d = null; try { d = await r.json(); } catch (_) { /* none */ }
    if (r.ok && d && d.ok) { wz.ble = "ok"; haptic([8, 30, 8]); }
    else { wz.ble = "failed"; wz.bleMsg = (d && d.error) || `server answered ${r.status}`; }
  } catch (e) {
    // the Mac's Wi-Fi hop can drop this request; the result is in the camera list
    await sleep(3000); await loadCameras();
    const c = state.cameras.find((x) => x.id === wz.camId);
    wz.ble = c && c.ble && c.ble.registered ? "ok" : "failed";
    wz.bleMsg = "Lost the server while this Mac was on the camera's Wi-Fi. " + (wz.ble === "ok" ? "" : "Try again.");
  }
  await loadCameras();
  await renderWzBle();
}

function suggestName(c) {
  if (c && c.nickname && !/^(camera|new camera)( \d+)?$/i.test(c.nickname)) return c.nickname;
  const cam = (c && c.camera) || {};
  const m = (cam.friendlyName || "").match(/^([A-Z0-9]{2,6})-[0-9A-F]{6}$/);
  if (m) return m[1];
  if (cam.modelNumber) return cam.modelNumber.replace(/^DC-/, "");
  return c && c.nickname ? c.nickname : "My camera";
}
function renderWzName() {
  const c = state.cameras.find((x) => x.id === wz.camId);
  $("wzname").value = suggestName(c);
  updateNameHint();
  setTimeout(() => $("wzname").focus(), 350);
}
function updateNameHint() {
  const v = $("wzname").value.trim() || "…";
  const hint = $("wznamehint");
  hint.replaceChildren("From a terminal: ", el("code", {}, `lumix --camera ${/\s/.test(v) ? JSON.stringify(v) : v} shoot`));
}

function renderWzDone() {
  const c = state.cameras.find((x) => x.id === wz.camId) || {};
  const cam = c.camera || {};
  $("wzdonetitle").textContent = `${c.nickname || "Camera"} is ready`;
  $("wzdonesub").textContent = c.status === "ready" ? "Connected and remembered. It reconnects by itself next time." : "Saved. It connects as soon as the camera is in remote-shooting mode on your Wi-Fi.";
  const rows = [["Model", cam.modelNumber ? `${cam.modelNumber}${c.profile ? " · " + c.profile.name : ""}` : (c.profile ? c.profile.name : "detected on connect")],
    ["Wi-Fi", c.status === "ready" ? `connected · ${c.ip}` : "not connected yet"],
    ["Bluetooth", c.ble && c.ble.registered ? "paired" : "not paired"], ["ID", c.id || "–"]];
  $("wzsummary").replaceChildren(...rows.map(([k, v]) => el("div", { class: "cell" }, el("div", { class: "cell-text" }, el("b", {}, k)), el("span", { class: "val" }, v))));
  $("wzanother").hidden = false;
}

function wireWizard() {
  $("wzcancel").addEventListener("click", () => closeWizard(wz.step !== "done"));
  $("wzback").addEventListener("click", () => { const i = WZ.indexOf(wz.step); if (i > 0) wzGo(WZ[i - 1], true); });
  $("wzmodelnext").addEventListener("click", async () => {
    $("wzmodelnext").disabled = true;
    const ok = await wzEnsureCamera();
    $("wzmodelnext").disabled = false;
    if (ok) wzGo("wifi");
  });
  $("wzwifinext").addEventListener("click", () => wzGo("ble"));
  $("wzwifiskip").addEventListener("click", () => wzGo("ble"));
  $("wzipform").addEventListener("submit", async (e) => {
    e.preventDefault();
    const ip = $("wzip").value.trim();
    if (!ip || !wz.camId) return;
    try { await api(`/api/cameras/${enc(wz.camId)}/connection/reconnect`, { ip }); toast(`Connecting to ${ip}…`); }
    catch (err) { toast(err.message, true); }
  });
  $("wzblepair").addEventListener("click", wzPair);
  $("wzbleskip").addEventListener("click", () => wzGo("name"));
  $("wzname").addEventListener("input", updateNameHint);
  $("wzname").addEventListener("keydown", (e) => { if (e.key === "Enter") $("wznamenext").click(); });
  $("wznamenext").addEventListener("click", async () => {
    const v = $("wzname").value.trim();
    if (!v) { $("wzname").focus(); return; }
    const r = await patchCamera(wz.camId, { nickname: v });
    if (r) wzGo("done");
  });
  $("wzfinish").addEventListener("click", async () => {
    const id = wz.camId;
    await closeWizard(false);
    state.skipConnect = false;
    if (id) await switchCamera(id);
  });
  $("wzanother").addEventListener("click", async () => {
    await closeWizard(false);
    setTimeout(() => openWizard({}), 420);
  });
}

// ---------------------------------------------------------------- timelapse
const TL_INTERVALS = [1, 2, 3, 5, 10, 15, 30, 60, 120, 300, 600];
const TL_COUNTS = [30, 60, 120, 240, 360, 720, 1440];
const TL_DURATIONS = [300, 600, 1800, 3600, 7200, 14400, 28800];
const tl = {
  status: null, skew: 0, busy: false,
  cfg: Object.assign({ engine: "server", interval: 10, lenmode: "count", count: 360, duration: 3600,
    ae_lock: false, import: false, video: false, later: false, start: "" }, LS.get("tlcfg", {})),
};
const TL_ACTIVE = ["scheduled", "running", "paused", "stopping", "importing", "assembling"];
function fmtSecs(s) {
  if (s < 60) return s + "s";
  if (s < 3600) return (s % 60 ? (s / 60).toFixed(1) : s / 60) + "m";
  return (s % 3600 ? (s / 3600).toFixed(1) : s / 3600) + "h";
}
function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60, s = sec % 60;
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : m ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}
function tlJob() { return tl.status && tl.status.job; }
function tlActive() { const j = tlJob(); return !!(j && TL_ACTIVE.includes(j.state)); }
async function loadTimelapse() {
  try { renderTimelapse(await api("/api/timelapse")); } catch (_) { /* older server / no camera */ }
}
function saveTlCfg() { LS.set("tlcfg", tl.cfg); }
function chipRow(box, values, cur, fmt, onPick) {
  box.replaceChildren(...values.map((v) => el("button", {
    class: "chip" + (v === cur ? " on" : ""), onclick: () => { haptic(); onPick(v); },
  }, fmt(v))));
  const on = box.querySelector(".chip.on");
  if (on) on.scrollIntoView({ block: "nearest", inline: "center" });
}
function renderTlSetup() {
  const c = tl.cfg, caps = (tl.status && tl.status.capabilities) || {};
  if (!caps.native && c.engine === "native") c.engine = "server";
  [...$("tlengine").children].forEach((b) => {
    b.classList.toggle("on", b.dataset.v === c.engine);
    b.disabled = b.dataset.v === "native" && caps.native === false;
  });
  chipRow($("tlinterval"), TL_INTERVALS, c.interval, fmtSecs, (v) => { c.interval = v; saveTlCfg(); renderTlSetup(); });
  [...$("tllenmode").children].forEach((b) => b.classList.toggle("on", b.dataset.v === c.lenmode));
  if (c.lenmode === "count") chipRow($("tllength"), TL_COUNTS, c.count, String, (v) => { c.count = v; saveTlCfg(); renderTlSetup(); });
  else chipRow($("tllength"), TL_DURATIONS, c.duration, fmtSecs, (v) => { c.duration = v; saveTlCfg(); renderTlSetup(); });
  for (const b of $("tlopts").querySelectorAll(".tl-opt")) {
    const k = b.dataset.opt;
    b.classList.toggle("on", !!c[k]);
    b.setAttribute("aria-pressed", c[k] ? "true" : "false");
    if (k === "video") b.disabled = caps.ffmpeg === false;
    if (k === "ae_lock") b.disabled = c.engine === "native";
  }
  $("tlstart").hidden = !c.later;
  if (c.later && !$("tlstart").value) $("tlstart").value = c.start || new Date(Date.now() + 3600e3).toTimeString().slice(0, 5);
  const shots = c.lenmode === "count" ? c.count : Math.floor(c.duration / c.interval) + 1;
  const run = (shots - 1) * c.interval;
  const clip = shots / 24;
  $("tlsum").replaceChildren(el("b", {}, `${shots} shots`), ` · ${fmtDur(run)} · `, el("b", {}, `${clip < 10 ? clip.toFixed(1) : Math.round(clip)} s`), " clip at 24 fps",
    c.import || c.video ? ` · ${c.video ? "MP4" : "frames"} in ${(caps.default_dest || "~/Pictures/Lumix").replace(/^\/Users\/[^/]+/, "~")}` : "",
    caps.ffmpeg === false && c.video ? " (ffmpeg missing)" : "");
}
function renderTlLast() {
  const j = tlJob(), box = $("tllast");
  if (!j || TL_ACTIVE.includes(j.state)) { box.hidden = true; return; }
  const bad = j.state === "failed" || !!j.video_error || !!j.import_error;
  box.className = "tl-last" + (bad ? " bad" : "");
  const what = j.video_path ? j.video_path.split("/").slice(-1)[0] : j.frames_total ? `${j.frames_total} frames in ${j.folder.replace(/^.*\/Pictures\//, "~/Pictures/")}` : "frames on the camera's card";
  const extra = j.video_error || j.import_error || (j.state === "failed" && (j.errors || []).slice(-1)[0]) || "";
  if (!j.shots) {
    box.className = "tl-last";
    box.replaceChildren(el("b", {}, `Last run ${j.state}`), j.state === "failed" && extra ? `: ${extra}` : " before the first shot.");
    box.hidden = false;
    return;
  }
  box.replaceChildren(el("b", {}, `Last run ${j.state}: `), `${j.shots} shots${(j.skipped || 0) + (j.missed || 0) ? ` (${(j.skipped || 0) + (j.missed || 0)} skipped)` : ""} → ${what}`, extra ? ` · ${extra}` : "");
  box.hidden = false;
}
function renderTimelapse(d) {
  const cur = tl.status && tl.status.job;
  if (d && d.job && cur && d.job.id === cur.id && d.job.now && cur.now && d.job.now < cur.now) {
    d = null;   // an HTTP answer older than a WebSocket push already shown (stop -> stopped race)
  }
  if (d) { tl.status = d; const j = d.job; if (j && j.now) tl.skew = j.now - Date.now() / 1000; }
  const j = tlJob();
  const active = tlActive();
  $("app").classList.toggle("tl-running", !!(j && ["scheduled", "running", "paused"].includes(j.state)));
  $("tlsetup").hidden = active;
  $("tlrun").hidden = !active;
  if (!active) { renderTlSetup(); renderTlLast(); return; }
  const p = $("tlpanel");
  p.classList.toggle("paused", j.state === "paused");
  p.classList.toggle("waiting", !!j.waiting);
  const C = 326.73;
  $("tlringfg").style.strokeDashoffset = String(C * (1 - Math.min(1, j.progress || 0)));
  $("tlring").setAttribute("aria-valuenow", String(Math.round((j.progress || 0) * 100)));
  $("tlshots").textContent = j.shots;
  $("tltarget").textContent = j.target ? `of ${j.target}` : "shots";
  const names = { scheduled: "Starts soon", running: j.engine === "native" ? "In-camera run" : "Shooting", paused: "Paused",
    stopping: "Stopping…", importing: "Importing frames…", assembling: "Making the video…" };
  let title = names[j.state] || j.state;
  if (j.state === "paused" && j.paused_by && j.paused_by !== "user") title += ` (${{ poweroff: "camera off", "server-stop": "server stopped", files: "browsing files" }[j.paused_by] || j.paused_by})`;
  if (j.waiting) title = "Waiting: " + j.waiting.replace("camera ", "camera is ");
  $("tlstate").textContent = title;
  tlTick();
  const sk = (j.skipped || 0) + (j.missed || 0);
  const last = (j.skips || []).slice(-1)[0];
  $("tlskip").textContent = sk ? `${sk} skipped${last ? " · " + last.reason : ""}` : (j.failed ? `${j.failed} failed` : `every ${fmtSecs(j.interval_s)}${j.ae_lock ? " · AE lock" : ""}${j.auto_import ? " · import" : ""}`);
  const img = $("tlframe");
  if (d && d.frame_url && j.frame_at) {
    const src = cp(d.frame_url) + "?t=" + Math.round(j.frame_at * 1000);
    if (img.dataset.src !== src) { img.dataset.src = src; img.src = src; }
    img.hidden = false;
  } else if (!j.has_frame) img.hidden = true;
  $("tlpause").textContent = j.state === "paused" ? "Resume" : "Pause";
  $("tlpause").disabled = !["scheduled", "running", "paused"].includes(j.state);
  $("tlstop").disabled = !["scheduled", "running", "paused"].includes(j.state);
}
function tlTick() {
  const j = tlJob();
  if (!j || !tlActive()) return;
  const now = Date.now() / 1000 + tl.skew;
  if (["scheduled", "running"].includes(j.state) && j.eta_s !== null && j.eta_s !== undefined) {
    const eta = Math.max(0, j.eta_s - (now - j.now));
    $("tleta").textContent = `${fmtDur(eta)} left · done ${new Date((now + eta - tl.skew) * 1000).toTimeString().slice(0, 5)}`;
  } else $("tleta").textContent = j.state === "paused" ? `${j.remaining ?? "?"} shots to go` : "";
  if (j.state === "scheduled" && j.start_at) $("tlnext").textContent = `starts at ${new Date(j.start_at * 1000).toTimeString().slice(0, 5)}`;
  else if (j.state === "running" && j.next_shot_at) $("tlnext").textContent = `next shot in ${Math.max(0, Math.ceil(j.next_shot_at - now))}s`;
  else $("tlnext").textContent = j.state === "importing" ? `${j.frames_total || 0} of ${j.shots} imported` : "";
}
setInterval(() => { if (state.mode === "timelapse") tlTick(); }, 1000);
async function tlCall(path, body, okMsg) {
  if (tl.busy) return;
  tl.busy = true;
  try { renderTimelapse(await api(path, body)); if (okMsg) toast(okMsg); }
  catch (e) { toast(e.message, true); }
  finally { tl.busy = false; }
}
function tlStart() {
  const c = tl.cfg;
  const body = { interval: c.interval, native: c.engine === "native", ae_lock: c.engine !== "native" && c.ae_lock,
    import: c.import || c.video, video: c.video };
  if (c.lenmode === "count") body.count = c.count; else body.duration = c.duration;
  if (c.later && $("tlstart").value) { c.start = $("tlstart").value; saveTlCfg(); body.start_at = c.start; }
  haptic([8, 30, 8]);
  return tlCall("/api/timelapse/start", body, c.later ? "Timelapse scheduled" : "Timelapse started");
}
function tlShutter() {
  const j = tlJob();
  if (j && ["scheduled", "running", "paused"].includes(j.state)) tlCall("/api/timelapse/stop", {}, "Timelapse stopped");
  else if (!tlActive()) tlStart();
}
function wireTimelapse() {
  $("tlengine").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b || b.disabled) return; tl.cfg.engine = b.dataset.v; saveTlCfg(); haptic(); renderTlSetup(); });
  $("tllenmode").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; tl.cfg.lenmode = b.dataset.v; saveTlCfg(); haptic(); renderTlSetup(); });
  $("tlopts").addEventListener("click", (e) => {
    const b = e.target.closest(".tl-opt"); if (!b || b.disabled) return;
    const k = b.dataset.opt; tl.cfg[k] = !tl.cfg[k];
    if (k === "video" && tl.cfg.video) tl.cfg.import = true;
    if (k === "import" && !tl.cfg.import) tl.cfg.video = false;
    saveTlCfg(); haptic(); renderTlSetup();
  });
  $("tlstart").addEventListener("change", () => { tl.cfg.start = $("tlstart").value; saveTlCfg(); });
  $("tlpause").addEventListener("click", () => {
    const j = tlJob(); if (!j) return;
    tlCall(j.state === "paused" ? "/api/timelapse/resume" : "/api/timelapse/pause", {});
  });
  $("tlstop").addEventListener("click", () => tlCall("/api/timelapse/stop", {}, "Timelapse stopped"));
}

// ---------------------------------------------------------------- power-off sheet
const PLAN_ICON = { import: "↓", recording: "●", timelapse: "⏱", liveview: "◉", events: "≋" };
async function openPowerSheet() {
  const list = $("powerplan");
  list.replaceChildren(el("li", { class: "plan-empty" }, "Checking what's running…"));
  $("powernote").hidden = true;
  $("powertext").textContent = blePaired()
    ? "Turn it back on from here with Turn on over Bluetooth (needs Remote Wakeup on the camera), or switch it on by hand."
    : "You'll need to switch it on by hand afterwards. Wi-Fi can't wake it; pair Bluetooth (Cameras › Run setup again…) to turn it on from here.";
  $("powerdlg").showModal();
  let plan;
  try { plan = await api("/api/power/plan"); } catch (e) { list.replaceChildren(el("li", { class: "plan-empty" }, e.message)); return; }
  // live view and the event subscription always end cleanly: not worth a line each
  const items = (plan.items || []).filter((it) => !["liveview", "events"].includes(it.kind));
  const verb = { pause: "Pauses", stop: "Stops", unsubscribe: "Ends" };
  list.replaceChildren(...(items.length ? items.map((it) => el("li", { class: it.action === "pause" ? "pause" : it.kind === "recording" ? "stop" : "quiet" },
    el("span", { class: "plan-ico" }, PLAN_ICON[it.kind] || "•"),
    el("span", { class: "plan-txt" }, el("b", {}, `${verb[it.action] || it.action}: ${it.label}`), it.detail ? el("small", {}, it.detail) : null)))
    : [el("li", { class: "plan-empty" }, "Nothing running. Safe to power off.")]));
  const resumable = items.some((it) => it.action === "pause");
  if (resumable) {
    $("powernote").textContent = plan.resume_on_reconnect ? "Paused work continues automatically when the camera is back on." : "Paused work stays paused until you resume it.";
    $("powernote").hidden = false;
  }
}
function quiesceToast(r) {
  if (!r || r.reason === "poweroff" || r.reason === "server-stop") return;
  const paused = (r.paused || []).map((x) => x.label), stopped = (r.stopped || []).map((x) => x.label);
  if (!paused.length && !stopped.length) return;
  toast((paused.length ? "Paused " + paused.join(", ") : "") + (stopped.length ? (paused.length ? " · stopped " : "Stopped ") + stopped.join(", ") : ""));
}

// ---------------------------------------------------------------- Bluetooth wake
// POST /api/ble/wake: BLE login + "start Wi-Fi via network". Powers the camera up from
// off when its Remote Wakeup is on, then the server reconnects. Takes 30-90 s.
const wake = { running: false, camId: null, t0: 0, secs: 0, ok: null, data: null, error: "", needPair: false, timer: null };
function blePaired() { const c = currentCamera(); return !!(c && c.ble && c.ble.registered); }
// worth offering: the server is up, a camera is set up, and it isn't connected
function canWake(s) { return !!s && !["ready", "offline", "no_camera", "paused", "starting"].includes(s.status || "starting"); }

async function bleWake() {
  if (wake.running) return;
  haptic();
  Object.assign(wake, { camId: state.camId, ok: null, data: null, error: "", needPair: !blePaired() });
  if (wake.needPair) { renderWake(); return; }
  wake.running = true; wake.t0 = Date.now(); wake.secs = 0;
  wake.timer = setInterval(() => { wake.secs = Math.round((Date.now() - wake.t0) / 1000); renderWake(); }, 1000);
  renderWake();
  try {
    const r = await fetch(cp("/api/ble/wake"), { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    let d = null;
    try { d = await r.json(); } catch (_) { /* non-JSON */ }
    wake.data = d;
    wake.ok = !!(r.ok && d && d.ok);
    if (!wake.ok) wake.error = (d && d.error) || `server answered ${r.status}`;
  } catch (e) {
    wake.ok = false; wake.error = "Lost the server: " + e.message;
  }
  clearInterval(wake.timer); wake.timer = null;
  wake.running = false; wake.secs = Math.round((Date.now() - wake.t0) / 1000);
  if (wake.ok) { haptic([8, 30, 8]); toast("Camera is waking up; reconnecting over Wi-Fi"); } else toast("Bluetooth wake failed: " + wake.error, true);
  renderWake();
}

function openPairing() {
  const id = state.camId;
  closeSheet();
  openWizard({ camId: id, reuse: true, step: "ble" });
}

function wakeDetails() {
  if (!wake.data) return null;
  return el("details", {}, el("summary", {}, "Details"), el("pre", {}, JSON.stringify(wake.data, null, 2)));
}

function renderWake() {
  const paired = blePaired();
  const label = wake.running ? `Turning on… ${wake.secs} s` : "Turn on over Bluetooth";
  $("wakelabel").textContent = label;
  $("wakesub").textContent = wake.running ? "Keep the camera near this computer. Takes up to 90 s."
    : paired ? "Switches the camera on and joins it to Wi-Fi" : "Needs Bluetooth pairing first";
  $("btnwake").disabled = wake.running;
  $("cswake").textContent = label;
  $("cswake").disabled = wake.running;
  $("bannerwake").textContent = wake.running ? `Turning on… ${wake.secs} s` : "Turn on";
  $("bannerwake").disabled = wake.running;
  const show = wake.camId === state.camId && (wake.running || wake.ok !== null || wake.needPair);
  for (const box of [$("wakestate"), $("cswakestate")]) {
    box.hidden = !show;
    if (!show) { box.replaceChildren(); continue; }
    if (wake.running) {
      box.className = "wake-state callout";
      box.replaceChildren(el("div", { class: "wake-head" }, el("span", { class: "spinner" }),
        el("div", {}, el("b", {}, "Turning the camera on…"), el("span", {}, `Bluetooth login, then the camera joins Wi-Fi. ${wake.secs} s so far.`))));
    } else if (wake.needPair) {
      box.className = "wake-state callout warn";
      box.replaceChildren(el("b", {}, "Not paired over Bluetooth"),
        el("span", {}, "Pair this computer once in Cameras › your camera › Run setup again… (Bluetooth step). To power up from off, the camera also needs Remote Wakeup on."),
        el("button", { class: "link", onclick: openPairing }, "Run setup again…"));
    } else if (wake.ok) {
      box.className = "wake-state callout ok";
      box.replaceChildren(el("b", {}, "Camera is waking up"),
        el("span", {}, `Bluetooth wake done in ${wake.secs} s. It reconnects over Wi-Fi by itself.`), wakeDetails());
    } else {
      box.className = "wake-state callout bad";
      box.replaceChildren(el("b", {}, "Couldn't turn it on"),
        el("span", {}, wake.error.replace(/[.\s]+$/, "") + ". Check that the camera is in range and its Bluetooth (and Remote Wakeup, if it's off) is on."), wakeDetails());
    }
  }
}

// ---------------------------------------------------------------- websocket
let wsCur = null;
function connectWs() {
  if (wsCur) { const old = wsCur; wsCur = null; old.onclose = null; try { old.close(); } catch (_) { /* gone */ } }
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + cp("/api/ws"));
  wsCur = ws;
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.type === "status") renderStatus(msg.data);
    else if (msg.type === "cameras") setCameras(msg.data);
    else if (msg.type === "exposure") { state.exposure = msg.data; renderQC(); }
    else if (msg.type === "settings") loadSettings();
    else if (msg.type === "preset") { state.auto = msg.data; renderAuto(); }
    else if (msg.type === "timelapse") renderTimelapse(msg.data);
    else if (msg.type === "quiesce") quiesceToast(msg.data);
    else if (msg.type === "event") addLog(msg.data);
    else if (msg.type === "events") { $("log").replaceChildren(); for (const ev of msg.data) addLog(ev); }
  };
  ws.onclose = () => {
    if (ws !== wsCur) return;
    renderStatus({ status: "offline", message: "server unreachable", liveview: {} });
    setTimeout(() => { if (ws === wsCur) { loadCameras().finally(connectWs); } }, 2000);
  };
}

// ---------------------------------------------------------------- wiring
function init() {
  $("btnshutter").addEventListener("click", shutterPressed);
  $("btnaf").addEventListener("click", () => { haptic(); cameraCall("/api/af", {}); });
  $("btnauto").addEventListener("click", () => setAuto(!state.auto.on));
  $("btnconn").addEventListener("click", () => {
    if (state.cameras.length > 1) openSheet("cams");
    else if (state.status && state.status.status === "ready") openSheet("conn");
    else { $("connect").hidden = false; state.skipConnect = false; }
  });
  $("btnwizard").addEventListener("click", () => openWizard({ camId: state.camId, reuse: true }));
  $("bannerbtn").addEventListener("click", () => reconnect());
  $("bannerwake").addEventListener("click", bleWake);
  $("cswake").addEventListener("click", bleWake);
  $("btnreconnect").addEventListener("click", () => reconnect());
  $("btnskip").addEventListener("click", () => { state.skipConnect = true; $("connect").hidden = true; if (state.status) renderStatus(state.status); });
  $("btnforget").addEventListener("click", () => { LS.set("camera", null); reconnect(null, true); });
  $("ipform").addEventListener("submit", (e) => { e.preventDefault(); const ip = $("ipinput").value.trim(); if (ip) reconnect(ip); });
  for (const b of $("modestrack").children) b.addEventListener("click", (e) => e.preventDefault());
  wireGestures();
  wireSheet();
  wireFiles();
  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.target.matches("input, select, textarea")) {
      // Esc in a field: first clears it (search fields do that natively), then leaves it
      // and closes like anywhere else
      if (e.key !== "Escape" || e.target.closest("dialog") || e.target.value) return;
      e.target.blur();
    }
    if (document.querySelector("dialog[open]")) return;
    const k = e.key;
    if (k === "Escape") {
      if (wz.open) closeWizard(true);
      else if (!$("viewer").hidden) closeViewer();
      else if (!$("files").hidden) { if (files.selecting) setSelecting(false); else closeFiles(); }
      else if (sheetOpen()) { if (navStack.length > 1) pop(); else closeSheet(); }
      return;
    }
    if (!$("files").hidden || !$("viewer").hidden || wz.open) return;
    if (e.code === "Space") { e.preventDefault(); if (state.mode === "timelapse") tlShutter(); else shoot(); }
    else if (k === "r" || k === "R") toggleRec();
    else if (k === "p" || k === "P") (sheetOpen() ? closeSheet() : openSheet());
    else if (k === "a" || k === "A") setAuto(!state.auto.on);
    else if (k === "f" || k === "F") { haptic(); cameraCall("/api/af", {}); }
    else if (k === "g" || k === "G") openFiles();
    else if (k === "1" || k === "2" || k === "3") setMode(MODES[+k - 1]);
    else if (k === "ArrowLeft") stepMode(-1);
    else if (k === "ArrowRight") stepMode(1);
  });
  wireCameras();
  wireWizard();
  wireTimelapse();
  setMode(state.mode, true);
  renderChecklist(DEFAULT_STEPS);
  renderConnect({ status: "starting" });
  loadCameras().finally(() => {
    keepLiveView();
    connectWs();
    loadSettings();
    loadConnection();
    loadPresets();
    loadTimelapse();
    hashRoute();
  });
  window.addEventListener("hashchange", hashRoute);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(positionModes);
}
// Statuses of the other cameras: refresh the list (fast while the switcher is open).
let camPollAt = 0;
setInterval(() => {
  const open = sheetOpen() && ["cams", "cam"].includes(currentPage());
  if (Date.now() - camPollAt > (open ? 1500 : 6000) && state.cameras.length) { camPollAt = Date.now(); loadCameras(); }
}, 500);
function loadPresets() {
  api("/api/presets").then((r) => { state.auto = r.auto || { on: false }; renderAuto(); }).catch(() => {});
}
// Deep links: #wizard[=model|wifi|ble|name|done], #cameras
function hashRoute() {
  const h = decodeURIComponent(location.hash.slice(1));
  if (h.startsWith("wizard")) openWizard({ camId: state.camId, reuse: true, step: h.split("=")[1] || "model" });
  else if (h === "cameras") openSheet("cams");
}
init();
