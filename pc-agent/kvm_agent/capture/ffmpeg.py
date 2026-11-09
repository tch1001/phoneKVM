"""ffmpeg-based capture: x11grab (or pipewire) -> MJPEG on stdout.

ffmpeg does the scaling and the JPEG encoding in C, so Python only has to
split the MJPEG byte stream into frames. That is the cheapest option and it
is also the most portable one on X11.
"""

from __future__ import annotations

import logging
import os
import select
import shutil
import subprocess
import time
from typing import Optional

from ..wire import jpeg_dimensions
from .base import CaptureError, CaptureSource, Frame
from .encode import qscale_for_quality

log = logging.getLogger(__name__)

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"


class MjpegParser:
    """Split a concatenated MJPEG byte stream into individual JPEG images."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, chunk: bytes) -> None:
        self.buf += chunk

    def next_frame(self) -> Optional[bytes]:
        buf = self.buf
        start = buf.find(SOI)
        if start < 0:
            # keep a trailing 0xFF, it may be the first byte of the next SOI
            if len(buf) > 1:
                del buf[:-1]
            return None
        if start:
            del buf[:start]
        end = buf.find(EOI, 2)
        if end < 0:
            return None
        frame = bytes(buf[:end + 2])
        del buf[:end + 2]
        return frame

    def __len__(self) -> int:
        return len(self.buf)


def _scale_filter(cfg, out_w: int, out_h: int) -> str:
    if cfg.height and cfg.height > 0:
        # letterbox instead of stretching when the desktop aspect differs
        return (f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:"
                f"flags=fast_bilinear,"
                f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color=black,format=yuv420p")
    return f"scale={out_w}:-2:flags=fast_bilinear,format=yuv420p"


class FfmpegSource(CaptureSource):
    """x11grab (default) or pipewire input via an ffmpeg subprocess."""

    paced = False  # ffmpeg paces the capture with -framerate

    def __init__(self, cfg, input_kind: str = "x11grab", node: Optional[int] = None):
        super().__init__(cfg)
        if input_kind not in ("x11grab", "pipewire"):
            raise ValueError(input_kind)
        self.input_kind = input_kind
        self.node = node
        self.name = f"ffmpeg-{input_kind}"
        self.proc: Optional[subprocess.Popen] = None
        self.parser = MjpegParser()
        self._stderr_tail = bytearray()
        self._pending: Optional[bytes] = None
        self._ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        self.media_w = 0
        self.media_h = 0

    # -- process management ------------------------------------------------
    def _build_cmd(self) -> list[str]:
        cfg = self.cfg
        q = qscale_for_quality(cfg.quality)
        cmd = [self._ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
               "-fflags", "nobuffer"]
        if self.input_kind == "x11grab":
            cmd += ["-f", "x11grab", "-draw_mouse", "1", "-framerate", f"{cfg.fps:g}",
                    "-i", cfg.x11_display]
        else:
            cmd += ["-f", "pipewire", "-framerate", f"{cfg.fps:g}",
                    "-i", str(self.node)]
        cmd += ["-vf", _scale_filter(cfg, self.out_width, self.out_height),
                "-q:v", str(q), "-an", "-f", "mjpeg", "-y", "pipe:1"]
        return cmd

    def open(self) -> None:
        cmd = self._build_cmd()
        log.debug("starting capture: %s", " ".join(cmd))
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0, close_fds=True)
        except FileNotFoundError as exc:
            raise CaptureError(f"ffmpeg not found: {exc}") from exc

        # wait for the first complete frame to prove the backend actually works
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            frame = self._pump_towards_frame(deadline)
            if frame is not None:
                w, h = jpeg_dimensions(frame)
                self.media_w, self.media_h = w, h
                self._pending = frame
                log.debug("%s: media size %dx%d", self.name, w, h)
                return
            if self.proc.poll() is not None:
                break
        tail = self._stderr_tail.decode("utf-8", "replace").strip()
        self.close()
        raise CaptureError(f"ffmpeg produced no frames: {tail or 'timeout'}")

    def _read_available(self, timeout: float) -> bool:
        """Read whatever is available from stdout/stderr within `timeout`."""
        assert self.proc is not None and self.proc.stdout is not None
        fds = [self.proc.stdout.fileno()]
        err_fd = self.proc.stderr.fileno() if self.proc.stderr else None
        if err_fd is not None:
            fds.append(err_fd)
        try:
            ready, _, _ = select.select(fds, [], [], timeout)
        except (OSError, ValueError):
            return False
        got = False
        for fd in ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                continue
            if not chunk:
                continue
            got = True
            if fd == fds[0]:
                self.parser.feed(chunk)
            else:
                self._stderr_tail += chunk
                if len(self._stderr_tail) > 4096:
                    del self._stderr_tail[:-4096]
        return got

    def _pump_towards_frame(self, deadline: float) -> Optional[bytes]:
        while True:
            frame = self.parser.next_frame()
            if frame is not None:
                return frame
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if not self._read_available(min(remaining, 1.0)):
                if self.proc is not None and self.proc.poll() is not None:
                    return None

    def _read_frame(self) -> Optional[Frame]:
        if self.proc is None:
            return None
        if self._pending is not None:
            pending, self._pending = self._pending, None
            return self._make_frame(pending)

        deadline = time.monotonic() + 3.0
        while True:
            frame = self.parser.next_frame()
            if frame is not None:
                return self._make_frame(frame)
            if self.proc.poll() is not None:
                # process died: drain whatever is still buffered, then stop
                if not self._read_available(0.0):
                    return None
                continue
            if time.monotonic() > deadline:
                log.debug("%s: no frame for 3s (stalled?)", self.name)
                return None
            self._read_available(0.5)

    def _make_frame(self, jpeg: bytes) -> Frame:
        try:
            w, h = jpeg_dimensions(jpeg)
        except ValueError:
            w, h = self.media_w, self.media_h
        if (w, h) != (self.media_w, self.media_h):
            log.info("%s: media size changed to %dx%d", self.name, w, h)
            self.media_w, self.media_h = w, h
        return Frame(jpeg, w, h, self.monotonic_ms())

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        except Exception:  # pragma: no cover - best effort cleanup
            pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass
