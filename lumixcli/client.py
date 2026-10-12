"""Tiny HTTP client for the Lumix Remote server API (stdlib only).

The server owns the single camera session; this module never talks to the
camera directly.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

DEFAULT_SERVER = "http://127.0.0.1:8770"


def normalize_url(url: Optional[str]) -> str:
    url = (url or os.environ.get("LUMIX_SERVER") or DEFAULT_SERVER).strip()
    if "://" not in url:
        url = "http://" + url
    return url.rstrip("/")


class CliError(Exception):
    """Error with a process exit code."""

    exit_code = 1

    def __init__(self, message: str, payload: Any = None):
        super().__init__(message)
        self.payload = payload


class ServerUnreachable(CliError):
    exit_code = 3


class HttpError(CliError):
    """Non-2xx answer from the server (409 busy/not connected, 400, 502 ...)."""

    def __init__(self, status: int, message: str, payload: Any = None):
        super().__init__(message, payload)
        self.status = status


class CameraError(CliError):
    """The camera answered something other than ok (err_busy, err_param, ...)."""

    def __init__(self, result: str, payload: Any = None, what: str = ""):
        super().__init__("%scamera answered %s" % (what + ": " if what else "", result), payload)
        self.result = result


class Client:
    def __init__(self, base: Optional[str] = None, timeout: float = 30.0):
        self.base = normalize_url(base)
        self.timeout = timeout

    # ----------------------------------------------------------- transport
    def _request(self, method: str, path: str, body: Any = None,
                 timeout: Optional[float] = None) -> "tuple[int, str, bytes]":
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", "") if e.headers else "", e.read()
        except (urllib.error.URLError, ConnectionError, socket.timeout, OSError) as e:
            reason = getattr(e, "reason", e)
            raise ServerUnreachable(
                "cannot reach Lumix Remote server at %s (%s); start it with `lumix server start`"
                % (self.base, reason))

    def call(self, method: str, path: str, body: Any = None, check_result: bool = True,
             timeout: Optional[float] = None) -> Any:
        status, ctype, raw = self._request(method, path, body, timeout)
        try:
            payload = json.loads(raw.decode() or "null")
        except ValueError:
            payload = raw.decode(errors="replace")
        if status >= 400:
            msg = payload.get("error") if isinstance(payload, dict) else payload
            raise HttpError(status, "server %d: %s" % (status, msg or "error"), payload)
        if check_result and isinstance(payload, dict) and "result" in payload:
            check(payload, path)
        return payload

    def get(self, path: str, **kw: Any) -> Any:
        return self.call("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self.call("POST", path, body if body is not None else {}, **kw)

    def get_bytes(self, path: str) -> bytes:
        status, ctype, raw = self._request("GET", path)
        if status >= 400:
            try:
                msg = json.loads(raw.decode()).get("error")
            except Exception:
                msg = raw.decode(errors="replace")[:200]
            raise HttpError(status, "server %d: %s" % (status, msg))
        return raw

    def ws_url(self, path: str = "/api/ws") -> str:
        p = urllib.parse.urlsplit(self.base)
        scheme = "wss" if p.scheme == "https" else "ws"
        return urllib.parse.urlunsplit((scheme, p.netloc, p.path + path, "", ""))

    def reachable(self) -> bool:
        try:
            self._request("GET", "/api/status", timeout=2)
            return True
        except ServerUnreachable:
            return False


def check(payload: Dict[str, Any], what: str = "") -> Dict[str, Any]:
    """Raise CameraError unless every camera answer in payload is ok."""
    for key in ("result", "release"):
        r = payload.get(key)
        if r is not None and not str(r).startswith("ok"):
            raise CameraError(str(r), payload, what)
    return payload
