"""Entry point: ``python -m kvm_agent``."""

from __future__ import annotations

import logging
import sys

from aiohttp import web

from .capture import detect_screen_size
from .config import parse_args
from .server import ScreenServer, local_ip


def main(argv: list[str] | None = None) -> int:
    cfg = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, cfg.log_level),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )

    if not cfg.screen_width:
        size = detect_screen_size(cfg.x11_display)
        if size:
            cfg.screen_width, cfg.screen_height = size

    server = ScreenServer(cfg)
    app = server.build_app()

    lan = local_ip()
    print(f"\n  kvm-lite pc-agent")
    print(f"  stream : {server.out_w}x{server.out_h} @ {cfg.fps:g} fps, "
          f"JPEG q{cfg.quality}, header {server.status()['header_bytes']} B")
    print(f"  capture: {cfg.source} (auto order: ffmpeg -> x11 -> pipewire)")
    print(f"  desktop: {cfg.screen_width}x{cfg.screen_height}")
    print(f"  open   : http://127.0.0.1:{cfg.port}/         (test page)")
    print(f"           http://{lan}:{cfg.port}/   (from the phone)")
    print(f"           ws://{lan}:{cfg.port}/ws    (frame stream)")
    print(f"  health : http://127.0.0.1:{cfg.port}/healthz\n", flush=True)

    web.run_app(app, host=cfg.host, port=cfg.port, print=None,
                access_log=None, shutdown_timeout=3.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
