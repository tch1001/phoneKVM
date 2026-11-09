#!/usr/bin/env python3
"""Robustness checks against a running agent.

1. Kills the ffmpeg capture child and verifies the server restarts capture and
   keeps streaming.
2. Sends malformed input messages and verifies they are rejected (and that
   out-of-range coordinates are clamped instead of rejected).

    .venv/bin/python tools/robustness_check.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import time
import urllib.request
from urllib.parse import urlparse

import aiohttp

BAD_INPUTS = [
    "not json",
    '{"t":"nope"}',
    '{"t":"mousemove","x":"left","y":3}',
    '{"t":"key","key":""}',
    '{"t":"scroll","dx":0,"dy":0}',
    '{"t":"mousemove","x":99999,"y":-5}',
]


def health(base: str) -> dict:
    with urllib.request.urlopen(f"{base}/healthz", timeout=3) as resp:
        return json.loads(resp.read())


async def check_inputs(ws_url: str) -> bool:
    ok = True
    async with aiohttp.ClientSession() as sess:
        async with sess.ws_connect(ws_url, compress=0, max_msg_size=16 << 20) as ws:
            for case in BAD_INPUTS:
                await ws.send_str(case)
                while True:
                    msg = await ws.receive(timeout=3)
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    data = json.loads(msg.data)
                    if data.get("t") in ("error", "ack"):
                        kind = "error" if "x" not in data else "ack(clamped)"
                        print(f"  {case[:38]:40} -> {kind}: "
                              f"{data.get('msg') or f'{data.get(chr(120))},{data.get(chr(121))}'}")
                        if data.get("t") == "ack":
                            # clamped input must still be in range
                            ok = ok and 0 <= data["x"] < 1e6 and 0 <= data["y"] < 1e6
                        break
            await ws.send_bytes(b"\x00\x01")
            while True:
                msg = await ws.receive(timeout=3)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("t") == "error":
                        print(f"  {'<binary input>':40} -> error: {data['msg']}")
                        break
    return ok


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8080")
    p.add_argument("--skip-kill", action="store_true")
    args = p.parse_args()
    base = args.url.rstrip("/")
    ws_url = base.replace("http", "ws", 1) + "/ws"

    ok = True
    if not args.skip_kill:
        before = health(base)
        print(f"before        : {before['source']} frames={before['frames']}")
        pids = subprocess.run(["pgrep", "-x", "ffmpeg"], capture_output=True,
                              text=True).stdout.split()
        if not pids:
            print("no ffmpeg child to kill (x11 backend?); skipping restart check")
        else:
            for pid in pids:
                subprocess.run(["kill", "-9", pid], check=False)
            time.sleep(1.2)
            mid = health(base)
            time.sleep(6.0)
            after = health(base)
            recovered = after["frames"] > mid["frames"]
            ok = ok and recovered
            print(f"after kill    : {mid['source']} frames={mid['frames']}")
            print(f"7s later      : {after['source']} frames={after['frames']} "
                  f"recovered={recovered}")

    print("malformed input:")
    ok = asyncio.run(check_inputs(ws_url)) and ok
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
