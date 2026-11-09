# Kernel changes

Tree: `kernel/samsung/sm6150`, Linux 4.14.190 (Samsung vendor tree).
`a70q_defconfig` here is the exact config that booted and enumerated the
gadget; `gadget-Kconfig` is the patched `drivers/usb/gadget/Kconfig`.

## 1. Enable UAC2 as modules

Append to `arch/arm64/configs/a70q_defconfig`:

    CONFIG_USB_U_AUDIO=m
    CONFIG_USB_F_UAC2=m
    CONFIG_USB_CONFIGFS_F_UAC2=m

`CONFIG_USB_CONFIGFS_F_HID` and `CONFIG_USB_F_HID` should already be `y` on this
tree (the vendor HAL ships keyboard/mouse HID descriptors in `vendor/etc/hid`).

## 2. Make the UAC2 configfs symbol tristate

In `drivers/usb/gadget/Kconfig`:

    config USB_CONFIGFS_F_UAC2
    -       bool "Audio Class 2.0"
    +       tristate "Audio Class 2.0"

Without this, `=m` is silently downgraded and no `usb_f_uac2.ko` is produced.
(Compare against `gadget-Kconfig` in this directory.)

## 3. Bypass Samsung's module signature policy

In `kernel/module.c`, the `lkmauth()` stub under `CONFIG_UH_LKMAUTH` /
`CONFIG_UH_LKM_BLOCK` must not return `RET_UH_LKMAUTH_LKM_BLOCK_FORCE`.
Replace its body with:

    static int lkmauth(Elf_Ehdr * hdr, int len)
    {
        pr_info("UH: lkmauth bypassed (local build); allowing module load\n");
        return RET_LKMAUTH_SUCCESS;
    }

Required for any `insmod` on this device; see docs/TRAPS.md.

## 4. Do NOT move spi_geni to late_initcall

Tried, does not fix the SError panic, and is not needed once UAC2 is a module.
Leave `module_platform_driver(spi_geni_driver);` alone.

## Build

    source build/envsetup.sh && breakfast a70q
    m bootimage      # kernel + modules
    m otapackage     # signed flashable zip

Modules land in `out/target/product/a70q/vendor/lib/modules/` and are loaded by
the init service before the gadget function is created.
