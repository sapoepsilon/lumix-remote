#!/usr/bin/env bash
# Real-camera check (DC-S5) for the 2026-10-12 11:55 incident fix: an in-camera
# (native) interval run must survive a server restart untouched, and the new
# server must adopt it without a single mode switch.
#
#   LUMIX_VERIFY_IP=<camera ip> scripts/real-verify-interval-restart.sh
#
# Run it only on a healthy, freshly power-cycled camera with a charged battery.
#
# Steps:
#   1. restart the server fresh, camera connected normally (not read-only)
#   2. note the drive mode, image count and photo count on the card
#   3. start a small native run (6 shots @ 10 s by default)
#   4. after 2 shots: stop the server, wait ~1 shot, start it again
#   5. the camera keeps shooting while no server runs; the new server connects
#      READ-ONLY and adopts the run; until the run ends the camera log must show
#      zero recmode/playmode/pictmode, setsetting, startstream, shutter or
#      interval pause
#   6. the run completes, the job ends "done" with all shots, the photo count
#      on the card grew by exactly that many
#   7. the connection then completes (events + live view; not verified on the
#      body before this script)
#   8. drive mode back to stopmotion_interval/interval, image count and
#      interval back to what they were
#
# Safety: one command at a time, nothing is retried here. The run stops at the
# first err_busy/err_critical in the camera log, a wedged flag, someone using
# the body, or anything unexpected; after a stop nothing more is sent (the
# settings are only restored when no interval run is going). Never powers the
# camera off, never scans the LAN (--no-scan, fixed --camera-ip), no Bluetooth.
#
# Env: LUMIX_VERIFY_IP (camera address), LUMIX_VERIFY_SERVER_ARGS (replaces the
# server start arguments), LUMIX_VERIFY_SHOTS (6), LUMIX_VERIFY_INTERVAL (10),
# LUMIX_VERIFY_RESTORE_INTERVAL (interval to put back, default 30: the S5 has no
# known safe read for it), LUMIX_VERIFY_RESTORE_COUNT (image count to put back;
# default = getstate interval_remain_num before the run), LUMIX_SERVER /
# LUMIX_REMOTE_HOME / LUMIX_LOG_DIR (as for the CLI), LUMIX_VERIFY_SETTLE_S (120).
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LUMIX="$ROOT/bin/lumix"
PY="$ROOT/.venv/bin/python"
IP="${LUMIX_VERIFY_IP:-192.0.2.28}"
SHOTS="${LUMIX_VERIFY_SHOTS:-6}"
INTERVAL="${LUMIX_VERIFY_INTERVAL:-10}"
RESTORE_INTERVAL="${LUMIX_VERIFY_RESTORE_INTERVAL:-30}"
SETTLE_S="${LUMIX_VERIFY_SETTLE_S:-120}"
if [ -n "${LUMIX_VERIFY_SERVER_ARGS:-}" ]; then
  read -r -a SERVER_ARGS <<< "$LUMIX_VERIFY_SERVER_ARGS"
else
  SERVER_ARGS=(--camera-ip "$IP" --no-scan)
fi
LOGDIR="${LUMIX_LOG_DIR:-$ROOT/logs}"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/verify-interval-restart-$(date +%Y%m%d-%H%M%S).log"

STEP=0
WEDGE_SEEN=0
RESTORE=0          # 1 once the run's settings were changed
ORIG_COUNT=""
CAMLOG=""          # the server's camera command log
CAMLOG_START=0     # its line count when this script started

say()  { printf '%s\n' "$*" | tee -a "$LOG"; }
step() { STEP=$((STEP + 1)); say ""; say "[$STEP] $*"; }
ok()   { say "    ok   $*"; }
info() { say "    ..   $*"; }

cli() {
  local out rc
  out=$("$LUMIX" "$@" 2>&1); rc=$?
  printf '$ lumix %s\n%s\n[exit %d]\n' "$*" "$out" "$rc" >> "$LOG"
  printf '%s' "$out"
  return $rc
}
status_json() { "$LUMIX" status --json 2>>"$LOG"; }
state_json()  { "$LUMIX" state --json 2>>"$LOG"; }
tl_json()     { "$LUMIX" timelapse status --json 2>>"$LOG"; }
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

camlog_path() {
  # newest camera command log the server writes (camera-<date>.log / camera-<id>-<date>.log)
  ls -t "$LOGDIR"/camera-*.log 2>/dev/null | head -1
}
camlog_lines() { [ -n "$CAMLOG" ] && [ -f "$CAMLOG" ] && wc -l < "$CAMLOG" | tr -d ' ' || echo 0; }
# camera log lines after line $1 (cam.cgi requests only)
camlog_since() { [ -n "$CAMLOG" ] && [ -f "$CAMLOG" ] && tail -n +"$(( $1 + 1 ))" "$CAMLOG" | grep 'GET /cam.cgi' || true; }

finish() {
  # settings back only when no interval run is going (the server refuses them
  # during a run anyway, and that is exactly what this script checks)
  if [ "$RESTORE" = 1 ]; then
    RESTORE=0
    local iv
    iv=$(state_json | field interval_status)
    if [ "$iv" = "rec" ] || [ "$iv" = "pause" ] || [ "$iv" = "run" ]; then
      say "    WARN an interval run is still $iv on the camera: settings NOT restored."
      say "         Stop it (lumix timelapse stop) and set drive mode / image count / interval by hand."
      return
    fi
    if [ "$WEDGE_SEEN" = 1 ]; then
      say "    WARN camera wedged: settings NOT restored; power-cycle it, then set them by hand."
      return
    fi
    say ""
    say "Restoring the camera settings"
    local out
    out=$("$LUMIX" set --raw drivemode stopmotion_interval interval 2>&1) \
      && say "    ..   drive mode -> stopmotion_interval/interval" \
      || say "    WARN drive mode not restored: $out"
    if [ -n "$ORIG_COUNT" ]; then
      out=$("$LUMIX" set --raw interval_shoot_num "$ORIG_COUNT" 2>&1) \
        && say "    ..   image count -> $ORIG_COUNT" || say "    WARN image count not restored: $out"
    fi
    out=$("$LUMIX" set --raw interval_shoot_interval "$RESTORE_INTERVAL" 2>&1) \
      && say "    ..   interval -> ${RESTORE_INTERVAL}s" || say "    WARN interval not restored: $out"
    sleep 1.5
    say "    ..   getstate interval_remain_num now $(state_json | field interval_remain_num) (expected ${ORIG_COUNT:-?})"
  fi
}
trap finish EXIT
trap 'say ""; say "interrupted"; exit 130' INT TERM

fail() {
  say "    FAIL $*"
  say ""
  say "VERIFICATION STOPPED at step $STEP. Nothing more is sent to the camera (except the settings"
  say "restore, and only when no interval run is going)."
  if [ "$WEDGE_SEEN" = 1 ]; then
    say "The camera is wedged. Switch it OFF and ON with its power switch. Do not retry commands."
  fi
  say "Log: $LOG   Camera log: ${CAMLOG:-?}   Server log: $LOGDIR/lumix-server.log"
  exit 1
}

# first err_busy / err_critical anywhere in the camera log since the start = stop
check_errs() {
  local bad
  bad=$(camlog_since "$CAMLOG_START" | grep -E '<result>err_(busy|critical)</result>' | head -1)
  if [ -n "$bad" ]; then
    WEDGE_SEEN=1
    fail "camera answered: $bad (before: $1)"
  fi
}

guard() {
  local st
  st=$(status_json) || fail "server not answering (before: $1)"
  if [ "$(field wedged <<< "$st")" = "True" ]; then WEDGE_SEEN=1; fail "camera is WEDGED (before: $1)"; fi
  if [ "$(field body_busy <<< "$st")" = "True" ]; then fail "someone is using the camera body (before: $1)"; fi
  [ "$(field status <<< "$st")" = "ready" ] || \
    fail "camera not ready: $(field status <<< "$st") $(field message <<< "$st") (before: $1)"
  check_errs "$1"
}

wait_ready() {
  local t0 last="" st s m
  t0=$(date +%s)
  while :; do
    st=$(status_json) || fail "server stopped answering"
    s=$(field status <<< "$st"); m=$(field message <<< "$st")
    if [ "$s $m" != "$last" ]; then info "$s: $m"; last="$s $m"; fi
    if [ "$(field wedged <<< "$st")" = "True" ]; then WEDGE_SEEN=1; fail "camera reported WEDGED while connecting"; fi
    [ "$s" = "ready" ] && break
    [ "$s" = "error" ] && fail "server error: $m"
    [ "$s" = "paused" ] && fail "the server does not run this camera: $m"
    [ $(( $(date +%s) - t0 )) -ge "$SETTLE_S" ] && fail "camera not ready after ${SETTLE_S}s ($s $m)"
    sleep 1
  done
}

photo_total() {
  # card listing = playback round trip (normal, verified path); only while idle
  local out t
  out=$(cli files list --kind photo --limit 1 --refresh --json) || fail "files list: $out"
  t=$(field total <<< "$out")
  out=$(cli files "done") || fail "files done: $out"
  printf '%s' "$t"
}

wait_live() {
  local st
  for _ in $(seq 1 30); do
    st=$(status_json)
    [ "$(field cammode <<< "$st")" = "rec" ] && "$PY" -c 'import sys; sys.exit(0 if float(sys.argv[1] or 99) < 2 else 1)' \
      "$(field liveview.age <<< "$st")" && return 0
    sleep 1
  done
  return 1
}

say "Lumix real-camera check: native interval run across a server restart  $(date)"
say "camera $IP, ${SHOTS} shots @ ${INTERVAL}s, server ${LUMIX_SERVER:-http://127.0.0.1:8770}, start args: ${SERVER_ARGS[*]}"
say "log $LOG"

# ------------------------------------------------------------ fresh server
step "Stop the server (only the one in the pidfile) and start it fresh"
out=$(cli server stop) || true
info "$out"
"$LUMIX" status --json >/dev/null 2>&1 && fail "a server still answers (started without a pidfile?); stop it yourself"
out=$(cli server start -- "${SERVER_ARGS[@]}") || fail "server start: $out"
info "$out"
wait_ready
CAMLOG=$(camlog_path)
[ -n "$CAMLOG" ] || fail "no camera command log in $LOGDIR (server started without --log-dir?)"
CAMLOG_START=$(camlog_lines)
st=$(status_json)
[ -z "$(field readonly <<< "$st")" ] || fail "connected read-only ($(field readonly <<< "$st")): the camera is busy; start from an idle camera"
batt=$(field battery <<< "$st")
case "$batt" in 0/*|1/*) fail "battery $batt: charge it first";; esac
guard "preflight"
ok "connected normally: $(field camera.friendlyName <<< "$st"), battery $batt, camera log $CAMLOG"

step "Preflight: no timelapse, no camera interval run, live view up"
case "$(tl_json | field job.state)" in scheduled|running|paused|stopping|importing|assembling)
  fail "a timelapse is active on this camera; stop it first";; esac
s=$(state_json)
iv=$(field interval_status <<< "$s")
case "$iv" in rec|pause|run) fail "the camera already runs an interval run ($iv)";; esac
wait_live || fail "no live view (rec mode) after connecting"
ok "interval_status ${iv:-none}, live view running"

# ------------------------------------------------------------ originals
step "Note the current settings and the photo count"
drive=$(cli get --raw drivemode --json | field values.drivemode)
info "drive mode: ${drive:-?} (restored to stopmotion_interval/interval at the end)"
if [ -n "${LUMIX_VERIFY_RESTORE_COUNT:-}" ]; then ORIG_COUNT="$LUMIX_VERIFY_RESTORE_COUNT"
elif [ "$iv" = "enable" ]; then ORIG_COUNT=$(field interval_remain_num <<< "$s")   # enable: remain = image count
fi
info "image count: ${ORIG_COUNT:-unknown (set LUMIX_VERIFY_RESTORE_COUNT)}; interval restored to ${RESTORE_INTERVAL}s"
guard "photo count"
before=$(photo_total)
[ -n "$before" ] || fail "could not read the photo count"
wait_live || fail "camera did not come back to shooting after the card listing"
guard "start"
ok "$before photos on the card"

# ------------------------------------------------------------ run
step "Start an in-camera run: $SHOTS shots every ${INTERVAL}s"
RESTORE=1
out=$(cli timelapse start --native --interval "$INTERVAL" --count "$SHOTS" --name verify-restart --json) \
  || fail "timelapse start: $out"
for _ in $(seq 1 20); do
  j=$(tl_json)
  [ "$(field job.engine <<< "$j")" = "server" ] && fail "the camera did not start its own run (fell back to server shots): $(field job.message <<< "$j")"
  [ "$(state_json | field interval_status)" = "rec" ] && break
  sleep 1
done
[ "$(state_json | field interval_status)" = "rec" ] || fail "camera interval_status never became rec"
check_errs "run start"
ok "camera runs its interval (interval_status rec)"

step "Wait for 2 shots"
deadline=$(( $(date +%s) + 3 * INTERVAL + 20 ))
while :; do
  rem=$(state_json | field interval_remain_num)
  [ -n "$rem" ] && [ "$rem" -le $((SHOTS - 2)) ] && break
  [ "$(date +%s)" -ge "$deadline" ] && fail "no 2 shots in time (remain $rem)"
  check_errs "waiting for shots"
  sleep 2
done
ok "remain $rem"

# ------------------------------------------------------------ restart
step "Stop the server mid-run (the run must NOT be paused)"
mark=$(camlog_lines)
out=$(cli server stop) || fail "server stop: $out"
info "$out"
pause_sent=$(camlog_since "$mark" | grep -c 'type=interval&value=pause' || true)
[ "${pause_sent:-0}" = "0" ] || fail "the server sent camctrl interval pause on shutdown"
ok "server stopped, no interval pause sent"
info "waiting $((INTERVAL + 3))s without a server (the camera shoots alone)"
sleep $((INTERVAL + 3))

step "Start the server again: it must connect read-only and adopt the run"
mark=$(camlog_lines)
out=$(cli server start -- "${SERVER_ARGS[@]}") || fail "server start: $out"
info "$out"
wait_ready
st=$(status_json)
ro=$(field readonly <<< "$st")
[ -n "$ro" ] || fail "server did not connect read-only (interval: $(field interval.status <<< "$st"))"
ok "read-only connect: $ro"
info "camera: interval $(field interval.status <<< "$st"), $(field interval.remain <<< "$st") left, events subscribed: $(field events_sub <<< "$st")"
[ "$(field events_sub <<< "$st")" = "False" ] || fail "server SUBSCRIBEd during the run"
check_errs "adopt"

step "Run completes on the camera; zero mode switches meanwhile"
deadline=$(( $(date +%s) + SHOTS * INTERVAL + 90 ))
last=""
while :; do
  st=$(status_json) || fail "server stopped answering"
  [ "$(field wedged <<< "$st")" = "True" ] && { WEDGE_SEEN=1; fail "camera WEDGED during the adopted run"; }
  ivs=$(field interval.status <<< "$st"); rem=$(field interval.remain <<< "$st")
  [ "$ivs $rem" != "$last" ] && info "interval $ivs, remain ${rem:-?}, job $(tl_json | field job.state) $(tl_json | field job.shots) shot(s)"
  last="$ivs $rem"
  case "$ivs" in rec|run|pause) ;; *) break;; esac
  [ "$(date +%s)" -ge "$deadline" ] && fail "run not finished in time"
  check_errs "adopted run"
  sleep 2
done
bad=$(camlog_since "$mark" | grep -E 'camcmd&value=(recmode|playmode|pictmode|capture)|mode=setsetting|mode=startstream|type=interval&value=pause' || true)
if [ -n "$bad" ]; then
  say "$bad"
  fail "the server sent mode switches / writes into the running interval run (above)"
fi
ok "run over; camera log since restart has no recmode/playmode/setsetting/startstream/shutter"

step "Job done with every shot"
for _ in $(seq 1 30); do
  j=$(tl_json); [ "$(field job.state <<< "$j")" = "done" ] && break; sleep 1
done
[ "$(field job.state <<< "$j")" = "done" ] || fail "job state $(field job.state <<< "$j"): $(field job.message <<< "$j")"
[ "$(field job.shots <<< "$j")" = "$SHOTS" ] || fail "job shots $(field job.shots <<< "$j"), expected $SHOTS"
ok "done, $SHOTS shots ($(field job.message <<< "$j"))"

step "The connection completes after the run (events + live view; first time on the body)"
for _ in $(seq 1 60); do
  st=$(status_json)
  [ "$(field wedged <<< "$st")" = "True" ] && { WEDGE_SEEN=1; fail "camera WEDGED while completing the connection"; }
  [ -z "$(field readonly <<< "$st")" ] && break
  sleep 1
done
[ -z "$(field readonly <<< "$st")" ] || fail "still read-only 60 s after the run ended"
check_errs "connect completion"
wait_live || fail "no live view after the connection completed"
guard "photo count after"
ok "events subscribed: $(field events_sub <<< "$st"), live view running"

step "Photo count on the card"
after=$(photo_total)
[ -n "$after" ] || fail "could not read the photo count"
wait_live || fail "camera did not come back to shooting after the card listing"
check_errs "photo count"
[ $((after - before)) -eq "$SHOTS" ] || fail "card has $((after - before)) new photos, expected $SHOTS"
ok "$before -> $after photos (+$SHOTS)"

finish
say ""
say "VERIFIED ($STEP steps): the camera kept shooting through the server restart, the server adopted"
say "the run with zero mode switches, the run completed and the settings are back. Log: $LOG"
