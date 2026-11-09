"""Capture backend interface and the shared async frame iterator."""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple, Optional

from ..wire import CODEC_JPEG

log = logging.getLogger(__name__)


class CaptureError(RuntimeError):
    """Raised when a backend cannot start or dies mid-stream."""


class Frame(NamedTuple):
    """One encoded screen frame.

    ``payload`` is codec-specific: a complete JPEG for CODEC_JPEG, or one
    Annex-B access unit for CODEC_H264 / CODEC_HEVC.  ``captured_ms`` is the
    earliest CLOCK_MONOTONIC timestamp the agent can observe for this frame
    (right after the grab for in-process capture, right after the encoder
    emitted it for the ffmpeg pipelines) -- it is what the phone subtracts to
    compute end-to-end latency.
    """

    payload: bytes
    width: int
    height: int
    captured_ms: int  # CLOCK_MONOTONIC ms, low 32 bits
    codec: int = CODEC_JPEG
    key: bool = True          # only meaningful for inter-frame codecs

    @property
    def jpeg(self) -> bytes:  # compatibility alias for the JPEG-only paths
        return self.payload


class CaptureSource:
    """A screen capture backend.

    Backends are synchronous (blocking) and are therefore executed on a
    dedicated single worker thread, which also keeps Xlib calls on one thread.

    Subclasses implement ``open()``, ``_read_frame()`` and ``close()``.
    ``paced`` says whether ``frames()`` should throttle to ``cfg.fps``
    (True) or whether the backend paces itself (False, e.g. ffmpeg).
    """

    name = "base"
    paced = True

    def __init__(self, cfg):
        self.cfg = cfg
        self.out_width, self.out_height = cfg.effective_size()
        self._executor = ThreadPoolExecutor(max_workers=1,
                                            thread_name_prefix=f"cap-{self.name}")
        self._first: Optional[Frame] = None

    # --- backend hooks ---------------------------------------------------
    def open(self) -> None:
        raise NotImplementedError

    def _read_frame(self) -> Optional[Frame]:
        """Blocking read of one frame; None means transient failure / no frame."""
        raise NotImplementedError

    def close(self) -> None:
        pass

    # --- async front end -------------------------------------------------
    async def start(self) -> None:
        """Open the backend and validate it by grabbing one real frame."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self.open)
        self._first = await loop.run_in_executor(self._executor, self._read_frame)
        if self._first is None:
            await loop.run_in_executor(self._executor, self.close)
            raise CaptureError(f"{self.name}: backend produced no frame on startup")
        log.info("%s: %dx%d @ %.1f fps, first frame %d bytes", self.name,
                 self.out_width, self.out_height, self.cfg.fps,
                 len(self._first.payload))

    async def frames(self):
        """Async iterator of Frame. Raises CaptureError if the backend dies."""
        loop = asyncio.get_running_loop()
        if self._first is not None:
            first, self._first = self._first, None
            yield first
        interval = 1.0 / self.cfg.fps
        deadline = loop.time()
        while True:
            if self.paced:
                now = loop.time()
                if deadline > now:
                    await asyncio.sleep(deadline - now)
                deadline = max(deadline + interval, loop.time() - interval)
            frame = await loop.run_in_executor(self._executor, self._read_frame)
            if frame is None:
                if self.paced:
                    continue
                raise CaptureError(f"{self.name}: capture stream ended")
            yield frame

    async def stop(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(self._executor, self.close)
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def monotonic_ms() -> int:
        return int(time.monotonic() * 1000) & 0xFFFFFFFF
