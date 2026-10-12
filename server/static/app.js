"use strict";
// Lumix Remote UI: vanilla JS, talks to the JSON API + /api/ws push channel.

const $ = (id) => document.getElementById(id);
const state = { status: null, exposure: null, settings: [], quick: [], filter: "" };

// ---------------------------------------------------------------- helpers
async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch (_) { /* non-JSON */ }
  if (!r.ok) throw new Error((data && data.error) || r.statusText);
  return data;
}

let toastTimer = null;
function toast(msg, err) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (err ? " err" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, err ? 5000 : 2500);
}

const BUSY_HINT = "The camera refused (err_busy). Check its screen: close any menu or playback, or power-cycle it.";
function explain(result) {
  if (!result || result === "ok") return null;
  if (result === "err_busy") return BUSY_HINT;
  return "Camera answered " + result;
}

function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids) if (k !== null && k !== undefined) e.append(k);
  return e;
}

function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60, s = sec % 60;
  const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

// ---------------------------------------------------------------- status
function renderStatus(s) {
  state.status = s;
  const dot = $("dot");
  dot.className = "dot " + (s.status === "ready" ? "ready" :
    ["connecting", "waiting_approval", "discovering"].includes(s.status) ? "wait" : "bad");
  const cam = s.camera || {};
  $("camname").textContent = cam.friendlyName
    ? `${cam.modelNumber || ""} ${cam.friendlyName}` : "Lumix Remote";
  const meta = $("meta");
  meta.replaceChildren();
  const add = (label, val) => { if (val !== null && val !== undefined && val !== "") meta.append(el("span", {}, label + " ", el("b", {}, String(val)))); };
  add("", s.status === "ready" ? "connected" : `${s.status}${s.message ? " - " + s.message : ""}`);
  if (s.status === "ready") {
    add("Battery", s.battery);
    const sd = s.sd || {};
    add("Card", sd.current ? sd.current.toUpperCase() : null);
    add("Photos left", s.remain_photos);
    if (s.remain_video_s) add("Video left", fmtTime(+s.remain_video_s));
    add("Lens", s.lens);
    if (s.op_owner) add("Busy", s.op_owner);
  }
  const lv = s.liveview || {};
  $("lvstats").textContent = lv.fps ? `${lv.fps} fps` : "";
  $("lvempty").hidden = !!(lv.age !== null && lv.age < 3);
  // recording indicator + timer
  const rec = !!s.recording;
  $("btnrec").classList.toggle("on", rec);
  $("recbadge").hidden = !rec;
  if (rec) {
    const prog = s.rec_progress && +s.rec_progress > 0 ? +s.rec_progress : null;
    const elapsed = prog !== null ? prog : (s.rec_started_at ? Date.now() / 1000 - s.rec_started_at : 0);
    $("rectime").textContent = fmtTime(elapsed);
  }
}

// ---------------------------------------------------------------- settings
function optionTitle(o) { return o.title || o.value; }

function settingSelect(s, onChange) {
  const sel = el("select", { "aria-label": s.title, disabled: !s.enabled });
  let matched = false;
  for (const o of s.options) {
    if (o.value === null) continue; // parametric option the UI can't fill in
    const isCur = o.id === s.current_option;
    matched = matched || isCur;
    sel.append(el("option", { value: o.id, selected: isCur, disabled: !o.enabled && !isCur }, optionTitle(o)));
  }
  if (!matched) sel.prepend(el("option", { value: "", selected: true, disabled: true }, s.current_title || "-"));
  sel.addEventListener("change", () => onChange(sel.value, sel));
  return sel;
}

async function applySetting(settingId, optionId, sel) {
  const field = sel.closest(".field, .row");
  if (field) field.classList.add("pending");
  try {
    const r = await api(`/api/settings/${encodeURIComponent(settingId)}`, { option: optionId });
    const hint = explain(r.result);
    if (hint) toast(hint, true); else toast(`${r.setting.title}: ${r.setting.current_title}`);
  } catch (e) {
    toast(e.message, true);
  } finally {
    if (field) field.classList.remove("pending");
    loadSettings();
  }
}

async function applyRaw(type, value, sel) {
  const field = sel.closest(".field");
  if (field) field.classList.add("pending");
  try {
    const r = await api("/api/setting", { type, value });
    const hint = explain(r.result);
    if (hint) toast(type === "shtrspeed" || type === "focal"
      ? `${hint} (in A/S/P modes the camera may control this value)` : hint, true);
  } catch (e) { toast(e.message, true); }
  finally { if (field) field.classList.remove("pending"); }
}

function exposureSelect(label, type, cur, curTitle, options) {
  const sel = el("select", { "aria-label": label, disabled: !options.length });
  let matched = false;
  for (const o of options) {
    const isCur = o.value === cur;
    matched = matched || isCur;
    sel.append(el("option", { value: o.value, selected: isCur }, o.title));
  }
  if (!matched) sel.prepend(el("option", { value: "", selected: true, disabled: true }, curTitle || "-"));
  sel.addEventListener("change", () => applyRaw(type, sel.value, sel));
  return el("div", { class: "field" }, el("label", {}, label), sel);
}

function renderQuick() {
  const q = $("quick");
  if (document.activeElement && q.contains(document.activeElement) && document.activeElement.tagName === "SELECT") return;
  q.replaceChildren();
  const ex = state.exposure;
  if (ex) {
    q.append(exposureSelect("Aperture", "focal", ex.focal, ex.focal_title, ex.aperture_options || []));
    q.append(exposureSelect("Shutter", "shtrspeed", ex.shtrspeed, ex.shtrspeed_title, ex.shutter_options || []));
  }
  for (const item of state.quick) {
    const s = item.setting;
    q.append(el("div", { class: "field" + (s.enabled ? "" : " disabled") },
      el("label", {}, item.label),
      settingSelect(s, (opt, sel) => applySetting(s.id, opt, sel))));
  }
}

const SECTION_NAMES = {
  record_top: "Exposure", record_qmenu: "Quick menu", record_menu: "Recording",
  record_tab: "Display", record_tab2: "Display (2)", play_menu: "Playback", home_menu: "Home",
};

function renderAll() {
  const box = $("allsettings");
  if (document.activeElement && box.contains(document.activeElement) && document.activeElement.tagName === "SELECT") return;
  const open = new Set([...box.querySelectorAll("details[open]")].map((d) => d.dataset.sec));
  box.replaceChildren();
  if (!state.settings.length) { box.append(el("p", { class: "muted" }, "Settings appear once the camera is connected.")); return; }
  const f = state.filter.toLowerCase();
  const groups = new Map();
  for (const s of state.settings) {
    const text = (s.title + " " + s.path.join(" ") + " " + (s.current_title || "") + " " + s.cmd_types.join(" ")).toLowerCase();
    if (f && !text.includes(f)) continue;
    if (!groups.has(s.section)) groups.set(s.section, []);
    groups.get(s.section).push(s);
  }
  for (const [sec, list] of groups) {
    const d = el("details", { "data-sec": sec, open: f !== "" || open.has(sec) },
      el("summary", {}, `${SECTION_NAMES[sec] || sec} (${list.length})`));
    for (const s of list) {
      d.append(el("div", { class: "row" + (s.enabled ? "" : " disabled"), title: s.cmd_types.join(", ") },
        el("span", { class: "name" }, s.title, s.path.length ? el("span", { class: "path" }, s.path.join(" › ")) : null),
        settingSelect(s, (opt, sel) => applySetting(s.id, opt, sel))));
    }
    box.append(d);
  }
  if (!groups.size) box.append(el("p", { class: "muted" }, "No setting matches."));
}

let loadTimer = null;
function loadSettings() {
  clearTimeout(loadTimer);
  loadTimer = setTimeout(async () => {
    try {
      const [all, quick] = await Promise.all([api("/api/settings"), api("/api/quick")]);
      state.settings = all.settings || [];
      state.quick = quick.items || [];
      state.exposure = quick.exposure;
      renderQuick();
      renderAll();
    } catch (e) { /* server restarting */ }
  }, 150);
}

// ---------------------------------------------------------------- event log
function addLog(ev) {
  const log = $("log");
  const t = new Date(ev.ts * 1000).toLocaleTimeString();
  log.prepend(el("li", { class: "k-" + ev.kind }, el("time", {}, t), `[${ev.kind}] ${ev.text}`));
  while (log.children.length > 300) log.lastChild.remove();
}

// ---------------------------------------------------------------- actions
async function shoot() {
  const b = $("btnshutter");
  if (b.classList.contains("busy")) return;
  b.classList.add("busy");
  try {
    const r = await api("/api/capture", {});
    const hint = explain(r.result);
    if (hint) toast(hint, true);
    else toast(r.count_changed ? `Photo taken (${r.remain_after} left)` : "Shutter released");
  } catch (e) { toast(e.message, true); }
  finally { b.classList.remove("busy"); }
}

async function toggleRec() {
  const rec = state.status && state.status.recording;
  try {
    const r = await api(rec ? "/api/video/stop" : "/api/video/start", {});
    const hint = explain(r.result);
    if (hint) toast(hint, true);
  } catch (e) { toast(e.message, true); }
}

function lvPoint(ev) {
  // Map the click to 0..1000 inside the displayed image (object-fit: contain).
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
  const m = $("touchmark");
  m.style.left = p.px + "px";
  m.style.top = p.py + "px";
  m.classList.remove("show"); void m.offsetWidth; m.classList.add("show");
  try {
    const r = await api("/api/touch", { x: p.x, y: p.y });
    const hint = explain(r.result);
    if (hint) toast(hint, true);
  } catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------------- live view
function keepLiveView() {
  // Reconnect the MJPEG stream if it stalls (server restart, camera reconnect).
  const img = $("lvimg");
  img.addEventListener("error", () => setTimeout(() => { img.src = "/api/liveview.mjpg?t=" + Date.now(); }, 2000));
  let lastSeq = 0, stalls = 0;
  setInterval(() => {
    const lv = state.status && state.status.liveview;
    if (!lv) return;
    if (lv.seq === lastSeq) stalls++; else stalls = 0;
    lastSeq = lv.seq;
    if (lv.viewers === 0 && lv.age !== null && lv.age < 2) {
      img.src = "/api/liveview.mjpg?t=" + Date.now(); // our stream died but frames flow
    }
  }, 4000);
}

// ---------------------------------------------------------------- websocket
function connectWs() {
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/api/ws");
  let prevStatus = null;
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.type === "status") {
      const was = prevStatus;
      prevStatus = msg.data.status;
      renderStatus(msg.data);
      if (msg.data.status === "ready" && was !== "ready") loadSettings();
    } else if (msg.type === "exposure") {
      state.exposure = msg.data;
      renderQuick();
    } else if (msg.type === "settings") {
      loadSettings();
    } else if (msg.type === "event") {
      addLog(msg.data);
    } else if (msg.type === "events") {
      $("log").replaceChildren();
      for (const ev of msg.data) addLog(ev);
    }
  };
  ws.onclose = () => {
    renderStatus({ status: "offline", message: "server unreachable", liveview: {} });
    setTimeout(connectWs, 2000);
  };
}

// ---------------------------------------------------------------- wiring
function init() {
  $("btnshutter").addEventListener("click", shoot);
  $("btnrec").addEventListener("click", toggleRec);
  $("btnaf").addEventListener("click", async () => {
    try { const r = await api("/api/af", {}); const h = explain(r.result); if (h) toast(h, true); }
    catch (e) { toast(e.message, true); }
  });
  $("lv").addEventListener("click", touchFocus);
  $("filter").addEventListener("input", (e) => { state.filter = e.target.value; renderAll(); });
  $("clearlog").addEventListener("click", () => $("log").replaceChildren());
  $("btnpower").addEventListener("click", () => $("powerdlg").showModal());
  $("powerdlg").addEventListener("close", async () => {
    if ($("powerdlg").returnValue !== "ok") return;
    try { const r = await api("/api/power/off", { confirm: true }); toast("Power off: " + r.result); }
    catch (e) { toast(e.message, true); }
  });
  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, select, textarea") || e.metaKey || e.ctrlKey) return;
    if (e.code === "Space") { e.preventDefault(); shoot(); }
    else if (e.key === "r" || e.key === "R") toggleRec();
  });
  keepLiveView();
  connectWs();
  loadSettings();
}
init();
