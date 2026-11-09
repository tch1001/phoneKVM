#!/usr/bin/env python3
"""Measure the part of the pipeline Python cannot see: X11 grab + encode.

Method: a probe window (bench/x11_clock.py) paints an 8-bit binary counter every
15 ms and logs (counter, CLOCK_MONOTONIC).  We run the *real* capture backend
from kvm_agent.capture (identical ffmpeg command line, identical filters) for a
few seconds, timestamp every encoded frame as it arrives, then decode the frames
and read back which counter pattern each one contains.  For frame k:

    grab+encode latency = arrival_time(k) - paint_time(pattern in frame k)

The paint time is when Python flushed the X request, so any window-manager
compositing delay (~1 refresh) is *included*: the number is an upper bound.

    bench/measure_capture.py --codec h264 --width 1920 --height 1080 --fps 30
    bench/measure_capture.py --codec jpeg --width 1280 --height 720 --fps 20
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH.parent))

from kvm_agent.config import Config                      # noqa: E402
from kvm_agent.capture import CaptureError                # noqa: E402
from kvm_agent.capture.ffmpeg import FfmpegSource         # noqa: E402
from kvm_agent.capture.h264 import EncodedSource          # noqa: E402
from kvm_agent.wire import CODEC_H264, CODEC_HEVC         # noqa: E402

PROBE_X, PROBE_Y = 420, 240
CELL, BORDER, CELLS = 60, 10, 8
# cell centres inside the probe window
CELL_CXY = [(PROBE_X + BORDER + i * CELL + CELL // 2,
             PROBE_Y + BORDER + CELL // 2) for i in range(CELLS)]


def read_pattern(rgb: bytes, w: int, h: int) -> int:
    """Read the 8 probe cells out of one RGB24 frame of the *stream*."""
    sx, sy = w / 1920.0, h / 1080.0
    value = 0
    for i, (cx, cy) in enumerate(CELL_CXY):
        x, y = int(cx * sx), int(cy * sy)
        off = (y * w + x) * 3
        r, g, b = rgb[off], rgb[off + 1], rgb[off + 2]
        bit = 1 if (r + g + b) > 380 else 0
        value = (value << 1) | bit
    return value


def decode_h264(path: Path, w: int, h: int):
    p = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-f", "h264" if path.suffix == ".h264" else "hevc",
         "-i", str(path), "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
        stdout=subprocess.PIPE)
    size = w * h * 3
    while True:
        buf = p.stdout.read(size)
        if len(buf) < size:
            break
        yield buf
    p.stdout.close()
    p.wait()


def decode_jpeg(frames: list[bytes]):
    from PIL import Image
    import io
    for data in frames:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        yield im.tobytes(), im.width, im.height


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codec", default="h264", choices=["jpeg", "h264", "hevc"])
    ap.add_argument("--encoder", default="auto")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=float, default=30)
    ap.add_argument("--bitrate", type=int, default=0)
    ap.add_argument("--hw-upload", action="store_true")
    ap.add_argument("--seconds", type=float, default=8)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    cfg = Config(codec=args.codec, encoder=args.encoder, width=args.width,
                 height=args.height, fps=args.fps, quality=70,
                 bitrate=args.bitrate, hw_upload=args.hw_upload)
    if args.codec == "jpeg":
        src = FfmpegSource(cfg, input_kind="x11grab")
    else:
        codec = CODEC_H264 if args.codec == "h264" else CODEC_HEVC
        src = EncodedSource(cfg, codec, args.encoder)

    flash_log = Path(tempfile.mktemp(suffix=".jsonl"))
    probe = subprocess.Popen(
        [sys.executable, str(BENCH / "x11_clock.py"), "--seconds",
         str(args.seconds + 3), "--out", str(flash_log)],
        stdout=subprocess.DEVNULL)
    time.sleep(0.8)          # let it paint and get mapped

    arrivals: list[float] = []
    payloads: list[bytes] = []
    try:
        src.open()
        t_end = time.monotonic() + args.seconds
        while time.monotonic() < t_end:
            f = src._read_frame()
            if f is None:
                break
            arrivals.append(time.monotonic())
            payloads.append(f.payload)
    finally:
        src._executor.shutdown(wait=False)
        try:
            src.close()
        except Exception:
            pass
        probe.terminate()
        probe.wait(timeout=3)

    # pattern -> list of paint times (the 8-bit counter repeats every ~3.8 s,
    # so pick the latest paint before the frame arrived)
    paint: dict[int, list[float]] = {}
    for line in flash_log.read_text().splitlines():
        rec = json.loads(line)
        paint.setdefault(rec["c"], []).append(rec["t"])
    flash_log.unlink(missing_ok=True)

    w, h = args.width, args.height
    if args.codec == "jpeg":
        frames = list(decode_jpeg(payloads))          # [(rgb, w, h)]
    else:
        tmp = Path(tempfile.mktemp(suffix=".h264" if args.codec == "h264" else ".hevc"))
        tmp.write_bytes(b"".join(payloads))
        frames = [(rgb, w, h) for rgb in decode_h264(tmp, w, h)]
        tmp.unlink(missing_ok=True)

    lats: list[float] = []
    matched = 0
    for idx, (rgb, fw, fh) in enumerate(frames):
        if idx >= len(arrivals):
            break
        pattern = read_pattern(rgb, fw, fh)
        times = paint.get(pattern)
        if not times:
            continue
        t_paint = max(t for t in times if t <= arrivals[idx])
        lat = (arrivals[idx] - t_paint) * 1000.0
        if -20 < lat < 400:
            lats.append(lat)
            matched += 1
    if not lats:
        print("no matches -- is the probe window visible (not covered)?")
        return 1
    lats_sorted = sorted(lats)
    def pct(p: float) -> float:
        return lats_sorted[min(len(lats_sorted) - 1, int(p * len(lats_sorted)))]
    out = {
        "codec": args.codec,
        "encoder": getattr(src, "name", "?"),
        "size": f"{w}x{h}",
        "fps": args.fps,
        "frames_decoded": len(frames),
        "frames_matched": matched,
        "grab_encode_ms_min": round(lats_sorted[0], 1),
        "grab_encode_ms_p50": round(pct(0.5), 1),
        "grab_encode_ms_p95": round(pct(0.95), 1),
        "grab_encode_ms_max": round(lats_sorted[-1], 1),
        "avg_frame_kb": round(sum(len(p) for p in payloads) / len(payloads) / 1024, 1),
    }
    print(json.dumps(out, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
