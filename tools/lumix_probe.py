#!/usr/bin/env python3
"""Read-only Wi-Fi probe for Panasonic Lumix cameras (written for the DC-S5).

Discovers the camera (SSDP, then a TCP scan of the local /24 and the AP
address 192.168.54.1), fetches the UPnP `ddd` description, walks the
cam.cgi pairing ladder (req_acc, req_acc_a, req_acc_g + req_acc_e variants),
dumps getstate/getinfo, and captures a few UDP live-view datagrams.

It never sends capture, video, poweroff or setsetting commands.

Python 3 stdlib only. Implemented from protocol notes in
docs/protocol-research.md (no third-party code copied).

Usage:
    python3 tools/lumix_probe.py [--ip 192.168.1.x] [--out DIR]
                                 [--wait 180] [--no-stream]
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import errno
import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET

DEVICE_NAME = "LumixRemoteMac"
APP_UUID = str(uuid.uuid5(uuid.NAMESPACE_DNS, "lumixremotemac.local"))
EXCLUDED_HOSTS = set()  # hosts the LAN scan must never probe (add your own)
AP_MODE_IP = "192.168.54.1"
SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_TARGETS = [
    "urn:schemas-upnp-org:device:MediaServer:1",
    "ssdp:all",
    "upnp:rootdevice",
]
STREAM_PORT = 49199
PENDING_MARKERS = ("under_research", "pending", "wait")
# Constants for the req_acc_e "variant B" derivation (protocol notes, sec. 1).
VARIANT_B_CONSTS = [892617780, 808663348, 808529965, 808529200, 942485552,
                    758198320, 809579056, 842018864, 942748980]
VARIANT_B_CONST2 = 4281684038


class Probe:
    def __init__(self, out_dir, wait_s):
        self.out = out_dir
        self.wait_s = wait_s
        os.makedirs(out_dir, exist_ok=True)
        self.log_path = os.path.join(out_dir, "probe.log")
        self._log_lock = threading.Lock()
        self.step = 0
        self.session_id = None
        self.findings = {"errors": [], "local_network_errors": 0}

    # ---------- logging ----------
    def log(self, msg):
        line = "[%s] %s" % (dt.datetime.now().strftime("%H:%M:%S.%f")[:-3], msg)
        with self._log_lock:
            print(line, flush=True)
            with open(self.log_path, "a") as f:
                f.write(line + "\n")

    def save(self, name, data):
        path = os.path.join(self.out, name)
        mode = "wb" if isinstance(data, (bytes, bytearray)) else "w"
        with open(path, mode) as f:
            f.write(data)
        return path

    def save_json(self):
        self.save("findings.json", json.dumps(self.findings, indent=2, default=str))

    # ---------- HTTP ----------
    def http_get(self, host, port, path, headers=None, timeout=4.0, record=True, tag=None):
        """GET and return (status, headers dict, body bytes) or (None, None, error str)."""
        hdrs = {"User-Agent": "LumixRemoteMac/0.1", "Connection": "close"}
        if headers:
            hdrs.update(headers)
        req_text = "GET %s HTTP/1.1\r\nHost: %s:%d\r\n%s" % (
            path, host, port, "".join("%s: %s\r\n" % kv for kv in hdrs.items()))
        try:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
            conn.request("GET", path, headers=hdrs)
            resp = conn.getresponse()
            body = resp.read()
            status = resp.status
            rh = dict(resp.getheaders())
            conn.close()
        except OSError as e:
            if getattr(e, "errno", None) in (errno.EHOSTUNREACH, errno.ENETUNREACH):
                self.findings["local_network_errors"] += 1
            err = "%s: %s" % (type(e).__name__, e)
            if record:
                self._record(tag, req_text, "ERROR " + err)
            return None, None, err
        if record:
            resp_text = "HTTP %d\r\n%s\r\n" % (
                status, "".join("%s: %s\r\n" % kv for kv in rh.items()))
            self._record(tag, req_text, resp_text + body.decode("utf-8", "replace"))
        return status, rh, body

    def _record(self, tag, req, resp):
        self.step += 1
        name = "%03d_%s.txt" % (self.step, re.sub(r"[^A-Za-z0-9_.-]", "_", tag or "req"))
        self.save(name, ">>> REQUEST\n%s\n<<< RESPONSE\n%s\n" % (req, resp))
        short = resp.replace("\r", "").replace("\n", " | ")
        self.log("  %s -> %s" % (req.split("\r\n")[0], short[:300]))

    def cam(self, ip, query, tag, with_session=True, timeout=5.0, record=True):
        headers = {}
        if with_session and self.session_id:
            headers["X-SESSION_ID"] = self.session_id
        status, _, body = self.http_get(ip, 80, "/cam.cgi?" + query, headers=headers,
                                        timeout=timeout, record=record, tag=tag)
        if status is None:
            return None
        return body.decode("utf-8", "replace").strip()

    # ---------- discovery ----------
    def local_ip(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.168.1.1", 9))
            return s.getsockname()[0]
        except OSError:
            return "0.0.0.0"
        finally:
            s.close()

    def ssdp(self, listen_s=4.0):
        self.log("== SSDP discovery")
        found = {}
        raw = []
        my_ip = self.local_ip()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(my_ip))
        except OSError as e:
            self.log("  IP_MULTICAST_IF failed: %s" % e)
        sock.bind((my_ip if my_ip != "0.0.0.0" else "", 0))
        sock.settimeout(0.3)
        for st in SSDP_TARGETS:
            msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
                   'MAN: "ssdp:discover"\r\nMX: 2\r\nST: %s\r\n\r\n' % st)
            for _ in range(2):
                try:
                    sock.sendto(msg.encode(), SSDP_ADDR)
                except OSError as e:
                    self.log("  SSDP send failed (%s): %s" % (st, e))
                    self.findings["errors"].append("ssdp send: %s" % e)
                    if getattr(e, "errno", None) in (errno.EHOSTUNREACH, errno.ENETUNREACH):
                        self.findings["local_network_errors"] += 1
        end = time.time() + listen_s
        while time.time() < end:
            try:
                data, addr = sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError as e:
                self.log("  SSDP recv error: %s" % e)
                break
            text = data.decode("utf-8", "replace")
            raw.append("### from %s\n%s" % (addr[0], text))
            hdr = {}
            for line in text.split("\r\n")[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    hdr[k.strip().lower()] = v.strip()
            loc = hdr.get("location", "")
            server = hdr.get("server", "")
            if "/lumix/" in loc.lower() or "panasonic" in server.lower():
                found[addr[0]] = {"location": loc, "server": server, "st": hdr.get("st")}
        sock.close()
        self.save("ssdp_raw.txt", "\n".join(raw))
        self.log("  SSDP: %d responses total, %d Lumix-looking: %s"
                 % (len(raw), len(found), list(found)))
        return found

    def scan(self, cidr="192.168.1.0/24"):
        self.log("== TCP scan of %s (ports 60606, 80) + %s" % (cidr, AP_MODE_IP))
        me = self.local_ip()
        hosts = [str(h) for h in ipaddress.ip_network(cidr).hosts()]
        hosts = [h for h in hosts if h not in EXCLUDED_HOSTS and h != me]
        hosts.append(AP_MODE_IP)
        unreach = []

        def check(ip):
            for port in (60606, 80):
                s = socket.socket()
                s.settimeout(0.8)
                try:
                    rc = s.connect_ex((ip, port))
                finally:
                    s.close()
                if rc in (errno.EHOSTUNREACH, errno.ENETUNREACH):
                    unreach.append((ip, port, rc))
                if rc != 0:
                    continue
                path = "/Lumix/Server0/ddd" if port == 60606 else "/cam.cgi?mode=getstate"
                status, hdrs, body = self.http_get(ip, port, path, timeout=2.0, record=False)
                server = (hdrs or {}).get("Server", "") if hdrs else ""
                if "panasonic" in server.lower() or (body and b"LUMIX" in body.upper()
                                                       and port == 60606 and status == 200):
                    return ip, port, server
            return None

        hits = []
        with cf.ThreadPoolExecutor(max_workers=64) as ex:
            for r in ex.map(check, hosts):
                if r:
                    hits.append(r)
        self.findings["scan_unreachable_count"] = len(unreach)
        if unreach:
            self.log("  %d EHOSTUNREACH/ENETUNREACH results (sample %s)"
                     % (len(unreach), unreach[:5]))
            self.findings["local_network_errors"] += len(unreach)
        self.log("  scan hits: %s" % hits)
        return hits

    def fetch_ddd(self, ip, location=None):
        self.log("== fetch ddd")
        url = location or "http://%s:60606/Lumix/Server0/ddd" % ip
        u = urllib.parse.urlparse(url)
        status, _, body = self.http_get(u.hostname, u.port or 80, u.path or "/", tag="ddd",
                                        timeout=12.0)
        if status != 200:
            return {}
        self.save("ddd.xml", body)
        info = {}
        try:
            root = ET.fromstring(body)
            for el in root.iter():
                tag = el.tag.split("}")[-1]
                if tag in ("friendlyName", "modelName", "modelNumber", "modelDescription",
                           "serialNumber", "UDN", "manufacturer", "X_FirmVersion",
                           "presentationURL") and el.text:
                    info.setdefault(tag, el.text.strip())
        except ET.ParseError as e:
            self.log("  ddd parse error: %s" % e)
        self.log("  ddd info: %s" % info)
        return info

    # ---------- handshake ----------
    @staticmethod
    def classify(reply):
        if reply is None:
            return "neterr"
        r = reply.strip().lower()
        if any(m in r for m in PENDING_MARKERS):
            return "pending"
        if r.startswith("ok") or "<result>ok</result>" in r:
            return "ok"
        return "err"

    def poll_until(self, ip, query, tag, with_session=False):
        """Send query; while camera says pending, retry every 1 s up to wait_s."""
        deadline = time.time() + self.wait_s
        n = 0
        last = None
        while True:
            n += 1
            last = self.cam(ip, query, "%s_try%d" % (tag, n), with_session=with_session,
                            record=(n <= 3 or n % 10 == 0))
            kind = self.classify(last)
            if kind != "pending":
                if n > 3:
                    self.log("  %s final reply after %d tries: %r" % (tag, n, last))
                    self._record("%s_final" % tag, query, str(last))
                return kind, last, n
            if n == 1:
                self.log("  >>> camera says pending: approve on the camera screen now "
                         "(waiting up to %ds)" % self.wait_s)
            if time.time() > deadline:
                return "timeout", last, n
            time.sleep(1.0)

    def parse_ok(self, reply):
        parts = [p.strip() for p in reply.strip().split(",")]
        sid = None
        if len(parts) >= 5:
            sid = parts[4]
        return parts, sid

    def handshake(self, ip, udn):
        name = DEVICE_NAME
        qn = urllib.parse.quote(name)
        attempts = []
        self.log("== handshake ladder (app uuid %s, name %s)" % (APP_UUID, name))

        def done(variant, query, reply, sid):
            self.session_id = sid
            self.findings["handshake"] = {"variant": variant, "query": query,
                                          "reply": reply, "session_id": sid}
            self.log("  HANDSHAKE OK via %s, session_id=%r" % (variant, sid))
            return True

        # 1/2: legacy req_acc and req_acc_a
        for t in ("req_acc", "req_acc_a"):
            q = "mode=accctrl&type=%s&value=%s&value2=%s" % (t, APP_UUID, qn)
            kind, rep, n = self.poll_until(ip, q, t)
            attempts.append({"variant": t, "query": q, "result": kind, "reply": rep, "tries": n})
            if kind == "ok":
                _, sid = self.parse_ok(rep)
                self.findings["handshake_attempts"] = attempts
                return done(t, q, rep, sid)

        # 3: req_acc_g nonce + req_acc_e variants
        udn_bare = (udn or "").replace("uuid:", "").strip()
        name_hex = name.encode().hex()

        def get_nonce(tag):
            rep = self.cam(ip, "mode=accctrl&type=req_acc_g", tag, with_session=False)
            attempts.append({"variant": tag, "query": "req_acc_g", "reply": rep})
            if rep and rep.lower().startswith("ok,"):
                return rep.split(",")[1].strip()
            return None

        def variant_b(nonce_hex):
            nonce = struct.unpack("<I", bytes.fromhex(nonce_hex.zfill(8)[-8:]))[0]
            v1 = b"".join(struct.pack(">I", (nonce ^ c) & 0xFFFFFFFF) for c in VARIANT_B_CONSTS)
            v2 = struct.pack(">I", (nonce ^ VARIANT_B_CONST2) & 0xFFFFFFFF)
            return v1.hex(), v2.hex()

        candidates = []
        if udn_bare:
            candidates.append(("req_acc_e_A_hexudn", lambda n: (udn_bare.encode().hex(), name_hex)))
            candidates.append(("req_acc_e_A_rawudn", lambda n: (udn_bare.replace("-", ""), name_hex)))
        candidates.append(("req_acc_e_B_hashwifi", variant_b))
        candidates.append(("req_acc_e_A_hexappuuid", lambda n: (APP_UUID.encode().hex(), name_hex)))
        candidates.append(("req_acc_e_md5",
                           lambda n: (hashlib.md5(APP_UUID.encode()).hexdigest(),
                                      hashlib.md5(name.encode()).hexdigest())))

        for label, fn in candidates:
            nonce = get_nonce("req_acc_g_for_" + label)
            if not nonce:
                self.log("  req_acc_g gave no nonce; skipping %s" % label)
                continue
            v1, v2 = fn(nonce)
            q = "mode=accctrl&type=req_acc_e&value=%s&value2=%s" % (v1, v2)
            kind, rep, n = self.poll_until(ip, q, label)
            entry = {"variant": label, "nonce": nonce, "query": q, "result": kind,
                     "reply": rep, "tries": n}
            attempts.append(entry)
            if kind == "ok":
                parts, sid = self.parse_ok(rep)
                if not sid:
                    # Some bodies answer ok with 4 fields first; ask again for the id.
                    kind2, rep2, n2 = self.poll_until(ip, q, label + "_second")
                    entry["second_reply"] = rep2
                    if kind2 == "ok":
                        _, sid = self.parse_ok(rep2)
                        rep = rep2
                self.findings["handshake_attempts"] = attempts
                return done(label, q, rep, sid)
        self.findings["handshake_attempts"] = attempts
        self.log("  every handshake variant failed")
        return False

    # ---------- post-session dumps ----------
    def dumps(self, ip):
        self.log("== read-only dumps")
        out = {}
        for tag, q in [
            ("getstate_before_recmode", "mode=getstate"),
            ("set_device_name_skipped", None),
            ("recmode", "mode=camcmd&value=recmode"),
            ("getstate", "mode=getstate"),
            ("getinfo_capability", "mode=getinfo&type=capability"),
            ("getinfo_allmenu", "mode=getinfo&type=allmenu"),
            ("getinfo_curmenu", "mode=getinfo&type=curmenu"),
            ("getinfo_lens", "mode=getinfo&type=lens"),
            ("getsetting_touch_type", "mode=getsetting&type=touch_type"),
            ("getsetting_focusmode", "mode=getsetting&type=focusmode"),
        ]:
            if q is None:
                continue
            rep = self.cam(ip, q, tag, timeout=10.0)
            if tag == "recmode":
                # Camera can answer err_critical right after pairing / from play
                # mode; retry a few times before giving up.
                for n in range(2, 9):
                    if rep and "<result>ok</result>" in rep:
                        break
                    time.sleep(1.5)
                    rep = self.cam(ip, q, "recmode_retry%d" % n, timeout=10.0)
                self.findings["recmode_reply"] = rep
            out[tag] = (rep or "")[:200]
            if rep:
                self.save(tag + ".xml", rep)
            if tag == "recmode":
                time.sleep(1.0)
        # Is X-SESSION_ID required? Compare getstate without the header.
        if self.session_id:
            rep = self.cam(ip, "mode=getstate", "getstate_without_session_header",
                           with_session=False)
            out["getstate_without_session_header"] = (rep or "")[:200]
            self.findings["getstate_without_session_header"] = rep
        self.findings["dumps"] = out

    def live_view(self, ip, want=20, max_s=20.0):
        self.log("== live view on UDP %d" % STREAM_PORT)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        sock.bind(("", STREAM_PORT))
        sock.settimeout(1.0)
        stop = threading.Event()

        def keepalive():
            while not stop.is_set():
                self.cam(ip, "mode=getstate", "keepalive", record=False, timeout=3.0)
                stop.wait(1.0)

        rep = self.cam(ip, "mode=startstream&value=%d" % STREAM_PORT, "startstream")
        ka = threading.Thread(target=keepalive, daemon=True)
        ka.start()
        packets = []
        senders = set()
        end = time.time() + max_s
        while len(packets) < want and time.time() < end:
            try:
                data, addr = sock.recvfrom(65536)
            except socket.timeout:
                continue
            packets.append(data)
            senders.add(addr)
        stop.set()
        ka.join(timeout=3)
        self.cam(ip, "mode=stopstream", "stopstream")
        sock.close()

        lv = {"startstream_reply": rep, "datagrams": len(packets),
              "senders": sorted("%s:%d" % a for a in senders),
              "sizes": [len(p) for p in packets]}
        dump_lines = []
        jpeg_info = []
        for i, p in enumerate(packets):
            soi = p.find(b"\xff\xd8\xff")
            eoi = p.rfind(b"\xff\xd9")
            hdr_be30 = struct.unpack(">H", p[30:32])[0] if len(p) >= 32 else None
            info = {"i": i, "size": len(p), "soi": soi, "eoi": eoi,
                    "be16_at_30": hdr_be30,
                    "be16_at_30_plus_32": (hdr_be30 + 32) if hdr_be30 is not None else None}
            if soi >= 0 and eoi > soi:
                jpg = p[soi:eoi + 2]
                info["jpeg_len"] = len(jpg)
                info["dims"] = jpeg_dims(jpg)
                if i < 5:
                    self.save("frame_%02d.jpg" % i, jpg)
            jpeg_info.append(info)
            if i < 3:
                dump_lines.append("--- packet %d (%d bytes, SOI at %d)\n%s"
                                  % (i, len(p), soi, hexdump(p[:max(64, soi + 4 if soi > 0 else 64)])))
        for i, p in enumerate(packets[:5]):
            self.save("packet_%02d.bin" % i, p)
        self.save("liveview_headers.txt", "\n".join(dump_lines))
        lv["frames"] = jpeg_info
        self.findings["live_view"] = lv
        self.log("  got %d datagrams, sizes %s" % (len(packets), lv["sizes"][:10]))
        if jpeg_info:
            self.log("  first frame: %s" % jpeg_info[0])


def jpeg_dims(jpg):
    i = 2
    while i + 9 < len(jpg):
        if jpg[i] != 0xFF:
            i += 1
            continue
        marker = jpg[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", jpg[i + 5:i + 9])
            return "%dx%d" % (w, h)
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seglen = struct.unpack(">H", jpg[i + 2:i + 4])[0]
        i += 2 + seglen
    return None


def hexdump(b):
    lines = []
    for off in range(0, len(b), 16):
        chunk = b[off:off + 16]
        lines.append("%04x  %-48s %s" % (off, " ".join("%02x" % c for c in chunk),
                                         "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ip", help="skip discovery and use this camera IP")
    ap.add_argument("--out", help="output dir (default probe-results/<timestamp>)")
    ap.add_argument("--wait", type=int, default=180, help="seconds to wait on pending replies")
    ap.add_argument("--no-stream", action="store_true")
    args = ap.parse_args()

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = args.out or os.path.join(repo, "probe-results",
                                   dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    p = Probe(out, args.wait)
    p.log("output dir %s; local ip %s" % (out, p.local_ip()))

    ip, location = args.ip, None
    if ip and ip in EXCLUDED_HOSTS:
        sys.exit("refusing to probe excluded host %s" % ip)
    if not ip:
        found = p.ssdp()
        p.findings["ssdp_hits"] = found
        if found:
            ip = next(iter(found))
            location = found[ip]["location"]
            p.findings["discovery"] = "ssdp"
        else:
            hits = p.scan()
            p.findings["scan_hits"] = hits
            if hits:
                ip = hits[0][0]
                p.findings["discovery"] = "tcp-scan port %d" % hits[0][1]
    if not ip:
        p.log("!! camera not found (SSDP + scan). Local-network errors: %d"
              % p.findings["local_network_errors"])
        p.findings["result"] = "not_found"
        p.save_json()
        return 2
    if ip in EXCLUDED_HOSTS:
        sys.exit("refusing excluded host")
    p.findings["camera_ip"] = ip
    p.log("camera at %s" % ip)

    ddd = p.fetch_ddd(ip, location)
    p.findings["ddd"] = ddd
    p.save_json()

    if not p.handshake(ip, ddd.get("UDN")):
        p.findings["result"] = "handshake_failed"
        p.save_json()
        return 3
    p.save_json()
    p.dumps(ip)
    p.save_json()
    if not args.no_stream:
        p.live_view(ip)
    p.findings["result"] = "ok"
    p.save_json()
    p.log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
