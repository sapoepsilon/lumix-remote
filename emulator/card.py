"""Synthetic SD card: DIDL-Lite Browse results and :50001 file bodies.

Object ids, titles, URL prefixes (DO original, DT thumbnail, DL large preview),
protocolInfo strings, the fixed fake ``size`` attributes on DT/DL (5000 and
100000, as the real S5 reports them) and the time-lapse containers
(``<id>DIR`` with ``pana:X_RecGroupType=Interval``) follow the recorded S5
Browse replies in tests/fixtures/s5_cds_*.xml.

File bodies are generated on demand and support random access (Range):
a real JPEG (or a container header) followed by a deterministic filler, the
way Panasonic originals carry trailing MPF preview data after the first EOI.
"""

from __future__ import annotations

import datetime as dt
import struct
import zlib
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

from .jpeg import still_jpeg

PI_ORG = ("http-get:*:image/jpeg:DLNA.ORG_PN=JPEG_LRG;DLNA.ORG_OP=01;DLNA.ORG_CI=0;"
          "DLNA.ORG_FLAGS=00900000000000000000000000000000;PANASONIC.COM_PN=CAM_ORG")
PI_TN = ("http-get:*:image/jpeg:DLNA.ORG_PN=JPEG_TN;DLNA.ORG_OP=01;DLNA.ORG_CI=1;"
         "DLNA.ORG_FLAGS=00900000000000000000000000000000;PANASONIC.COM_PN=CAM_TN")
PI_MED = ("http-get:*:image/jpeg:DLNA.ORG_PN=JPEG_MED;DLNA.ORG_OP=01;DLNA.ORG_CI=1;"
          "DLNA.ORG_FLAGS=00900000000000000000000000000000;PANASONIC.COM_PN=CAM_LRGTN")
PI_SM = ("http-get:*:image/jpeg:DLNA.ORG_PN=JPEG_SM;DLNA.ORG_OP=01;DLNA.ORG_CI=1;"
         "DLNA.ORG_FLAGS=00900000000000000000000000000000;PANASONIC.COM_PN=CAM_LRGTN")
# RAW+JPEG: the capability lists CAM_RAW_JPG; the exact S5 <res> for the .RW2 was
# never recorded, so this one is a best guess.
PI_RAW = "http-get:*:application/octet-stream:DLNA.ORG_OP=01;PANASONIC.COM_PN=CAM_RAW_JPG"
PI_MP4 = "http-get:*:application/octet-stream:DLNA.ORG_OP=01;PANASONIC.COM_PN=CAM_AVC_MP4_4K_ORG"
PI_MOV = "http-get:*:application/octet-stream:DLNA.ORG_OP=01;PANASONIC.COM_PN=CAM_AVC_MOV_ORG"

DIDL_OPEN = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
             'xmlns:dc="http://purl.org/dc/elements/1.1/" '
             'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
             'xmlns:pana="urn:schemas-panasonic-com:pana">')

SOAP_RESPONSE = (
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">\n  <s:Body>\n'
    '    <u:BrowseResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">\n'
    '      <Result>%s</Result>\n      <NumberReturned>%d</NumberReturned>\n'
    '      <TotalMatches>%d</TotalMatches>\n      <UpdateID>%d</UpdateID>\n'
    '    </u:BrowseResponse>\n  </s:Body>\n</s:Envelope>\n')

SOAP_FAULT = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><s:Fault>'
    '<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>'
    '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0"><errorCode>%d</errorCode>'
    '<errorDescription>%s</errorDescription></UPnPError></detail></s:Fault></s:Body></s:Envelope>\n')

THUMB_ATTR = 5000        # the S5 reports these fixed sizes for DT/DL, not the real ones
PREVIEW_ATTR = 100000


@dataclass
class Entry:
    object_id: str
    parent_id: str
    title: str
    kind: str                       # photo | raw | mp4 | mov | group
    date: Optional[str]
    size: int = 0                   # original size
    raw_size: int = 0
    duration: str = ""
    children: List["Entry"] = field(default_factory=list)
    group_type: str = ""

    @property
    def is_container(self) -> bool:
        return self.kind == "group"


def _q(s: str) -> str:
    return escape(s, {'"': "&quot;"})


class Card:
    def __init__(self, base_date: str = "2026-10-01T09:00:00", photos: int = 4,
                 group_size: int = 120, seed: int = 1):
        self.seed = seed
        self.update_id = 1
        self.root: List[Entry] = []
        self.by_id: Dict[str, Entry] = {}
        self.folder = 100
        self.number = 0
        self._t = dt.datetime.fromisoformat(base_date)
        self._bodies: "OrderedDict[str, bytes]" = OrderedDict()
        # a few stills (one RAW+JPEG), an MP4, a MOV (no date, like the S5), a time-lapse group
        for i in range(photos):
            self.add_photo(raw=(i == 1))
        self.add_video("mp4", seconds=6)
        self.add_video("mov", seconds=2)
        if group_size:
            g = self.start_group("Interval")
            for _ in range(group_size):
                self.add_photo(group=g, step_s=30)
        self.add_photo()

    # ------------------------------------------------------------ building
    def _next_id(self) -> Tuple[str, str]:
        self.number += 1
        if self.number > 9999:
            self.number, self.folder = 1, self.folder + 1
        oid = "1%03d%04d" % (self.folder, self.number)
        return oid, "%03d-%04d" % (self.folder, self.number)

    def _stamp(self, step_s: int) -> str:
        self._t += dt.timedelta(seconds=step_s)
        return self._t.strftime("%Y-%m-%dT%H:%M:%S")

    def _size(self, oid: str, lo: int, hi: int) -> int:
        return lo + zlib.crc32(oid.encode()) % (hi - lo)

    def add_photo(self, raw: bool = False, group: Optional[Entry] = None, step_s: int = 97,
                  now: bool = False) -> Entry:
        oid, title = self._next_id()
        date = (dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S") if now else self._stamp(step_s))
        e = Entry(oid, group.object_id if group else "0", title, "raw" if raw else "photo", date,
                  size=self._size(oid, 900_000, 1_600_000),
                  raw_size=self._size(oid + "r", 2_000_000, 3_000_000) if raw else 0)
        self._add(e, group)
        return e

    def add_video(self, fmt: str = "mp4", seconds: int = 6, now: bool = False) -> Entry:
        oid, title = self._next_id()
        date = None
        if fmt == "mp4":
            date = dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S") if now else self._stamp(300)
        e = Entry(oid, "0", title, fmt, date, size=self._size(oid, 3_000_000, 5_000_000),
                  duration="0:00:%02d" % max(1, min(59, seconds)))
        self._add(e, None)
        return e

    def start_group(self, group_type: str = "Interval") -> Entry:
        # The container id is its first child's id + "DIR"; filled in by the first add.
        g = Entry("", "0", "", "group", None, group_type=group_type)
        return g

    def _add(self, e: Entry, group: Optional[Entry]) -> None:
        self.by_id[e.object_id] = e
        if group is None:
            self.root.append(e)
        else:
            if not group.children:
                group.object_id = e.object_id + "DIR"
                group.title = e.title
                self.by_id[group.object_id] = group
                self.root.append(group)
            e.parent_id = group.object_id
            group.children.append(e)
        self.update_id += 1

    # ------------------------------------------------------------- queries
    def counts(self) -> Tuple[int, int]:
        """(total files incl. group members, top-level entries)."""
        total = sum(len(e.children) if e.is_container else 1 for e in self.root)
        return total, len(self.root)

    def children_of(self, object_id: str) -> Optional[List[Entry]]:
        if object_id == "0":
            return self.root
        e = self.by_id.get(object_id)
        if e is None or not e.is_container:
            return None
        return e.children

    # ---------------------------------------------------------------- DIDL
    def didl_entry(self, e: Entry, base: str) -> str:
        if e.is_container:
            first = e.children[0].object_id if e.children else e.object_id[:-3]
            return ('<container id="%s" parentID="0" restricted="0" childCount="%d">'
                    '<dc:title>%s</dc:title><upnp:writeStatus>WRITABLE</upnp:writeStatus>'
                    '<upnp:class>object.container</upnp:class>'
                    '<pana:X_RecGroupType>%s</pana:X_RecGroupType>'
                    '<pana:X_ThumbURI>%s/DT%s.JPG</pana:X_ThumbURI>'
                    '<pana:X_RatingNum>0</pana:X_RatingNum><pana:X_Rating>0</pana:X_Rating>'
                    '</container>' % (e.object_id, len(e.children), e.title, e.group_type,
                                      base, first))
        res = []
        if e.kind in ("photo", "raw"):
            cls = '<upnp:class name="imageItem">object.item.imageItem</upnp:class>'
            res.append((PI_ORG, ' size="%d"' % e.size, "DO%s.JPG" % e.object_id))
            if e.kind == "raw":
                res.append((PI_RAW, ' size="%d"' % e.raw_size, "DO%s.RW2" % e.object_id))
            res.append((PI_TN, ' size="%d"' % THUMB_ATTR, "DT%s.JPG" % e.object_id))
            res.append((PI_MED, ' size="%d"' % PREVIEW_ATTR, "DL%s.JPG" % e.object_id))
        else:
            cls = '<upnp:class name="videoItem">object.item.videoItem.movie</upnp:class>'
            pi, ext = (PI_MP4, "MP4") if e.kind == "mp4" else (PI_MOV, "MOV")
            res.append((pi, ' duration="%s" size="%d"' % (e.duration, e.size),
                        "DO%s.%s" % (e.object_id, ext)))
            res.append((PI_TN, ' size="%d"' % THUMB_ATTR, "DT%s.JPG" % e.object_id))
            res.append((PI_SM, ' size="%d"' % PREVIEW_ATTR, "DL%s.JPG" % e.object_id))
        body = "".join('<res protocolInfo="%s"%s>%s/%s</res>' % (pi, attrs, base, name)
                       for pi, attrs, name in res)
        date = "<dc:date>%s</dc:date>" % e.date if e.date else ""
        return ('<item id="%s" parentID="%s" restricted="0"><dc:title>%s</dc:title>'
                '<upnp:writeStatus>WRITABLE</upnp:writeStatus>%s%s%s'
                '<pana:X_Rating>0</pana:X_Rating></item>' % (e.object_id, e.parent_id, e.title,
                                                             cls, body, date))

    def browse(self, object_id: str, flag: str, start: int, count: int,
               base: str, max_page: int = 100) -> Optional[str]:
        """SOAP BrowseResponse text, or None for an unknown object (UPnP 701)."""
        if flag == "BrowseMetadata":
            e = self.by_id.get(object_id)
            if e is None:
                return None
            didl = DIDL_OPEN + self.didl_entry(e, base) + "</DIDL-Lite>"
            return SOAP_RESPONSE % (_q(didl), 1, 1, self.update_id)
        kids = self.children_of(object_id)
        if kids is None:
            return None
        n = count if count > 0 else max_page
        page = kids[start:start + min(n, max_page)]
        didl = DIDL_OPEN + "".join(self.didl_entry(e, base) for e in page) + "</DIDL-Lite>"
        return SOAP_RESPONSE % (_q(didl), len(page), len(kids), self.update_id)

    # ---------------------------------------------------------------- files
    def lookup(self, name: str) -> Optional[Tuple[Entry, str]]:
        """'DO11000001.JPG' -> (entry, 'org'|'raw'|'thumb'|'preview')."""
        n = name.upper()
        if len(n) < 7 or "." not in n:
            return None
        prefix, rest = n[:2], n[2:]
        oid, _, ext = rest.partition(".")
        e = self.by_id.get(oid)
        if e is None or e.is_container:
            return None
        if prefix == "DT" and ext == "JPG":
            return e, "thumb"
        if prefix == "DL" and ext == "JPG":
            return e, "preview"
        if prefix != "DO":
            return None
        if e.kind in ("photo", "raw") and ext == "JPG":
            return e, "org"
        if e.kind == "raw" and ext == "RW2":
            return e, "raw"
        if (e.kind == "mp4" and ext == "MP4") or (e.kind == "mov" and ext == "MOV"):
            return e, "org"
        return None

    def _hue(self, e: Entry) -> float:
        return (zlib.crc32(e.object_id.encode()) % 1000) / 1000.0

    def _prefix(self, e: Entry, variant: str) -> bytes:
        key = "%s/%s" % (e.object_id, variant)
        hit = self._bodies.get(key)
        if hit is not None:
            self._bodies.move_to_end(key)
            return hit
        label = e.title.replace("-", "")
        if variant == "thumb":
            data = still_jpeg(20, 15, self._hue(e), label)          # 160x120
        elif variant == "preview":
            data = still_jpeg(160, 90 if e.kind in ("mp4", "mov") else 107, self._hue(e), label)
        elif variant == "org" and e.kind in ("photo", "raw"):
            data = still_jpeg(240, 160, self._hue(e), label)       # 1920x1280
        elif variant == "raw":
            data = b"IIU\x00\x08\x00\x00\x00" + b"\x00" * 8
        elif e.kind == "mp4":
            data = struct.pack(">I4s4sI", 24, b"ftyp", b"mp42", 0) + b"isommp42"
            data += struct.pack(">I4s", e.size - len(data), b"mdat")
        else:
            data = struct.pack(">I4s4sI", 20, b"ftyp", b"qt  ", 0x200) + b"qt  "
            data += struct.pack(">I4s", e.size - len(data), b"mdat")
        self._bodies[key] = data
        while len(self._bodies) > 64:
            self._bodies.popitem(last=False)
        return data

    def file_size(self, e: Entry, variant: str) -> int:
        if variant in ("thumb", "preview"):
            return len(self._prefix(e, variant))
        if variant == "raw":
            return e.raw_size
        return max(e.size, len(self._prefix(e, variant)))

    def read(self, e: Entry, variant: str, offset: int, length: int) -> bytes:
        """Bytes [offset, offset+length) of the file: real header + deterministic filler."""
        prefix = self._prefix(e, variant)
        size = self.file_size(e, variant)
        end = min(size, offset + length)
        out = bytearray()
        if offset < len(prefix):
            out += prefix[offset:min(end, len(prefix))]
        pos = max(offset, len(prefix))
        if pos < end:
            seed = zlib.crc32(e.object_id.encode()) & 0xFF
            block = bytes(((i * 131 + seed) & 0xFF) for i in range(4096))
            while pos < end:
                i = pos % 4096
                take = min(end - pos, 4096 - i)
                out += block[i:i + take]
                pos += take
        return bytes(out)

    @staticmethod
    def content_type(e: Entry, variant: str) -> str:
        if variant in ("thumb", "preview") or (variant == "org" and e.kind in ("photo", "raw")):
            return "image/jpeg"
        if variant == "raw":
            return "application/octet-stream"
        return "video/mp4" if e.kind == "mp4" else "video/quicktime"
