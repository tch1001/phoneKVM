#!/usr/bin/env python3
"""End-to-end latency measurement against a running agent + the real phone.

Drives the phone app (adb), collects the per-frame latency reports the app sends
back, and optionally does an independent optical cross-check: a probe window
paints an 8-bit counter (bench/x11_clock.py), so a screenshot of the phone tells
us exactly which frame is on the panel and when it was painted on the PC.

    bench/measure_e2e.py --port 8095 --label h264-1080p30 --seconds 20 --optical
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ADB = ["adb", "-s", "RR8MC0223XL"]
# probe window position/geometry (bench/x11_clock.py defaults)
PROBE_X, PROBE_Y, CELL, BORDER, CELLS = 420, 240, 60, 10, 8
# the video is drawn 1:1 centred-ish on the 2400x1080 panel; the probe keeps its
# position inside the video rect, so display coords = video_origin + probe coords
VIDEO_X = 278


def pct(vals: list[float], p: float) -> float:
    if not vals:
        return float("nan")
    s = sorted(vals)
    return s[min(len(s) - 1, int(p * len(s)))]


def adb(*args: str, timeout: int = 60) -> str:
    return subprocess.run(ADB + list(args), capture_output=True, text=True,
                          timeout=timeout).stdout


def optical(probe_log: Path, shot: Path, t_before: float, t_after: float) -> dict:
    """Read the counter pattern from a phone screenshot and convert to latency."""
    from PIL import Image
    paints: dict[int, list[float]] = {}
    for line in probe_log.read_text().splitlines():
        rec = json.loads(line)
        paints.setdefault(rec["c"], []).append(rec["t"])
    im = Image.open(shot).convert("RGB")
    px = im.load()
    w, h = im.size
    # locate the probe window by its magenta frame: robust against rotation,
    # scaling and letterboxing
    # the probe background is pure magenta; require long horizontal runs so
    # magenta-ish text elsewhere on the desktop cannot fool the detector
    xs, ys = [], []
    for y in range(0, h, 1):
        run = 0
        for x in range(0, w, 1):
            r, g, b = px[x, y]
            if r > 170 and b > 150 and g < 110:
                run += 1
                if run >= 60:
                    for k in range(x - run + 1, x + 1):
                        xs.append(k)
                    ys.append(y)
            else:
                run = 0
    if not xs:
        return {"error": "probe window not visible in screenshot"}
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    scale = (x1 - x0) / float(CELLS * CELL + 2 * BORDER)
    value = 0
    for i in range(CELLS):
        cx = int(x0 + scale * (BORDER + i * CELL + CELL // 2))
        cy = int(y0 + scale * (BORDER + CELL // 2))
        r, g, b = px[cx, cy]
        value = (value << 1) | (1 if (r + g + b) > 380 else 0)
    times = paints.get(value, [])
    if not times:
        return {"pattern": value, "error": "pattern not painted"}
    # the frame on screen cannot be newer than the screenshot command starting
    t_frame = max(t for t in times if t <= t_after)
    return {
        "pattern": value,
        "latency_from_cmd_start_ms": round((t_before - t_frame) * 1000, 1),
        "latency_from_cmd_end_ms": round((t_after - t_frame) * 1000, 1),
        "pattern_age_ms": round((t_after - t_frame) * 1000, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--label", default="run")
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--warmup", type=float, default=7)
    ap.add_argument("--optical", action="store_true")
    ap.add_argument("--url", default=None)
    args = ap.parse_args()

    lat_file = Path(f"/tmp/lat-{args.port}.jsonl")
    lat_file.write_text("")
    probe_log = Path(f"/tmp/probe-{args.port}.jsonl")
    probe = None
    if args.optical:
        probe = subprocess.Popen(
            [sys.executable, str(BENCH / "x11_clock.py"), "--seconds",
             str(args.warmup + args.seconds + 8), "--out", str(probe_log)],
            stdout=subprocess.DEVNULL)
        time.sleep(0.8)

    url = args.url or f"ws://127.0.0.1:{args.port}/ws"
    adb("reverse", f"tcp:{args.port}", f"tcp:{args.port}")
    adb("shell", "am", "force-stop", "com.kvmlite.app")
    adb("logcat", "-c")
    adb("shell", "am", "start", "-n", "com.kvmlite.app/.MainActivity",
        "--es", "url", url, "--ez", "connect", "true")
    time.sleep(args.warmup)

    t_end = time.monotonic() + args.seconds
    while time.monotonic() < t_end:
        time.sleep(1.0)

    samples = []
    for line in lat_file.read_text().splitlines():
        try:
            samples.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    if not samples:
        print("no latency samples -- is the app connected?", file=sys.stderr)
        return 1

    stages = ("intake_ms", "transport_ms", "decode_ms", "render_ms", "total_ms")
    out = {"label": args.label, "port": args.port, "samples": len(samples)}
    for s in stages:
        vals = [x[s] for x in samples]
        out[s] = {"p50": round(pct(vals, 0.5), 1), "p95": round(pct(vals, 0.95), 1),
                  "mean": round(sum(vals) / len(vals), 1)}
    out["kbps"] = int(sum(x["bytes"] for x in samples) * 8 / 1000.0
                      / max(0.1, args.seconds))
    out["kb_per_frame"] = round(sum(x["bytes"] for x in samples) / len(samples) / 1024, 1)

    if args.optical:
        shots = []
        for i in range(3):
            shot = Path(f"/tmp/phone-opt-{args.port}-{i}.png")
            t0 = time.monotonic()
            with open(shot, "wb") as fh:
                subprocess.run(ADB + ["exec-out", "screencap", "-p"], stdout=fh,
                               timeout=60)
            t1 = time.monotonic()
            shots.append(optical(probe_log, shot, t0, t1))
            time.sleep(1.0)
        out["optical"] = shots
    if probe:
        probe.terminate()
        probe.wait(timeout=5)

    print(json.dumps(out, indent=2))
    Path(BENCH / "results").mkdir(exist_ok=True)
    (BENCH / "results" / f"{args.label}.json").write_text(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
