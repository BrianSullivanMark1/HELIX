"""ComponentService — the component library in service: resolves a parts-list row to a library
part, turns "which components do I need" into a readable brief, and describes a part in one line.

WHY: the IronEye session spent dozens of turns choosing a mic, an amp and a camera from chat memory,
then baked guessed sizes into the model. Here the choosing reads from the catalog (real parts, real
sizes, a confidence per number) and the parts list carries the catalog key so the enclosure
generator sizes its pockets from the library, an ad-hoc measurement, or a LiPo size code — never
from the model's memory.

Readable on autonomous runs: `suggest()` names no fenced tool (tests pin the list); it reads like a
knowledgeable friend and says plainly what the library doesn't know.

Contract: READ_ME/MAKER_FLOW.md §2 (bottom).
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from helix.domain import components as lib
from helix.domain.components import Component, format_dims

if TYPE_CHECKING:
    from helix.services.parts import Part, PartsService

_MAX_PER_ROLE = 3
_ROLE_TITLES = {
    "vision": "Vision (camera)", "hearing": "Hearing (microphone)", "speaking": "Speaking (amp + speaker)",
    "compute": "Brain (microcontroller)", "power": "Power (battery)", "charging": "Charging",
    "sensing": "Sensing", "display": "Display", "motion": "Motion", "storage": "Storage",
    "wireless": "Wireless", "lighting": "Lighting", "input": "Input (buttons, knobs)",
}
# The one-line "why" a friend would give. Falls back to the catalog note's first sentence.
_WHY: dict[str, str] = {
    "xiao_esp32s3_sense": "camera, PDM mic and microSD on one thumb-size board — the smallest full wearable brain",
    "esp32_cam": "the cheapest camera board; needs a USB programmer base and has no mic",
    "pi_camera_v3": "autofocus 12 MP with an official drawing — the safe choice on a Pi",
    "pi_camera_v2": "8 MP, same mount as v3, cheaper and everywhere",
    "esp32_devkitc": "the standard ESP32 dev board; wide, no holes, plenty of pins",
    "esp32_s3_devkitc": "ESP32-S3 with native USB and more RAM for audio/vision work",
    "xiao_esp32s3": "thumb-size ESP32-S3 without the camera board",
    "xiao_esp32c3": "tiny, cheap Wi-Fi/BLE brain; no camera, needs its little antenna",
    "pi_zero_2w": "a real Linux computer the size of a stick of gum; runs vision models and the Pi camera",
    "pi_pico_w": "cheap, cool-running RP2040 with Wi-Fi; MicroPython-friendly",
    "pi_4": "full Pi with USB 3 and dual HDMI; big and warm",
    "pi_5": "fastest Pi; needs airflow and a beefy USB-C supply",
    "arduino_uno": "the classic; big board, 5 V logic, no Wi-Fi",
    "arduino_nano": "Uno brains in a breadboard-size board",
    "inmp441": "digital I2S mic with clean audio; the ESP32 favourite",
    "inmp441_round": "the same mic on a round board",
    "electret_mic_9_7": "analog capsule — needs a preamp, but fits a 2 mm hole",
    "max98357a": "I2S amp straight to a small speaker — the 4-pack is cheap",
    "max98357a_adafruit": "the same amp with Adafruit's docs and a known outline",
    "pam8403": "analog stereo amp for DAC/analog audio, not I2S",
    "speaker_28mm": "the sweet spot for a wearable: 2 W, thin, loud enough",
    "speaker_20mm": "smaller and quieter; fits where 28 won't",
    "speaker_36mm": "fuller sound; needs a bigger face",
    "speaker_40mm": "the loudest small round speaker; check its depth",
    "buzzer_12mm": "beeps only — not for speech",
    "cell_18650": "cheap capacity in a holder; the enclosure gets long",
    "lipo_500mah_adafruit": "flat pouch cell with a JST-PH lead; sizes by code (603048 etc.) for other capacities",
    "cell_aa": "off-the-shelf cells; no charging circuit needed",
    "holder_18650_1": "holds one 18650 with leads",
    "tp4056_usb_c": "USB-C charge + protect for one LiPo cell; the standard",
    "tp4056_micro_usb": "the micro-USB version of the same charger",
    "usb_c_breakout": "a USB-C port on a wire for power in",
    "oled_096_ssd1306": "the tiny I2C screen everyone has a library for",
    "oled_13_sh1106": "the same idea, a bit bigger",
    "st7789_154_adafruit": "colour square TFT with a proper drawing",
    "lcd_1602": "16×2 characters, cheap, big, and has real mounting holes",
    "servo_sg90": "the 9 g servo; light duty",
    "servo_mg996r": "strong standard servo",
    "nema17": "a proper stepper with a standard bolt pattern",
    "stepper_28byj48": "cheap geared 5 V stepper with its own driver board",
    "hc_sr04": "the classic ultrasonic ranger; two 16 mm eyes",
    "vl53l0x_gy530": "laser ranging in a tiny board; needs a clear window",
    "dht22": "temperature + humidity; needs air",
    "bme280_gy": "temperature, humidity and pressure on a thumbnail",
    "mpu6050_gy521": "6-axis motion sensing",
    "hc_sr501": "PIR presence with a dome to poke through the wall",
    "mlx90640_adafruit": "a 32×24 thermal camera — heat vision, not night vision",
    "ir_led_850nm": "IR illuminators for a night-vision camera",
    "led_5mm": "a status light",
    "ws2812_ring_12": "addressable ring for a glowing face",
    "microsd_module": "SD storage for logging or recordings",
    "microsd_adafruit": "a small, documented SD breakout",
    "nrf24l01": "cheap 2.4 GHz link between two boards",
    "lora_ra02": "kilometre-range LoRa link",
    "neo6m_gy": "GPS position, with a separate patch antenna",
    "switch_ss12d00": "the little slide power switch",
    "tactile_6x6": "a plain push button",
    "push_button_12mm": "a proper panel push button with a nut",
    "encoder_ky040": "a turn-and-click knob",
    "tsop38238": "reads IR remotes through a small dark window",
    "mpu6050_adafruit": "the same IMU with a documented outline and STEMMA QT",
    "oled_096_adafruit": "the tiny OLED with a known outline and STEMMA QT",
    "oled_13_adafruit": "the bigger OLED with a known outline",
    "rfm95w_adafruit": "LoRa on a documented breakout with an antenna pad",
    "bme280_adafruit": "the environment sensor on a documented board",
    "vl53l0x_adafruit": "laser ranging on a documented board",
    "ds18b20_probe": "a sealed temperature probe on a wire — for liquids and outdoors",
    "bmp280_gy": "pressure and temperature on a thumbnail (no humidity)",
    "mq2_module": "smoke and gas; runs warm, needs a vent",
    "rc522": "reads RFID cards through a plastic wall",
    "ds3231_zs042": "keeps the time through power loss on a coin cell",
    "bh1750_gy302": "measures light level through a clear window",
    "hc05": "classic Bluetooth serial link to a phone",
    "esp01": "the smallest Wi-Fi module; awkward header",
    "gps_antenna_25": "the patch antenna a GPS module needs under a plastic lid",
    "xiao_esp32c3": "tiny, cheap Wi-Fi/BLE brain; no camera, needs its little antenna",
    "st7789_20_adafruit": "a bright 2\" colour TFT with a known outline",
    "st7789_20_waveshare": "a bright 2\" colour TFT, Waveshare's outline",
    "lcd_2004": "20×4 characters with real mounting holes",
    "tm1637_4digit": "four big digits for a clock or a count",
    "max7219_8x8": "an 8×8 LED matrix for icons and scrolling text",
    "ws2812_ring_8": "a small addressable ring",
    "ws2812_ring_16": "a mid-size addressable ring",
    "ws2812_ring_24": "a big addressable ring for a face or a dial",
    "ws2812_stick_8": "an addressable bar of eight",
    "led_3mm": "a small status light",
    "fan_30mm": "a small fan with the standard 24 mm bolt pattern",
    "fan_40mm": "a 40 mm fan with the standard 32 mm bolt pattern",
    "servo_mg90s": "the 9 g servo with metal gears",
    "motor_tt_gear": "the yellow robot-wheel motor",
    "vibration_motor_10mm": "a coin buzzer for haptics",
    "tactile_12x12": "a big push button that takes a cap",
    "switch_kcd1": "a snap-in rocker power switch",
    "pot_10k": "a volume-style knob",
    "cell_cr2032": "a coin cell for a clock or a tiny sensor",
    "holder_cr2032": "a clip for a replaceable coin cell",
    "holder_18650_2": "holds two 18650s side by side",
    "holder_aa_2": "holds two AAs",
    "esp32_devkitc_30": "the narrower 30-pin ESP32 board",
    "esp32_cam_mb": "the USB base the ESP32-CAM programs through",
    "esp32_c3_supermini": "tiny ESP32-C3 with USB-C",
    "esp8266_nodemcu": "the ESP8266 classic; Wi-Fi, no Bluetooth",
    "nodemcu_lolin_v3": "the wide ESP8266 board",
    "wemos_d1_mini": "small ESP8266 with a known outline",
    "arduino_uno_r4": "the Uno with USB-C and (WiFi version) Wi-Fi",
    "arduino_mega": "the Uno's big brother with 54 I/O pins",
    "arduino_nano_esp32": "Nano-size ESP32-S3 with USB-C",
    "arduino_pro_mini": "a bare 3.3/5 V Arduino with no USB",
    "teensy_40": "a fast ARM board for audio work",
    "ov2640_24pin": "the bare camera lens block an ESP32-CAM uses",
    "ov2640_night_vision": "the no-IR-filter lens that makes an ESP32-CAM see at night",
    "microsd_module": "SD storage for logging or recordings",
    "breadboard_half": "a half breadboard for a prototype in a box",
    "breadboard_mini": "a mini breadboard for a few parts",
    "protoboard_5x7": "a perfboard for the soldered version",
    "usb_c_breakout": "a USB-C port on a wire for power in",
    "barrel_jack_5521": "a 5.5 mm barrel jack for a wall adapter",
    "jst_ph_2": "the battery-lead connector",
    "jst_xh_2": "the connector on most 18650 holders",
    "screw_terminal_2": "screw terminal for thick wires",
    "mt3608": "boosts a battery up to 5–12 V",
    "buck_lm2596": "steps 12–24 V down to 5 V for the boards",
    "driver_uln2003": "the driver that comes with the 28BYJ-48",
    "driver_l298n": "drives two DC motors or a stepper; bulky",
    "driver_a4988": "the standard StepStick driver",
    "driver_tmc2209": "the quiet StepStick driver",
    "relay_1ch": "switches mains or a pump — keep it in its own bay",
    "relay_2ch": "two relays for two loads",
    "relay_4ch": "four relays; big board",
    "microswitch_kw11": "a lever limit switch",
    "speaker_20x30": "a thin rectangular speaker for a slim box",
    "speaker_30x40": "a louder rectangular speaker",
    "inmp441_round": "the same mic on a round board",
    "hc_sr04": "the classic ultrasonic ranger; two 16 mm eyes",
    "dht11": "the cheaper, coarser temperature-humidity sensor",
    "vl53l0x_gy530": "laser ranging in a tiny board; needs a clear window",
    "neo6m_gy": "GPS position, with a separate patch antenna",
    "cell_18650": "cheap capacity in a holder; the enclosure gets long",
}
# Roles a friend would say something honest about when they come up.
_HONEST: dict[str, str] = {
    "vision": ("No single ESP32-CAM board has built-in IR night vision: it's a standard ESP32-CAM plus an "
               "850 nm OV2640 lens module (no IR filter) and a few 850 nm IR LEDs around the lens. Lens "
               "positions on the XIAO Sense and the ESP32-CAM are read from photos, not drawings (±2 mm)."),
    "hearing": ("The INMP441 is a bottom-port mic — its sound hole is on the side opposite the chip, so that "
                "side needs the hole to the outside. Generic INMP441 boards vary by a millimetre."),
    "speaking": ("An I2S amp (MAX98357A) drives a small speaker directly; a PAM8403 needs an analog source. "
                 "Generic speaker depths vary 3–6 mm — check the listing or measure."),
    "power": ("Pouch LiPo sizes come from the code on the cell (603048 = 6 × 30 × 48 mm); real cells run "
              "up to 1 mm over. A LiPo needs the TP4056's protection; 18650s need a holder."),
    "charging": "TP4056 boards charge at 1 A by default — fine for 1000 mAh and up; smaller cells want the resistor swap.",
    "compute": "Dev boards without holes (ESP32 DevKitC, XIAO) sit in rib-walled pockets, not on standoffs.",
}
# Words that belong to the enclosure/printing side of the conversation, not a component role.
_NOISE = frozenset({
    "a", "an", "the", "and", "with", "for", "that", "it", "i", "want", "need", "needs", "build", "make",
    "device", "thing", "project", "enclosure", "case", "box", "print", "printed", "hat", "cam", "small",
    "tiny", "cheap", "please", "some", "kind", "of", "to", "on", "in", "my", "which", "components", "do",
    "parts", "should", "use", "runs", "run", "over", "also", "plus", "has", "have", "can", "so", "be", "is",
})


def _split_needs(text: str) -> list[str]:
    """"vision, hearing and speech; runs on a battery, charges over USB-C" → the need phrases the
    library can map, in the order they were said, plus the words it could not."""
    low = (text or "").lower().replace("&", " and ")
    chunks = [c.strip(" .") for c in re.split(r"[,;/+\n]|\band\b|\bwith\b|\bplus\b", low) if c.strip(" .")]
    needs: list[str] = []
    for chunk in chunks:
        # every need phrase in the chunk ("speech that runs on a battery" is two), longest first,
        # each word claimed once
        phrases = lib.need_phrases(chunk)
        if phrases:
            for ph in phrases:
                if ph not in needs:
                    needs.append(ph)
            continue
        words = [w for w in re.split(r"[^a-z0-9\-]+", chunk) if w and w not in _NOISE]
        if words:
            needs.append(" ".join(words))
    return needs


class ComponentService:
    def __init__(self, parts: "PartsService | None", amazon=None) -> None:
        self._parts = parts
        self._amazon = amazon  # reserved: an AmazonWeb for live prices (never called on autonomous runs)

    # ----- resolving -----
    def resolve(self, name_or_key: str, *, dims=None) -> Component | None:
        """Catalog first (find), then a LiPo code in the text, then ad-hoc when dims are given —
        dims as (L, W, H) mm or a listing line dims_from_text can read."""
        text = str(name_or_key or "").strip()
        hit = lib.find_loose(text) if text else None
        if hit is not None:
            return hit
        cell = lib.lipo_from_code(text) if lib._lipo_code(text) and _looks_like_cell(text) else None
        if cell is not None:
            return cell
        trio = _dims(dims)
        if trio is not None and text:
            return lib.adhoc(text, *trio)
        return None

    def resolve_parts(self, project: str) -> tuple[list[tuple["Part", Component]], list["Part"]]:
        """Every row of a parts list mapped to a Component (by `component` key, then by name, then a
        LiPo code in the name/spec, then ad-hoc from the row's length/width/height), and the rows it
        could not resolve."""
        if self._parts is None:
            return [], []
        resolved: list[tuple[Part, Component]] = []
        unresolved: list[Part] = []
        for row in self._parts.rows(project):
            c = self._resolve_row(row)
            if c is None:
                unresolved.append(row)
            else:
                resolved.append((row, c))
        return resolved, unresolved

    def _resolve_row(self, row: "Part") -> Component | None:
        if row.component:
            c = lib.find(row.component)
            if c is not None:
                return c
        c = lib.find_loose(row.name)
        if c is not None:
            return c
        for text in (row.name, row.spec, row.component):
            if text and lib._lipo_code(text) and _looks_like_cell(text):
                cell = lib.lipo_from_code(text)
                if cell is not None:
                    return cell
        if row.dims is not None:
            return lib.adhoc(row.name, *row.dims)
        return None

    # ----- the brief -----
    def suggest(self, needs_text: str) -> str:
        """The model-facing text for "which components do I need": roles, 2–3 candidates each with
        size and why, an honest line about what the library doesn't know. Names no fenced tool."""
        needs = _split_needs(needs_text)
        if not needs:
            return ("Tell me what the device should do — see, hear, speak, run on a battery, show a "
                    "readout, sense temperature, move something — and I'll pick real parts with sizes.")
        kit = lib.kit_for(needs)
        unknown = kit.pop("unknown", [])
        out: list[str] = []
        if kit:
            out.append("Here's what I'd build it from (size is length × width × height, the tallest point included):")
        for tag, cands in kit.items():
            picks = _spread(cands, tag)[:_MAX_PER_ROLE]
            out.append(f"\n{_ROLE_TITLES.get(tag, tag.title())}:")
            for c in picks:
                out.append(f"- {c.name} — {format_dims(c)}{_sure(c)}: {_why(c)}.")
            if tag in _HONEST:
                out.append(f"  Note: {_HONEST[tag]}")
            if tag == "vision" and "night vision" in needs:
                ir = lib.CATALOG.get("ir_led_850nm")
                lens = lib.CATALOG.get("ov2640_night_vision")
                if ir and lens:
                    out.append(f"  For night vision add: {lens.name} — {format_dims(lens)}{_sure(lens)}; "
                               f"{ir.name} × 4–6 — {format_dims(ir)}.")
        if unknown:
            out.append("\nI don't have library parts for: " + ", ".join(unknown) + ". Name the exact part "
                       "or give me its size (or hold it under the camera and we'll measure it) and I'll "
                       "carry it as an ad-hoc part with a pocket.")
        out.append("\nSizes marked (approx) are community-measured — the enclosure leaves them 0.5 mm more "
                   "room. When you've picked, save the parts to the project's list with their library "
                   "keys, a face hint for anything that must reach a wall (camera, speaker, USB), and "
                   "'on the lid' for a battery; then the enclosure can be designed from the list.")
        return "\n".join(out).strip()

    # ----- one line -----
    @staticmethod
    def describe(c: Component) -> str:
        """One line: name, L×W×H, mount, ports, apertures, confidence."""
        bits = [f"{c.name} — {format_dims(c)}", f"{c.mount} mount"]
        if c.holes:
            bits.append(f"{len(c.holes)} mounting holes")
        if c.ports:
            bits.append("ports: " + ", ".join(f"{_kind(p.kind)} on the {p.side}" for p in c.ports))
        if c.apertures:
            bits.append("apertures: " + ", ".join(f"{a.kind} ({a.face})" for a in c.apertures))
        bits.append(f"confidence {c.confidence:.2f} ({c.source}{', approx' if c.approx else ''})")
        return "; ".join(bits)


# ----- helpers -----
def _looks_like_cell(text: str) -> bool:
    low = (text or "").lower()
    return len(lib._norm(low)) == 6 or any(w in low for w in ("lipo", "battery", "mah", "cell", "li-po", "lithium"))


def _dims(dims) -> tuple[float, float, float] | None:
    if dims is None:
        return None
    if isinstance(dims, str):
        return lib.dims_from_text(dims)
    try:
        vals = [abs(float(v)) for v in dims]
    except (TypeError, ValueError):
        return None
    if len(vals) < 2 or any(v <= 0 for v in vals[:2]):
        return None
    while len(vals) < 3:
        vals.append(0.0)
    return (vals[0], vals[1], vals[2])


_ROLE_CATEGORIES: dict[str, tuple[str, ...]] = {
    "vision": ("camera", "mcu"), "hearing": ("mic", "mcu"), "speaking": ("amp", "speaker"),
    "compute": ("mcu",), "power": ("battery",), "charging": ("charger",), "sensing": ("sensor",),
    "display": ("display",), "motion": ("motor",), "storage": ("storage", "mcu"), "wireless": ("comm", "mcu"),
    "lighting": ("led",), "input": ("button", "switch"),
}
_SPREAD_ROLES = frozenset({"vision", "hearing", "speaking", "storage", "input"})


def _spread(cands: list[Component], tag: str) -> list[Component]:
    """Best first within the categories that answer the role (a battery for "power", not a USB
    breakout); for roles answered by two kinds of part, one of each before a second of either —
    "speaking" shows an amp and a speaker, not three speakers."""
    cats = _ROLE_CATEGORIES.get(tag)
    pool = [c for c in cands if c.category in cats] if cats else list(cands)
    if len(pool) < 2:
        pool = list(cands)
    if tag not in _SPREAD_ROLES:
        return pool[:_MAX_PER_ROLE]
    out: list[Component] = []
    seen: set[str] = set()
    for c in pool:
        if c.category in seen:
            continue
        out.append(c)
        seen.add(c.category)
        if len(out) >= _MAX_PER_ROLE:
            break
    for c in pool:
        if len(out) >= _MAX_PER_ROLE:
            break
        if c not in out:
            out.append(c)
    return out


def _sure(c: Component) -> str:
    return " (approx)" if c.approx else ""


def _why(c: Component) -> str:
    if c.key in _WHY:
        return _WHY[c.key]
    # the first sentence of the note that isn't just numbers
    for sentence in re.split(r"(?<=[.;])\s+", c.notes.strip()):
        s = sentence.strip().rstrip(".;")
        if s and not s[0].isdigit() and not s.startswith(("~", "Ø", "Body", "Board")):
            return s
    return f"a {c.category} part with a {c.source} outline"


def _kind(kind: str) -> str:
    return {"usb_c": "USB-C", "micro_usb": "micro-USB", "usb_a": "USB-A", "barrel_5_5": "5.5 mm barrel jack",
            "jst_ph": "JST-PH", "jst_xh": "JST-XH", "sd": "SD slot", "hdmi": "HDMI", "audio_3_5": "3.5 mm audio",
            "header": "pin header", "antenna": "antenna", "other": "connector"}.get(kind, kind)
