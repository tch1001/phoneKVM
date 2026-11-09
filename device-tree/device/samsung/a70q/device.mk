#
# Copyright (C) 2023 The LineageOS Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

DEVICE_PATH := device/samsung/a70q

# Local overlays
DEVICE_PACKAGE_OVERLAYS += \
    $(DEVICE_PATH)/overlay

# Audio
PRODUCT_COPY_FILES += \
	$(DEVICE_PATH)/audio/configs/mixer_paths_idp.xml:$(TARGET_COPY_OUT_VENDOR)/etc/mixer_paths_idp.xml

# Boot animation
TARGET_SCREEN_HEIGHT := 2400
TARGET_SCREEN_WIDTH := 1080

# Init scripts
PRODUCT_PACKAGES += \
    init.a70q.rc

# UAC2 USB audio sink: gadget composition script + host->speaker bridge
# hidbridge: root daemon that owns /dev/hidg* so apps can inject HID
PRODUCT_PACKAGES += \
    uac2sink \
    hidbridge

PRODUCT_COPY_FILES += \
	device/samsung/sm6150-common/rootdir/bin/uac2-gadget.sh:$(TARGET_COPY_OUT_VENDOR)/bin/uac2-gadget.sh \
	device/samsung/a70q/hid/keyboard.bin:$(TARGET_COPY_OUT_VENDOR)/etc/hid/keyboard.bin \
	device/samsung/a70q/hid/mouse.bin:$(TARGET_COPY_OUT_VENDOR)/etc/hid/mouse.bin

# Custom apps
PRODUCT_PACKAGES += \
    QuickRecord

# Inherit from the common makefile
$(call inherit-product, device/samsung/sm6150-common/common.mk)

# Inherit from the proprietary files makefile
$(call inherit-product, vendor/samsung/a70q/a70q-vendor.mk)
