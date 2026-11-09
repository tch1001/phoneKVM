# Traps

Every entry here cost a bootloop, a wasted flash, or hours of guessing. Read
before building.

## Boot image: never repack it with magiskboot

`magiskboot unpack` + swap kernel + `repack` then `dd` to `/dev/block/by-name/boot`
produces a bootloop on this device: the repack invalidates the vbmeta embedded
in Samsung's boot image. Even though the ROM's vbmeta has verity/verification
disabled (Flags 3), the repacked image still fails.

Flash a **clean** `boot.img`, or the whole OTA, through recovery:
`adb reboot sideload` then `adb sideload lineage-*.zip`. If the phone's USB link
does not re-enumerate after the reboot (common with hubs), replug the cable.

## f_uac2 must be a module, not built-in

Adding `CONFIG_USB_CONFIGFS_F_UAC2=y` shifts `device_initcall` link order on
this vendor tree. `spi_geni` then probes before the TLMM pinctrl is ready and
the kernel panics at ~0.68 s:

    Bad mode in Error handler detected on CPU7, code=0xbe000411 -- SError
    Kernel panic - not syncing: bad mode
    msm_pinmux_set_mux <- pinctrl_select_state <- driver_probe_device <-
    __platform_driver_register <- spi_geni_driver_init <- do_one_initcall

Moving `spi_geni` to `late_initcall` alone does **not** fix it. Build UAC2 as
`=m`, which keeps the kernel image's built-in driver set identical to the
working kernel.

## A `=m` on a `bool` symbol is silently dropped

`config USB_CONFIGFS_F_UAC2` is declared `bool` in
`drivers/usb/gadget/Kconfig`, so `=m` becomes "not set": the build prints
`# CONFIG_USB_CONFIGFS_F_UAC2 is not set` and no `usb_f_uac2.ko` appears.
Change the symbol to `tristate` first (see `kernel/README.md`).

## Samsung LKMAUTH blocks every insmod

Loading any module fails with:

    PKCS#7 signature not signed with a trusted key
    LKM is not allowed by Samsung security policy

`lkmauth()` in `kernel/module.c` returns `RET_UH_LKMAUTH_LKM_BLOCK_FORCE`
unconditionally under `CONFIG_UH_LKM_BLOCK`; no key, sysfs node or cmdline flag
bypasses it. Patch it to return `RET_LKMAUTH_SUCCESS`. Without this, modules
cannot be loaded at all on a user-built kernel.

## configfs rejects absolute symlinks

    ln -s /config/usb_gadget/g1/functions/uac2.0 configs/b.1/uac2.0   # EPERM

Use a relative target: `ln -s functions/uac2.0 configs/b.1/uac2.0`.

## Module load order matters

`insmod u_audio.ko` before `usb_f_uac2.ko`; the function module depends on it.
The init service does this in order after the gadget exists.

## SELinux: /proc/asound is a vendor label

`/proc/asound/cards` is labelled `vendor_proc_audiod` on this device. A
platform-domain daemon cannot reference a vendor type at all:

    ERROR 'unknown type vendor_proc_audiod'

and `coredomain.te:136` forbids *any* coredomain from reading generic `sysfs`,
so `/sys/class/sound/card*/id` is no escape either. The daemon finds its card
by opening `/dev/snd/controlC*` and issuing `SNDRV_CTL_IOCTL_CARD_INFO`, which
uses the `audio_device` label it already needs.

## SELinux: apps cannot write /dev/hidg* even with an allow rule

`allow untrusted_app hidg_device:chr_file rw_file_perms;` compiles, passes the
neverallow check, and appears in the shipped
`system_ext_sepolicy.cil`, yet in enforcing mode the app is still denied:

    avc: denied { write } for comm="com.kvmlite.app" name="hidg0"
    scontext=u:r:untrusted_app:s0:c162,... tcontext=u:object_r:hidg_device:s0
    tclass=chr_file

Same app can write `/dev/null`, so it is not an MLS constraint and not DAC
(node is 0666). Do not spend another ROM build on this. The supported path is
the `hidbridge` daemon (root, own domain, owns the nodes, apps talk to
`127.0.0.1:7700`) — it is included in `device-tree/`.

## The gadget node paths are per-interface

The keyboard and mouse are two HID functions: `/dev/hidg0` (keyboard, 8-byte
boot report) and `/dev/hidg1` (mouse, 4-byte report). Writing a mouse report to
`hidg0` is silently dropped by the host — touch "does nothing". The app keeps
one writer per node.

## `adb reverse` is the USB transport

Video does not need to go over WiFi. `adb reverse tcp:8080 tcp:8080` tunnels the
phone's `127.0.0.1:8080` to the PC's agent over the USB adb link. Keep it alive
with `scripts/kvm-usb-tunnel.sh` (systemd user unit included), otherwise it is
lost on every replug.

## `/dev/hidg*` permissions come from ueventd

The nodes are created 0600 root:root, so even a permissive policy is not
enough for the app. `ueventd.qcom.rc` in this repo sets 0666; the label comes
from `sepolicy/private/file_contexts`.

## Signing the sideload zip

`signapk.jar` needs `-Djava.library.path=$OUT/host/linux-x86/lib64` for
conscrypt, or it dies on a missing native library. Easier: build `otapackage`,
whose zip is already signed with the platform test keys.
