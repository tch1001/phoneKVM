#!/vendor/bin/sh
#
# Add the extra USB gadget functions this ROM needs but the vendor USB HAL does
# not know about:
#
#   uac2.0    audio sink   - device acts as a USB speaker for the host
#   hid.usb0  keyboard     - device acts as a USB keyboard for the host
#   hid.usb1  mouse        - device acts as a USB mouse for the host
#
# The HAL is a prebuilt with a fixed function list, so the functions are linked
# into the live composition afterwards and the gadget is re-bound once, which
# makes the host re-enumerate with the extra interfaces.
#
# Idempotent: if everything is already linked (same composition, e.g. after a
# replug that did not change the config) it exits without touching the UDC.

CFG=/config/usb_gadget/g1
UDC_FILE=$CFG/UDC
HID_DIR=/vendor/etc/hid

log() { log -t usb-extras "$*"; }

# The HAL composes asynchronously: wait (bounded) for a config that already has
# a linked function before touching anything.
i=0
CONF=""
while [ $i -lt 40 ]; do
    CONF=$(ls -d $CFG/configs/* 2>/dev/null | head -n 1)
    if [ -n "$CONF" ] && [ -n "$(ls -A "$CONF" 2>/dev/null | grep -v 'strings\|MaxPower\|bmAttributes')" ]; then
        break
    fi
    usleep 250000 2>/dev/null || sleep 1
    i=$((i + 1))
done
[ -n "$CONF" ] || { log "no active composition"; exit 0; }
CONFNAME=$(basename "$CONF")

UDC=$(cat "$UDC_FILE" 2>/dev/null)
[ -n "$UDC" ] || { log "gadget not bound"; exit 0; }

changed=0

# --- audio sink (host -> phone) -------------------------------------------
FUNC_UAC2=$CFG/functions/uac2.0
if [ ! -d "$FUNC_UAC2" ]; then
    mkdir -p "$FUNC_UAC2" 2>/dev/null || log "uac2 unavailable (module not loaded?)"
fi
if [ -d "$FUNC_UAC2" ]; then
    echo 48000 > "$FUNC_UAC2/c_srate"
    echo 48000 > "$FUNC_UAC2/p_srate"
    echo 3 > "$FUNC_UAC2/c_chmask"
    echo 0 > "$FUNC_UAC2/p_chmask"
    echo 2 > "$FUNC_UAC2/c_ssize"
    if [ ! -e "$CONF/uac2.0" ]; then
        ( cd "$CFG" && ln -s functions/uac2.0 "configs/$CONFNAME/uac2.0" ) && changed=1
    fi
fi

# --- HID keyboard + mouse -------------------------------------------------
add_hid() {
    name="$1"      # hid.usb0 / hid.usb1
    proto="$2"     # 1 = keyboard, 2 = mouse
    len="$3"       # report length in bytes
    desc="$4"      # descriptor file

    f=$CFG/functions/$name
    if [ ! -d "$f" ]; then
        mkdir -p "$f" 2>/dev/null || { log "$name mkdir failed"; return; }
        echo "$proto" > "$f/protocol"
        echo 1 > "$f/subclass"
        echo "$len" > "$f/report_length"
        if [ -f "$desc" ]; then
            cat "$desc" > "$f/report_desc"
        else
            log "missing descriptor $desc"
        fi
    fi

    if [ ! -e "$CONF/$name" ]; then
        ( cd "$CFG" && ln -s "functions/$name" "configs/$CONFNAME/$name" ) && changed=1
    fi
}

add_hid hid.usb0 1 8 "$HID_DIR/keyboard.bin"
add_hid hid.usb1 2 4 "$HID_DIR/mouse.bin"

if [ "$changed" -eq 0 ]; then
    log "composition already carries uac2 + hid: $(ls "$CONF" | tr '\n' ' ')"
    exit 0
fi

# One rebind for everything that was added. adb drops for a second; it comes
# back because the HAL's own function (f1) stays linked.
log "rebinding gadget with uac2 + hid"
echo "" > "$UDC_FILE"
sleep 1
echo "$UDC" > "$UDC_FILE"
log "done: $(ls "$CONF" | tr '\n' ' ')"
