#!/bin/bash
# Keep an adb reverse tunnel alive over USB so the phone can reach the PC agent
# at 127.0.0.1:8080 without any network. Re-establishes it after every replug,
# reboot, `adb root` (which restarts adbd and drops mappings) or adb server
# restart.
#
# adb is often not in /usr/bin: it may come from a distro package, an SDK, or a
# local AOSP build. This service runs without the user's shell PATH, so the
# binary is resolved explicitly — otherwise the loop silently does nothing.

PORT=8080

find_adb() {
    if [ -n "$ADB" ]; then echo "$ADB"; return; fi
    for c in /usr/bin/adb /usr/local/bin/adb \
             "$HOME/Android/Sdk/platform-tools/adb" \
             /mnt/build/lineage/out/host/linux-x86/bin/adb; do
        [ -x "$c" ] && { echo "$c"; return; }
    done
    command -v adb 2>/dev/null
}

ADB_BIN=$(find_adb)
if [ -z "$ADB_BIN" ]; then
    logger -t kvm-usb "adb not found; install android-tools-adb or set ADB"
    exit 1
fi

while true; do
    if "$ADB_BIN" -d get-state >/dev/null 2>&1; then
        if ! "$ADB_BIN" -d reverse --list 2>/dev/null | grep -q "tcp:$PORT"; then
            if "$ADB_BIN" -d reverse "tcp:$PORT" "tcp:$PORT" >/dev/null 2>&1; then
                logger -t kvm-usb "reverse tcp:$PORT established over USB ($ADB_BIN)"
            fi
        fi
    fi
    sleep 3
done
