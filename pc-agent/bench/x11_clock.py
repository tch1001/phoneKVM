#!/usr/bin/env python3
"""A tiny always-on-top X11 window that draws an 8-bit binary counter.

Why: to measure the part of the pipeline that Python cannot see from inside --
the X11 grab plus the encoder.  Every 15 ms the window paints a new counter
value and appends (counter, CLOCK_MONOTONIC) to a log.  A frame that arrives in
Python (or a screenshot of the phone) therefore carries an exact timestamp: the
counter pattern identifies the moment it was painted.

    bench/x11_clock.py --seconds 12 --out /tmp/flash.jsonl

The counter runs mod 256 with 15 ms steps, so the pattern is unique for ~3.8 s,
which is plenty to disambiguate a single frame.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import time
from ctypes import (CFUNCTYPE, POINTER, Structure, byref, c_char_p, c_int,
                    c_uint, c_ulong, c_void_p)

X11 = ctypes.CDLL("/usr/lib/x86_64-linux-gnu/libX11.so.6")
CWBackPixel = 1 << 1
CWOverrideRedirect = 1 << 9
InputOutput = 1
CopyFromParent = 0
ZPIXMAP = 2


class XSetWindowAttributes(Structure):
    _fields_ = [
        ("background_pixmap", c_ulong), ("background_pixel", c_ulong),
        ("border_pixmap", c_ulong), ("border_pixel", c_ulong),
        ("bit_gravity", c_int), ("win_gravity", c_int),
        ("backing_store", c_int), ("backing_planes", c_ulong),
        ("backing_pixel", c_ulong), ("save_under", c_int),
        ("event_mask", c_ulong), ("do_not_propagate_mask", c_ulong),
        ("override_redirect", c_int), ("colormap", c_ulong), ("cursor", c_ulong),
    ]


class XGCValues(Structure):
    _fields_ = [("function", c_int), ("plane_mask", c_ulong),
                ("foreground", c_ulong), ("background", c_ulong),
                ("line_width", c_int), ("line_style", c_int), ("cap_style", c_int)]


X11.XOpenDisplay.restype = c_void_p
X11.XOpenDisplay.argtypes = [c_char_p]
X11.XDefaultRootWindow.restype = c_ulong
X11.XDefaultRootWindow.argtypes = [c_void_p]
X11.XCreateWindow.restype = c_ulong
X11.XCreateWindow.argtypes = [c_void_p, c_ulong, c_int, c_int, c_uint, c_uint,
                              c_uint, c_int, c_uint, c_void_p, c_ulong,
                              POINTER(XSetWindowAttributes)]
X11.XMapRaised.argtypes = [c_void_p, c_ulong]
X11.XCreateGC.restype = c_void_p
X11.XCreateGC.argtypes = [c_void_p, c_ulong, c_ulong, POINTER(XGCValues)]
X11.XSetForeground.argtypes = [c_void_p, c_void_p, c_ulong]
X11.XFillRectangle.argtypes = [c_void_p, c_ulong, c_void_p, c_int, c_int, c_uint, c_uint]
X11.XFlush.argtypes = [c_void_p]
X11.XSync.argtypes = [c_void_p, c_int]
X11.XDestroyWindow.argtypes = [c_void_p, c_ulong]
X11.XStoreName.argtypes = [c_void_p, c_ulong, c_char_p]

WHITE = 0xFFFFFFFF
BLACK = 0x00000000
MAGENTA = 0xFF00FF

# geometry: 8 cells of `cell` px, plus a border of dark grey
CELL = 60
CELLS = 8
BORDER = 10


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=12)
    ap.add_argument("--x", type=int, default=420)
    ap.add_argument("--y", type=int, default=240)
    ap.add_argument("--out", default="/tmp/flash.jsonl")
    ap.add_argument("--display", default=None)
    args = ap.parse_args()

    dpy = X11.XOpenDisplay((args.display or "").encode() or None)
    if not dpy:
        raise SystemExit("cannot open display")
    root = X11.XDefaultRootWindow(dpy)
    w = CELLS * CELL + 2 * BORDER
    h = CELL + 2 * BORDER
    attrs = XSetWindowAttributes()
    attrs.background_pixel = MAGENTA
    attrs.override_redirect = 1
    win = X11.XCreateWindow(dpy, root, args.x, args.y, w, h, 0, 24, InputOutput,
                            None, CWBackPixel | CWOverrideRedirect, byref(attrs))
    X11.XStoreName(dpy, win, b"kvm-latency-probe")
    X11.XMapRaised(dpy, win)
    gc = X11.XCreateGC(dpy, win, 0, None)
    X11.XSync(dpy, 0)

    fh = open(args.out, "w", buffering=1)
    t_end = time.monotonic() + args.seconds
    counter = 0
    period = 0.015
    nxt = time.monotonic()
    while time.monotonic() < t_end:
        now = time.monotonic()
        X11.XSetForeground(dpy, gc, MAGENTA)
        X11.XFillRectangle(dpy, win, gc, 0, 0, w, h)
        for i in range(CELLS):
            on = (counter >> (CELLS - 1 - i)) & 1
            X11.XSetForeground(dpy, gc, WHITE if on else BLACK)
            X11.XFillRectangle(dpy, win, gc, BORDER + i * CELL, BORDER,
                               CELL - 4, CELL)
        X11.XFlush(dpy)
        fh.write(json.dumps({"c": counter, "t": round(now, 6)}) + "\n")
        counter = (counter + 1) & 0xFF
        nxt += period
        dt = nxt - time.monotonic()
        if dt > 0:
            time.sleep(dt)
        else:
            nxt = time.monotonic()
    fh.close()
    X11.XDestroyWindow(dpy, win)
    X11.XSync(dpy, 0)
    print(f"painted {counter} patterns in {args.seconds}s -> {args.out}")


if __name__ == "__main__":
    main()
