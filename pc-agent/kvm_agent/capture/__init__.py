"""Capture backend detection and fallback."""

from __future__ import annotations

import logging
import shutil
from typing import Optional

from .base import CaptureError, CaptureSource, Frame
from .ffmpeg import FfmpegSource
from .h264 import EncodedSource
from .pipewire import PipeWireSource, wayland_session
from .x11 import X11Source, detect_screen_size

log = logging.getLogger(__name__)

__all__ = ["CaptureError", "CaptureSource", "Frame", "start", "candidates",
           "detect_screen_size", "describe"]


def _auto_order(cfg) -> list[str]:
    has_x11 = bool(cfg.x11_display)
    if wayland_session() and cfg.pipewire_node is not None:
        order = ["pipewire"]
    else:
        order = []
    if has_x11:
        order += ["ffmpeg", "x11"]
    order += ["pipewire"]
    return order


def _make(name: str, cfg) -> CaptureSource:
    if name in ("ffmpeg-h264x", "ffmpeg-hevcx"):
        from ..wire import CODEC_H264, CODEC_HEVC
        if not shutil.which("ffmpeg"):
            raise CaptureError("ffmpeg not installed (apt install ffmpeg)")
        codec = CODEC_H264 if name == "ffmpeg-h264x" else CODEC_HEVC
        return EncodedSource(cfg, codec, getattr(cfg, "encoder", "auto"))
    if name == "ffmpeg":
        if not shutil.which("ffmpeg"):
            raise CaptureError("ffmpeg not installed (apt install ffmpeg)")
        return FfmpegSource(cfg, input_kind="x11grab")
    if name == "x11":
        return X11Source(cfg)
    if name == "pipewire":
        return PipeWireSource(cfg)
    raise CaptureError(f"unknown capture source {name!r}")


def candidates(cfg) -> list[str]:
    if getattr(cfg, "codec", "jpeg") != "jpeg":
        # The inter-frame path is a single x11grab -> encoder pipeline; there is
        # no software fallback that is worth the latency, so be explicit.
        return ["ffmpeg-h264x" if cfg.codec == "h264" else "ffmpeg-hevcx"]
    if cfg.source == "auto":
        return _auto_order(cfg)
    return [cfg.source]


async def start(cfg) -> CaptureSource:
    """Open the first backend that actually delivers a frame."""
    tried: list[str] = []
    last_error: Optional[Exception] = None
    for name in candidates(cfg):
        try:
            source = _make(name, cfg)
        except CaptureError as exc:
            log.warning("capture %s unavailable: %s", name, str(exc).splitlines()[0])
            tried.append(f"{name}: {exc}")
            last_error = exc
            continue
        try:
            await source.start()
            if tried:
                log.info("capture backend %s started after falling back from %s",
                         source.name, ", ".join(tried))
            return source
        except CaptureError as exc:
            last_error = exc
            tried.append(f"{name}: {exc}")
            log.warning("capture %s failed: %s", name, exc)
            await source.stop()
            continue
    raise CaptureError("no working capture backend. Tried:\n  " +
                       "\n  ".join(tried or ["none"]) ) from last_error


def describe(cfg) -> str:
    if getattr(cfg, "codec", "jpeg") != "jpeg":
        enc = getattr(cfg, "encoder", "auto")
        return f"x11grab -> {cfg.codec} ({enc})"
    return " -> ".join(candidates(cfg))
