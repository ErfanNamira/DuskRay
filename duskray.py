"""
DuskRay — Display Brightness & Warmth Control App
-------------------------------------------------
A lightweight Windows system tray utility to control screen brightness and
color warmth (in Kelvin), with a one-click enable/disable and the option to
run automatically at Windows startup.

INSTALL (once):
    pip install pystray Pillow screen_brightness_control

RUN:
    pythonw duskray.py      (no console window, recommended)
    python  duskray.py      (with console, for debugging)

OPTIONAL - build a standalone DuskRay.exe (recommended, see README):
    pip install pyinstaller
    pyinstaller --onefile --noconsole --clean --strip --name "DuskRay" \
        --icon=duskray.ico --version-file=version_info.txt \
        --exclude-module tkinter --exclude-module unittest \
        --exclude-module pdb --exclude-module doctest --exclude-module test \
        duskray.py
"""

import os
import sys
import json
import math
import ctypes
import winreg
import threading

from PIL import Image, ImageDraw
import pystray
import screen_brightness_control as sbc

APP_NAME = "DuskRay"
CONFIG_PATH = os.path.join(os.getenv("APPDATA"), APP_NAME, "config.json")

BRIGHTNESS_LEVELS = list(range(10, 101, 10))            # 10..100 (%)
WARMTH_LEVELS_K = [6500, 6000, 5500, 5000, 4500, 4000,   # Kelvin, high->low
                    3500, 3000, 2500, 2000]
NEUTRAL_K = 6500  # "no warmth" reference point

DEFAULT_CONFIG = {
    "enabled": True,
    "brightness": 100,
    "warmth_k": 4000,
    "run_at_startup": False,
}

# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------

def load_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            data = json.load(f)
        cfg = DEFAULT_CONFIG.copy()
        cfg.update(data)
        return cfg
    except Exception:
        return DEFAULT_CONFIG.copy()


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


config = load_config()
config_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Brightness control
# ---------------------------------------------------------------------------

def apply_brightness(percent):
    try:
        sbc.set_brightness(percent)
    except Exception as e:
        print(f"[brightness] failed: {e}")

# ---------------------------------------------------------------------------
# Warmth control (gamma ramp, driven by a real Kelvin->RGB approximation)
# ---------------------------------------------------------------------------

gdi32 = ctypes.windll.gdi32
user32 = ctypes.windll.user32

WORD = ctypes.c_ushort
GammaRampArray = WORD * 256


class GammaRamp(ctypes.Structure):
    _fields_ = [("Red", GammaRampArray), ("Green", GammaRampArray), ("Blue", GammaRampArray)]


def _clamp(v, lo=0.0, hi=255.0):
    return max(lo, min(hi, v))


def kelvin_to_rgb(kelvin):
    """Classic Tanner Helland blackbody approximation -> RGB (0-255 each)."""
    temp = kelvin / 100.0

    # Red
    if temp <= 66:
        red = 255.0
    else:
        red = 329.698727446 * ((temp - 60) ** -0.1332047592)

    # Green
    if temp <= 66:
        green = 99.4708025861 * math.log(temp) - 161.1195681661
    else:
        green = 288.1221695283 * ((temp - 60) ** -0.0755148492)

    # Blue
    if temp >= 66:
        blue = 255.0
    elif temp <= 19:
        blue = 0.0
    else:
        blue = 138.5177312231 * math.log(temp - 10) - 305.0447927307

    return _clamp(red), _clamp(green), _clamp(blue)


# Reference white point at our "neutral" temperature, so that factor == 1.0
# there and no tint is applied when warmth is at its lowest/neutral setting.
_REF_R, _REF_G, _REF_B = kelvin_to_rgb(NEUTRAL_K)


def _build_ramp(kelvin):
    r, g, b = kelvin_to_rgb(kelvin)
    red_factor = r / _REF_R
    green_factor = g / _REF_G
    blue_factor = b / _REF_B

    ramp = GammaRamp()
    for i in range(256):
        base = i * 257  # identity ramp spans 0..65535 over 256 steps
        ramp.Red[i] = int(_clamp(base * red_factor, 0, 65535))
        ramp.Green[i] = int(_clamp(base * green_factor, 0, 65535))
        ramp.Blue[i] = int(_clamp(base * blue_factor, 0, 65535))
    return ramp


def apply_warmth(kelvin):
    hdc = user32.GetDC(0)
    try:
        gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(_build_ramp(kelvin)))
    finally:
        user32.ReleaseDC(0, hdc)


def reset_warmth():
    hdc = user32.GetDC(0)
    try:
        gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(_build_ramp(NEUTRAL_K)))
    finally:
        user32.ReleaseDC(0, hdc)

# ---------------------------------------------------------------------------
# Windows startup registry
# ---------------------------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _startup_command():
    if getattr(sys, "frozen", False):
        # Running as a bundled DuskRay.exe - Task Manager will show its own
        # embedded name ("DuskRay") instead of a generic interpreter name.
        return f'"{sys.executable}"'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    script = os.path.abspath(__file__)
    return f'"{pythonw}" "{script}"'


def set_startup(enabled):
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _startup_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        print(f"[startup] failed: {e}")

# ---------------------------------------------------------------------------
# Apply current state
# ---------------------------------------------------------------------------

NEUTRAL_BRIGHTNESS = 100  # brightness restored when DuskRay is disabled


def apply_all():
    with config_lock:
        cfg = config.copy()
    if cfg["enabled"]:
        apply_brightness(cfg["brightness"])
        apply_warmth(cfg["warmth_k"])
    else:
        apply_brightness(NEUTRAL_BRIGHTNESS)
        reset_warmth()

# ---------------------------------------------------------------------------
# Tray icon image
# ---------------------------------------------------------------------------

def make_icon_image(enabled):
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = (255, 150, 40, 255) if enabled else (130, 130, 130, 255)
    d.ellipse((10, 10, size - 10, size - 10), fill=color)
    if enabled:
        for angle in range(0, 360, 45):
            rad = math.radians(angle)
            x1 = size / 2 + math.cos(rad) * 24
            y1 = size / 2 + math.sin(rad) * 24
            x2 = size / 2 + math.cos(rad) * 30
            y2 = size / 2 + math.sin(rad) * 30
            d.line((x1, y1, x2, y2), fill=color, width=3)
    return img

# ---------------------------------------------------------------------------
# Menu callbacks
# ---------------------------------------------------------------------------

def set_brightness_level(level):
    def action(icon, item):
        with config_lock:
            config["brightness"] = level
            save_config(config)
        apply_all()
    return action


def set_warmth_level(kelvin):
    def action(icon, item):
        with config_lock:
            config["warmth_k"] = kelvin
            save_config(config)
        apply_all()
    return action


def toggle_enabled(icon, item):
    with config_lock:
        config["enabled"] = not config["enabled"]
        save_config(config)
    apply_all()
    icon.icon = make_icon_image(config["enabled"])


def toggle_startup(icon, item):
    with config_lock:
        config["run_at_startup"] = not config["run_at_startup"]
        save_config(config)
    set_startup(config["run_at_startup"])


def quit_app(icon, item):
    reset_warmth()
    icon.stop()

# ---------------------------------------------------------------------------
# Build menu
# ---------------------------------------------------------------------------

def build_menu():
    brightness_items = [
        pystray.MenuItem(
            f"{lvl}%",
            set_brightness_level(lvl),
            checked=lambda item, lvl=lvl: config["brightness"] == lvl,
            radio=True,
        )
        for lvl in BRIGHTNESS_LEVELS
    ]
    warmth_items = [
        pystray.MenuItem(
            f"{k}K",
            set_warmth_level(k),
            checked=lambda item, k=k: config["warmth_k"] == k,
            radio=True,
        )
        for k in WARMTH_LEVELS_K
    ]

    return pystray.Menu(
        # Clicking the tray icon itself toggles enabled/disabled (default action)
        pystray.MenuItem("Enable / Disable DuskRay", toggle_enabled,
                          checked=lambda item: config["enabled"], default=True, visible=False),
        pystray.MenuItem("Brightness", pystray.Menu(*brightness_items)),
        pystray.MenuItem("Warmth", pystray.Menu(*warmth_items)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Enabled", toggle_enabled, checked=lambda item: config["enabled"]),
        pystray.MenuItem("Run at startup", toggle_startup, checked=lambda item: config["run_at_startup"]),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", quit_app),
    )

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if config["run_at_startup"]:
        set_startup(True)  # keep registry entry in sync in case the path moved
    apply_all()
    icon = pystray.Icon(APP_NAME, make_icon_image(config["enabled"]), APP_NAME, build_menu())
    icon.run()


if __name__ == "__main__":
    main()
