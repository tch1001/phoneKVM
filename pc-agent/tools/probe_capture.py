#!/usr/bin/env python3
"""Measure a capture backend directly, without the network.

    python tools/probe_capture.py                 # auto backend
    python tools/probe_capture.py --source x11 -n 40
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kvm_agent.capture import start  # noqa: E402
from kvm_agent.config import Config  # noqa: E402


async def run(args) -> int:
    cfg = Config(source=args.source, width=args.width, height=args.height,
                 fps=args.fps, quality=args.quality)
    t0 = time.monotonic()
    source = await start(cfg)
    startup = (time.monotonic() - t0) * 1000
    print(f"backend      {source.name} (startup {startup:.0f} ms)")
    print(f"target       {cfg.effective_size()[0]}x{cfg.effective_size()[1]} "
          f"@ {cfg.fps:g} fps q{cfg.quality}")

    sizes, stamps, dims = [], [], set()
    t_first = None
    try:
        async for frame in source.frames():
            now = time.monotonic()
            if t_first is None:
                t_first = now
            sizes.append(len(frame.jpeg))
            stamps.append(now)
            dims.add((frame.width, frame.height))
            if len(sizes) >= args.frames:
                break
    finally:
        await source.stop()

    duration = stamps[-1] - t_first if len(stamps) > 1 else 0.0
    fps = (len(stamps) - 1) / duration if duration else 0.0
    intervals = [(b - a) * 1000 for a, b in zip(stamps, stamps[1:])]
    print(f"frames       {len(sizes)} in {duration:.2f} s -> {fps:.2f} fps")
    print(f"dims         {sorted(dims)}")
    print(f"frame size   avg {statistics.fmean(sizes):.0f} B "
          f"({statistics.fmean(sizes) / 1024:.1f} kB), min {min(sizes)}, "
          f"max {max(sizes)}")
    print(f"interval ms  mean {statistics.fmean(intervals):.1f}, "
          f"max {max(intervals):.1f}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--source", default="auto", choices=["auto", "ffmpeg", "x11", "pipewire"])
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--fps", type=float, default=20.0)
    p.add_argument("--quality", type=int, default=70)
    p.add_argument("--frames", "-n", type=int, default=40)
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
