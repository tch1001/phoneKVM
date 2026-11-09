# kvm-lite — PC agent (screen → phone over WebSocket)

PC-side half of a "phone as KVM" project: captures this Linux desktop, encodes
each frame as JPEG and pushes it over a WebSocket to the phone (or to any
browser). Input events travel back on the *same* WebSocket; this agent only
validates and logs them — the real injection path is the USB HID gadget owned
by another component, which consumes the normalised event records.

```
   ┌──────────── pc-agent (this project) ────────────┐
   │  capture ──► scale/JPEG ──► 24B header + payload│──► ws://0.0.0.0:8080/ws
   │  ffmpeg x11grab | X11 ctypes | pipewire          │◄── JSON input events
   │  logs + optional JSONL (input.jsonl)             │
   └──────────────────────────────────────────────────┘
              ▲ http://0.0.0.0:8080/  (test page)
```

* Video: 1280x720 @ 20 fps, JPEG quality 70 by default (both configurable).
* Transport: one binary WebSocket message per frame, fixed 24-byte header.
* Clients: many at once; a slow client drops its own frames, never blocks others.
* Input: mouse/keyboard JSON from clients → validated, coordinate-mapped, logged.

## Install

```bash
cd ~/kvm-lite/pc-agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt        # aiohttp, pillow

# optional but recommended: ffmpeg for the primary capture backend
sudo apt install ffmpeg                          # ffmpeg 4.4 on Ubuntu 22.04 is fine
```

Python ≥ 3.10. No numpy, no OpenCV, no GStreamer, no portal DBus client.

## Run

```bash
.venv/bin/python -m kvm_agent                    # 0.0.0.0:8080, 1280x720@20, q70 (legacy JPEG)
.venv/bin/python -m kvm_agent --codec h264 --width 1920 --height 1080 --fps 30 \
    --bitrate 12000                              # NVENC H.264, the low-latency path
.venv/bin/python -m kvm_agent --codec hevc --bitrate 5000   # HEVC, ~40% less bitrate
.venv/bin/python -m kvm_agent --source x11       # force the ctypes X11 backend (JPEG only)
.venv/bin/python -m kvm_agent --dedupe           # JPEG only: stop sending identical frames
```

`--codec h264|hevc` is the recommended mode: it is ~2x lower end-to-end latency,
~2x lower bitrate and visibly sharper than the JPEG path (see **Latency** below).
The JPEG path stays the default so existing scripts/tests behave identically.

Then open `http://<pc-lan-ip>:8080/` on the phone (USB tethering or adb
`reverse tcp:8080 tcp:8080` both work), or on this PC's browser for a quick
check. `GET /healthz` returns JSON status.

### Options

| flag | default | meaning |
|---|---|---|
| `--host` / `--port` | `0.0.0.0` / `8080` | HTTP + WebSocket bind |
| `--width` / `--height` | `1280` / `720` | output size; `--height 0` keeps the desktop aspect ratio |
| `--fps` | `20` | frame rate cap (1–120) |
| `--quality` | `70` | JPEG quality (1–100) |
| `--codec` | `jpeg` | `jpeg` (intra-only, legacy) \| `h264` \| `hevc` (inter-frame) |
| `--encoder` | `auto` | `auto` \| `nvenc` \| `x264` (H.264/HEVC backend) |
| `--bitrate` | `0` | target kbit/s, `0` = derive (~10 Mbit/s at 1080p30) |
| `--gop` | `0` | frames between IDRs, `0` = 1 second |
| `--intra-refresh` | off | spread intra updates over the GOP instead of full IDRs |
| `--hw-upload` | off | do the RGB→NV12 conversion on the GPU (CUDA) |
| `--lat-log` | — | per-frame latency samples (JSONL) reported by the phone |
| `--source` | `auto` | `auto` \| `ffmpeg` \| `x11` \| `pipewire` |
| `--display` | `$DISPLAY` | X11 display to capture |
| `--pipewire-node` | — | PipeWire screencast node id (Wayland) |
| `--dedupe` | off | skip byte-identical consecutive frames |
| `--input-log` | off | append input events to a JSONL file |
| `--log-level` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |

## Wire format (protocol v1)

### Binary frames — server → client

One WebSocket **binary** message per frame: a fixed 24-byte header immediately
followed by the JPEG bytes. Little-endian throughout.

| offset | size | type | field |
|---:|---:|---|---|
| 0 | 4 | `char[4]` | magic `KVM1` (`4b 56 4d 31`) |
| 4 | 1 | `uint8` | protocol version = `1` |
| 5 | 1 | `uint8` | codec: `1` = JPEG, `2` = H.264 Annex-B access unit, `3` = HEVC Annex-B access unit |
| 6 | 2 | `uint16` | flags (bit0 = duplicate of previous frame, else 0) |
| 8 | 4 | `uint32` | sequence number, starts at 1, wraps at 2³² |
| 12 | 4 | `uint32` | pixel width of the JPEG |
| 16 | 4 | `uint32` | pixel height of the JPEG |
| 20 | 4 | `uint32` | capture time, `CLOCK_MONOTONIC` ms truncated to 32 bits |
| 24 | n | `uint8[]` | payload: one JPEG (`FF D8` … `FF D9`), or exactly one Annex-B access unit for H.264/HEVC |

For codec 2/3 the payload is a complete access unit (`00 00 00 01` + NALs,
starting with an AUD, with SPS/PPS repeated in front of every IDR). A client can
feed it to MediaCodec unchanged, one WebSocket message per input buffer.

Real JPEG frame captured from this machine (1280x720, seq 3197, 34599 bytes total):

```
4b 56 4d 31  01 01 00 00  7d 0c 00 00  00 05 00 00  d0 02 00 00  b6 55 10 02
magic/ver/codec/flags      seq=3197     w=1280       h=720        ts=34624950
... ff d8 ff fe ... (JPEG payload, 34575 bytes) ... ff d9
```

Parsing in Python:

```python
import struct
magic, ver, codec, flags, seq, w, h, ts = struct.unpack_from("<4sBBHIIII", buf, 0)
jpeg = buf[24:]
```

Parsing in JavaScript:

```js
const dv = new DataView(buf);
const seq = dv.getUint32(8, true), w = dv.getUint32(12, true), h = dv.getUint32(16, true);
const blob = new Blob([new Uint8Array(buf, 24)], { type: "image/jpeg" });
const bitmap = await createImageBitmap(blob);
```

A fixed header (rather than JSON metadata) keeps per-frame overhead at 24 bytes
and lets a client render without parsing JSON on the hot path. The header is
still self-describing: magic + version + dimensions mean a client can resync or
reject bad frames without out-of-band state.

### Text messages — server → client

```json
{"t":"hello","protocol":1,"wire_format":"binary-frame-v1","header_bytes":24,
 "codec":"jpeg","width":1280,"height":720,"fps":20.0,"quality":70,
 "screen":{"width":1920,"height":1080},"source":"ffmpeg-x11grab"}
```
sent once on connect (clients should size their canvas from it).

```json
{"t":"stats","fps":20.0,"avg_frame_bytes":124818,"frames":1027,
 "clients":1,"dropped":0,"width":1280,"height":720}
```
every 2 s.

```json
{"t":"ack","type":"mousemove","id":"mv1","x":640,"y":360,"px":960,"py":540,
 "server_ms":3235543887}
```
one per accepted input event — `x`,`y` echo stream pixels, `px`,`py` are desktop
pixels after scaling.

```json
{"t":"error","msg":"unknown event type 'foo'"}
{"t":"pong","at_ms":3235543900}
```

### Text messages — client → server (input)

All are JSON objects with a `t` field. `x`/`y` are pixels in the **streamed
image** (0…width-1, 0…height-1); the agent maps them onto the desktop with
`px = x * screen_width / stream_width`.

| `t` | fields | notes |
|---|---|---|
| `mousemove` | `x`, `y` | |
| `mousedown` / `mouseup` | `x`, `y`, `button` | button: `left`/`middle`/`right` or `1`–`5`, default `left` |
| `click` | `x`, `y`, `button` | convenience, logged as one event |
| `scroll` | `x`, `y` (optional), `dx`, `dy` | **`dy > 0` scrolls up**; ±50 clamp |
| `key` | `key`, `action`, `mods`, `code` (optional) | `action`: `press`/`down`/`up`; `key`: `a`, `Enter`, `ArrowUp`, `F1`…; `code`: physical key (`KeyA`) |
| `text` | `text` | up to 512 chars, convenience |
| `ping` | — | answered with `pong` |
| any | `id` | optional, echoed in the ack so clients can measure RTT |

Examples:

```json
{"t":"mousemove","x":640,"y":360}
{"t":"click","x":960,"y":540,"button":"right"}
{"t":"scroll","x":640,"y":360,"dy":-3}
{"t":"key","key":"c","code":"KeyC","action":"down","mods":["ctrl"]}
```

Invalid messages get `{"t":"error",...}` and never reach the log. Accepted ones
are logged as

```
23:10:06 INFO  kvm.server: input mousemove stream=(640,360) desktop=(960,540) from 127.0.0.1
23:10:07 INFO  kvm.server: input key down ctrl+c code=KeyC from 127.0.0.1
```

and, with `--input-log`, appended to JSONL:

```json
{"t":"key","at_ms":3235645013,"key":"c","action":"down","mods":["ctrl"],"code":"KeyC","peer":"127.0.0.1","type":"key"}
```

That record is the contract for the USB HID component: `px`/`py` for absolute
pointer moves, `button` 1/2/3, `key`+`code`+`mods` for keys.

## Verify without the phone

Three independent checks, all included:

```bash
# 1. capture backend only, no networking
.venv/bin/python tools/probe_capture.py -n 40
.venv/bin/python tools/probe_capture.py --source x11 -n 40

# 2. full end-to-end: 200 frames, header validation, input round trip
.venv/bin/python tools/test_client.py --duration 10 --expect-width 1280 --expect-height 720
.venv/bin/python tools/test_client.py --url ws://127.0.0.1:8095/ws   # h264 too

# 3. browser page
$BROWSER http://127.0.0.1:8080/        # frames + live fps + input acks in the footer

# 4. robustness: kill the capture child (server must restart), send bad input
.venv/bin/python tools/robustness_check.py
```

`tools/test_client.py` exits 0 only if: frames arrive at ≥80 % of the target
fps, every header parses (magic/version), header dimensions match the JPEG SOF,
payloads start with `FF D8` and end with `FF D9`, sampled JPEGs decode with
Pillow, the sequence has no gaps, and all 9 input events are acked.

## Latency

The end-to-end figure that matters is *screen change on the PC → photons on the
phone*. It is measured in two halves that multiply out cleanly:

* **grab + encode** — a probe window paints an 8-bit counter every 15 ms
  (`bench/x11_clock.py`); the encoded frames are timestamped as they arrive and
  decoded afterwards, so each frame names the instant it was painted.
* **encoder output → panel** — the phone reports, per frame, when the WebSocket
  delivered it, when the decoder emitted it and when it was drawn, all converted
  into the PC's `CLOCK_MONOTONIC` with a ping/pong offset (sub-millisecond: the
  measured transport time is ~1 ms). The agent logs the breakdown, and
  `--lat-log` writes it as JSONL.

| stage | JPEG 1280x720@20 q70 (legacy) | H.264 1920x1080@30 12 Mbit/s |
|---|---|---|
| grab + encode (ffmpeg) | 71 ms | 25 ms |
| socket write (`intake`) | 0.7 ms | 0.8 ms |
| USB transport | 4 ms | 10 ms |
| phone decode | 17 ms | 20 ms |
| phone draw/compose | 25 ms | ~0 (SurfaceView, MediaCodec hand-off) |
| **total (median)** | **~120 ms** | **~60 ms** |
| total p95 | ~148 ms | ~92 ms |
| bitrate | 24 Mbit/s | 12–15 Mbit/s |
| PC CPU | 11 ms/frame (22 % of a core) | 8.5 ms/frame (26 % of a core at 30 fps) |

Reproduce with `bench/measure_capture.py` and `bench/measure_e2e.py`; method,
raw numbers and caveats are in [`bench/RESULTS.md`](bench/RESULTS.md).

Why the JPEG path is so slow: intra-only compression at 720p costs ~20 ms of CPU
per frame inside ffmpeg *and* the MJPEG pipe has no frame delimiter, so a frame
can only be forwarded once the next one starts arriving. H.264 with NVENC
encodes on the GPU in a few milliseconds, and the prototype reads RTP packets
(marker bit = end of access unit) instead of an Annex-B pipe, which removes that
extra frame of delay. On the phone, JPEG needs ~17 ms of `BitmapFactory` plus a
Compose recomposition and draw; MediaCodec renders straight into a `SurfaceView`
with no Bitmap allocation at all.

## Measured on this machine

Ubuntu 22.04, Xorg :0 at 1920x1080, ffmpeg 4.4.2, Python 3.13, 32 threads.

| case | backend | result |
|---|---|---|
| default 1280x720 @20 q70, static desktop | ffmpeg x11grab | **20.1 fps**, avg **140.7 kB**/frame (range 140.4–140.9 kB), 22.7 Mbit/s, 0 seq gaps, input RTT 0.2–0.5 ms, startup 0.38 s |
| same, 3 concurrent clients | ffmpeg x11grab | 19.93 / 19.95 / 19.95 fps, 0 dropped frames, 124 kB each |
| fullscreen moving test pattern | ffmpeg x11grab | 19.96 fps, 34 kB/frame, 5.4 Mbit/s |
| `--source x11` (ctypes XGetImage + Pillow) | x11 | 15.1–16.1 fps at 1280x720, 156 kB/frame |
| `--fps 5 --width 640 --height 480` | ffmpeg x11grab | exactly 5.0 fps, 16.8 kB/frame, 0.7 Mbit/s |
| `--width 960 --height 540 --quality 50` | ffmpeg x11grab | 20.1 fps, 17 kB/frame, 2.8 Mbit/s |
| `--codec h264 --width 1920 --height 1080 --fps 30 --bitrate 12000` | ffmpeg x11grab + h264_nvenc | **30.0 fps**, 48.9 kB/frame, 12.1 Mbit/s, 0 seq gaps, input RTT 0.1–0.2 ms, SSIM 0.994 vs 0.93 for the JPEG path |

Frame size is content-driven: 122–141 kB for this desktop's wallpaper, 34 kB for
flat test bars, 17 kB at 960x540/q50. At the default 20 fps and 1280x720 that is
roughly 2.5–2.8 MB/s (20–23 Mbit/s), comfortable for USB tethering.

CPU: ~47 % of one core for capture+scale+JPEG at 1080p→720p/20 fps (ffmpeg
does all of it in C; Python only splits the MJPEG stream).

## Backends and fallback

`--source auto` probes in this order and keeps the first one that delivers a
real frame at startup:

1. **`ffmpeg`** — `ffmpeg -f x11grab -framerate N -i :0.0 -vf scale=… -q:v Q -f mjpeg pipe:1`.
   Fastest, does scaling and JPEG in C. Needs `$DISPLAY`.
2. **`x11`** — ctypes bindings to `libX11`/`libXext`: tries `XShmGetImage`
   (zero-copy via a SysV shared segment), and falls back to `XGetImage` when the
   server refuses; then Pillow resizes and encodes with exact `quality`.
   Pure-Python dependency-free fallback, roughly 15 fps at 720p.
3. **`pipewire`** — `ffmpeg -f pipewire -i <node>` for Wayland screencast nodes.

The `--quality` value is exact on the `x11` backend; on the `ffmpeg` backend it
is mapped onto ffmpeg's MPEG-style `-q:v` scale (`70 → q6`, measured to match
Pillow's q70 output size within ~10 %).

Aspect handling: with an explicit `--height`, mismatched aspects are letterboxed
(black bars) rather than stretched. Input mapping is uniform over the whole
frame, so a click in a black bar maps to the screen edge.

## Limitations

* **Wayland**: there is no silent way to read a Wayland framebuffer. The
  xdg-desktop-portal ScreenCast flow needs a DBus client *and* a per-session
  "Share your screen" dialog; this agent does not implement that handshake. Use
  `--source pipewire --pipewire-node N` with a node you obtained elsewhere
  (ffmpeg must be built with pipewire input — Ubuntu 22.04's ffmpeg 4.4 is not),
  or stay on X11/XWayland (XWayland only shows X11 windows).
* **XShm on this box**: `XShmAttach` succeeds but `XShmGetImage` returns
  `success=0`, so the ctypes backend silently uses `XGetImage` (a socket
  readback, ~41 ms/frame at 1080p). That is why it tops out near 15 fps.
* **No input injection here.** Mouse/keyboard are validated, mapped and logged
  only; the HID gadget component performs the actual injection.
* **No audio, no cursor-shape changes, no clipboard.** The mouse cursor *is*
  drawn by x11grab (`-draw_mouse 1`) but is not part of the X11 backend's image.
* **No auth, no TLS.** Binds `0.0.0.0:8080` by default — fine over USB tethering
  or a trusted LAN, not for the open internet.
* The JPEG path is intra-only and full-frame: with `--dedupe` off an idle
  desktop still costs ~2.5 MB/s at 720p/20. `--codec h264` removes that (tiny
  P-frames when idle, ~12 Mbit/s steady under CBR).
* H.264/HEVC capture is X11-only (`x11grab`); there is no pipewire encoder path.
* No runtime keyframe request: ffmpeg 4.4's NVENC wrapper cannot force an IDR, so
  a client that connects mid-GOP waits up to one GOP (default 1 s) for the first
  decodable frame. Drop `--gop` to shorten that wait.
* HEVC decoding on the phone is supported (`--codec hevc`) but was only smoke
  tested; H.264 is the default recommendation.
* Frame pacing relies on ffmpeg's `-framerate`; the X11 backend paces in Python,
  so its effective fps is `min(fps, 1/(capture+encode time))`.
* One display (:0) at a time; no per-window capture.

## Files

```
kvm_agent/
  __main__.py        CLI entry point, banner, aiohttp runner
  config.py          argument parsing / Config dataclass
  wire.py            24-byte header pack/unpack + JPEG SOF dimensions
  server.py          HTTP + WebSocket, fan-out, per-client queues, stats, latency
  input.py           input schema validation, coord mapping, logging
  static/index.html  self-contained test page (vanilla JS, no build step)
  capture/h264.py    x11grab -> NVENC -> RTP/loopback -> Annex-B access units
  bench/             latency rig: x11_clock.py, measure_capture.py, measure_e2e.py
  capture/
    __init__.py      backend detection + fallback
    base.py          CaptureSource async frame iterator (single worker thread)
    ffmpeg.py        x11grab/pipewire → MJPEG parser
    x11.py           ctypes XShm/XGetImage + Pillow encoding
    pipewire.py      Wayland node handling + honest error messages
    encode.py        Pillow JPEG encode, ffmpeg qscale mapping
tools/
  test_client.py     headless verification client (metrics + input RTT)
  probe_capture.py   backend benchmark without networking
  robustness_check.py  capture-restart + malformed-input checks
```
