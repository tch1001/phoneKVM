# Latency: measurement rig, numbers, and what changed

Machine: Ryzen 9 5950X, RTX 3090 (NVENC), Ubuntu 22.04, Xorg :0 @1920x1080/60 Hz,
ffmpeg 4.4.2. Phone: Samsung SM-A705MN (Snapdragon 675), 1080x2400@60 Hz,
Android 15, connected over USB with `adb reverse`. Video is measured with the
phone app itself plus a probe window on the PC.

## TL;DR

| | JPEG 1280x720@20 q70 (shipped) | H.264 1920x1080@30 CBR 12 Mbit/s |
|---|---|---|
| **end-to-end, median** | **~120 ms** | **~60 ms** |
| end-to-end, p95 | ~147 ms | ~92 ms |
| grab + encode (PC) | 71 ms | 25 ms |
| transport (USB) | 4 ms | 11 ms |
| decode (phone) | 17 ms | 20 ms |
| draw (phone) | 25 ms | ~0 (MediaCodec → SurfaceView) |
| bitrate | 24 Mbit/s | 15 Mbit/s |
| still-image quality (SSIM vs 1080p source) | 0.93 | 0.99 |
| PC CPU | 11 ms/frame (22 % of a core) | 8.5 ms/frame (26 % at 30 fps) |

i.e. **~50 % lower latency and 0.99 vs 0.93 SSIM**, at a *higher* resolution and
frame rate, for slightly more CPU (26 % vs 22 % of one core) and ~40 % less
bitrate.

## How the numbers are produced

End-to-end latency is split at the encoder boundary, because the two halves need
different instruments:

**(a) screen → encoder output** — `bench/x11_clock.py` opens an override-redirect
X11 window that paints an 8-bit binary counter every 15 ms and logs
`(pattern, CLOCK_MONOTONIC)` for every paint. `bench/measure_capture.py` then runs
the *real* capture backend (`kvm_agent.capture.h264.EncodedSource` /
`ffmpeg.FfmpegSource` — identical ffmpeg command lines to the agent), timestamps
every encoded frame as it arrives, decodes the frames afterwards and reads the
counter pattern out of each one. For frame *k*:

```
grab+encode(k) = arrival_time(k) - paint_time(pattern_in_frame_k)
```

Because the pattern changes every 15 ms and identifies the frame uniquely, this
needs no clock comparison between two machines, and it includes what people
actually experience: a screen change is only visible to the encoder at its next
capture tick (mean 16.7 ms at 30 fps, 25 ms at 20 fps) plus the encoder's own
cost. The paint timestamp is taken when Python flushed the X request, so any
window-manager compositing delay is included too — the number is an upper bound.

**(b) encoder output → panel** — the phone measures it. The app timestamps
`recv` (WebSocket delivery), `dec` (decoder output buffer available) and `rend`
(MediaCodec frame-rendered callback, or the Compose frame clock for the JPEG
path) and converts them into the PC's `CLOCK_MONOTONIC` using a ping/pong offset
(`{"t":"ping"}` → `{"t":"pong","pc_ms":…}`, every 2 s, RTT < 1 ms over the USB
tunnel). Each frame is reported back as `{"t":"lat",…}`; the agent turns that
into the per-stage breakdown in its log and, with `--lat-log`, into JSONL.
`bench/measure_e2e.py` drives the phone, collects the samples and prints
percentiles.

The offset is self-checking: the computed `transport` number is ~1 ms in the
median, so a clock error would show up immediately as a large positive or
negative transport. Measured `transport` medians of 4–12 ms are real tunnel
time, and the p95 values (20–40 ms) are scheduling jitter on the phone side.

Caveats, stated plainly:

* The H.264 path reports `rend` = the instant the decoded buffer is handed to
  the display pipeline; `setOnFrameRenderedListener` on this Qualcomm codec
  echoes the presentation timestamp instead of a scan-out time, so it carries no
  information (the app detects that and falls back). Real scan-out adds 0–17 ms
  (mean ~8 ms at 60 Hz) — so the H.264 total is a *lower* bound by about half a
  refresh. The JPEG path measures at the Compose frame clock, i.e. a real vsync.
* Grab+encode is a upper bound: it contains the capture sampling interval and
  any compositor delay in the probe window's own painting.
* The optical cross-check (screenshot the phone, read the counter) works but its
  own capture instant is ~110 ms after `adb exec-out screencap` starts, so it is
  only used as a sanity check: it shows the prototype's panel content is
  ~40–55 ms fresher than the JPEG path's, consistent with the table above but
  with ±15 ms granularity (the counter's step).

## Baseline (the shipped pipeline)

`--codec jpeg --width 1280x720 --fps 20 --quality 70`, three runs of 14 s:

```
enc_out->panel p50/p95 : 53/65, 45/71, 50/67 ms
  intake 0.7 | transport 1.5-9.2 | decode 17 | render 25
grab+encode           : 69.7/79.8, 74.0/79.4, 68.2/79.8 ms
bandwidth             : 24-25 Mbit/s, 110 kB/frame
PC CPU                : 11.2 ms/frame (23 % of one core)
```

So ~120 ms median end-to-end, ~147 ms p95. That matches the complaint: at 120 ms
the pointer trails the finger and typing feels mushy.

Where it goes:

* **71 ms inside ffmpeg** — 1080p→720p swscale plus a quality-6 MJPEG encode is
  ~20–25 ms of CPU per frame, on top of the 25 ms mean capture sampling at
  20 fps. Switching to `-threads`/`-q:v` does not fix this: intra-only coding is
  simply expensive and it re-encodes the whole desktop every frame.
* **25 ms Compose draw** — `BitmapFactory` produces a 3.7 MB ARGB bitmap per
  frame, which is uploaded and drawn through the compositor; measured at the
  Compose frame clock.
* **17 ms decode** on the phone, plus per-frame allocation churn (the JPEG path
  allocates ~74 MB/s of bitmaps at 20 fps).

## Scheme evaluation (offline sweep)

`bench/media/sweep.py` encodes a lossless 6 s recording of this desktop and a
scrolling-text clip with each candidate; quality is SSIM against the source,
scaled to 1080p (what the phone shows). CPU is (utime+stime)/frame, which for
this test includes decoding the source, so treat it as a relative figure.

| scheme | kB/frame | Mbit/s @20 fps | SSIM | CPU ms/frame |
|---|---|---|---|---|
| JPEG 720p q6 (shipped) | 138.9 | 22.8 | 0.927 | 16.7 |
| JPEG 1080p q6 | 265.7 | 43.5 | 0.985 | 25.2 |
| JPEG 1080p q3 (best quality) | 369.0 | 60.5 | 0.994 | 26.8 |
| x264 ultrafast zerolatency crf18 | 7.9 | 1.3 | **0.999** | 20.7 |
| x264 superfast zerolatency crf18 | 4.7 | 0.8 | 0.998 | 29.4 |
| nvenc p1 ll CBR 6 Mbit/s | 24.4 | 4.0 | 0.990 | 11.5 |
| nvenc p1 ll CBR 10 Mbit/s | 40.7 | 6.7 | 0.994 | 11.1 |
| nvenc p1 ll CBR 16 Mbit/s | 65.1 | 10.7 | 0.997 | 11.8 |
| nvenc 720p CBR 10 Mbit/s | 40.7 | 6.7 | 0.938 | 11.3 |
| hevc_nvenc p1 ll CBR 5 Mbit/s | 7.6 | 1.3 | 0.991 | 12.4 |
| hevc_nvenc p1 ll CBR 8 Mbit/s | 13.9 | 2.3 | 0.994 | 12.0 |

Reading:

* **Tuned JPEG is a dead end for this content.** Even 1080p q3 (60 Mbit/s) only
  reaches SSIM 0.994 at 369 kB/frame, i.e. more bitrate than a 1080p H.264
  stream and 6x the size of an HEVC one, and it is still intra-only.
* **libx264 gives the best compression but costs 20–30 ms of CPU per frame** on
  this box — enough to become the bottleneck at 30 fps and worse on a laptop.
* **NVENC is the right trade**: 11–12 ms of CPU per frame, GPU does the encode,
  SSIM 0.994 at 10 Mbit/s, and it supports `-zerolatency`, `-delay 0`, `-tune ll`
  and 2-frame VBV so the encoder cannot queue.
* **HEVC halves the bitrate again** (5 Mbit/s at SSIM 0.991) and the phone has a
  hardware HEVC decoder (`OMX.qcom.video.decoder.hevc`). It is implemented and
  smoke-tested (`--codec hevc`); H.264 stays the recommendation because decode
  support/latency is more predictable.
* 4:4:4 is not an option: no Android hardware decoder supports High 4:4:4, so
  text sharpness has to come from resolution (1080p, 1:1 on the panel) rather
  than chroma.

## Prototype

`--codec h264 --width 1920 --height 1080 --fps 30 --bitrate 12000`, three runs:

```
enc_out->panel p50/p95 : 39/61, 33/60, 39/62 ms
  intake 0.8 | transport 9-13 | decode 20 | render 0 (hand-off)
grab+encode           : 34.3, 25.4, 19.2 ms (p50 per run)
bandwidth             : 15.5 Mbit/s, 48.9 kB/frame
PC CPU                : 8.5 ms/frame (26 % of one core at 30 fps)
```

Median total ≈ 60 ms, p95 ≈ 92 ms, so **~50 % / ~38 % better** than the shipped
JPEG pipeline, while showing 1920x1080 (1:1 pixel mapping on the phone's
2400x1080 panel in landscape) instead of 1280x720 upscaled by 1.5x.

Variants measured (median enc_out→panel, and grab+encode where relevant):

| variant | enc_out→panel p50 | notes |
|---|---|---|
| 1080p30, GOP 2 s | 38 ms | |
| 1080p30, GOP 1 s (default) | 38 ms | bounds mid-GOP join to 1 s |
| 1080p30, `--intra-refresh` | 35 ms | no keyframe spikes, same latency; slower recovery |
| 1080p60 @14 Mbit/s | 23 ms | but grab+encode rises to 41–50 ms (ffmpeg can't sustain 60 fps 1080p x11grab + swscale) → worse overall (~70 ms) |
| 720p30 @6 Mbit/s | 25 ms | ~45 ms total, but text is upscaled 1.5x (SSIM 0.94) |
| `--hw-upload` (CUDA conversion) | — | works, no measurable gain at 30 fps; kept as an option |

Why RTP instead of an Annex-B pipe: a raw `-f h264` byte stream has no frame
delimiter, so a frame can only be forwarded once the next one starts — a silent
+1 frame (33 ms) of latency. RTP packets carry a marker bit for the last packet
of an access unit, so the agent forwards each frame as soon as it is encoded.
Measured with the probe rig: **55.6 ms → 24.8 ms** for grab+encode at 1080p30
just from that change. (This is `kvm_agent/capture/h264.py`; it reassembles
single-NAL/STAP-A/FU-A packets back into Annex-B access units.)

## What changed

PC (`pc-agent`):

* `kvm_agent/capture/h264.py` (new) — x11grab → NVENC/libx264 → RTP/loopback →
  Annex-B AUs, SPS/PPS caching, keyframe detection, swscale fallback if the CUDA
  graph is unavailable.
* `kvm_agent/wire.py` — codec ids 1/2/3 in the existing header byte,
  `pack_packet()` (the 24-byte header is unchanged).
* `kvm_agent/capture/base.py` — `Frame` carries codec + keyframe flag.
* `kvm_agent/capture/__init__.py` — codec-aware backend selection.
* `kvm_agent/config.py` — `--codec --encoder --bitrate --gop --intra-refresh
  --hw-upload --lat-log`.
* `kvm_agent/server.py` — latency probes (`lat` messages, pong carries `pc_ms`),
  per-frame stage logging, JSONL latency log, codec-aware payload packing and
  stats.
* `tools/test_client.py` — validates H.264/HEVC access units (start code, IDR,
  ffprobe geometry) instead of assuming JPEG.
* `bench/` (new) — `x11_clock.py`, `measure_capture.py`, `measure_e2e.py`,
  `measure_cpu.sh`, `agent.sh`, `media/sweep.py`.

Android (`app`):

* `net/AnnexB.kt` (new) — start-code/NAL parsing, SPS/PPS extraction, IDR test.
* `net/VideoDecoder.kt` (new) — persistent MediaCodec decoder: async callbacks,
  `KEY_LOW_LATENCY`, single-slot newest-wins queue, renders straight to a
  Surface, drops frames until the first IDR, reports per-frame timings.
* `net/VideoClient.kt` — codec routing (JPEG stays on BitmapFactory/Compose),
  ping/pong clock offset, `{"t":"lat"}` reporting, surface plumbing.
* `ui/KvmScreen.kt` — SurfaceView sized to the stream aspect when the stream is
  inter-frame, transparent touch overlay so HID gestures never get eaten by the
  SurfaceView, latency in the status pill.
* `KvmController.kt`, `MainActivity.kt` — surface callbacks, draw callback,
  `onNewIntent` so `am start --es url …` can re-target a running instance.

## Running both ends

```bash
# PC (leave the production agent on 8080 alone)
cd ~/kvm-lite/pc-agent
.venv/bin/python -m kvm_agent --port 8095 --codec h264 \
    --width 1920 --height 1080 --fps 30 --bitrate 12000

# phone: tunnel the port, then point the app at it
adb -s RR8MC0223XL reverse tcp:8095 tcp:8095
adb -s RR8MC0223XL shell am start -n com.kvmlite.app/.MainActivity \
    --es url ws://127.0.0.1:8095/ws --ez connect true

# fallback to the old path: run the agent with --codec jpeg and use port 8080/8096
```

The app auto-detects the codec from the `hello` message and the header's codec
byte, so the same APK serves both pipelines. `--lat-log /tmp/lat.jsonl` on the
PC records every latency sample.

## Not verified

* Real scan-out time on the phone for the H.264 path (see caveats): the reported
  total is a lower bound by ~0–17 ms.
* HEVC end-to-end on the phone: decoded and rendered without errors in a short
  run, but no latency numbers and no long-run soak.
* HID/touch input after the UI change: the gesture layer and HID writers are
  untouched and the touch overlay sits above the SurfaceView, but I could not
  press a physical finger on the device, so touch → HID → PC was not re-tested
  end to end.
* 60 fps capture: ffmpeg on this box cannot sustain 1080p60 x11grab + conversion;
  a machine with a faster single-core path (or a Wayland/pipewire source) might
  make 60 fps worthwhile.
* Long-run stability beyond ~60 s continuous, and behaviour under packet loss
  (loss recovery is "wait for the next IDR", ≤1 s).
