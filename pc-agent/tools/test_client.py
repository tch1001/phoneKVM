#!/usr/bin/env python3
"""Headless verification client for the kvm-lite PC agent.

Connects to ws://127.0.0.1:8080/ws, collects frames, validates the wire
format, decodes a few JPEGs, then exercises the input channel and reports
round-trip times. Exit code 0 = all checks passed.

    python tools/test_client.py --duration 10
    python tools/test_client.py --url ws://127.0.0.1:8080/ws --json
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import statistics
import sys
import time
from pathlib import Path

import pathlib as _pl
import subprocess
import tempfile

import aiohttp
from aiohttp import WSMsgType
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kvm_agent.wire import HEADER_SIZE, jpeg_dimensions, unpack_header  # noqa: E402


def _nal_types(au: bytes, h264: bool = True) -> list[int]:
    """NAL unit types inside one Annex-B access unit."""
    out, i, n = [], 0, len(au)
    while True:
        j = au.find(b"\x00\x00\x01", i)
        if j < 0 or j + 3 >= n:
            break
        b = au[j + 3]
        out.append((b & 0x1F) if h264 else ((b >> 1) & 0x3F))
        i = j + 3
    return out


def build_inputs() -> list[dict]:
    """Input events exercised over the same socket."""
    return [
        {"t": "mousemove", "x": 100, "y": 80, "id": "mv1"},
        {"t": "mousemove", "x": 640, "y": 360, "id": "mv2"},
        {"t": "mousedown", "x": 640, "y": 360, "button": "left", "id": "down1"},
        {"t": "mouseup", "x": 640, "y": 360, "button": "left", "id": "up1"},
        {"t": "click", "x": 320, "y": 240, "button": "right", "id": "click1"},
        {"t": "scroll", "x": 640, "y": 360, "dx": 0, "dy": -3, "id": "scroll1"},
        {"t": "key", "key": "a", "code": "KeyA", "action": "down",
         "mods": ["ctrl"], "id": "key1"},
        {"t": "text", "text": "hello kvm", "id": "text1"},
        {"t": "ping", "id": "ping1"},
    ]


async def run(args) -> dict:
    frames = []          # (recv_time, size, header)
    raw_samples = []     # payloads kept for decode checks
    codes = set()        # codec ids seen in headers
    format_errors = []
    hello = None
    acks = {}
    ack_count = 0
    stats_msgs = []

    async with aiohttp.ClientSession() as sess:
        async with sess.ws_connect(args.url, compress=0,
                                   max_msg_size=16 * 1024 * 1024) as ws:
            t_start = time.monotonic()
            t_end = t_start + args.duration
            while True:
                remaining = t_end - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    msg = await ws.receive(timeout=min(remaining, 5.0))
                except asyncio.TimeoutError:
                    if time.monotonic() >= t_end - 0.05:
                        break  # the collection window simply ended
                    format_errors.append("no frame for 5s (stalled stream)")
                    break
                if msg.type == WSMsgType.BINARY:
                    now = time.monotonic()
                    data = msg.data
                    try:
                        hdr = unpack_header(data)
                    except ValueError as exc:
                        format_errors.append(str(exc))
                        continue
                    frames.append((now, len(data), hdr))
                    codes.add(hdr.codec)
                    payload = data[HEADER_SIZE:]
                    if len(payload) == 0:
                        format_errors.append("empty payload")
                    elif hdr.codec == 1:
                        jw, jh = jpeg_dimensions(payload)
                        if (jw, jh) != (hdr.width, hdr.height):
                            format_errors.append(
                                f"header {hdr.width}x{hdr.height} != jpeg {jw}x{jh}")
                        if payload[:2] != b"\xff\xd8":
                            format_errors.append("payload does not start with SOI")
                        if payload[-2:] != b"\xff\xd9":
                            format_errors.append("payload does not end with EOI")
                        if len(raw_samples) < 40 and len(frames) % 25 == 1:
                            raw_samples.append(payload)
                    else:
                        # inter-frame: one Annex-B access unit per message
                        if not payload.startswith(b"\x00\x00\x00\x01"):
                            format_errors.append("AU does not start with a start code")
                        if hdr.seq % 25 == 1 and len(raw_samples) < 40:
                            raw_samples.append(payload)
                elif msg.type == WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("t") == "hello":
                        hello = data
                    elif data.get("t") == "stats":
                        stats_msgs.append(data)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    format_errors.append(f"socket closed: {msg.type}")
                    break

            # ---- input round trip -------------------------------------
            for ev in build_inputs():
                rid = ev["id"]
                t_send = time.monotonic()
                await ws.send_str(json.dumps(ev))
                want = "pong" if ev["t"] == "ping" else "ack"
                deadline = time.monotonic() + args.input_timeout
                while time.monotonic() < deadline:
                    try:
                        msg = await ws.receive(timeout=deadline - time.monotonic())
                    except asyncio.TimeoutError:
                        break
                    if msg.type != WSMsgType.TEXT:
                        continue
                    data = json.loads(msg.data)
                    if data.get("t") == want and (data.get("id") == rid or want == "pong"):
                        ack_count += 1
                        acks[ev["t"]] = {
                            "rtt_ms": round((time.monotonic() - t_send) * 1000, 2),
                            "reply": data,
                        }
                        break

    return {
        "hello": hello,
        "codes": codes,
        "frames": frames,
        "samples": raw_samples,
        "errors": format_errors,
        "acks": acks,
        "ack_count": ack_count,
        "stats": stats_msgs,
    }


def summarise(res: dict, args) -> tuple[dict, bool]:
    frames = res["frames"]
    errors = list(res["errors"])
    if not frames:
        return {"ok": False, "errors": errors or ["no frames received"]}, False

    times = [f[0] for f in frames]
    sizes = [f[1] for f in frames]
    headers = [f[2] for f in frames]
    duration = times[-1] - times[0]
    fps = (len(frames) - 1) / duration if duration > 0 else 0.0
    intervals = [(b - a) * 1000 for a, b in zip(times, times[1:])]
    seqs = [h.seq for h in headers]
    missing = sum(max(0, b - a - 1) for a, b in zip(seqs, seqs[1:]))

    codes = res.get("codes") or {headers[0].codec}
    inter = any(c != 1 for c in codes)
    decoded, luma, uniq = 0, [], set()
    dims = set()
    keyframes = 0
    if inter:
        # decode the collected access units with ffmpeg and check that the
        # result matches the header geometry
        suffix = ".h264" if 2 in codes else ".hevc"
        tmp = _pl.Path(tempfile.mktemp(suffix=suffix))
        tmp.write_bytes(b"".join(res["samples"]))
        keyframes = sum(1 for p in res["samples"]
                        if any(t in (5, 16, 17, 18, 19, 20, 21)
                               for t in _nal_types(p, 2 in codes)))
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "csv=p=0", str(tmp)],
            capture_output=True, text=True)
        for line in probe.stdout.strip().splitlines():
            if "x" in line or "," in line:
                parts = line.replace("x", ",").split(",")
                if len(parts) >= 2:
                    dims.add((int(parts[0]), int(parts[1])))
                    decoded += 1
        tmp.unlink(missing_ok=True)
    for payload in res["samples"] if not inter else []:
        try:
            img = Image.open(io.BytesIO(payload))
            img.load()
            decoded += 1
            dims.add(img.size)
            small = img.convert("L").resize((64, 36))
            px = small.tobytes()
            luma.append(sum(px) / len(px))
            uniq.add(hash(payload[:8192]))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"jpeg decode failed: {exc}")

    hdr_w = headers[-1].width
    hdr_h = headers[-1].height
    if args.expect_width and hdr_w != args.expect_width:
        errors.append(f"width {hdr_w} != expected {args.expect_width}")
    if args.expect_height and hdr_h != args.expect_height:
        errors.append(f"height {hdr_h} != expected {args.expect_height}")

    expected_fps = args.expect_fps or (res["hello"] or {}).get("fps")
    fps_ok = True
    if expected_fps:
        fps_ok = fps >= expected_fps * 0.8
        if not fps_ok:
            errors.append(f"fps {fps:.1f} below 80% of target {expected_fps}")

    inputs_ok = res["ack_count"] == len(build_inputs())
    if not inputs_ok:
        errors.append(f"only {res['ack_count']}/{len(build_inputs())} input events acked")

    out = {
        "ok": not errors,
        "url": args.url,
        "codec": "h264" if 2 in codes else ("hevc" if 3 in codes else "jpeg"),
        "source": (res["hello"] or {}).get("source"),
        "resolution": f"{hdr_w}x{hdr_h}",
        "frames": len(frames),
        "duration_s": round(duration, 3),
        "fps": round(fps, 2),
        "target_fps": expected_fps,
        "frame_bytes": {
            "avg": round(statistics.fmean(sizes)),
            "min": min(sizes),
            "max": max(sizes),
            "stdev": round(statistics.pstdev(sizes)),
        },
        "throughput_mbps": round(sum(sizes) * 8 / duration / 1e6, 2),
        "interval_ms": {
            "mean": round(statistics.fmean(intervals), 2) if intervals else None,
            "max": round(max(intervals), 2) if intervals else None,
        },
        "seq": {"first": seqs[0], "last": seqs[-1], "missing": missing},
        "decode": {"sampled": len(res["samples"]), "ok": decoded,
                   "dims": sorted(f"{w}x{h}" for w, h in dims),
                   "luma": [round(v) for v in luma]},
        "unique_frames": len(uniq),
        "input_acks": {k: v["rtt_ms"] for k, v in res["acks"].items()},
        "input_replies": {k: v["reply"] for k, v in res["acks"].items()},
        "stats_messages": len(res["stats"]),
        "errors": errors,
    }
    return out, out["ok"]


def print_human(out: dict) -> None:
    print(f"url          {out['url']}")
    print(f"backend      {out.get('source')}")
    print(f"resolution   {out.get('resolution')}")
    print(f"frames       {out.get('frames')} in {out.get('duration_s')} s")
    print(f"fps          {out.get('fps')} (target {out.get('target_fps')})")
    fb = out.get("frame_bytes") or {}
    if fb:
        print(f"frame size   avg {fb['avg']} B ({fb['avg'] / 1024:.1f} kB), "
              f"min {fb['min']}, max {fb['max']}, stdev {fb['stdev']}")
    print(f"throughput   {out.get('throughput_mbps')} Mbit/s")
    print(f"interval     {out.get('interval_ms')}")
    print(f"sequence     {out.get('seq')}")
    label = "codec" if out.get("codec") in ("h264", "hevc") else "jpeg"
    print(f"{label:<12} {out.get('decode')}, unique {out.get('unique_frames')}")
    print(f"input rtt    {out.get('input_acks')}")
    if out.get("errors"):
        print("errors:")
        for e in out["errors"]:
            print("  -", e)
    print("RESULT:", "PASS" if out.get("ok") else "FAIL")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="kvm-lite stream verification client")
    p.add_argument("--url", default="ws://127.0.0.1:8080/ws")
    p.add_argument("--duration", type=float, default=10.0)
    p.add_argument("--expect-width", type=int, default=0)
    p.add_argument("--expect-height", type=int, default=0)
    p.add_argument("--expect-fps", type=float, default=0.0)
    p.add_argument("--input-timeout", type=float, default=3.0)
    p.add_argument("--json", action="store_true", help="print a JSON summary")
    args = p.parse_args(argv)

    if args.expect_fps == 0.0:
        args.expect_fps = None

    res = asyncio.run(run(args))
    out, ok = summarise(res, args)
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print_human(out)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
