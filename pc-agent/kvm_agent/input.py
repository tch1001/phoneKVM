"""Input event parsing, validation and logging.

The real injection path is the USB HID gadget owned by another component, so
this module only normalises client events into a stable JSON shape, maps
stream coordinates onto desktop coordinates, and logs them. The normalised
record is exactly what the HID layer is expected to consume later.

Accepted client -> server messages (all JSON objects with a ``t`` field):

    {"t":"mousemove","x":640,"y":360}
    {"t":"mousedown","x":640,"y":360,"button":"left"}
    {"t":"mouseup","x":640,"y":360,"button":"left"}
    {"t":"click","x":640,"y":360,"button":"left"}        # down+up, acks once
    {"t":"scroll","x":640,"y":360,"dx":0,"dy":-3}         # dy>0 scrolls up
    {"t":"key","key":"a","action":"press","mods":["ctrl"]}
    {"t":"text","text":"hello"}                           # convenience
    {"t":"ping"}

``x``/``y`` are pixels inside the streamed image (0..width-1 / 0..height-1).
The event signed as ``px``/``py`` in the ack and in the log is the same point
converted to desktop pixels using the screen/stream scale factor.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

BUTTONS = {"left": 1, "middle": 2, "right": 3, "1": 1, "2": 2, "3": 3}
MODIFIERS = {"ctrl", "shift", "alt", "super", "meta"}
ACTIONS = {"press", "down", "up"}
MAX_KEY_LEN = 32
MAX_TEXT_LEN = 512


class InputError(ValueError):
    """Invalid input message from a client."""


def _num(msg: dict, key: str, default: Optional[float] = None,
         required: bool = True) -> Optional[float]:
    if key not in msg or msg[key] is None:
        if required:
            raise InputError(f"missing '{key}'")
        return default
    val = msg[key]
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise InputError(f"'{key}' must be a number")
    if val != val or val in (float("inf"), float("-inf")):
        raise InputError(f"'{key}' must be finite")
    return float(val)


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _coords(msg: dict, ctx: dict, required: bool = True) -> tuple[Optional[int], Optional[int]]:
    x = _num(msg, "x", required=required)
    y = _num(msg, "y", required=required)
    if x is None or y is None:
        return None, None
    sw, sh = ctx["stream_width"], ctx["stream_height"]
    x = _clamp(x, 0.0, max(0.0, sw - 1))
    y = _clamp(y, 0.0, max(0.0, sh - 1))
    return int(round(x)), int(round(y))


def _to_desktop(x: Optional[int], y: Optional[int], ctx: dict) -> tuple[Optional[int], Optional[int]]:
    if x is None or y is None:
        return None, None
    sw, sh = ctx["stream_width"], ctx["stream_height"]
    dw, dh = ctx["screen_width"], ctx["screen_height"]
    if not sw or not sh or not dw or not dh:
        return x, y
    px = int(round(x * dw / sw))
    py = int(round(y * dh / sh))
    return min(max(px, 0), dw - 1), min(max(py, 0), dh - 1)


def _button(msg: dict) -> int:
    raw = msg.get("button", "left")
    if isinstance(raw, bool):
        raise InputError("'button' must be a name or index")
    if isinstance(raw, int):
        if not 1 <= raw <= 5:
            raise InputError("'button' index must be 1..5")
        return raw
    if isinstance(raw, str) and raw.lower() in BUTTONS:
        return BUTTONS[raw.lower()]
    raise InputError(f"unknown button {raw!r} (use left/middle/right/1..5)")


def parse(msg: Any, ctx: dict) -> dict:
    """Validate one client message and return a normalised event record."""
    if not isinstance(msg, dict):
        raise InputError("message must be a JSON object")
    kind = msg.get("t")
    if not isinstance(kind, str):
        raise InputError("missing 't' (event type)")
    kind = kind.lower()

    ev: dict[str, Any] = {"t": kind, "at_ms": int(time.time() * 1000) & 0xFFFFFFFF}
    if "id" in msg:
        ev["id"] = msg["id"] if isinstance(msg["id"], (int, str)) else str(msg["id"])

    if kind == "ping":
        return ev

    if kind in ("mousemove", "mousedown", "mouseup", "click"):
        x, y = _coords(msg, ctx)
        ev.update(x=x, y=y)
        if kind != "mousemove":
            ev["button"] = _button(msg)
        px, py = _to_desktop(x, y, ctx)
        ev.update(px=px, py=py)
        return ev

    if kind == "scroll":
        x, y = _coords(msg, ctx, required=False)
        dx = _num(msg, "dx", default=0.0, required=False) or 0.0
        dy = _num(msg, "dy", default=0.0, required=False) or 0.0
        if dx == 0 and dy == 0:
            raise InputError("scroll needs a non-zero dx or dy")
        px, py = _to_desktop(x, y, ctx)
        ev.update(x=x, y=y, px=px, py=py,
                  dx=int(_clamp(dx, -50, 50)), dy=int(_clamp(dy, -50, 50)))
        return ev

    if kind == "key":
        key = msg.get("key")
        if not isinstance(key, str) or not key or len(key) > MAX_KEY_LEN:
            raise InputError("'key' must be a non-empty string (max 32 chars)")
        action = str(msg.get("action", "press")).lower()
        if action not in ACTIONS:
            raise InputError("'action' must be press, down or up")
        mods = msg.get("mods") or []
        if not isinstance(mods, list) or not all(isinstance(m, str) for m in mods):
            raise InputError("'mods' must be a list of strings")
        norm_mods = []
        for m in mods:
            ml = m.lower()
            if ml not in MODIFIERS:
                raise InputError(f"unknown modifier {m!r} (ctrl/shift/alt/super)")
            if ml not in norm_mods:
                norm_mods.append(ml)
        ev.update(key=key, action=action, mods=norm_mods)
        code = msg.get("code")
        if code is not None:
            if not isinstance(code, str) or not code or len(code) > MAX_KEY_LEN:
                raise InputError("'code' must be a non-empty string (max 32 chars)")
            ev["code"] = code
        return ev

    if kind == "text":
        text = msg.get("text")
        if not isinstance(text, str) or not text or len(text) > MAX_TEXT_LEN:
            raise InputError(f"'text' must be a non-empty string (max {MAX_TEXT_LEN})")
        ev.update(text=text)
        return ev

    raise InputError(f"unknown event type {kind!r}")


def format_event(ev: dict) -> str:
    """Human readable one-liner for the log."""
    kind = ev["t"]
    if kind in ("mousemove", "mousedown", "mouseup", "click"):
        btn = f" button={ev['button']}" if "button" in ev else ""
        return f"{kind} stream=({ev['x']},{ev['y']}) desktop=({ev['px']},{ev['py']}){btn}"
    if kind == "scroll":
        where = f" at=({ev['px']},{ev['py']})" if ev.get("px") is not None else ""
        return f"scroll dx={ev['dx']} dy={ev['dy']}{where}"
    if kind == "key":
        mods = "+".join(ev["mods"]) + "+" if ev["mods"] else ""
        code = f" code={ev['code']}" if ev.get("code") else ""
        return f"key {ev['action']} {mods}{ev['key']}{code}"
    if kind == "text":
        return f"text {ev['text']!r}"
    return kind


def dumps(ev: dict) -> str:
    return json.dumps(ev, separators=(",", ":"), ensure_ascii=False)
