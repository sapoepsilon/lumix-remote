#!/usr/bin/env bash
# Real-camera check (DC-S5) for the 2026-10-12 16:46-16:52 wedge fix: a
# server-engine time-lapse must not fight the camera over its mode.
#
#   LUMIX_VERIFY_IP=<camera ip> scripts/real-verify-server-timelapse.sh
#
# Run it only after a power cycle (switch OFF and ON), on a healthy camera with
# a charged battery, nothing on its screen but the shooting view.
#
# Steps:
#   1. restart the server fresh (new code), camera connected normally
#   2. preflight: not wedged, not read-only, no timelapse, no interval run,
#      battery >= 2/5, live view up; note drive mode and the photo count
#   3. server-engine time-lapse, 6 shots @ 15 s (no import)
#   4. while it runs, the camera log must show: zero err_busy / err_critical,
#      zero refused startstream, no mode switch the camera did not ask for
#      (recmode/playmode with no mod_Play before it), and if the camera did
#      drop to playback on its own (the 16:46 fault): every recmode >= 20 s
#      after the drop and at most 2 (the job then pauses itself, which also
#      fails this check, with the reason)
#   5. job done with 6 shots, the card has exactly 6 more photos
#   6. drive mode back to what it was (the job restores it; checked, and set
#      again if it is not)
#
# Safety: one command at a time, nothing is retried here. The run stops at the
# first err_busy/err_critical in the camera log, a wedged flag, someone using
# the body, a paused job or anything unexpected; after a stop the running
# time-lapse is stopped and nothing else is sent. Never powers the camera off,
# never scans the LAN (--no-scan, fixed --camera-ip), no Bluetooth.
#
# Env: LUMIX_VERIFY_IP (camera address), LUMIX_VERIFY_SERVER_ARGS (replaces the
# server start arguments), LUMIX_VERIFY_SHOTS (6), LUMIX_VERIFY_INTERVAL (15),
# LUMIX_SERVER / LUMIX_REMOTE_HOME / LUMIX_LOG_DIR (as for the CLI),
# LUMIX_VERIFY_SETTLE_S (120).
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LUMIX="$ROOT/bin/lumix"
PY="$ROOT/.venv/bin/python"
IP="${LUMIX_VERIFY_IP:-192.0.2.28}"
SHOTS="${LUMIX_VERIFY_SHOTS:-6}"
INTERVAL="${LUMIX_VERIFY_INTERVAL:-15}"
SETTLE_S="${LUMIX_VERIFY_SETTLE_S:-120}"
if [ -n "${LUMIX_VERIFY_SERVER_ARGS:-}" ]; then
  read -r -a SERVER_ARGS <<< "$LUMIX_VERIFY_SERVER_ARGS"
else
  SERVER_ARGS=(--camera-ip "$IP" --no-scan)
fi
LOGDIR="${LUMIX_LOG_DIR:-$ROOT/logs}"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/verify-server-timelapse-$(date +%Y%m%d-%H%M%S).log"

STEP=0
WEDGE_SEEN=0
JOB_STARTED=0
ORIG_DRIVE=""      # drive mode before the run ("value value2")
CAMLOG=""
CAMLOG_START=0

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

camlog_path() { ls -t "$LOGDIR"/camera-*.log 2>/dev/null | head -1; }
camlog_lines() { [ -n "$CAMLOG" ] && [ -f "$CAMLOG" ] && wc -l < "$CAMLOG" | tr -d ' ' || echo 0; }
camlog_since() { [ -n "$CAMLOG" ] && [ -f "$CAMLOG" ] && tail -n +"$(( $1 + 1 ))" "$CAMLOG" || true; }

finish() {
  if [ "$JOB_STARTED" = 1 ]; then
    JOB_STARTED=0
    case "$(tl_json | field job.state)" in scheduled|running|paused)
      if [ "$WEDGE_SEEN" = 1 ]; then
        say "    WARN camera wedged: the time-lapse job is left as it is (stop it after the power cycle)."
      else
        cli timelapse stop >/dev/null && info "time-lapse stopped"
      fi;;
    esac
  fi
  if [ -n "$ORIG_DRIVE" ] && [ "$WEDGE_SEEN" = 0 ]; then
    local now v1 v2
    sleep 1
    now=$(cli get --raw drivemode --json)
    v1=$(field values.drivemode <<< "$now")
    if [ -n "$v1" ] && [ "$v1" != "${ORIG_DRIVE%% *}" ]; then
      read -r v1 v2 <<< "$ORIG_DRIVE"
      out=$("$LUMIX" set --raw drivemode $v1 ${v2:-} 2>&1) \
        && say "    ..   drive mode -> $ORIG_DRIVE (restored)" \
        || say "    WARN drive mode not restored: $out (set it to $ORIG_DRIVE by hand)"
    else
      info "drive mode is ${v1:-?} (was ${ORIG_DRIVE%% *})"
    fi
    ORIG_DRIVE=""
  fi
}
trap finish EXIT
trap 'say ""; say "interrupted"; exit 130' INT TERM

fail() {
  say "    FAIL $*"
  say ""
  say "VERIFICATION STOPPED at step $STEP."
  if [ "$WEDGE_SEEN" = 1 ]; then
    say "The camera is wedged. Switch it OFF and ON with its power switch. Do not retry commands."
  fi
  say "Log: $LOG   Camera log: ${CAMLOG:-?}"
  exit 1
}

check_errs() {
  local bad
  bad=$(camlog_since "$CAMLOG_START" | grep 'GET /cam.cgi' | grep -E '<result>err_(busy|critical)</result>' | head -1)
  if [ -n "$bad" ]; then
    WEDGE_SEEN=1
    fail "camera answered: $bad (during: $1)"
  fi
}

guard() {
  local st
  st=$(status_json) || fail "server not answering ($1)"
  if [ "$(field wedged <<< "$st")" = "True" ]; then WEDGE_SEEN=1; fail "camera is WEDGED ($1)"; fi
  if [ "$(field body_busy <<< "$st")" = "True" ]; then fail "someone is using the camera body ($1)"; fi
  [ "$(field status <<< "$st")" = "ready" ] || \
    fail "camera not ready: $(field status <<< "$st") $(field message <<< "$st") ($1)"
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

photo_total() {
  local out t
  out=$(cli files list --kind photo --limit 1 --refresh --json) || fail "files list: $out"
  t=$(field total <<< "$out")
  out=$(cli files "done") || fail "files done: $out"
  printf '%s' "$t"
}

# Mode-switch discipline in the camera log after line $1: prints a verdict line
# and exits 1 on a fight. Timestamps are [HH:MM:SS.mmm] (same day).
analyse_modes() {
  camlog_since "$1" | "$PY" -c '
import re, sys
def sec(t):
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)
switches, plays, refused, stream_bad = [], [], [], []
for line in sys.stdin:
    m = re.match(r"\[(\d\d:\d\d:\d\d\.\d+)\] (.*)", line)
    if not m:
        continue
    t, rest = sec(m.group(1)), m.group(2)
    if "NOTIFY X_Panasonic_Cam_Sync=mod_Play" in rest:
        plays.append(t)
    elif rest.startswith("NOT SENT"):
        refused.append(rest[:120])
    elif re.search(r"camcmd&value=(recmode|playmode|pictmode)", rest) and rest.startswith("GET"):
        switches.append((t, rest.split("value=")[1].split(" ")[0]))
    elif "mode=startstream" in rest and rest.startswith("GET") and "<result>ok</result>" not in rest:
        stream_bad.append(rest[:120])
bad = []
for t, what in switches:
    before = [p for p in plays if p <= t]
    if not before:
        bad.append("%s with no camera drop to playback before it" % what)
    elif t - before[-1] < 20:
        bad.append("%s %.1f s after the camera dropped to playback (< 20 s: fighting it)" % (what, t - before[-1]))
if len(switches) > 2:
    bad.append("%d mode switches (max 2 in a run)" % len(switches))
bad += ["startstream refused: " + s for s in stream_bad]
bad += ["withheld locally: " + s for s in refused]
print("mode switches %d, camera drops to playback %d, refused startstream %d, withheld %d"
      % (len(switches), len(plays), len(stream_bad), len(refused)))
for b in bad:
    print("  - " + b)
sys.exit(1 if bad else 0)
'
}

say "Lumix real-camera check: server-engine time-lapse without mode fights  $(date)"
say "camera $IP, ${SHOTS} shots @ ${INTERVAL}s, server ${LUMIX_SERVER:-http://127.0.0.1:8770}, start args: ${SERVER_ARGS[*]}"
say "log $LOG"

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
[ -z "$(field readonly <<< "$st")" ] || fail "connected read-only ($(field readonly <<< "$st")): the camera is busy"
batt=$(field battery <<< "$st")
case "$batt" in 0/*|1/*) fail "battery $batt: charge it first";; esac
guard "preflight"
ok "connected: $(field camera.friendlyName <<< "$st"), battery $batt, camera log $CAMLOG"

step "Preflight: no timelapse, no camera interval run, live view up"
case "$(tl_json | field job.state)" in scheduled|running|paused|stopping|importing|assembling)
  fail "a timelapse is active on this camera; stop it first";; esac
iv=$(state_json | field interval_status)
case "$iv" in rec|pause|run) fail "the camera runs an interval run ($iv)";; esac
wait_live || fail "no live view (rec mode) after connecting"
ok "interval_status ${iv:-none}, live view running"

step "Note the drive mode and the photo count"
ORIG_DRIVE=$(cli get --raw drivemode --json | field values.drivemode)
# getsetting gives no value2; the S5's Time Lapse drive is stopmotion_interval/interval
[ "$ORIG_DRIVE" = "stopmotion_interval" ] && ORIG_DRIVE="stopmotion_interval interval"
info "drive mode: ${ORIG_DRIVE:-?} (the job switches to single and puts it back)"
guard "photo count"
before=$(photo_total)
[ -n "$before" ] || fail "could not read the photo count"
wait_live || fail "camera did not come back to shooting after the card listing"
guard "start"
ok "$before photos on the card"

step "Server-engine time-lapse: $SHOTS shots every ${INTERVAL}s"
mark=$(camlog_lines)
CAMLOG_START=$mark
JOB_STARTED=1
out=$(cli timelapse start --interval "$INTERVAL" --count "$SHOTS" --name verify-server --json) \
  || fail "timelapse start: $out"
[ "$(tl_json | field job.engine)" = "server" ] || fail "not the server engine: $(tl_json | field job.engine)"
ok "started"

step "Run: zero err_busy, zero fights"
deadline=$(( $(date +%s) + SHOTS * INTERVAL + 120 ))
last=""
while :; do
  st=$(status_json) || fail "server stopped answering"
  [ "$(field wedged <<< "$st")" = "True" ] && { WEDGE_SEEN=1; fail "camera WEDGED during the run"; }
  j=$(tl_json)
  js=$(field job.state <<< "$j")
  line="job $js, $(field job.shots <<< "$j") shot(s), skipped $(field job.skipped <<< "$j"), cammode $(field cammode <<< "$st")"
  [ "$line" != "$last" ] && info "$line"
  last="$line"
  check_errs "the run"
  case "$js" in
    done) break;;
    paused) fail "the job paused itself: $(field job.message <<< "$j")";;
    failed|stopped) fail "job $js: $(field job.errors <<< "$j")";;
  esac
  [ "$(date +%s)" -ge "$deadline" ] && fail "run not finished in time"
  sleep 2
done
JOB_STARTED=0
verdict=$(analyse_modes "$mark"); rc=$?
say "    ..   $verdict"
[ $rc = 0 ] || fail "mode discipline (above)"
j=$(tl_json)
[ "$(field job.shots <<< "$j")" = "$SHOTS" ] || fail "job shots $(field job.shots <<< "$j"), expected $SHOTS"
[ "$(field job.failed <<< "$j")" = "0" ] || fail "failed shots: $(field job.errors <<< "$j")"
info "skipped slots: $(field job.skipped <<< "$j") (missed $(field job.missed <<< "$j"))"
ok "done: $SHOTS shots, no err_busy, no mode fights"

step "Photo count on the card"
wait_live || fail "no live view after the run"
guard "photo count after"
after=$(photo_total)
[ -n "$after" ] || fail "could not read the photo count"
wait_live || fail "camera did not come back to shooting after the card listing"
check_errs "photo count"
[ $((after - before)) -eq "$SHOTS" ] || fail "card has $((after - before)) new photos, expected $SHOTS"
ok "$before -> $after photos (+$SHOTS)"

step "Settings back"
finish
say ""
say "VERIFIED ($STEP steps): $SHOTS server-engine shots, zero err_busy, no mode switch fights, drive"
say "mode restored. Log: $LOG"
