#!/usr/bin/env bash
# Safe one-command smoke test for the real camera (DC-S5), e.g. after a power cycle:
#
#   scripts/real-camera-smoke.sh            # camera at 192.0.2.28
#   LUMIX_SMOKE_IP=192.168.1.50 scripts/real-camera-smoke.sh
#
# Steps: stop the server, start it fresh, wait until the camera has settled,
# check live view frames, take ONE photo with drive mode Single (the previous
# drive mode is restored), record a 4 s video, list the card, go back to
# shooting and leave the server running.
#
# Safety: one command at a time, and nothing is retried here (the server
# already does LUMIX Sync's 5 x 1 s err_busy retry). The run stops at the first
# sign of trouble: the camera flagged as wedged, someone using the body, an
# err_* answer, a timelapse/import already running, or the camera not settling.
# After a stop nothing more is sent, except putting the drive mode back (a
# setsetting, which still works on a wedged camera). It never powers the camera
# off, never scans the LAN (--no-scan, fixed --camera-ip) and never uses
# Bluetooth.
#
# Env: LUMIX_SMOKE_IP (camera address), LUMIX_SMOKE_SERVER_ARGS (replaces the
# server start arguments, e.g. "--camera-ip 192.0.2.28 --no-scan --lan"),
# LUMIX_SERVER / LUMIX_REMOTE_HOME (as for the CLI), LUMIX_SMOKE_SETTLE_S
# (default 120), LUMIX_SMOKE_KEEP_SERVER=0 (stop the server at the end).
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LUMIX="$ROOT/bin/lumix"
PY="$ROOT/.venv/bin/python"
IP="${LUMIX_SMOKE_IP:-192.0.2.28}"
SETTLE_S="${LUMIX_SMOKE_SETTLE_S:-120}"
if [ -n "${LUMIX_SMOKE_SERVER_ARGS:-}" ]; then
  read -r -a SERVER_ARGS <<< "$LUMIX_SMOKE_SERVER_ARGS"
else
  SERVER_ARGS=(--camera-ip "$IP" --no-scan)
fi
LOGDIR="${LUMIX_LOG_DIR:-$ROOT/logs}"
mkdir -p "$LOGDIR"
# the CLI talks to the camera the server is started for (--camera ID), else the active one
for ((i = 0; i < ${#SERVER_ARGS[@]}; i++)); do
  if [ "${SERVER_ARGS[$i]}" = "--camera" ] && [ $((i + 1)) -lt ${#SERVER_ARGS[@]} ]; then
    export LUMIX_CAMERA="${SERVER_ARGS[$((i + 1))]}"
  fi
done
LOG="$LOGDIR/smoke-$(date +%Y%m%d-%H%M%S).log"

STEP=0
DRIVE_ORIG=""      # set once the drive mode was changed; restored on exit
WEDGE_SEEN=0

say()  { printf '%s\n' "$*" | tee -a "$LOG"; }
step() { STEP=$((STEP + 1)); say ""; say "[$STEP] $*"; }
ok()   { say "    ok   $*"; }
info() { say "    ..   $*"; }
fail() {
  say "    FAIL $*"
  say ""
  say "SMOKE TEST STOPPED at step $STEP. Nothing more is sent to the camera."
  if [ "$WEDGE_SEEN" = 1 ]; then
    say "The camera is wedged (it answers err_busy to every camcmd). Switch it OFF and ON"
    say "with its power switch, then run this again. Do not retry commands meanwhile."
  fi
  say "Log: $LOG   Server log: $LOGDIR/lumix-server.log"
  exit 1
}

restore_drive() {
  if [ -n "$DRIVE_ORIG" ]; then
    local d="$DRIVE_ORIG" out
    DRIVE_ORIG=""
    if out=$("$LUMIX" set --raw drivemode "$d" 2>&1); then
      say "    ..   drive mode restored to $d"
    else
      say "    WARN could not restore the drive mode to $d: $out (set it on the camera)"
    fi
  fi
}
trap restore_drive EXIT
trap 'say ""; say "interrupted"; exit 130' INT TERM

# lumix CLI call: prints its output, logs it, returns the CLI's exit code
cli() {
  local out rc
  out=$("$LUMIX" "$@" 2>&1); rc=$?
  printf '$ lumix %s\n%s\n[exit %d]\n' "$*" "$out" "$rc" >> "$LOG"
  printf '%s' "$out"
  return $rc
}

status_json() { "$LUMIX" status --json 2>>"$LOG"; }
# field from JSON on stdin, dotted path (liveview.age); booleans print True/False
field() {
  "$PY" -c 'import json,sys
try:
    d = json.load(sys.stdin)
except ValueError:
    d = None
for k in sys.argv[1].split("."):
    d = d.get(k) if isinstance(d, dict) else None
print("" if d is None else d)' "$1"
}

# stop at once on wedge / body in use / not ready; $1 = what comes next
guard() {
  local st
  st=$(status_json) || fail "server not answering (before: $1)"
  if [ "$(field wedged <<< "$st")" = "True" ]; then WEDGE_SEEN=1; fail "camera is WEDGED (before: $1)"; fi
  if [ "$(field body_busy <<< "$st")" = "True" ]; then
    fail "someone is using the camera body (before: $1); leave it alone and run again"
  fi
  [ "$(field status <<< "$st")" = "ready" ] || \
    fail "camera not ready: $(field status <<< "$st") $(field message <<< "$st") (before: $1)"
  return 0
}

lt() { "$PY" -c 'import sys; sys.exit(0 if float(sys.argv[1] or 1e9) < float(sys.argv[2]) else 1)' "$1" "$2"; }

say "Lumix real-camera smoke test  $(date)"
say "camera $IP${LUMIX_CAMERA:+ (id $LUMIX_CAMERA)}, server ${LUMIX_SERVER:-http://127.0.0.1:8770}, start args: ${SERVER_ARGS[*]}"
say "log $LOG"

# ---------------------------------------------------------------- server
step "Stop the server (only the one in the pidfile)"
out=$(cli server stop) || true
info "$out"
if "$LUMIX" status --json >/dev/null 2>&1; then
  fail "a server still answers (started without a pidfile?); stop it yourself first"
fi
ok "no server running"

step "Start the server fresh"
out=$(cli server start -- "${SERVER_ARGS[@]}") || fail "server start: $out"
info "$out"
ok "server up"

# ---------------------------------------------------------------- connect
step "Wait for the camera to connect and settle (up to ${SETTLE_S}s; the S5 takes 5-30 s after SUBSCRIBE)"
t0=$(date +%s); last=""
while :; do
  st=$(status_json) || fail "server stopped answering"
  s=$(field status <<< "$st"); m=$(field message <<< "$st")
  if [ "$s $m" != "$last" ]; then info "$s: $m"; last="$s $m"; fi
  if [ "$(field wedged <<< "$st")" = "True" ]; then WEDGE_SEEN=1; fail "camera reported WEDGED while connecting"; fi
  [ "$s" = "ready" ] && break
  [ "$s" = "error" ] && fail "server error: $m"
  [ "$s" = "paused" ] && fail "the server does not run this camera: $m (set LUMIX_CAMERA or --camera)"
  [ "$s" = "no_camera" ] && fail "no camera set up on this server"
  if [ $(( $(date +%s) - t0 )) -ge "$SETTLE_S" ]; then
    fail "camera not ready after ${SETTLE_S}s (last: $s $m). On the camera: Wi-Fi > Remote Shooting & View, same network as this Mac"
  fi
  sleep 1
done
ok "connected in $(( $(date +%s) - t0 ))s: $(field camera.modelNumber <<< "$st") $(field camera.friendlyName <<< "$st") fw $(field camera.X_FirmVersion <<< "$st"), battery $(field battery <<< "$st")"
tl=$(cli timelapse status --json | field job.state)
case "$tl" in scheduled|running|paused|stopping|importing|assembling)
  fail "a timelapse is $tl on this camera; stop it first (lumix timelapse stop)";; esac
jobs=$(cli files jobs --json | "$PY" -c 'import json,sys
try:
    print(sum(j.get("state") in ("queued", "running") for j in json.load(sys.stdin)))
except ValueError:
    print(0)')
[ "${jobs:-0}" = "0" ] || fail "an import from the card is running; let it finish first"

# ---------------------------------------------------------------- live view
step "Live view frames"
good=0; age=""; fps=""
for _ in $(seq 1 15); do
  st=$(status_json) || fail "server stopped answering"
  age=$(field liveview.age <<< "$st"); fps=$(field liveview.fps <<< "$st")
  if lt "$age" 2 && ! lt "${fps:-0}" 5; then
    good=$((good + 1)); [ $good -ge 3 ] && break
  fi
  sleep 1
done
if [ $good -lt 3 ]; then
  guard "live view check"
  fail "no live view frames (age ${age:-none}s, ${fps:-0} fps); is UDP to this Mac allowed by the firewall?"
fi
snap="$LOGDIR/smoke-frame.jpg"
cli snapshot -o "$snap" >/dev/null || fail "snapshot failed"
size=$(wc -c < "$snap" | tr -d ' ')
[ "$size" -gt 2000 ] || fail "live view frame is only $size bytes"
ok "$fps fps, frame age ${age}s, snapshot $snap ($size bytes)"

# ---------------------------------------------------------------- photo
step "One photo with drive mode Single"
guard "photo"
cur=$(cli get --raw drivemode --json | field values.drivemode)
[ -n "$cur" ] || fail "could not read the drive mode"
if [ "$cur" != "normal" ]; then
  DRIVE_ORIG="$cur"
  out=$(cli set --raw drivemode normal) || fail "setting drive mode Single: $out"
  info "drive mode $cur -> normal (Single); put back afterwards"
else
  info "drive mode already Single"
fi
out=$(cli shoot --json); rc=$?
res=$(field result <<< "$out")
if [ $rc -ne 0 ] || [ "$res" != "ok" ]; then
  st=$(status_json); [ "$(field wedged <<< "$st")" = "True" ] && WEDGE_SEEN=1
  fail "shutter answered ${res:-an error}: $out"
fi
[ "$(field count_changed <<< "$out")" = "True" ] || info "remaining-shots count did not change yet (camera may still be writing)"
ok "photo taken, remaining $(field remain_before <<< "$out") -> $(field remain_after <<< "$out")"
restore_drive

# ---------------------------------------------------------------- video
step "4 s video"
sleep 2                       # card write after the photo
guard "video start"
out=$(cli video start --json); rc=$?
if [ $rc -ne 0 ] || [ "$(field result <<< "$out")" != "ok" ]; then
  st=$(status_json); [ "$(field wedged <<< "$st")" = "True" ] && WEDGE_SEEN=1
  fail "video start answered $(field result <<< "$out"): $out"
fi
info "recording..."
sleep 4
out=$(cli video stop --json); rc=$?
if [ $rc -ne 0 ] || [ "$(field result <<< "$out")" != "ok" ]; then
  st=$(status_json); [ "$(field wedged <<< "$st")" = "True" ] && WEDGE_SEEN=1
  fail "video stop answered $(field result <<< "$out"). CHECK THE CAMERA: it may still be recording (press its record button)"
fi
for _ in $(seq 1 20); do      # wait until the card is written
  st=$(status_json)
  [ "$(field recording <<< "$st")" = "False" ] && [ "$(field sd.access <<< "$st")" != "on" ] && break
  sleep 0.5
done
[ "$(field recording <<< "$st")" = "False" ] || fail "camera still reports recording after video stop; check it"
ok "video recorded and written"

# ---------------------------------------------------------------- files
step "Card listing (switches the camera to playback)"
guard "file listing"
out=$(cli files list --limit 5) || fail "files list: $out"
while IFS= read -r line; do info "$line"; done <<< "$out"
newest=$(cli files list --limit 2 --json | "$PY" -c 'import json,sys
try:
    print(" ".join(i["kind"] for i in json.load(sys.stdin)["items"]))
except (ValueError, KeyError):
    print("?")')
if [ "$newest" = "video photo" ]; then ok "newest files: the video and the photo just taken"
else info "newest kinds: $newest (expected: video photo)"; fi

step "Back to shooting"
out=$(cli files done) || fail "files done: $out"
for _ in $(seq 1 20); do
  st=$(status_json)
  [ "$(field cammode <<< "$st")" = "rec" ] && lt "$(field liveview.age <<< "$st")" 2 && break
  sleep 1
done
guard "final check"
[ "$(field cammode <<< "$st")" = "rec" ] || fail "camera did not return to rec mode"
lt "$(field liveview.age <<< "$st")" 2 || fail "no live view after returning to shooting"
ok "rec mode, live view running"

if [ "${LUMIX_SMOKE_KEEP_SERVER:-1}" = "0" ]; then
  step "Stop the server"
  cli server stop >/dev/null || true
  ok "stopped"
fi

say ""
say "SMOKE TEST PASSED ($STEP steps). Camera healthy, server left running at ${LUMIX_SERVER:-http://127.0.0.1:8770}."
say "Log: $LOG"
