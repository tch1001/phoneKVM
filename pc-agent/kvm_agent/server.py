"""HTTP + WebSocket server: streams JPEG frames and logs input events."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from aiohttp import WSMsgType, web

from . import capture
from . import input as input_mod
from .config import Config
from .wire import (CODEC_JPEG, CODEC_NAMES, FLAG_DUPLICATE, HEADER_SIZE,
                   PROTOCOL_VERSION, pack_packet)

log = logging.getLogger("kvm.server")
STATIC_DIR = Path(__file__).resolve().parent / "static"


@dataclass(eq=False)  # identity hash: clients live in a set
class Client:
    ws: web.WebSocketResponse
    peer: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=2))
    sent: int = 0
    dropped: int = 0
    closed: bool = False
    # seq -> CLOCK_MONOTONIC seconds when send_bytes() returned (latency probe)
    sent_at: dict = field(default_factory=dict)

    def offer(self, seq: int, packet: bytes) -> None:
        """Hand a frame to the client, dropping the oldest one if it is slow."""
        try:
            self.queue.put_nowait((seq, packet))
            return
        except asyncio.QueueFull:
            pass
        with contextlib.suppress(asyncio.QueueEmpty):
            self.queue.get_nowait()
        self.dropped += 1
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait((seq, packet))

    async def sender(self) -> None:
        while True:
            seq, packet = await self.queue.get()
            await self.ws.send_bytes(packet)
            self.sent += 1
            self.sent_at[seq] = time.monotonic()
            if len(self.sent_at) > 900:
                for old in sorted(self.sent_at)[:-600]:
                    self.sent_at.pop(old, None)


class ScreenServer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.out_w, self.out_h = cfg.effective_size()
        self.clients: set[Client] = set()
        self.source: Optional[capture.CaptureSource] = None
        self.seq = 0
        self.total_frames = 0
        self.total_bytes = 0
        self.dropped_frames = 0
        self._frame_ts: deque[float] = deque(maxlen=400)
        self._last_payload: Optional[bytes] = None
        # latency probe state: seq -> (captured_ms, payload bytes)
        self._meta: dict[int, tuple[int, int]] = {}
        self._lat: deque[dict] = deque(maxlen=600)
        self._lat_fh = None
        self._video_task: Optional[asyncio.Task] = None
        self._stats_task: Optional[asyncio.Task] = None
        self._stopping = False
        self._input_fh = None
        self._last_log = 0.0
        self.codec_int = 1
        from .wire import CODEC_BY_NAME
        self.codec_int = CODEC_BY_NAME.get(getattr(cfg, "codec", "jpeg"), CODEC_JPEG)

    # ------------------------------------------------------------------ app
    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self.handle_index)
        app.router.add_get("/index.html", self.handle_index)
        app.router.add_get("/healthz", self.handle_health)
        app.router.add_get("/ws", self.handle_ws)
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        return app

    async def _on_startup(self, _app: web.Application) -> None:
        if self.cfg.input_log:
            self._input_fh = open(self.cfg.input_log, "a", buffering=1, encoding="utf-8")
            log.info("input events appended to %s", self.cfg.input_log)
        if getattr(self.cfg, "lat_log", None):
            self._lat_fh = open(self.cfg.lat_log, "a", buffering=1, encoding="utf-8")
            log.info("latency samples appended to %s", self.cfg.lat_log)
        self._video_task = asyncio.create_task(self._video_loop(), name="video")
        self._stats_task = asyncio.create_task(self._stats_loop(), name="stats")
        log.info("streaming %dx%d @ %.0f fps, %s (capture: %s)", self.out_w,
                 self.out_h, self.cfg.fps,
                 f"jpeg q{self.cfg.quality}" if self.codec_int == CODEC_JPEG
                 else f"{self.cfg.codec} {self.cfg.bitrate or 'auto'} kbit/s",
                 capture.describe(self.cfg))

    async def _on_cleanup(self, _app: web.Application) -> None:
        self._stopping = True
        for task in (self._video_task, self._stats_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self.source is not None:
            await self.source.stop()
            self.source = None
        if self._input_fh:
            self._input_fh.close()
            self._input_fh = None
        if self._lat_fh:
            self._lat_fh.close()
            self._lat_fh = None

    # -------------------------------------------------------------- handlers
    async def handle_index(self, request: web.Request) -> web.StreamResponse:
        page = STATIC_DIR / "index.html"
        if not page.exists():
            return web.Response(status=500, text="index.html missing")
        resp = web.FileResponse(page)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    async def handle_health(self, request: web.Request) -> web.StreamResponse:
        return web.json_response(self.status())

    def status(self) -> dict:
        return {
            "ok": self.source is not None,
            "source": self.source.name if self.source else None,
            "protocol": PROTOCOL_VERSION,
            "header_bytes": HEADER_SIZE,
            "width": self.out_w,
            "height": self.out_h,
            "fps_target": self.cfg.fps,
            "codec": CODEC_NAMES.get(self.codec_int, "jpeg"),
            "quality": self.cfg.quality,
            "screen": {"width": self.cfg.screen_width, "height": self.cfg.screen_height},
            "frames": self.total_frames,
            "bytes": self.total_bytes,
            "clients": len(self.clients),
        }

    async def handle_ws(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse(heartbeat=20.0, compress=False,
                                   max_msg_size=1 << 20)
        await ws.prepare(request)
        client = Client(ws, request.remote or "?")
        self.clients.add(client)
        log.info("client connected: %s (%d total)", client.peer, len(self.clients))
        await self._send_json(client, {
            "t": "hello",
            "protocol": PROTOCOL_VERSION,
            "wire_format": "binary-frame-v1",
            "header_bytes": HEADER_SIZE,
            "codec": CODEC_NAMES.get(self.codec_int, "jpeg"),
            "codec_id": self.codec_int,
            "width": self.out_w,
            "height": self.out_h,
            "fps": self.cfg.fps,
            "quality": self.cfg.quality,
            "bitrate_kbps": self.cfg.bitrate,
            "gop": self.cfg.gop,
            "screen": {"width": self.cfg.screen_width, "height": self.cfg.screen_height},
            "source": self.source.name if self.source else None,
        })
        sender = asyncio.create_task(client.sender(), name=f"send-{client.peer}")
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._handle_text(client, msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await self._send_json(client, {
                        "t": "error",
                        "msg": "input must be JSON text; binary messages are "
                               "server->client frames only"})
                elif msg.type == WSMsgType.ERROR:
                    log.debug("ws error from %s: %s", client.peer, ws.exception())
                    break
        except (ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            client.closed = True
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender
            self.clients.discard(client)
            log.info("client disconnected: %s (sent=%d dropped=%d)", client.peer,
                     client.sent, client.dropped)
        return ws

    async def _send_json(self, client: Client, payload: dict) -> None:
        if client.ws.closed:
            return
        with contextlib.suppress(ConnectionResetError, RuntimeError):
            await client.ws.send_str(json.dumps(payload))

    async def _handle_text(self, client: Client, data: str) -> None:
        try:
            msg = json.loads(data)
        except json.JSONDecodeError as exc:
            await self._send_json(client, {"t": "error", "msg": f"invalid JSON: {exc}"})
            return
        if msg.get("t") == "lat":
            try:
                self._on_lat(client, msg)
            except Exception as exc:  # never let a probe kill the video socket
                log.warning("bad latency report: %s", exc)
            return
        try:
            ev = input_mod.parse(msg, self.input_ctx())
        except input_mod.InputError as exc:
            await self._send_json(client, {"t": "error", "msg": str(exc)})
            return
        if ev["t"] == "ping":
            # pc_ms lets the phone convert its CLOCK_BOOTTIME into the agent's
            # CLOCK_MONOTONIC: offset = pc_ms - (at_ms + rtt/2).  RTT over the
            # USB tunnel is well under a millisecond, so the offset is too.
            await self._send_json(client, {"t": "pong", "at_ms": ev["at_ms"],
                                           "pc_ms": int(time.monotonic() * 1000)
                                           & 0xFFFFFFFF})
            return

        self._record_input(client, ev)
        ack = {"t": "ack", "type": ev["t"]}
        for key in ("id", "x", "y", "px", "py", "button", "dx", "dy", "key",
                    "code", "action", "mods", "text"):
            if ev.get(key) is not None:
                ack[key] = ev[key]
        ack["server_ms"] = ev["at_ms"]
        await self._send_json(client, ack)

    def input_ctx(self) -> dict:
        return {
            "stream_width": self.out_w,
            "stream_height": self.out_h,
            "screen_width": self.cfg.screen_width or self.out_w,
            "screen_height": self.cfg.screen_height or self.out_h,
        }

    def _record_input(self, client: Client, ev: dict) -> None:
        line = input_mod.format_event(ev)
        log.info("input %s from %s", line, client.peer)
        if self._input_fh is not None:
            record = dict(ev)
            record["peer"] = client.peer
            record["type"] = ev["t"]
            self._input_fh.write(json.dumps(record, separators=(",", ":"),
                                            ensure_ascii=False) + "\n")

    # ------------------------------------------------------- latency probing
    @staticmethod
    def _signed_ms(a_ms: float, b_ms: float) -> float:
        """Signed difference of two 32-bit-wrapping millisecond stamps."""
        d = (a_ms - b_ms) % (1 << 32)
        return d - (1 << 32) if d > (1 << 31) else d

    def _on_lat(self, client: Client, msg: dict) -> None:
        """A phone-side latency report for one frame.

        The phone converts its own clock into the agent's CLOCK_MONOTONIC with
        the ping/pong offset, then reports three instants: websocket delivery,
        decoder output, and actual scan-out of the frame onto its display.
        Correlating them with what the agent knows gives the full breakdown.
        """
        try:
            seq = int(msg.get("seq", -1))
        except (TypeError, ValueError):
            return
        meta = self._meta.get(seq)
        sent = client.sent_at.get(seq)
        if meta is None or sent is None:
            return

        def num(key: str) -> float:
            v = msg.get(key)
            return float(v) if isinstance(v, (int, float)) else 0.0

        cap_ms = meta[0]
        # The phone already converted all three instants into this clock using
        # the ping/pong offset, so they are directly comparable (mod 2**32).
        recv_pc = num("recv")
        dec_pc = num("dec")
        rend_pc = num("rend")
        sent_ms = (sent * 1000.0) % (1 << 32)   # local, same space as cap

        sample = {
            "t": "lat",
            "seq": seq,
            "bytes": meta[1],
            # encoder output -> socket write returned (queue + send)
            "intake_ms": round(self._signed_ms(sent_ms, cap_ms), 2),
            # socket -> phone's websocket thread
            "transport_ms": round(self._signed_ms(recv_pc, sent_ms), 2),
            "decode_ms": round(dec_pc - recv_pc, 2),
            "render_ms": round(rend_pc - dec_pc, 2),
            "total_ms": round(self._signed_ms(rend_pc, cap_ms), 2),
        }
        # Everything above is measured from "encoder emitted the frame".  The
        # capture+encode part is added by the caller-side measurement of the
        # same pipeline (see bench/latency_probe.py) and is reported as
        # `total_ms` + capture_encode_ms.
        self._lat.append(sample)
        if self._lat_fh is not None:
            self._lat_fh.write(json.dumps(sample, separators=(",", ":")) + "\n")

    def latency_summary(self, window_s: float = 2.0) -> Optional[dict]:
        if not self._lat:
            return None
        keys = ("intake_ms", "transport_ms", "decode_ms", "render_ms", "total_ms")
        out = {"n": len(self._lat)}
        for k in keys:
            vals = sorted(s[k] for s in self._lat)
            out[k.replace("_ms", "")] = round(vals[len(vals) // 2], 1)
            out[k.replace("_ms", "_p95")] = round(vals[min(len(vals) - 1,
                                                           int(len(vals) * 0.95))], 1)
        self._lat.clear()
        return out

    # ------------------------------------------------------------- video path
    async def _video_loop(self) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                source = await capture.start(self.cfg)
            except capture.CaptureError as exc:
                log.error("capture unavailable: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            self.source = source
            backoff = 1.0
            try:
                async for frame in source.frames():
                    self._publish(frame)
            except capture.CaptureError as exc:
                log.warning("capture stopped (%s); restarting", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("unexpected capture error: %s", exc)
            finally:
                await source.stop()
                self.source = None
            await asyncio.sleep(0.5)

    def _publish(self, frame: capture.Frame) -> None:
        codec = getattr(frame, "codec", CODEC_JPEG)
        # Byte-identical dedupe is a JPEG-only optimisation: for an inter-frame
        # codec a "duplicate" frame is still a (tiny) P-frame the decoder needs.
        duplicate = (codec == CODEC_JPEG and self.cfg.dedupe
                     and self._last_payload is not None
                     and frame.payload == self._last_payload)
        if duplicate:
            return
        if codec == CODEC_JPEG:
            self._last_payload = frame.payload
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        flags = FLAG_DUPLICATE if duplicate else 0
        packet = pack_packet(self.seq, frame.width, frame.height,
                             frame.captured_ms, frame.payload, codec, flags)
        self.total_frames += 1
        self.total_bytes += len(packet)
        self._frame_ts.append(time.monotonic())
        self._meta[self.seq] = (frame.captured_ms & 0xFFFFFFFF, len(packet))
        if len(self._meta) > 1500:
            for old in sorted(self._meta)[:-1000]:
                self._meta.pop(old, None)
        for client in self.clients:
            client.offer(self.seq, packet)

    async def _stats_loop(self) -> None:
        last_sent = 0
        last_bytes = 0
        while True:
            await asyncio.sleep(2.0)
            now = time.monotonic()
            recent = [t for t in self._frame_ts if now - t <= 2.0]
            fps = len(recent) / 2.0 if recent else 0.0
            frames = self.total_frames - last_sent
            nbytes = self.total_bytes - last_bytes
            last_sent, last_bytes = self.total_frames, self.total_bytes
            avg = nbytes / max(1, frames)
            payload = {
                "t": "stats",
                "fps": round(fps, 2),
                "avg_frame_bytes": int(avg),
                "frames": self.total_frames,
                "clients": len(self.clients),
                "dropped": sum(c.dropped for c in self.clients),
                "width": self.out_w,
                "height": self.out_h,
            }
            for client in list(self.clients):
                await self._send_json(client, payload)
            lat = self.latency_summary()
            if lat:
                log.info("latency: e2e %5.1f ms (p95 %5.1f) | intake %4.1f "
                         "transport %4.1f decode %4.1f render %4.1f ms  n=%d",
                         lat["total"], lat["total_p95"], lat["intake"],
                         lat["transport"], lat["decode"], lat["render"], lat["n"])
            if now - self._last_log >= 10.0:
                self._last_log = now
                log.info("stream: %.1f fps, %.1f kB/frame, clients=%d, total=%d",
                         fps, avg / 1024.0, len(self.clients), self.total_frames)


def local_ip() -> str:
    """Best guess at the LAN IP a phone could reach (no traffic is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
