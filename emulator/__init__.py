"""Lumix camera emulator for overnight development and QA without a real body.

``python -m emulator --host 127.0.0.1 --http-port 8080 --upnp-port 60606 --dl-port 50001``

See docs/emulator.md for what is emulated, the fidelity gaps and the control API.
"""

from __future__ import annotations

import os
from typing import Dict

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Per-model constants not kept in data/<model>/ files. Recorded on a DC-S5, firmware 2.03.
MODELS: Dict[str, Dict[str, str]] = {
    "dc-s5": {
        "version": "VD4.30",
        "lens": ("ok,2304/256,939/256,3328/256,-1536/256,0,off,60,20,on,128/1024,on,off,l,"
                 "LUMIX S 20-60/F3.5-5.6,Panasonic,1653,256"),
        # 254-byte live-view header (frame 1, run 20261011-203625); bytes 0-1 = BE16
        # datagram length and 9-14 = camera MAC are filled in per frame.
        "lv_header": (
            "79200100ffffffff000000000000000021ffffffffffffffffffffffffff00de"
            "000c000a0001feffffff01010000fe0100d701f9022203b6ffffff0100000000"
            "0900010003ab0100041f01000d000100fa000100009901000001010000010100"
            "0001010001001400ff0100760200000200000000000000000100080500001d00"
            "00010000020000000000000a10000000000000000000000000ff000000000000"
            "0000000000000000000000000000000000000000000000000000000000000000"
            "0000010502000700000000000000010000051501ff0000000001000000000000"
            "0000000000010000000300002f0001000000000000000000000000000000"),
    },
}


def load_data(model: str, name: str) -> str:
    with open(os.path.join(DATA_DIR, model, name), encoding="utf-8") as f:
        return f.read()
