"""X11 capture through libX11/libXext via ctypes (no external Python deps).

Tries MIT-SHM (XShmGetImage) first because it avoids the round trip through
the X socket, and silently degrades to XGetImage when the server refuses
(observed on some Xorg/glamor builds where XShmAttach succeeds but
ShmGetImage returns success=0).

All Xlib calls must happen on a single thread -- CaptureSource guarantees that
by running every capture call on its dedicated worker thread.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import time
from ctypes import (CDLL, CFUNCTYPE, POINTER, Structure, byref, c_char_p, c_int,
                    c_uint, c_ulong, c_void_p, cast, string_at)
from typing import Optional

from PIL import Image, ImageOps

from .base import CaptureError, CaptureSource, Frame
from .encode import encode_jpeg

log = logging.getLogger(__name__)

ZPIXMAP = 2
LSB_FIRST = 0
IPC_PRIVATE = 0
IPC_CREAT = 0o1000
IPC_RMID = 0


class XImage(Structure):
    _fields_ = [
        ("width", c_int), ("height", c_int), ("xoffset", c_int), ("format", c_int),
        ("data", c_void_p), ("byte_order", c_int), ("bitmap_unit", c_int),
        ("bitmap_bit_order", c_int), ("bitmap_pad", c_int), ("depth", c_int),
        ("bytes_per_line", c_int), ("bits_per_pixel", c_int), ("red_mask", c_ulong),
        ("green_mask", c_ulong), ("blue_mask", c_ulong), ("obdata", c_void_p),
    ]


class XShmSegmentInfo(Structure):
    _fields_ = [("shmseg", c_ulong), ("shmid", c_int), ("shmaddr", c_void_p),
                ("readOnly", c_int)]


def _load(names: list[str]) -> CDLL:
    errors = []
    for n in names:
        try:
            return CDLL(n)
        except OSError as exc:  # pragma: no cover
            errors.append(f"{n}: {exc}")
    raise CaptureError("cannot load X11 libraries (" + "; ".join(errors) + ")")


_SYSTEM_LIBS = ["/usr/lib/x86_64-linux-gnu/libX11.so.6", "libX11.so.6",
                "/usr/lib64/libX11.so.6"]
_SYSTEM_XEXT = ["/usr/lib/x86_64-linux-gnu/libXext.so.6", "libXext.so.6",
                "/usr/lib64/libXext.so.6"]


class X11Capture:
    """Thin synchronous wrapper around XOpenDisplay + image grabbing."""

    def __init__(self, display_name: Optional[str] = None):
        self.display_name = display_name
        self.x11 = _load(_SYSTEM_LIBS)
        self.xext = _load(_SYSTEM_XEXT)
        self.libc = CDLL("libc.so.6", use_errno=True)
        self._setup_prototypes()
        self.dpy: Optional[int] = None
        self.root = 0
        self.width = self.height = 0
        self.depth = 24
        self.byte_order = LSB_FIRST
        self.mode = "none"
        self._img = None
        self._shm = None
        self._shm_addr = None
        self._buf = None
        self._errors = 0
        self._cb = None

    # -- prototypes --------------------------------------------------------
    def _setup_prototypes(self) -> None:
        x11, xext, libc = self.x11, self.xext, self.libc
        x11.XOpenDisplay.restype = c_void_p
        x11.XOpenDisplay.argtypes = [c_char_p]
        x11.XCloseDisplay.argtypes = [c_void_p]
        x11.XDefaultRootWindow.restype = c_ulong
        x11.XDefaultRootWindow.argtypes = [c_void_p]
        x11.XDefaultScreen.restype = c_int
        x11.XDefaultScreen.argtypes = [c_void_p]
        x11.XDefaultDepth.restype = c_int
        x11.XDefaultDepth.argtypes = [c_void_p, c_int]
        x11.XDisplayWidth.restype = c_int
        x11.XDisplayWidth.argtypes = [c_void_p, c_int]
        x11.XDisplayHeight.restype = c_int
        x11.XDisplayHeight.argtypes = [c_void_p, c_int]
        x11.XGetImage.restype = POINTER(XImage)
        x11.XGetImage.argtypes = [c_void_p, c_ulong, c_int, c_int, c_uint, c_uint,
                                  c_ulong, c_int]
        x11.XDestroyImage.argtypes = [POINTER(XImage)]
        x11.XSync.argtypes = [c_void_p, c_int]
        x11.XSetErrorHandler.restype = c_void_p
        x11.XSetErrorHandler.argtypes = [c_void_p]

        xext.XShmQueryExtension.restype = c_int
        xext.XShmQueryExtension.argtypes = [c_void_p]
        xext.XShmAttach.restype = c_int
        xext.XShmAttach.argtypes = [c_void_p, POINTER(XShmSegmentInfo)]
        xext.XShmDetach.restype = c_int
        xext.XShmDetach.argtypes = [c_void_p, POINTER(XShmSegmentInfo)]
        xext.XShmCreateImage.restype = POINTER(XImage)
        xext.XShmCreateImage.argtypes = [c_void_p, c_void_p, c_uint, c_int, c_char_p,
                                         POINTER(XShmSegmentInfo), c_uint, c_uint]
        xext.XShmGetImage.restype = c_int
        xext.XShmGetImage.argtypes = [c_void_p, c_ulong, POINTER(XImage), c_int,
                                      c_int, c_ulong]

        libc.shmget.restype = c_int
        libc.shmget.argtypes = [c_int, ctypes.c_size_t, c_int]
        libc.shmat.restype = c_void_p
        libc.shmat.argtypes = [c_int, c_void_p, c_int]
        libc.shmdt.argtypes = [c_void_p]
        libc.shmctl.restype = c_int
        libc.shmctl.argtypes = [c_int, c_int, c_void_p]

    def _install_error_handler(self) -> None:
        """Swallow async X errors so a failing XShm probe cannot spam stderr."""

        @CFUNCTYPE(c_int, c_void_p, c_void_p)
        def handler(_dpy, _event):  # pragma: no cover - called from C
            self._errors += 1
            return 0

        self._cb = handler
        self.x11.XSetErrorHandler(cast(handler, c_void_p))

    # -- lifecycle ---------------------------------------------------------
    def open(self, probe_capture: bool = True) -> None:
        name = (self.display_name or "").encode() or None
        self.dpy = self.x11.XOpenDisplay(name)
        if not self.dpy:
            raise CaptureError(
                f"cannot open X display {self.display_name or '$DISPLAY'!r} "
                f"(is DISPLAY set? are you on a Wayland session?)")
        self._install_error_handler()
        screen = self.x11.XDefaultScreen(self.dpy)
        self.root = self.x11.XDefaultRootWindow(self.dpy)
        self.width = self.x11.XDisplayWidth(self.dpy, screen)
        self.height = self.x11.XDisplayHeight(self.dpy, screen)
        self.depth = self.x11.XDefaultDepth(self.dpy, screen)
        log.debug("X11 %s: root %dx%d depth %d", self.display_name or "$DISPLAY",
                  self.width, self.height, self.depth)
        if not probe_capture:
            return
        if not self._try_shm():
            self._setup_xgetimage()

    def _try_shm(self) -> bool:
        if not self.xext.XShmQueryExtension(self.dpy):
            log.debug("X11: MIT-SHM extension not available")
            return False
        seg = XShmSegmentInfo()
        size = self.width * self.height * 4
        shmid = self.libc.shmget(IPC_PRIVATE, size, IPC_CREAT | 0o600)
        if shmid < 0:
            log.debug("X11: shmget failed")
            return False
        addr = self.libc.shmat(shmid, None, 0)
        if not addr or addr == c_void_p(-1).value:
            self.libc.shmctl(shmid, IPC_RMID, None)
            return False
        seg.shmseg = 0
        seg.shmid = shmid
        seg.shmaddr = addr
        seg.readOnly = 0
        img = self.xext.XShmCreateImage(self.dpy, None, self.depth, ZPIXMAP, None,
                                        byref(seg), self.width, self.height)
        if not img:
            self.libc.shmdt(addr)
            self.libc.shmctl(shmid, IPC_RMID, None)
            return False
        attached = self.xext.XShmAttach(self.dpy, byref(seg))
        self.x11.XSync(self.dpy, 0)
        ok = bool(attached) and self._errors == 0
        if ok:
            before = self._errors
            ok = bool(self.xext.XShmGetImage(self.dpy, self.root, img, 0, 0,
                                             0xFFFFFFFF))
            self.x11.XSync(self.dpy, 0)
            ok = ok and self._errors == before
        if not ok:
            log.info("X11: XShmGetImage unusable on this server, using XGetImage")
            self.xext.XShmDetach(self.dpy, byref(seg))
            self.x11.XSync(self.dpy, 0)
            self.libc.shmdt(addr)
            self.libc.shmctl(shmid, IPC_RMID, None)
            return False
        self._shm, self._img, self._shm_addr = seg, img, addr
        self.mode = "xshm"
        self.byte_order = img.contents.byte_order
        log.info("X11: using MIT-SHM capture (%dx%d)", self.width, self.height)
        return True

    def _setup_xgetimage(self) -> None:
        self.mode = "xgetimage"
        # probe once to learn byte order and validate the path
        img = self.x11.XGetImage(self.dpy, self.root, 0, 0, 1, 1, 0xFFFFFFFF, ZPIXMAP)
        if not img:
            raise CaptureError("XGetImage failed")
        self.byte_order = img.contents.byte_order
        self.x11.XDestroyImage(img)
        log.info("X11: using XGetImage capture (%dx%d)", self.width, self.height)

    def grab(self) -> tuple[bytes, int, int, int]:
        """Return (raw bytes, bytes_per_line, width, height) for the full screen."""
        if self.mode == "xshm":
            ok = self.xext.XShmGetImage(self.dpy, self.root, self._img, 0, 0,
                                        0xFFFFFFFF)
            if not ok:
                raise CaptureError("XShmGetImage failed mid-stream")
            img = self._img
            raw = string_at(self._shm_addr, img.contents.bytes_per_line * self.height)
            return raw, img.contents.bytes_per_line, self.width, self.height

        img = self.x11.XGetImage(self.dpy, self.root, 0, 0, self.width, self.height,
                                 0xFFFFFFFF, ZPIXMAP)
        if not img:
            raise CaptureError("XGetImage failed mid-stream")
        try:
            bpl = img.contents.bytes_per_line
            raw = string_at(img.contents.data, bpl * self.height)
            return raw, bpl, img.contents.width, img.contents.height
        finally:
            self.x11.XDestroyImage(img)

    def to_image(self, raw: bytes, bpl: int, w: int, h: int) -> Image.Image:
        mode = "BGRX" if self.byte_order == LSB_FIRST else "XRGB"
        stride = bpl - w * 4
        if stride < 0:
            stride = 0
        return Image.frombuffer("RGB", (w, h), raw, "raw", mode, 0, 1)

    def close(self) -> None:
        try:
            if self.mode == "xshm" and self._shm is not None:
                self.xext.XShmDetach(self.dpy, byref(self._shm))
                self.x11.XSync(self.dpy, 0)
                if self._shm_addr:
                    self.libc.shmdt(self._shm_addr)
                    self.libc.shmctl(self._shm.shmid, IPC_RMID, None)
                self._shm = None
                self._shm_addr = None
        except Exception:  # pragma: no cover
            pass
        finally:
            if self.dpy:
                try:
                    self.x11.XCloseDisplay(self.dpy)
                except Exception:  # pragma: no cover
                    pass
                self.dpy = None


class X11Source(CaptureSource):
    """Capture through X11 and encode with Pillow (quality is exact)."""

    name = "x11"
    paced = True

    def __init__(self, cfg):
        super().__init__(cfg)
        self.cap: Optional[X11Capture] = None
        self.subsampling = 2
        self.media_w = self.media_h = 0

    def open(self) -> None:
        self.cap = X11Capture(self.cfg.x11_display)
        self.cap.open()
        self.media_w, self.media_h = self.cap.width, self.cap.height
        if not self.cfg.screen_width:
            self.cfg.screen_width, self.cfg.screen_height = self.cap.width, self.cap.height

    def _read_frame(self) -> Optional[Frame]:
        if self.cap is None:
            return None
        t0 = time.monotonic()
        try:
            raw, bpl, w, h = self.cap.grab()
        except CaptureError:
            raise
        img = self.cap.to_image(raw, bpl, w, h)
        target = (self.out_width, self.out_height)
        if img.size != target:
            if img.width * target[1] == img.height * target[0]:
                img = img.resize(target, Image.BILINEAR)  # same aspect: no letterbox
            else:
                img = ImageOps.pad(img, target, color=(0, 0, 0), method=Image.BILINEAR)
        jpeg = encode_jpeg(img, self.cfg.quality, self.subsampling)
        if (w, h) != (self.media_w, self.media_h):
            self.media_w, self.media_h = w, h
        log.debug("x11 frame: grab+encode %.1f ms", (time.monotonic() - t0) * 1000)
        return Frame(jpeg, self.out_width, self.out_height, self.monotonic_ms())

    def close(self) -> None:
        if self.cap is not None:
            self.cap.close()
            self.cap = None


def detect_screen_size(display_name: Optional[str] = None) -> Optional[tuple[int, int]]:
    """Best-effort screen size lookup used for input coordinate mapping."""
    try:
        cap = X11Capture(display_name)
        cap.open(probe_capture=False)
        size = (cap.width, cap.height)
        cap.close()
        return size
    except Exception as exc:
        log.debug("cannot detect X11 screen size: %s", exc)
        return None
