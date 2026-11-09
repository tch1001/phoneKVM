"""Command-line configuration for the kvm-lite PC agent."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field


@dataclass
class Config:
    # network
    host: str = "0.0.0.0"
    port: int = 8080
    # video
    width: int = 1280
    height: int = 720          # 0 => derive from the screen aspect ratio
    fps: float = 20.0
    quality: int = 70
    codec: str = "jpeg"        # jpeg | h264 | hevc
    encoder: str = "auto"      # auto | nvenc | x264 (h264/hevc only)
    bitrate: int = 0           # kbit/s, 0 = derive from resolution+fps
    gop: int = 0               # frames between keyframes, 0 = 2 seconds
    intra_refresh: bool = False
    hw_upload: bool = False    # do the pixel format conversion on the GPU
    # capture
    source: str = "auto"       # auto | ffmpeg | x11 | pipewire
    display: str | None = None  # X11 display, default $DISPLAY or :0
    pipewire_node: int | None = None
    # misc
    dedupe: bool = False
    input_log: str | None = None
    lat_log: str | None = None  # JSONL of per-frame latency samples
    log_level: str = "INFO"
    screen_width: int = 0      # detected at startup, used for coord mapping
    screen_height: int = 0

    @property
    def x11_display(self) -> str:
        return self.display or os.environ.get("DISPLAY") or ":0"

    def effective_size(self) -> tuple[int, int]:
        """Resolve the output size, keeping the aspect ratio when height == 0."""
        w = max(2, int(self.width) & ~1)
        if self.height and self.height > 0:
            return w, max(2, int(self.height) & ~1)
        sw, sh = self.screen_width or 1920, self.screen_height or 1080
        h = int(round(w * sh / sw)) & ~1
        return w, max(2, h)


def parse_args(argv: list[str] | None = None) -> Config:
    p = argparse.ArgumentParser(
        prog="kvm-agent",
        description="Screen streaming agent for the kvm-lite phone-as-KVM project.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host", default="0.0.0.0", help="bind address")
    p.add_argument("--port", type=int, default=8080, help="HTTP + WebSocket port")
    p.add_argument("--width", type=int, default=1280, help="output width in pixels")
    p.add_argument("--height", type=int, default=720,
                   help="output height in pixels (0 = derive from screen aspect)")
    p.add_argument("--fps", type=float, default=20.0, help="frame rate cap")
    p.add_argument("--quality", type=int, default=70, help="JPEG quality 1-100")
    p.add_argument("--codec", default="jpeg", choices=["jpeg", "h264", "hevc"],
                   help="video codec: intra JPEG (legacy) or inter-frame H.264/HEVC")
    p.add_argument("--encoder", default="auto", choices=["auto", "nvenc", "x264"],
                   help="H.264/HEVC encoder backend (auto = nvenc if present)")
    p.add_argument("--bitrate", type=int, default=0,
                   help="target bitrate in kbit/s (0 = derive, ~10 Mbit/s at 1080p30)")
    p.add_argument("--gop", type=int, default=0,
                   help="frames between IDRs (0 = 2 seconds)")
    p.add_argument("--intra-refresh", action="store_true",
                   help="spread intra updates across the GOP instead of full IDRs")
    p.add_argument("--hw-upload", action="store_true",
                   help="convert/scaled on the GPU (cuda) instead of with swscale")
    p.add_argument("--source", default="auto", choices=["auto", "ffmpeg", "x11", "pipewire"],
                   help="capture backend; 'auto' probes in order ffmpeg -> x11 -> pipewire")
    p.add_argument("--display", default=None, help="X11 display (default: $DISPLAY or :0)")
    p.add_argument("--pipewire-node", type=int, default=None,
                   help="PipeWire node id for `ffmpeg -f pipewire` (Wayland screencast)")
    p.add_argument("--dedupe", action="store_true",
                   help="skip sending byte-identical consecutive frames")
    p.add_argument("--input-log", default=None,
                   help="append received input events to this JSONL file")
    p.add_argument("--lat-log", default=None,
                   help="append per-frame latency samples from the phone to this JSONL file")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    a = p.parse_args(argv)

    if not 1 <= a.quality <= 100:
        p.error("--quality must be between 1 and 100")
    if not 1 <= a.fps <= 120:
        p.error("--fps must be between 1 and 120")
    if a.width < 16:
        p.error("--width must be >= 16")

    return Config(host=a.host, port=a.port, width=a.width, height=a.height,
                  fps=float(a.fps), quality=a.quality, source=a.source,
                  display=a.display, pipewire_node=a.pipewire_node,
                  dedupe=a.dedupe, input_log=a.input_log, log_level=a.log_level,
                  codec=a.codec, encoder=a.encoder, bitrate=a.bitrate,
                  gop=a.gop, intra_refresh=a.intra_refresh, lat_log=a.lat_log,
                  hw_upload=a.hw_upload)
