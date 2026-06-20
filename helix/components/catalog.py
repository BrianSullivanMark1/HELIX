"""Curated electronics-parts taxonomy + structured query builder (§components).

Pure, Qt-free, stdlib-only — mirrors `helix/home/groceries.py`. The Components screen drives a
cascading filter UI from `CATEGORIES` (category → package types + spec dropdowns), and turns the
user's selections into a single vendor keyword query via `build_query()`. Every dropdown value is a
curated real-world string, so the vendor search can never be fed a typo or a meaningless term.

Design: each spec field that is a good *search keyword* (a part core, a sensor type, a chip family)
is flagged `query=True`; numeric/electrical fields (flash size, voltage, pitch) are filters the user
sees but are deliberately kept *out* of the keyword string so a search reads like
"ARM Cortex-M4 microcontroller", not "…8KB 3.3V I2C" (which no catalog matches).
"""
from __future__ import annotations

from typing import Any

ANY = "Any"  # the empty/unset choice shown first in every dropdown


def _spec(key: str, label: str, options: list[str], *, query: bool = False) -> dict:
    """One spec dropdown: a stable key, a human label, curated options, and whether its value
    contributes to the vendor keyword string."""
    return {"key": key, "label": label, "options": list(options), "query": query}


# Each category: an icon (emoji for quick visual scanning), a base keyword folded into the search,
# an ordered list of package / sub-type choices, and the ordered spec fields relevant to it.
CATEGORIES: list[dict] = [
    {
        "key": "mcu",
        "label": "Microcontrollers",
        "icon": "\U0001f9e0",  # brain
        "base": "microcontroller",
        "packages": ["SMD", "Through-hole", "Module / Dev Board"],
        "specs": [
            _spec("core", "Core", [
                "ARM Cortex-M0", "ARM Cortex-M0+", "ARM Cortex-M3", "ARM Cortex-M4",
                "ARM Cortex-M7", "RISC-V", "AVR", "ESP32 Xtensa", "PIC", "8051",
            ], query=True),
            _spec("flash", "Flash", ["8KB", "16KB", "32KB", "64KB", "128KB", "256KB", "512KB", "1MB", "2MB"]),
            _spec("ram", "RAM", ["2KB", "8KB", "16KB", "32KB", "64KB", "256KB", "512KB"]),
            _spec("voltage", "Voltage", ["1.8V", "3.3V", "5V"]),
            _spec("speed", "Speed", ["8MHz", "16MHz", "48MHz", "72MHz", "120MHz", "168MHz", "240MHz"]),
            _spec("interface", "Interface", ["I2C", "SPI", "UART", "USB", "CAN"]),
        ],
    },
    {
        "key": "camera",
        "label": "Cameras",
        "icon": "\U0001f4f7",  # camera
        "base": "camera module",
        "packages": ["Module", "Board", "Flex / FPC"],
        "specs": [
            _spec("interface", "Interface", ["MIPI CSI", "DVP Parallel", "USB", "SPI"], query=True),
            _spec("format", "Sensor", ["OV2640", "OV5640", "OV7670", "IMX219", "ArduCam", "GC0308"], query=True),
            _spec("resolution", "Resolution", ["VGA 0.3MP", "1MP", "2MP", "5MP", "8MP", "12MP"]),
            _spec("fps", "Frame rate", ["15 FPS", "30 FPS", "60 FPS", "120 FPS"]),
        ],
    },
    {
        "key": "sensor",
        "label": "Sensors",
        "icon": "\U0001f321",  # thermometer
        "base": "sensor",
        "packages": ["SMD", "Module", "Through-hole"],
        "specs": [
            _spec("type", "Type", [
                "Temperature", "Humidity", "IMU / Accelerometer", "Gas", "Light / Ambient",
                "Pressure / Barometric", "Proximity", "Hall Effect", "Current",
            ], query=True),
            _spec("interface", "Interface", ["I2C", "SPI", "Analog", "UART"]),
            _spec("voltage", "Voltage", ["1.8V", "3.3V", "5V"]),
        ],
    },
    {
        "key": "power",
        "label": "Power",
        "icon": "⚡",  # high voltage
        "base": "",
        "packages": ["SMD", "Module", "Through-hole"],
        "specs": [
            _spec("type", "Type", [
                "LDO Regulator", "Buck Converter", "Boost Converter", "Buck-Boost",
                "LiPo Charger", "Battery Fuel Gauge",
            ], query=True),
            _spec("vin", "Input V", ["3.3V", "5V", "12V", "24V"]),
            _spec("vout", "Output V", ["1.8V", "3.3V", "5V", "12V", "Adjustable"]),
            _spec("current", "Current", ["100mA", "500mA", "1A", "2A", "3A", "5A"]),
        ],
    },
    {
        "key": "connector",
        "label": "Connectors",
        "icon": "\U0001f50c",  # plug
        "base": "connector",
        "packages": ["SMD", "Through-hole", "Cable"],
        "specs": [
            _spec("type", "Type", [
                "JST-PH", "JST-SH", "USB-C", "USB Micro-B", "Barrel Jack", "Pin Header",
                "Pin Socket", "Terminal Block", "FFC / FPC",
            ], query=True),
            _spec("pitch", "Pitch", ["1.0mm", "1.25mm", "2.0mm", "2.54mm", "5.08mm"]),
            _spec("pins", "Pin count", ["2", "3", "4", "5", "6", "8", "10", "16", "20", "40"]),
        ],
    },
    {
        "key": "display",
        "label": "Displays",
        "icon": "\U0001f5a5",  # desktop computer
        "base": "display",
        "packages": ["Module", "Bare panel"],
        "specs": [
            _spec("type", "Type", [
                "OLED", "TFT LCD", "E-Paper", "LED Matrix", "7-Segment", "LCD Character",
            ], query=True),
            _spec("interface", "Interface", ["I2C", "SPI", "Parallel", "HDMI"]),
            _spec("size", "Size", ['0.96"', '1.3"', '1.8"', '2.4"', '2.8"', '3.5"', '5.0"', '7.0"']),
            _spec("resolution", "Resolution", ["128x64", "240x240", "320x240", "480x320", "800x480"]),
        ],
    },
    {
        "key": "audio",
        "label": "Audio",
        "icon": "\U0001f50a",  # speaker
        "base": "",
        "packages": ["SMD", "Module"],
        "specs": [
            _spec("type", "Type", [
                "MEMS Microphone", "Electret Microphone", "Speaker", "Audio Amplifier",
                "DAC", "ADC / Codec",
            ], query=True),
            _spec("interface", "Interface", ["I2S", "PDM", "Analog", "I2C"]),
            _spec("voltage", "Voltage", ["1.8V", "3.3V", "5V"]),
        ],
    },
    {
        "key": "rf",
        "label": "RF / Wireless",
        "icon": "\U0001f4e1",  # satellite antenna
        "base": "",
        "packages": ["SMD", "Module"],
        "specs": [
            _spec("type", "Type", [
                "Wi-Fi", "Bluetooth / BLE", "LoRa", "Zigbee", "nRF24", "GPS",
                "Cellular / LTE", "RFID / NFC",
            ], query=True),
            _spec("interface", "Interface", ["SPI", "UART", "USB", "I2C"]),
            _spec("frequency", "Frequency", ["433MHz", "868MHz", "915MHz", "2.4GHz", "5GHz"]),
            _spec("antenna", "Antenna", ["PCB", "U.FL / IPEX", "SMA", "Chip"]),
        ],
    },
    {
        "key": "storage",
        "label": "Storage",
        "icon": "\U0001f4be",  # floppy disk
        "base": "",
        "packages": ["SMD", "Module"],
        "specs": [
            _spec("type", "Type", [
                "EEPROM", "NOR Flash", "NAND Flash", "SD Card Slot", "FRAM", "SRAM",
            ], query=True),
            _spec("interface", "Interface", ["I2C", "SPI", "QSPI", "SDIO"]),
            _spec("capacity", "Capacity", ["4KB", "64KB", "1MB", "4MB", "16MB", "128MB", "32GB"]),
        ],
    },
    {
        "key": "passive",
        "label": "Passives",
        "icon": "\U0001f9e9",  # puzzle piece
        "base": "",
        "packages": ["0402", "0603", "0805", "1206", "Through-hole"],
        "specs": [
            _spec("type", "Type", [
                "Resistor", "Capacitor", "Inductor", "Ferrite Bead", "Crystal",
                "Diode", "MOSFET", "LED",
            ], query=True),
            _spec("value", "Value", [
                "10Ω", "100Ω", "1kΩ", "10kΩ", "100kΩ", "1MΩ",
                "1nF", "10nF", "100nF", "1µF", "10µF", "100µF",
            ]),
            _spec("tolerance", "Tolerance", ["1%", "5%", "10%", "20%"]),
        ],
    },
    {
        "key": "devboard",
        "label": "Dev Boards",
        "icon": "\U0001f6e0",  # hammer and wrench
        "base": "development board",
        "packages": [],
        "specs": [
            _spec("family", "Family", [
                "Raspberry Pi", "Arduino", "ESP32", "ESP8266", "STM32 Nucleo", "Teensy",
                "Adafruit Feather", "micro:bit", "BeagleBone",
            ], query=True),
            _spec("connectivity", "Connectivity", ["Wi-Fi", "Bluetooth", "Ethernet", "None"]),
            _spec("form", "Form factor", ["Full", "Mini", "Nano", "Zero"]),
        ],
    },
]

_BY_KEY = {c["key"]: c for c in CATEGORIES}
# Generic package words that are too vague to help a keyword search (so they stay out of the query).
_VAGUE_PACKAGES = {"", ANY, "Module", "Board", "Module / Dev Board", "Bare panel", "Cable", "Through-hole"}


def category(key: str) -> dict | None:
    """The category record for `key`, or None."""
    return _BY_KEY.get(key)


def selected_chips(category_key: str, package: str, specs: dict[str, str]) -> list[tuple[str, str, str]]:
    """The active selections as (kind, label, value) tuples for the dismissible filter chips.

    `kind` is "package" or a spec key, so the UI knows what to clear when a chip's ✕ is clicked.
    Skips anything unset (empty or "Any")."""
    cat = _BY_KEY.get(category_key)
    if not cat:
        return []
    chips: list[tuple[str, str, str]] = []
    if package and package != ANY:
        chips.append(("package", "Package", package))
    for field in cat["specs"]:
        value = (specs.get(field["key"]) or "").strip()
        if value and value != ANY:
            chips.append((field["key"], field["label"], value))
    return chips


def build_query(category_key: str, package: str, specs: dict[str, str]) -> str:
    """Turn the structured selections into a single vendor keyword string.

    Folds in the query-flagged spec values, the category base term, and a specific (non-vague)
    package, in that order. Falls back to the category label so a bare category still searches."""
    cat = _BY_KEY.get(category_key)
    if not cat:
        return ""
    terms: list[str] = []
    for field in cat["specs"]:
        value = (specs.get(field["key"]) or "").strip()
        if field.get("query") and value and value != ANY:
            terms.append(value)
    if cat.get("base"):
        terms.append(cat["base"])
    if package and package not in _VAGUE_PACKAGES:
        terms.append(package)
    if not terms:
        terms.append(cat["label"].lower())
    return " ".join(terms).strip()
