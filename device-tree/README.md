# Device tree files

Copy over the LineageOS tree keeping these paths:

    device/samsung/a70q/device.mk                      (modified: adds uac2sink, hidbridge)
    device/samsung/a70q/uac2sink/                      (new: daemon + Android.bp + rc)
    device/samsung/a70q/hidbridge/                     (new: HID daemon + Android.bp + rc)
    device/samsung/a70q/hid/{keyboard.bin,mouse.bin}   (new: HID report descriptors)
    device/samsung/sm6150-common/rootdir/bin/uac2-gadget.sh      (new: composition script)
    device/samsung/sm6150-common/rootdir/etc/init.qcom.usb.rc    (modified: service + triggers)
    device/samsung/sm6150-common/rootdir/etc/ueventd.qcom.rc     (modified: hidg 0666)
    device/samsung/sm6150-common/sepolicy/private/               (new: labels + domains)

`BoardConfigCommon.mk` already points `SYSTEM_EXT_PRIVATE_SEPOLICY_DIRS` at
`sepolicy/private`, which is why these `.te`/`file_contexts` files are picked
up as system_ext policy. Note that this means the *on-disk* path for labels is
`/system/system_ext/bin/...`, not `/system_ext/bin/...` — both forms are listed
in `file_contexts` for that reason.

## What each piece does

- `uac2-gadget.sh` — after the vendor USB HAL composes (it uses a fixed
  function list), this links `uac2.0`, `hid.usb0`, `hid.usb1` into the live
  config and rebinds the UDC once, so the host re-enumerates with the extra
  interfaces. Idempotent.
- `uac2sink` — resident init service. Waits for the gadget capture PCM, pumps
  it into AAudio on the primary output, and goes back to waiting when the
  gadget disappears, so replug never needs a restart.
- `hidbridge` — root daemon owning `/dev/hidg0`/`hidg1`, republished on
  `127.0.0.1:7700` ('K' + 8 bytes = keyboard report, 'M' + 4 bytes = mouse
  report). Exists because Android refuses app access to the raw nodes even
  with an SELinux allow rule (docs/TRAPS.md).
- `sepolicy/private/hidg.te` — declares the `hidg_device` type for the two
  nodes and (attempted) app access.
