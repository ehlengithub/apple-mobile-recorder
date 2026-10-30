"""Trusted build/flash profiles; phone geometry is independent of controller."""

from typing import Literal

Controller = Literal["usb-s3", "ble-esp32"]
PROFILES = {
    "usb-s3": {
        "chip": "esp32s3",
        "transport": "usb",
        "bootloader_offset": 0,
        "fqbn": "esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=cdc,FlashSize=8M",
        "name": "ESP32-S3 / USB",
    },
    "ble-esp32": {
        "chip": "esp32",
        "transport": "ble",
        "bootloader_offset": 4096,
        "fqbn": "esp32:esp32:esp32:FlashSize=4M,FlashMode=dio,FlashFreq=40",
        "name": "经典 ESP32 / 蓝牙",
    },
}


def profile(controller="usb-s3"):
    if controller not in PROFILES:
        raise ValueError("不支持的控制器板型")
    return PROFILES[controller]
