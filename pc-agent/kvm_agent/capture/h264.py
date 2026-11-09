"""Low-latency H.264/HEVC capture: x11grab -> NVENC -> RTP/loopback -> AUs.

ffmpeg does capture, scaling and hardware encoding; this module only receives
the encoded access units and hands one per WebSocket frame to the server.

Why RTP on loopback instead of reading a raw Annex-B pipe
--------------------------------------------------------
A raw ``-f h264`` byte stream has no frame delimiters: the only way to know an
access unit has ended is to see the *next* one begin.  That silently costs a
full frame of latency (33 ms at 30 fps, 50 ms at 20 fps) on every frame, and it
was the largest single contributor measured in this project's first prototype.
RTP packets carry an explicit "marker" bit for the last packet of an access
unit, so a frame can be forwarded the moment it is encoded.  ffmpeg's RTP muxer
packetises H.264 NAL units (single NAL / STAP-A / FU-A) over UDP, and since it
is loopback UDP there is no retransmission, no head-of-line blocking and no
kernel socket buffering to speak of.

The result is Annex-B access units (start codes re-inserted) which is exactly
what MediaCodec wants in ByteBuffer mode.

Other latency-relevant encoder settings:

* ``-zerolatency 1 -delay 0 -rc-lookahead 0`` and a 2-frame VBV so the encoder
  cannot build a queue;
* ``-bf 0``: no B-frames, no reordering delay;
* ``-g <2 s> -forced-idr 1``: rare but real IDRs, so the phone can resync;
* SPS/PPS are repeated before every IDR, and are also cached here as ``csd``.
"""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import time
from typing import Optional

from ..wire import CODEC_H264, CODEC_HEVC
from .base import CaptureError, CaptureSource, Frame

log = logging.getLogger(__name__)

H264_IDR = 5
HEVC_IDR = (16, 17, 18, 19, 20, 21)
START_CODE = b"\x00\x00\x00\x01"


def _iter_nals(buf: bytes):
    """Yield (offset, nal_bytes) for each Annex-B NAL in ``buf``."""
    n = len(buf)
    i = 0
    starts = []
    while True:
        j = buf.find(b"\x00\x00\x01", i)
        if j < 0 or j + 3 >= n:
            break
        starts.append(j + 3)
        i = j + 3
    for k, s in enumerate(starts):
        e = starts[k + 1] - 3 if k + 1 < len(starts) else n
        if e > s:
            yield s, buf[s:e]


class RtpAssembler:
    """Rebuild Annex-B access units from an RTP/H.264 (or H.265) stream."""

    def __init__(self, hevc: bool = False):
        self.hevc = hevc
        self.buf = bytearray()
        self.aus: list[bytes] = []
        self.last_seq: Optional[int] = None
        self.lost = 0
        self.csd_parts: dict[int, bytes] = {}

    def feed_packet(self, pkt: bytes) -> None:
        if len(pkt) < 13:
            return
        marker = pkt[1] & 0x80
        seq = int.from_bytes(pkt[2:4], "big")
        if self.last_seq is not None and ((seq - self.last_seq) & 0xFFFF) > 1:
            self.lost += (seq - self.last_seq - 1) & 0xFFFF
        self.last_seq = seq
        # 12-byte RTP header; no CSRC/extensions are emitted by ffmpeg here.
        payload = pkt[12:]
        if len(payload) < 1:
            return
        if self.hevc:
            self._feed_hevc(payload, marker)
        else:
            self._feed_h264(payload, marker)

    # -- H.264 ------------------------------------------------------------
    def _feed_h264(self, payload: bytes, marker: int) -> None:
        t = payload[0] & 0x1F
        if 1 <= t <= 23:                       # single NAL unit
            self.buf += START_CODE + payload
            self._note_ps(t, payload)
        elif t == 24:                          # STAP-A
            i = 1
            while i + 2 <= len(payload):
                ln = int.from_bytes(payload[i:i + 2], "big")
                i += 2
                nal = payload[i:i + ln]
                i += ln
                if not nal:
                    continue
                self.buf += START_CODE + nal
                self._note_ps(nal[0] & 0x1F, nal)
        elif t == 28 and len(payload) >= 2:    # FU-A
            fu = payload[1]
            if fu & 0x80:                      # start fragment: rebuild header
                hdr = bytes([(payload[0] & 0xE0) | (fu & 0x1F)])
                self.buf += START_CODE + hdr + payload[2:]
            else:
                self.buf += payload[2:]
        elif t == 29:                          # FU-B, ffmpeg does not use it
            log.debug("ignoring FU-B packet")
        if marker:
            self._flush()

    # -- H.265 ------------------------------------------------------------
    def _feed_hevc(self, payload: bytes, marker: int) -> None:
        t = (payload[0] >> 1) & 0x3F
        if 0 < t < 48:                         # single NAL unit
            self.buf += START_CODE + payload
            self._note_ps(t, payload)
        elif t == 48:                          # aggregation packet
            i = 2
            while i + 2 <= len(payload):
                ln = int.from_bytes(payload[i:i + 2], "big")
                i += 2
                nal = payload[i:i + ln]
                i += ln
                if not nal:
                    continue
                self.buf += START_CODE + nal
                self._note_ps((nal[0] >> 1) & 0x3F, nal)
        elif t == 49 and len(payload) >= 3:    # FU
            fu = payload[2]
            if fu & 0x80:
                hdr = bytes([(payload[0] & 0x81) | ((fu & 0x3F) << 1),
                             payload[1]])
                self.buf += START_CODE + hdr + payload[3:]
            else:
                self.buf += payload[3:]
        if marker:
            self._flush()

    def _note_ps(self, t: int, nal: bytes) -> None:
        if self.hevc:
            if t in (32, 33, 34):
                self.csd_parts[t] = nal
        elif t in (7, 8):
            self.csd_parts[t] = nal

    def _flush(self) -> None:
        if self.buf:
            self.aus.append(bytes(self.buf))
            self.buf = bytearray()

    def csd(self) -> Optional[bytes]:
        order = (32, 33, 34) if self.hevc else (7, 8)
        parts = [self.csd_parts[t] for t in order if t in self.csd_parts]
        if not parts:
            return None
        return b"".join(START_CODE + p for p in parts)

    def is_key(self, au: bytes) -> bool:
        for _off, nal in _iter_nals(au):
            if not nal:
                continue
            if self.hevc:
                if ((nal[0] >> 1) & 0x3F) in HEVC_IDR:
                    return True
            elif (nal[0] & 0x1F) == H264_IDR:
                return True
        return False


class EncodedSource(CaptureSource):
    """x11grab -> h264_nvenc/hevc_nvenc/libx264 -> RTP/loopback -> access units."""

    paced = False  # ffmpeg paces capture with -framerate

    def __init__(self, cfg, codec: int = CODEC_H264, encoder: str = "auto"):
        super().__init__(cfg)
        self.codec = codec
        self.vcodec = "h264" if codec == CODEC_H264 else "hevc"
        self.encoder = encoder
        self.name = f"ffmpeg-{self.vcodec}-{self._encoder_name()}"
        self.proc: Optional[subprocess.Popen] = None
        self.rtp = RtpAssembler(hevc=(codec == CODEC_HEVC))
        self.sock: Optional[socket.socket] = None
        self.port = 0
        self._stderr_tail = bytearray()
        self._pending: list[Frame] = []
        self._retried_sw = False
        self._ffmpeg = shutil.which("ffmpeg") or "ffmpeg"

    # -- encoder selection -------------------------------------------------
    def _encoder_name(self) -> str:
        if self.encoder != "auto":
            return self.encoder
        if shutil.which("nvidia-smi"):
            try:
                out = subprocess.run(["nvidia-smi", "-L"], capture_output=True,
                                     text=True, timeout=2).stdout
                if "GPU" in out:
                    return "nvenc"
            except Exception:  # pragma: no cover
                pass
        return "x264"

    def _encoder_args(self) -> list[str]:
        cfg = self.cfg
        fps = max(1.0, float(cfg.fps))
        enc = self._encoder_name()
        kbps = int(cfg.bitrate) if cfg.bitrate else int(
            round(10_000 * (self.out_width * self.out_height) / (1920 * 1080)
                  * fps / 30.0))
        kbps = max(800, kbps)
        # 1 s GOP: bounds the wait for a decodable IDR when the phone connects
        # mid-stream (a keyframe request would need ffmpeg >= 5 for NVENC) and
        # costs little bitrate on mostly-static desktop content.
        gop = max(1, int(cfg.gop) if cfg.gop else int(round(fps)))
        vbv = max(64_000, int(kbps * 1000 / fps * 2))   # ~2 frames of buffer
        args = ["-c:v", "libx264" if enc == "x264" else f"{self.vcodec}_{enc}"]
        if enc == "nvenc":
            args += ["-preset", "p1", "-tune", "ll", "-rc", "cbr",
                     "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k",
                     "-bufsize", str(vbv), "-zerolatency", "1", "-delay", "0",
                     "-rc-lookahead", "0", "-surfaces", "4",
                     "-forced-idr", "1", "-aud", "1"]
        else:
            args += ["-preset", "ultrafast", "-tune", "zerolatency",
                     "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k",
                     "-bufsize", str(vbv), "-x264-params",
                     f"nal-hrd=cbr:force-cfr=1:aud=1:bframes=0:"
                     f"sync-lookahead=0:keyint={gop}:min-keyint={gop}"]
        args += ["-g", str(gop), "-keyint_min", str(gop), "-bf", "0", "-an"]
        if not cfg.hw_upload:
            # with a CUDA filter graph the frames are already NV12 on the GPU;
            # asking for yuv420p would force an impossible software conversion
            args += ["-pix_fmt", "yuv420p"]
        if cfg.intra_refresh:
            if enc == "nvenc":
                args += ["-intra-refresh", "1"]
            else:
                args += ["-x264-params", f"intra-refresh=1:keyint={gop}"]
        return args

    def _build_cmd(self) -> list[str]:
        cfg = self.cfg
        cmd = [self._ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
               "-fflags", "nobuffer", "-flags", "low_delay"]
        if cfg.hw_upload:
            cmd += ["-init_hw_device", "cuda=cu", "-filter_hw_device", "cu"]
        cmd += [
               "-f", "x11grab", "-draw_mouse", "1",
               "-framerate", f"{cfg.fps:g}", "-i", cfg.x11_display]
        cmd += ["-vf", self._scale_filter(), *self._encoder_args(),
                "-flush_packets", "1",
                "-f", "rtp", "-payload_type", "96",
                f"rtp://127.0.0.1:{self.port}?pkt_size=1400"]
        return cmd

    def _scale_filter(self) -> str:
        cfg, w, h = self.cfg, self.out_width, self.out_height
        if cfg.hw_upload:
            # Keep the frame on the GPU: upload the BGRX surface once and let
            # CUDA do the colour conversion (and scaling) into an NV12 surface
            # that NVENC can consume directly.  ffmpeg 4.4's scale_cuda has no
            # `format` option, its output is NV12 already.
            return f"format=bgr0,hwupload_cuda,scale_cuda={w}:{h}"
        if cfg.height and cfg.height > 0:
            return (f"scale={w}:{h}:force_original_aspect_ratio=decrease:"
                    f"flags=fast_bilinear,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:"
                    f"color=black,format=nv12")
        return f"scale={w}:-2:flags=fast_bilinear,format=nv12"

    # -- process -----------------------------------------------------------
    def open(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.settimeout(0.5)
        cmd = self._build_cmd()
        log.debug("starting encoder: %s", " ".join(cmd))
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE, bufsize=0,
                                         close_fds=True)
        except FileNotFoundError as exc:
            raise CaptureError(f"ffmpeg not found: {exc}") from exc
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            au = self._next_au(deadline)
            if au is not None:
                self._pending.append(self._make_frame(au))
                log.debug("%s: encoder up on udp/%d, csd=%s", self.name,
                          self.port, "yes" if self.rtp.csd() else "no")
                return
            if self.proc.poll() is not None:
                break
        tail = self._stderr_tail.decode("utf-8", "replace").strip()
        self.close()
        if getattr(self.cfg, "hw_upload", False) and not self._retried_sw:
            # CUDA filter graph unavailable (old driver, no GPU): fall back to
            # the swscale path once instead of failing the stream.
            log.warning("%s: GPU filter graph failed (%s); retrying with swscale",
                        self.name, tail.splitlines()[-1] if tail else "timeout")
            self._retried_sw = True
            self.cfg.hw_upload = False
            self.rtp = RtpAssembler(hevc=(self.codec == CODEC_HEVC))
            return self.open()
        raise CaptureError(f"encoder produced no access units: "
                           f"{tail or 'timeout'}")

    def _drain_stderr(self) -> None:
        if self.proc is None or self.proc.stderr is None:
            return
        try:
            while True:
                chunk = self.proc.stderr.read1(4096) if hasattr(self.proc.stderr, "read1") \
                    else self.proc.stderr.read(4096)
                if not chunk:
                    break
                self._stderr_tail += chunk
                if len(self._stderr_tail) > 4096:
                    del self._stderr_tail[:-4096]
                if len(chunk) < 4096:
                    break
        except (BlockingIOError, OSError, ValueError):
            pass

    def _next_au(self, deadline: float) -> Optional[bytes]:
        """Receive RTP until one complete access unit is available."""
        while True:
            if self.rtp.aus:
                return self.rtp.aus.pop(0)
            if time.monotonic() > deadline:
                return None
            if self.sock is None:
                return None
            try:
                pkt, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                self._drain_stderr()
                if self.proc is not None and self.proc.poll() is not None:
                    return None
                continue
            except OSError:
                return None
            self.rtp.feed_packet(pkt)
            while len(self.rtp.aus) > 8:      # never let the queue grow
                self.rtp.aus.pop(0)

    def _make_frame(self, au: bytes) -> Frame:
        # Timestamp when the access unit left the encoder: the earliest point
        # the agent can observe.  Grab+encode is measured separately.
        return Frame(au, self.out_width, self.out_height, self.monotonic_ms(),
                     self.codec, self.rtp.is_key(au))

    def _read_frame(self) -> Optional[Frame]:
        if self.proc is None:
            return None
        if self._pending:
            return self._pending.pop(0)
        au = self._next_au(time.monotonic() + 3.0)
        if au is None:
            if self.proc.poll() is not None:
                self._drain_stderr()
                return None
            log.debug("%s: no access unit for 3s", self.name)
            return None
        return self._make_frame(au)

    def close(self) -> None:
        proc, self.proc = self.proc, None
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        except Exception:  # pragma: no cover
            pass
        try:
            if proc.stderr:
                proc.stderr.close()
        except Exception:
            pass
