#!/usr/bin/env python3
"""Encoder sweep: size / quality / CPU cost per frame for each candidate scheme.

Runs against a fixed lossless source clip so numbers are repeatable.
Quality is measured as SSIM against the source, after scaling to the
resolution the phone actually displays (1920x1080).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parent
OUT = BENCH / "out"
OUT.mkdir(exist_ok=True)

# (label, ffmpeg encoder args, size filter, ext)
CONFIGS = [
    # ---- JPEG (intra-only, what the project ships today) -------------------
    ("jpeg 720p q6 (today)", ["-q:v", "6"], 720, "mjpeg"),
    ("jpeg 1080p q6", ["-q:v", "6"], 1080, "mjpeg"),
    ("jpeg 1080p q3", ["-q:v", "3"], 1080, "mjpeg"),
    # ---- H.264 ------------------------------------------------------------
    ("x264 superfast zl crf18 1080p",
     ["-c:v", "libx264", "-preset", "superfast", "-tune", "zerolatency",
      "-crf", "18", "-g", "120", "-bf", "0"], 1080, "mkv"),
    ("x264 ultrafast zl crf18 1080p",
     ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
      "-crf", "18", "-g", "120", "-bf", "0"], 1080, "mkv"),
    ("nvenc p1 ll cbr6M 1080p",
     ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll", "-rc", "cbr",
      "-b:v", "6M", "-maxrate", "6M", "-bufsize", "400k", "-zerolatency", "1",
      "-g", "120", "-bf", "0"], 1080, "mkv"),
    ("nvenc p1 ll cbr10M 1080p",
     ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll", "-rc", "cbr",
      "-b:v", "10M", "-maxrate", "10M", "-bufsize", "600k", "-zerolatency", "1",
      "-g", "120", "-bf", "0"], 1080, "mkv"),
    ("nvenc p1 ll cbr16M 1080p",
     ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll", "-rc", "cbr",
      "-b:v", "16M", "-maxrate", "16M", "-bufsize", "1M", "-zerolatency", "1",
      "-g", "120", "-bf", "0"], 1080, "mkv"),
    ("nvenc p1 ll cbr10M 720p",
     ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll", "-rc", "cbr",
      "-b:v", "10M", "-maxrate", "10M", "-bufsize", "600k", "-zerolatency", "1",
      "-g", "120", "-bf", "0"], 720, "mkv"),
    # ---- HEVC -------------------------------------------------------------
    ("hevc_nvenc p1 ll cbr5M 1080p",
     ["-c:v", "hevc_nvenc", "-preset", "p1", "-tune", "ll", "-rc", "cbr",
      "-b:v", "5M", "-maxrate", "5M", "-bufsize", "350k", "-zerolatency", "1",
      "-g", "120", "-bf", "0"], 1080, "mkv"),
    ("hevc_nvenc p1 ll cbr8M 1080p",
     ["-c:v", "hevc_nvenc", "-preset", "p1", "-tune", "ll", "-rc", "cbr",
      "-b:v", "8M", "-maxrate", "8M", "-bufsize", "500k", "-zerolatency", "1",
      "-g", "120", "-bf", "0"], 1080, "mkv"),
]

BENCH_RE = re.compile(r"bench: utime=([0-9.]+)s stime=([0-9.]+)s")


def run(cmd: list[str]) -> tuple[float, float, float]:
    t0 = time.monotonic()
    p = subprocess.run(cmd, capture_output=True, text=True)
    wall = time.monotonic() - t0
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}\n{p.stderr[-1500:]}")
    err = p.stderr
    m = BENCH_RE.search(err)
    cpu = float(m.group(1)) + float(m.group(2)) if m else 0.0
    return wall, cpu, err


def frame_count(path: Path) -> int:
    p = subprocess.run(["ffprobe", "-v", "error", "-count_frames",
                        "-select_streams", "v:0", "-show_entries",
                        "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return int(p.stdout.strip())
    except ValueError:
        return 0


def ssim_vs_source(src: Path, enc: Path, decode_extra: list[str]) -> float:
    """Mean SSIM of the decoded stream against the source, at 1080p display size."""
    infmt = ["-f", "mjpeg"] if enc.suffix == ".mjpeg" else []
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", *infmt, "-i", str(enc), "-i", str(src),
         "-lavfi", "[0:v]scale=1920:1080:flags=bicubic,format=yuv420p[a];"
                   "[1:v]format=yuv420p[b];[a][b]ssim=stats_file=-",
         "-f", "null", "-"] + decode_extra,
        capture_output=True, text=True)
    vals = []
    for line in p.stdout.splitlines():
        if line.startswith("n:"):
            m = re.search(r"All:([0-9.]+)", line)
            if m:
                vals.append(float(m.group(1)))
    return sum(vals) / len(vals) if vals else float("nan")


def main() -> None:
    sources = [BENCH / "src_scroll.mkv", BENCH / "src_lossless.mkv"]
    rows = []
    for src in sources:
        nsrc = frame_count(src)
        for label, args, height, ext in CONFIGS:
            h = height
            vf = (f"scale=-2:{h}:flags=fast_bilinear" if h != 1080 else "null")
            out = OUT / (re.sub(r"[^a-z0-9]+", "_", label.lower()) + "." + ext)
            cmd = ["ffmpeg", "-hide_banner", "-benchmark", "-nostats", "-y", "-i", str(src),
                   "-vf", vf + ",format=yuv420p", *args, "-an",
                   "-f", "mjpeg" if ext == "mjpeg" else "matroska", str(out)]
            wall, cpu, _ = run(cmd)
            size = out.stat().st_size
            ssim = ssim_vs_source(src, out, [])
            rows.append({
                "source": src.name,
                "config": label,
                "height": h,
                "kb_per_frame": round(size / 1024 / max(1, nsrc), 1),
                "mbps_at_20fps": round(size * 8 * 20 / max(1, nsrc) / 1e6, 2),
                "ssim": round(ssim, 4),
                "cpu_ms_per_frame": round(cpu * 1000 / max(1, nsrc), 2),
                "encode_fps": round(nsrc / max(1e-6, wall), 0),
            })
            print(rows[-1], flush=True)
    (BENCH / "sweep_results.json").write_text(json.dumps(rows, indent=2))
    print("\n=== summary ===")
    hdr = f"{'source':<16} {'config':<34} {'kB/f':>7} {'Mbit/s@20':>9} {'SSIM':>7} {'cpu ms/f':>9} {'fps':>7}"
    print(hdr)
    for r in rows:
        print(f"{r['source']:<16} {r['config']:<34} {r['kb_per_frame']:>7} "
              f"{r['mbps_at_20fps']:>9} {r['ssim']:>7} {r['cpu_ms_per_frame']:>9} "
              f"{int(r['encode_fps']):>7}")


if __name__ == "__main__":
    sys.exit(main())
