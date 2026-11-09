#!/bin/bash
# Keep an adb reverse tunnel alive over USB so the phone can reach the PC
# agent at 127.0.0.1:8080 without any network. Re-establishes it after every
# replug, reboot or adb server restart.

PORT=8080

while true; do
    if adb -d get-state >/dev/null 2>&1; then
        if ! adb -d reverse --list 2>/dev/null | grep -q "tcp:$PORT"; then
            if adb -d reverse "tcp:$PORT" "tcp:$PORT" >/dev/null 2>&1; then
                logger -t kvm-usb "reverse tcp:$PORT established over USB"
            fi
        fi
    fi
    sleep 3
done
