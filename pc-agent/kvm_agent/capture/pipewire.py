"""Wayland / PipeWire capture.

On Wayland the compositor owns the framebuffer, so there is no public API to
read the screen directly. The supported route is an xdg-desktop-portal
ScreenCast session: the user approves a screen in a dialog, the portal hands
back a PipeWire node id, and a PipeWire client consumes that node.

This module does not implement the portal handshake (it needs a DBus client
and a GUI prompt per session). Instead it drives ``ffmpeg -f pipewire`` on a
node id you obtained yourself, and otherwise fails with instructions.

Getting a node id without writing a portal client::

    # one-liner using the reference python bindings (needs xdg-desktop-portal)
    python3 - <<'PY'
    ...  # see README "Wayland" section for the gst-launch / portal snippet
    PY

The pragmatic alternative on a Wayland desktop is to keep an X11 session (or
XWayland) and use the x11grab / XGetImage backends, which only see X11
windows but need no permission prompts.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Optional

from .base import CaptureError
from .ffmpeg import FfmpegSource

log = logging.getLogger(__name__)

PORTAL_HELP = """\
PipeWire capture needs a ScreenCast node id from xdg-desktop-portal.

  * The portal shows a one-time "Share your screen" dialog; there is no way
    to capture Wayland output silently.
  * Start a session, e.g. with `gst-launch-1.0 pipewiresrc path=<node> ...`
    or any portal-aware client, then pass the node id:

        kvm-agent --source pipewire --pipewire-node <NODE_ID>

  * ffmpeg must be built with PipeWire input support (`ffmpeg -devices`
    must list `pipewire`; Ubuntu 22.04 ships ffmpeg 4.4 without it).

On this machine the X11 backends (`--source x11` / `--source ffmpeg`) work
without prompts whenever $DISPLAY exists, including under XWayland."""


def ffmpeg_pipewire_available() -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-devices"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.split()[:2] == ["D", "pipewire"] or " pipewire " in f" {line} "
               for line in out.splitlines())


class PipeWireSource(FfmpegSource):
    name = "pipewire"

    def __init__(self, cfg, node: Optional[int] = None):
        node_id = node if node is not None else cfg.pipewire_node
        if node_id is None:
            raise CaptureError(PORTAL_HELP)
        if not ffmpeg_pipewire_available():
            raise CaptureError(
                "ffmpeg has no pipewire input device.\n" + PORTAL_HELP)
        super().__init__(cfg, input_kind="pipewire", node=int(node_id))
        self.name = f"pipewire-node{int(node_id)}"


def wayland_session() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY"))
