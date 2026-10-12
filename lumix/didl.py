"""UPnP ContentDirectory Browse (SOAP) request/response + DIDL-Lite parsing.

The camera's MediaServer lives on :60606. A Browse POST to
``/Server0/CDS_control`` returns a SOAP envelope whose ``<Result>`` is an
XML-escaped DIDL-Lite document. Each ``<item>`` carries several ``<res>``
elements on port 50001:

* ``DO<id>.JPG|.RW2|.MP4|.MOV`` original file(s); a RAW+JPEG shot has both
* ``DL<id>.JPG`` large preview (DLNA ``JPEG_LRG``/``JPEG_MED``)
* ``DT<id>.JPG`` thumbnail (DLNA ``JPEG_TN``)

The camera only answers Browse in play mode (``camcmd playmode``); in rec mode
it replies with UPnP error 701 "No such object".
"""

from __future__ import annotations

import datetime as dt
import hashlib
import posixpath
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

NS = {
    "didl": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "upnp": "urn:schemas-upnp-org:metadata-1-0/upnp/",
    "pana": "urn:schemas-panasonic-com:pana",
}

SOAP_ACTION = '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"'
CDS_PATH = "/Server0/CDS_control"
UA = "Panasonic Android/1 DM-CP"

RAW_EXT = {".rw2", ".raw"}
VIDEO_EXT = {".mp4", ".mov", ".mts", ".m2ts", ".avi"}
PHOTO_EXT = {".jpg", ".jpeg", ".hsp", ".heif", ".hif"} | RAW_EXT


def browse_body(object_id: str = "0", start: int = 0, count: int = 100,
                flag: str = "BrowseDirectChildren") -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        '<u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1" '
        'xmlns:pana="urn:schemas-panasonic-com:pana">'
        "<ObjectID>%s</ObjectID><BrowseFlag>%s</BrowseFlag><Filter>*</Filter>"
        "<StartingIndex>%d</StartingIndex><RequestedCount>%d</RequestedCount>"
        "<SortCriteria></SortCriteria><pana:X_FromCP>LumixLink2.0</pana:X_FromCP>"
        "</u:Browse></s:Body></s:Envelope>" % (escape(object_id), flag, start, count)
    ).encode()


class BrowseError(Exception):
    def __init__(self, code: str, desc: str):
        super().__init__("UPnP error %s: %s" % (code, desc))
        self.code = code
        self.desc = desc


@dataclass
class BrowseResult:
    didl: str
    returned: int
    total: int


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1]


def parse_browse_response(raw: bytes) -> BrowseResult:
    """SOAP envelope -> DIDL text + NumberReturned + TotalMatches. Raises BrowseError on a fault."""
    root = ET.fromstring(raw)
    vals: Dict[str, str] = {}
    for el in root.iter():
        name = _local(el.tag)
        if name in ("Result", "NumberReturned", "TotalMatches", "errorCode", "errorDescription"):
            vals[name] = el.text or ""
    if "errorCode" in vals:
        raise BrowseError(vals["errorCode"], vals.get("errorDescription", ""))
    if "Result" not in vals:
        raise BrowseError("?", "no Result in Browse response")
    return BrowseResult(didl=vals["Result"], returned=int(vals.get("NumberReturned") or 0),
                        total=int(vals.get("TotalMatches") or 0))


@dataclass
class Res:
    url: str
    protocol_info: str = ""
    size: Optional[int] = None
    resolution: Optional[str] = None
    duration: Optional[str] = None

    @property
    def name(self) -> str:
        return posixpath.basename(urllib.parse.urlparse(self.url).path)

    @property
    def ext(self) -> str:
        return posixpath.splitext(self.name)[1].lower()

    @property
    def mime(self) -> str:
        parts = self.protocol_info.split(":")
        return parts[2].lower() if len(parts) > 2 else ""

    @property
    def pn(self) -> str:
        m = re.search(r"DLNA\.ORG_PN=([A-Za-z0-9_]+)", self.protocol_info)
        return m.group(1).upper() if m else ""

    @property
    def prefix(self) -> str:
        """Lumix URL prefix: DO original, DL large preview, DT thumbnail."""
        n = self.name.upper()
        return n[:2] if len(n) > 2 and n[:2] in ("DO", "DL", "DT", "DS") else ""

    def role(self) -> str:
        """original | raw | preview | thumb."""
        if self.ext in RAW_EXT or "raw" in self.mime or "PANASONIC_RAW" in self.protocol_info.upper():
            return "raw"
        p, pn = self.prefix, self.pn
        if p == "DT" or pn == "JPEG_TN":
            return "thumb"
        if p in ("DL", "DS") or pn in ("JPEG_SM", "JPEG_MED"):
            return "preview"
        if p == "DO":
            return "original"
        if pn == "JPEG_LRG" or self.mime.startswith("video/") or self.ext in VIDEO_EXT:
            return "original"
        return "other"


@dataclass
class FileItem:
    object_id: str
    parent_id: str
    title: str
    upnp_class: str
    date: Optional[str]
    res: List[Res] = field(default_factory=list)
    extra: Dict[str, str] = field(default_factory=dict)

    # ---- derived
    def _pick(self, role: str) -> Optional[Res]:
        cands = [r for r in self.res if r.role() == role]
        if not cands:
            return None
        if role == "original":  # prefer the real still/video over e.g. an MPO side file
            cands.sort(key=lambda r: (r.ext not in VIDEO_EXT and r.ext not in PHOTO_EXT, -(r.size or 0)))
        if role == "preview":   # biggest preview wins
            cands.sort(key=lambda r: -(r.size or 0))
        return cands[0]

    @property
    def original(self) -> Optional[Res]:
        return self._pick("original") or self._pick("raw")

    @property
    def raw(self) -> Optional[Res]:
        r = self._pick("raw")
        return r if r is not None and r is not self.original else None

    @property
    def thumb(self) -> Optional[Res]:
        return self._pick("thumb") or self._pick("preview")

    @property
    def preview(self) -> Optional[Res]:
        return self._pick("preview") or self._pick("thumb")

    @property
    def kind(self) -> str:
        if "videoitem" in self.upnp_class.lower():
            return "video"
        if any(r.mime.startswith("video/") or r.ext in VIDEO_EXT for r in self.res):
            return "video"
        return "photo"

    @property
    def name(self) -> str:
        """File name as shown to the user (the card name, e.g. P1011793.JPG)."""
        o = self.original
        t = self.title.strip()
        if t and "." in t:
            # title sometimes lacks the extension of the actual original
            if o is not None and posixpath.splitext(t)[1].lower() != o.ext:
                return posixpath.splitext(t)[0] + o.ext.upper()
            return t
        if o is not None:
            base = posixpath.splitext(t)[0] if t else o.name[2:] if o.prefix else posixpath.splitext(o.name)[0]
            return base + o.ext.upper()
        return t or self.object_id

    @property
    def raw_name(self) -> Optional[str]:
        r = self.raw
        if r is None:
            return None
        return posixpath.splitext(self.name)[0] + r.ext.upper()

    @property
    def size(self) -> Optional[int]:
        o = self.original
        return o.size if o is not None else None

    @property
    def id(self) -> str:
        """URL-safe stable id for the API."""
        oid = self.object_id
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", oid):
            return oid
        return hashlib.sha1(oid.encode()).hexdigest()[:16]

    def sort_key(self) -> Tuple[str, str]:
        return (self.date or "", self.name)

    def date_folder(self) -> str:
        d = parse_date(self.date)
        return (d or dt.datetime.now()).strftime("%Y-%m-%d")

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "kind": self.kind, "date": self.date,
                "size": self.size, "has_raw": self.raw is not None}


@dataclass
class Container:
    object_id: str
    title: str
    child_count: Optional[int]


def _int(v: Optional[str]) -> Optional[int]:
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        return None


def parse_date(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
    s = s.strip().replace("Z", "")
    try:
        return dt.datetime.fromisoformat(s.replace(" ", "T", 1)).replace(tzinfo=None)
    except ValueError:
        pass
    m = re.match(r"(\d{4})[-/:](\d{2})[-/:](\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?", s)
    if m:
        return dt.datetime(*(int(x) for x in m.groups() if x is not None))
    return None


def parse_didl(didl: str) -> Tuple[List[FileItem], List[Container]]:
    """DIDL-Lite text -> (items, containers). Unknown elements are kept in ``extra``."""
    if not didl.strip():
        return [], []
    root = ET.fromstring(didl.encode("utf-8") if isinstance(didl, str) else didl)
    items: List[FileItem] = []
    conts: List[Container] = []
    for el in root:
        tag = _local(el.tag)
        if tag == "container":
            conts.append(Container(object_id=el.get("id", ""),
                                   title=el.findtext("dc:title", "", NS),
                                   child_count=_int(el.get("childCount"))))
            continue
        if tag != "item":
            continue
        it = FileItem(object_id=el.get("id", ""), parent_id=el.get("parentID", ""),
                      title=el.findtext("dc:title", "", NS) or "",
                      upnp_class=el.findtext("upnp:class", "", NS) or "",
                      date=(el.findtext("dc:date", None, NS) or None))
        for child in el:
            ctag = _local(child.tag)
            if ctag == "res":
                url = (child.text or "").strip()
                if url:
                    it.res.append(Res(url=url, protocol_info=child.get("protocolInfo", ""),
                                      size=_int(child.get("size")), resolution=child.get("resolution"),
                                      duration=child.get("duration")))
            elif ctag not in ("title", "class", "date"):
                it.extra[ctag] = (child.text or "").strip()
        items.append(it)
    return items, conts
