"""Aperture and shutter codes.

The camera encodes both as APEX values times 256, written ``"<n>/256"``:

* aperture (``setsetting&type=focal``): Av * 256, f-number = 2 ** (Av / 2).
  Example: 939/256 -> Av 3.67 -> f/3.5, 2304/256 -> Av 9 -> f/22.
* shutter (``setsetting&type=shtrspeed``): Tv * 256, exposure time = 2 ** -Tv.
  Example: 3328/256 -> Tv 13 -> 1/8000 s, -1536/256 -> Tv -6 -> 60 s.

Third-stop steps are round(256 * k / 3). ``getinfo&type=lens`` gives the
limits (closed aperture, open aperture, fastest shutter, slowest shutter).
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

# Nominal labels per third stop. Av index k -> f-number 2 ** (k / 6).
_F_NOMINAL = ["1.0", "1.1", "1.2", "1.4", "1.6", "1.8", "2.0", "2.2", "2.5", "2.8", "3.2",
              "3.5", "4.0", "4.5", "5.0", "5.6", "6.3", "7.1", "8.0", "9.0", "10", "11", "13",
              "14", "16", "18", "20", "22", "25", "29", "32", "36", "40", "45"]

# Tv index k (third stops, k=0 is 1 s). Negative = long exposures.
_SS_LONG = ["60\"", "50\"", "40\"", "30\"", "25\"", "20\"", "15\"", "13\"", "10\"", "8\"",
            "6\"", "5\"", "4\"", "3.2\"", "2.5\"", "2\"", "1.6\"", "1.3\"", "1\""]  # k=-18..0
_SS_SHORT = ["1.3", "1.6", "2", "2.5", "3", "4", "5", "6", "8", "10", "13", "15", "20", "25",
             "30", "40", "50", "60", "80", "100", "125", "160", "200", "250", "320", "400",
             "500", "640", "800", "1000", "1250", "1600", "2000", "2500", "3200", "4000",
             "5000", "6400", "8000", "10000", "13000", "16000"]  # k=1..42

BULB_CODE = 16384


def parse_code(v: Optional[str]) -> Optional[int]:
    """'939/256' -> 939. Returns None for 'auto'/garbage."""
    if not v:
        return None
    try:
        num, _, den = v.partition("/")
        n = int(num)
        d = int(den) if den else 256
        return round(n * 256 / d)
    except ValueError:
        return None


def code_str(code: int) -> str:
    return "%d/256" % code


def aperture_label(code: int) -> str:
    k = code * 3 / 256  # thirds of Av
    ki = round(k)
    if abs(k - ki) < 0.05 and 0 <= ki < len(_F_NOMINAL):
        return "F" + _F_NOMINAL[ki]
    return "F%.1f" % (2 ** (code / 512))


def shutter_label(code: int) -> str:
    if code >= BULB_CODE:
        return "B"
    k = code * 3 / 256
    ki = round(k)
    if abs(k - ki) < 0.05:
        if -18 <= ki <= 0:
            return _SS_LONG[ki + 18]
        if 1 <= ki <= len(_SS_SHORT):
            return "1/" + _SS_SHORT[ki - 1]
    t = 2 ** (-code / 256)
    return ("%.1f\"" % t) if t >= 1 else "1/%d" % round(1 / t)


def third_steps(lo: int, hi: int) -> List[int]:
    """All third-stop codes between lo and hi (inclusive), as round(256*k/3)."""
    if lo > hi:
        lo, hi = hi, lo
    k0 = math.ceil(lo * 3 / 256 - 0.05)
    k1 = math.floor(hi * 3 / 256 + 0.05)
    return [round(256 * k / 3) for k in range(k0, k1 + 1)]


def parse_lens(csv: List[str]) -> Dict[str, object]:
    """getinfo&type=lens CSV -> limits. Field meaning partly inferred (see docs)."""
    out: Dict[str, object] = {"raw": ",".join(csv)}
    if len(csv) < 5 or csv[0] != "ok":
        return out
    out["aperture_closed"] = parse_code(csv[1])
    out["aperture_open"] = parse_code(csv[2])
    out["shutter_fast"] = parse_code(csv[3])
    out["shutter_slow"] = parse_code(csv[4])
    if len(csv) >= 9:
        try:
            out["focal_max_mm"] = int(csv[7])
            out["focal_min_mm"] = int(csv[8])
        except ValueError:
            pass
    if len(csv) >= 15:
        out["name"] = csv[14]
    if len(csv) >= 16:
        out["maker"] = csv[15]
    return out


def aperture_options(lens: Dict[str, object]) -> List[Dict[str, str]]:
    lo, hi = lens.get("aperture_open"), lens.get("aperture_closed")
    if not isinstance(lo, int) or not isinstance(hi, int):
        return []
    return [{"value": code_str(c), "title": aperture_label(c)} for c in third_steps(lo, hi)]


def shutter_options(lens: Dict[str, object]) -> List[Dict[str, str]]:
    slow, fast = lens.get("shutter_slow"), lens.get("shutter_fast")
    if not isinstance(slow, int) or not isinstance(fast, int):
        return []
    opts = [{"value": code_str(c), "title": shutter_label(c)} for c in third_steps(slow, fast)]
    opts.reverse()  # fastest first, like the camera dial
    return opts
