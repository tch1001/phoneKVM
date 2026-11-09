# phoneKVM

Turn a Samsung Galaxy A70 into a **single-cable KVM console** for a Linux PC:
the phone shows the PC's desktop and acts as its keyboard, mouse and speaker
over the same USB-C cable. No network, no extra hardware, and no software agent
in the input path — the phone is a genuine USB HID device to the host.

    ┌────────────┐   USB-C cable   ┌──────────────────────────────┐
    │    PC      │◄───────────────►│  Galaxy A70 (LineageOS 22.2) │
    └────────────┘                 └──────────────────────────────┘
      screen  ──► encoded video ──► app renders it
      cursor  ◄── USB HID mouse  ── touch on screen
      keys    ◄── USB HID kbd    ── on-screen keyboard
      audio   ──► UAC2 sink      ── phone speaker

Weekend project; the phone cannot control the PC's power (no relay), so it is a
KVM console, not a full piKVM.

## Status (verified on the reference device)

| capability | state |
|---|---|
| phone enumerates as USB speaker (UAC2), audio plays out of the phone | verified |
| phone enumerates as USB keyboard (HID) | verified — typed into a focused window on the host |
| phone enumerates as USB mouse (HID) | verified — host pointer followed drags |
| desktop video, PC -> phone | verified, two codecs |
| touch -> host cursor | verified end to end, SELinux Enforcing, no root on the app |
| HDMI output from the phone | **not possible** — USB 2.0-only port, no DP alt mode |
| DisplayLink adapter | **not implemented** — needs DL-1xx + a framebuffer pump |

Latency measured on the host (median / p95), same cable:

    JPEG 1280x720@20, q70        ~120 / 147 ms
    H.264 1920x1080@30, 12 Mb/s   ~60 /  92 ms

## Hardware baseline

- Samsung Galaxy A70, `SM-A705MN` (`a70q`), Snapdragon 675
- Bootloader unlocked, Magisk root, LineageOS 22.2 (Android 15)
- Kernel: vendor tree `kernel/samsung/sm6150`, Linux 4.14.190
- Host: Linux with X11 or PipeWire capture, `adb`, plus NVENC or libx264

## Repo layout

    device-tree/    files to drop into the LineageOS tree (exact paths inside)
    kernel/         a70q_defconfig + kernel edits, with instructions
    app/            Android app (Kotlin/Compose) + prebuilt debug APK
    pc-agent/       Python capture/encode/input agent for the PC
    scripts/        host helpers (adb reverse tunnel + systemd unit)
    docs/TRAPS.md   the non-obvious failures and their causes — read first

## Reproduction

1. **Sync and build LineageOS 22.2 for `a70q`** until `breakfast a70q` works.
2. **Copy `device-tree/`** over the tree (it mirrors `device/samsung/...`),
   including the three modified files: `a70q/device.mk`,
   `sm6150-common/rootdir/etc/init.qcom.usb.rc`,
   `sm6150-common/rootdir/etc/ueventd.qcom.rc`.
3. **Apply the kernel edits** from `kernel/README.md`: UAC2 as a *module*
   (needs a `bool`->`tristate` fix in `drivers/usb/gadget/Kconfig`) and the
   `lkmauth()` bypass in `kernel/module.c`. `kernel/a70q_defconfig` is the
   exact config used.
4. `m otapackage`, then flash the zip through recovery with `adb sideload`.
   Do **not** repack the boot image with `magiskboot` — see `docs/TRAPS.md`.
5. On the PC: `pip install -r pc-agent/requirements.txt`, then from `pc-agent/`
   run `python -m kvm_agent --port 8080` (add `--codec h264 --bitrate 12000`
   for the low-latency path).
6. Install `app/prebuilt/kvm-lite-debug.apk` (or build `app/`), enable
   `scripts/kvm-usb-tunnel.service` so `adb reverse tcp:8080 tcp:8080` survives
   replugs, then launch the app: it connects to `ws://127.0.0.1:8080/ws`.

   The tunnel script finds `adb` itself (distro package, SDK, or local AOSP
   build) because a systemd user unit does not inherit your shell PATH; set
   `ADB=/path/to/adb` if it guesses wrong. Verify with
   `adb -d reverse --list` showing `tcp:8080`.

## How the three pieces work

**ROM.** `f_uac2` (audio sink) and `f_hid` (keyboard + mouse) are added to the
vendor USB gadget's composition by `rootdir/bin/uac2-gadget.sh`, which runs
after the vendor HAL composes and rebinds the UDC once. `uac2sink` is a
resident init service bridging the gadget's capture PCM to Android's primary
output. `hidbridge` owns `/dev/hidg0`/`hidg1` and republishes them on
`127.0.0.1:7700` so an ordinary app can inject HID without root.

**App.** Compose UI: video surface, touchpad gestures (drag = move, tap = left
click, long-press = right click, two-finger drag = scroll), on-screen keyboard,
and two separate HID writers, because the keyboard and mouse are different USB
interfaces (`/dev/hidg0` vs `/dev/hidg1`). Codec is auto-detected from the
stream header, so the JPEG path remains the fallback.

**PC agent.** Captures the desktop, encodes (JPEG or H.264/H.265 via NVENC or
x264 with `-tune ll -zerolatency`), serves over WebSocket, and replays incoming
pointer/keyboard events through `uinput`.

## Notes on the HID path

The app does **not** write `/dev/hidg*` itself. An
`allow untrusted_app hidg_device:chr_file write` rule compiles and appears in
the shipped policy, but Android still refuses the write at runtime, so the app
talks to `hidbridge` on `127.0.0.1:7700` instead and the daemon (root, own
domain) does the writing. This is verified working with SELinux **Enforcing**.
See `docs/TRAPS.md` § SELinux.
- DisplayLink (phone -> HDMI monitor) is parked: only DL-1xx has an open
  driver, and it requires USB host mode, which excludes the HID gadget role on
  a single port.
