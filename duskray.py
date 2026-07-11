"""
DuskRay — Display Brightness & Warmth Control App
A lightweight Windows system-tray utility to control screen brightness and
colour warmth (in Kelvin), with smooth animated transitions, an optional
circadian-rhythm scheduler, multi-monitor awareness, one-click enable/disable,
and the option to run automatically at Windows startup.

INSTALL (once):
pip install pystray Pillow screen_brightness_control

RUN:
pythonw duskray.py      (no console window — recommended)
python  duskray.py      (with console, for debugging)
python  duskray.py --reset   (restore factory defaults and exit)

BUILD standalone DuskRay.exe:
pip install pyinstaller
pyinstaller --onefile --noconsole --clean --strip --name "DuskRay" \
--icon=duskray.ico --version-file=version_info.txt \
--exclude-module tkinter --exclude-module unittest \
--exclude-module pdb --exclude-module doctest --exclude-module test \
duskray.py
"""
from __future__ import annotations

# ── Standard library ────────────────────────────────────────────────────────
import argparse
import atexit
import ctypes
import json
import logging
import math
import os
import signal
import sys
import threading
import time
import winreg
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# ── Third-party ─────────────────────────────────────────────────────────────
from PIL import Image, ImageDraw
import pystray
import screen_brightness_control as sbc

# ── Constants ───────────────────────────────────────────────────────────────
APP_NAME: str = "DuskRay"
APP_VERSION: str = "1.1.0"
CONFIG_DIR: Path = Path(os.getenv("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
CONFIG_PATH: Path = CONFIG_DIR / "config.json"
LOG_PATH: Path = CONFIG_DIR / "duskray.log"

BRIGHTNESS_LEVELS: tuple[int, ...] = tuple(range(10, 101, 10))  # 10 … 100 %
WARMTH_LEVELS_K: tuple[int, ...] = (6500, 6000, 5500, 5000, 4500, 4000, 3500, 3000, 2500, 2000)
HOURS_24: tuple[int, ...] = tuple(range(24))

NEUTRAL_K: int = 6500           # "no warmth" reference (daylight)
NEUTRAL_BRIGHTNESS: int = 100   # restored when DuskRay is disabled
TRANSITION_DURATION: float = 0.6   # seconds for smooth fade
TRANSITION_FPS: int = 30           # frames per second during transition
WIN_RUN_KEY: str = r"Software\Microsoft\Windows\CurrentVersion\Run"

# ── Logging ─────────────────────────────────────────────────────────────────
def _init_logging() -> logging.Logger:
    """Configure file + console logging."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.DEBUG)
    
    if not logger.handlers:
        fmt = logging.Formatter("[%(asctime)s] %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.WARNING)
        ch.setFormatter(fmt)
        logger.addHandler(ch)
        
    return logger

log: logging.Logger = _init_logging()

# ── Windows DPI Awareness ───────────────────────────────────────────────────
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI aware
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ── Configuration ───────────────────────────────────────────────────────────
@dataclass(slots=True)
class AppConfig:
    """Validated, serialisable application configuration."""
    enabled: bool = True
    brightness: int = 100
    warmth_k: int = 4000
    run_at_startup: bool = False
    smooth_transitions: bool = True
    circadian_enabled: bool = False
    circadian_day_k: int = 6500
    circadian_night_k: int = 3000
    circadian_day_hour: int = 7
    circadian_night_hour: int = 21

    def __post_init__(self) -> None:
        self.brightness = _clamp_int(self.brightness, 10, 100)
        self.warmth_k = _nearest(self.warmth_k, WARMTH_LEVELS_K)
        self.circadian_day_k = _nearest(self.circadian_day_k, WARMTH_LEVELS_K)
        self.circadian_night_k = _nearest(self.circadian_night_k, WARMTH_LEVELS_K)
        self.circadian_day_hour = _clamp_int(self.circadian_day_hour, 0, 23)
        self.circadian_night_hour = _clamp_int(self.circadian_night_hour, 0, 23)

    @classmethod
    def load(cls) -> AppConfig:
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("config root must be an object")
            valid_keys = {k for k in raw if k in cls.__dataclass_fields__}
            cfg = cls(**{k: raw[k] for k in valid_keys})
            log.info("Configuration loaded from %s", CONFIG_PATH)
            return cfg
        except Exception as exc:
            log.warning("Could not load config (%s); using defaults.", exc)
            return cls()

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(CONFIG_PATH)            # atomic on NTFS
        log.debug("Configuration saved.")

    def reset(self) -> None:
        """Restore factory defaults."""
        self.__init__()  # type: ignore[misc]
        self.save()
        log.info("Configuration reset to defaults.")

# ── Helpers ─────────────────────────────────────────────────────────────────
def _clamp(v: float, lo: float = 0.0, hi: float = 255.0) -> float:
    return max(lo, min(hi, v))

def _clamp_int(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(v)))

def _nearest(value: int, choices: Sequence[int]) -> int:
    return min(choices, key=lambda c: abs(c - value))

def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t

# ── Kelvin → RGB (Tanner Helland blackbody approximation) ──────────────────
def kelvin_to_rgb(kelvin: int) -> tuple[float, float, float]:
    """Return (R, G, B) each in 0-255 for a given colour temperature."""
    temp = kelvin / 100.0
    if temp <= 66:
        red = 255.0
        green = 99.4708025861 * math.log(temp) - 161.1195681661
    else:
        red = 329.698727446 * ((temp - 60) ** -0.1332047592)
        green = 288.1221695283 * ((temp - 60) ** -0.0755148492)
        
    if temp >= 66:
        blue = 255.0
    elif temp <= 19:
        blue = 0.0
    else:
        blue = 138.5177312231 * math.log(temp - 10) - 305.0447927307
        
    return _clamp(red), _clamp(green), _clamp(blue)

_REF_R, _REF_G, _REF_B = kelvin_to_rgb(NEUTRAL_K)

# ── Gamma Ramp (ctypes) ────────────────────────────────────────────────────
_WORD = ctypes.c_ushort
_GammaRampArray = _WORD * 256

class _GammaRamp(ctypes.Structure):
    _fields_ = [
        ("Red", _GammaRampArray),
        ("Green", _GammaRampArray),
        ("Blue", _GammaRampArray),
    ]

class _RampCache:
    """Pre-compute and memoise gamma ramps for every warmth level."""
    __slots__ = ("_cache",)

    def __init__(self) -> None:
        self._cache: dict[int, _GammaRamp] = {}

    def get(self, kelvin: int) -> _GammaRamp:
        ramp = self._cache.get(kelvin)
        if ramp is None:
            ramp = self._build(kelvin)
            self._cache[kelvin] = ramp
        return ramp

    @staticmethod
    def _build(kelvin: int) -> _GammaRamp:
        r, g, b = kelvin_to_rgb(kelvin)
        rf = r / _REF_R
        gf = g / _REF_G
        bf = b / _REF_B
        ramp = _GammaRamp()
        for i in range(256):
            base = i * 257  # identity: 0 → 0, 255 → 65535
            ramp.Red[i] = int(_clamp(base * rf, 0, 65535))
            ramp.Green[i] = int(_clamp(base * gf, 0, 65535))
            ramp.Blue[i] = int(_clamp(base * bf, 0, 65535))
        return ramp

_ramp_cache = _RampCache()
# Pre-warm the cache at import time for instant first apply
for _k in (*WARMTH_LEVELS_K, NEUTRAL_K):
    _ramp_cache.get(_k)

# ── Low-level display control ──────────────────────────────────────────────
_gdi32 = ctypes.windll.gdi32
_user32 = ctypes.windll.user32

class _DisplayDC:
    """Caches the Windows Display Device Context to prevent GDI leaks during animations."""
    __slots__ = ("_hdc",)
    
    def __init__(self) -> None:
        self._hdc = _user32.GetDC(0)
        if not self._hdc:
            log.error("GetDC(0) returned NULL")
            
    def get(self) -> int:
        return self._hdc
        
    def release(self) -> None:
        if self._hdc:
            _user32.ReleaseDC(0, self._hdc)
            self._hdc = 0

_display_dc = _DisplayDC()
atexit.register(_display_dc.release)

def _apply_gamma_ramp(ramp: _GammaRamp) -> bool:
    """Set the gamma ramp on all display DCs. Returns True on success."""
    hdc = _display_dc.get()
    if not hdc:
        return False
    ok = _gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(ramp))
    if not ok:
        log.warning("SetDeviceGammaRamp failed (may require elevation)")
    return bool(ok)

def apply_warmth(kelvin: int) -> None:
    _apply_gamma_ramp(_ramp_cache.get(kelvin))

def reset_warmth() -> None:
    _apply_gamma_ramp(_ramp_cache.get(NEUTRAL_K))

def apply_brightness(percent: int) -> None:
    try:
        sbc.set_brightness(percent)
    except Exception as exc:
        log.error("Brightness control failed: %s", exc)

def get_current_brightness() -> int:
    try:
        vals = sbc.get_brightness()
        return int(vals[0]) if vals else NEUTRAL_BRIGHTNESS
    except Exception:
        return NEUTRAL_BRIGHTNESS

# ── Smooth transition engine ───────────────────────────────────────────────
class _TransitionEngine:
    """Animate brightness and/or warmth from current → target values."""
    __slots__ = ("_lock", "_thread", "_cancel")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    def animate(self, fb: int, tb: int, fk: int, tk: int, duration: float = TRANSITION_DURATION, fps: int = TRANSITION_FPS) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                self._cancel.set()
                self._thread.join(timeout=1.0)
            self._cancel.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(fb, tb, fk, tk, duration, fps),
                daemon=True,
                name="DuskRay-Transition",
            )
            self._thread.start()

    def _run(self, fb: int, tb: int, fk: int, tk: int, duration: float, fps: int) -> None:
        steps = max(1, int(duration * fps))
        interval = duration / steps
        start_time = time.perf_counter()
        
        for step in range(1, steps + 1):
            if self._cancel.is_set():
                return
            
            # Precision timing to guarantee smooth FPS regardless of OS lag
            target_time = start_time + (step * interval)
            t = step / steps
            
            # Ease-in-out quad
            t = 2 * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 2 / 2
            
            cur_b = int(round(_lerp(fb, tb, t)))
            cur_k = int(round(_lerp(fk, tk, t)))
            
            apply_brightness(cur_b)
            apply_warmth(_nearest(cur_k, WARMTH_LEVELS_K))
            
            sleep_time = target_time - time.perf_counter()
            if sleep_time > 0:
                time.sleep(sleep_time)
                
        # Final snap to exact targets
        apply_brightness(tb)
        apply_warmth(tk)

_transition = _TransitionEngine()

# ── Circadian scheduler ────────────────────────────────────────────────────
class CircadianScheduler:
    """Background thread that smoothly shifts warmth based on time of day."""
    __slots__ = ("_cfg", "_on_change", "_stop", "_thread")

    def __init__(self, config: AppConfig, on_warmth_change: Callable[[int], None]) -> None:
        self._cfg = config
        self._on_change = on_warmth_change
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="DuskRay-Circadian")
        self._thread.start()
        log.info("Circadian scheduler started.")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        log.info("Circadian scheduler stopped.")

    def target_kelvin_now(self) -> int:
        """Compute the ideal warmth for the current time."""
        hour = time.localtime().tm_hour
        day_h = self._cfg.circadian_day_hour
        night_h = self._cfg.circadian_night_hour
        day_k = self._cfg.circadian_day_k
        night_k = self._cfg.circadian_night_k
        
        if day_h < night_h:
            return day_k if day_h <= hour < night_h else night_k
        else:
            return night_k if night_h <= hour < day_h else day_k

    def _loop(self) -> None:
        while not self._stop.is_set():
            target = self.target_kelvin_now()
            self._on_change(target)
            self._stop.wait(60)

# ── Windows startup registry ───────────────────────────────────────────────
def _startup_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = Path(sys.executable).parent / "pythonw.exe"
    interpreter = str(pythonw) if pythonw.exists() else sys.executable
    script = os.path.abspath(__file__)
    return f'"{interpreter}" "{script}"'

def set_startup(enabled: bool) -> None:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE)
        try:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _startup_command())
                log.info("Startup entry created.")
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                    log.info("Startup entry removed.")
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(key)
    except Exception as exc:
        log.error("Failed to modify startup registry: %s", exc)

# ── Tray icon image generator ──────────────────────────────────────────────
_ICON_CACHE: dict[bool, Image.Image] = {}

def _generate_icon_image(enabled: bool, size: int = 64) -> Image.Image:
    """Render an anti-aliased sun (enabled) or moon (disabled) icon."""
    scale = 2  
    big = size * scale
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, cy = big // 2, big // 2
    
    if enabled:
        body_color = (255, 160, 40, 255)
        ray_color = (255, 190, 60, 255)
        r_body = int(big * 0.28)
        d.ellipse((cx - r_body, cy - r_body, cx + r_body, cy + r_body), fill=body_color)
        
        ray_inner = int(big * 0.34)
        ray_outer = int(big * 0.46)
        ray_width = max(2, scale * 2)
        
        for angle in range(0, 360, 45):
            rad = math.radians(angle)
            cos_a = math.cos(rad)
            sin_a = math.sin(rad)
            x1 = cx + cos_a * ray_inner
            y1 = cy + sin_a * ray_inner
            x2 = cx + cos_a * ray_outer
            y2 = cy + sin_a * ray_outer
            d.line((x1, y1, x2, y2), fill=ray_color, width=ray_width)
    else:
        body_color = (160, 160, 170, 255)
        r_outer = int(big * 0.34)
        r_cut = int(big * 0.28)
        offset = int(big * 0.12)
        d.ellipse((cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer), fill=body_color)
        d.ellipse((cx - r_cut + offset, cy - r_cut - offset, cx + r_cut + offset, cy + r_cut - offset), fill=(0, 0, 0, 0))
        
    return img.resize((size, size), Image.LANCZOS)

def get_icon_image(enabled: bool) -> Image.Image:
    if enabled not in _ICON_CACHE:
        _ICON_CACHE[enabled] = _generate_icon_image(enabled)
    return _ICON_CACHE[enabled]

# ── Main application class ─────────────────────────────────────────────────
class DuskRayApp:
    """Orchestrates config, display control, transitions, and the tray icon."""
    __slots__ = ("config", "_lock", "_icon", "_circadian", "_menu_cache")

    def __init__(self) -> None:
        self.config = AppConfig.load()
        self._lock = threading.Lock()
        self._icon: pystray.Icon | None = None
        self._circadian: CircadianScheduler | None = None
        self._menu_cache: pystray.Menu | None = None

    def apply_all(self, animate: bool = True) -> None:
        """Apply current config state to the display."""
        with self._lock:
            cfg = self.config
        target_b = cfg.brightness if cfg.enabled else NEUTRAL_BRIGHTNESS
        target_k = cfg.warmth_k if cfg.enabled else NEUTRAL_K
        
        if cfg.smooth_transitions and animate:
            cur_b = get_current_brightness()
            _transition.animate(cur_b, target_b, NEUTRAL_K, target_k)
        else:
            apply_brightness(target_b)
            if cfg.enabled:
                apply_warmth(target_k)
            else:
                reset_warmth()

    def _update_config(self, **kwargs: Any) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self.config, k, v)
            self.config.save()

    def set_brightness(self, level: int) -> None:
        old = self.config.brightness
        self._update_config(brightness=level)
        if self.config.enabled and self.config.smooth_transitions:
            _transition.animate(old, level, self.config.warmth_k, self.config.warmth_k, duration=0.3)
        else:
            self.apply_all(animate=False)

    def set_warmth(self, kelvin: int) -> None:
        old = self.config.warmth_k
        self._update_config(warmth_k=kelvin)
        if self.config.enabled and self.config.smooth_transitions:
            _transition.animate(self.config.brightness, self.config.brightness, old, kelvin, duration=0.4)
        else:
            self.apply_all(animate=False)

    def toggle_enabled(self) -> None:
        new_state = not self.config.enabled
        self._update_config(enabled=new_state)
        self.apply_all()
        self._refresh_icon()

    def toggle_startup(self) -> None:
        new_state = not self.config.run_at_startup
        self._update_config(run_at_startup=new_state)
        set_startup(new_state)

    def toggle_smooth(self) -> None:
        self._update_config(smooth_transitions=not self.config.smooth_transitions)

    def toggle_circadian(self) -> None:
        new_state = not self.config.circadian_enabled
        self._update_config(circadian_enabled=new_state)
        if new_state:
            self._start_circadian()
        else:
            self._stop_circadian()

    def _start_circadian(self) -> None:
        if self._circadian:
            self._circadian.stop()
        def _on_warmth(kelvin: int) -> None:
            if self.config.enabled and self.config.circadian_enabled:
                self.set_warmth(_nearest(kelvin, WARMTH_LEVELS_K))
        self._circadian = CircadianScheduler(self.config, _on_warmth)
        self._circadian.start()

    def _stop_circadian(self) -> None:
        if self._circadian:
            self._circadian.stop()
            self._circadian = None

    def _refresh_icon(self) -> None:
        if self._icon:
            self._icon.icon = get_icon_image(self.config.enabled)

    def _cb_brightness(self, level: int) -> Callable:
        def _action(icon: pystray.Icon, item: pystray.MenuItem) -> None:
            self.set_brightness(level)
        return _action

    def _cb_warmth(self, kelvin: int) -> Callable:
        def _action(icon: pystray.Icon, item: pystray.MenuItem) -> None:
            self.set_warmth(kelvin)
        return _action

    def _cb_circadian_day_hour(self, hour: int) -> Callable:
        def _action(icon: pystray.Icon, item: pystray.MenuItem) -> None:
            self._update_config(circadian_day_hour=hour)
        return _action

    def _cb_circadian_night_hour(self, hour: int) -> Callable:
        def _action(icon: pystray.Icon, item: pystray.MenuItem) -> None:
            self._update_config(circadian_night_hour=hour)
        return _action

    def _cb_circadian_day_k(self, kelvin: int) -> Callable:
        def _action(icon: pystray.Icon, item: pystray.MenuItem) -> None:
            self._update_config(circadian_day_k=kelvin)
        return _action

    def _cb_circadian_night_k(self, kelvin: int) -> Callable:
        def _action(icon: pystray.Icon, item: pystray.MenuItem) -> None:
            self._update_config(circadian_night_k=kelvin)
        return _action

    def _cb_toggle_enabled(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self.toggle_enabled()

    def _cb_toggle_startup(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self.toggle_startup()

    def _cb_toggle_smooth(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self.toggle_smooth()

    def _cb_toggle_circadian(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self.toggle_circadian()

    def _cb_quit(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        log.info("Shutting down…")
        self._stop_circadian()
        reset_warmth()
        icon.stop()

    def _build_menu(self) -> pystray.Menu:
        if self._menu_cache:
            return self._menu_cache

        brightness_items = tuple(
            pystray.MenuItem(f"{lvl}%", self._cb_brightness(lvl), 
                             checked=lambda item, lvl=lvl: self.config.brightness == lvl, radio=True)
            for lvl in BRIGHTNESS_LEVELS
        )
        warmth_items = tuple(
            pystray.MenuItem(f"{k}K", self._cb_warmth(k), 
                             checked=lambda item, k=k: self.config.warmth_k == k, radio=True)
            for k in WARMTH_LEVELS_K
        )
        day_hour_items = tuple(
            pystray.MenuItem(f"{h:02d}:00", self._cb_circadian_day_hour(h), 
                             checked=lambda item, h=h: self.config.circadian_day_hour == h, radio=True)
            for h in HOURS_24
        )
        night_hour_items = tuple(
            pystray.MenuItem(f"{h:02d}:00", self._cb_circadian_night_hour(h), 
                             checked=lambda item, h=h: self.config.circadian_night_hour == h, radio=True)
            for h in HOURS_24
        )
        circ_day_k_items = tuple(
            pystray.MenuItem(f"{k}K", self._cb_circadian_day_k(k), 
                             checked=lambda item, k=k: self.config.circadian_day_k == k, radio=True)
            for k in WARMTH_LEVELS_K
        )
        circ_night_k_items = tuple(
            pystray.MenuItem(f"{k}K", self._cb_circadian_night_k(k), 
                             checked=lambda item, k=k: self.config.circadian_night_k == k, radio=True)
            for k in WARMTH_LEVELS_K
        )

        circadian_schedule_menu = pystray.Menu(
            pystray.MenuItem("Day Start", pystray.Menu(*day_hour_items)),
            pystray.MenuItem("Night Start", pystray.Menu(*night_hour_items)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Day Warmth", pystray.Menu(*circ_day_k_items)),
            pystray.MenuItem("Night Warmth", pystray.Menu(*circ_night_k_items)),
        )

        self._menu_cache = pystray.Menu(
            pystray.MenuItem("Enable / Disable DuskRay", self._cb_toggle_enabled, 
                             checked=lambda item: self.config.enabled, default=True, visible=False),
            pystray.MenuItem("Brightness", pystray.Menu(*brightness_items)),
            pystray.MenuItem("Warmth", pystray.Menu(*warmth_items)),
            pystray.MenuItem("Circadian Schedule", circadian_schedule_menu),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Enabled", self._cb_toggle_enabled, checked=lambda item: self.config.enabled),
            pystray.MenuItem("Smooth transitions", self._cb_toggle_smooth, checked=lambda item: self.config.smooth_transitions),
            pystray.MenuItem("Circadian mode", self._cb_toggle_circadian, checked=lambda item: self.config.circadian_enabled),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Run at startup", self._cb_toggle_startup, checked=lambda item: self.config.run_at_startup),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self._cb_quit),
        )
        return self._menu_cache

    def run(self) -> None:
        log.info("%s v%s starting…", APP_NAME, APP_VERSION)
        if self.config.run_at_startup:
            set_startup(True)
        if self.config.circadian_enabled:
            self._start_circadian()
            
        self.apply_all(animate=False)
        
        self._icon = pystray.Icon(
            APP_NAME,
            get_icon_image(self.config.enabled),
            f"{APP_NAME} v{APP_VERSION}",
            self._build_menu(),
        )
        
        atexit.register(self._on_exit)
        
        def _signal_handler(signum: int, frame: Any) -> None:
            log.info("Received signal %d, exiting…", signum)
            if self._icon:
                self._icon.stop()
                
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
        
        log.info("Tray icon active.")
        self._icon.run()

    def _on_exit(self) -> None:
        self._stop_circadian()
        reset_warmth()
        log.info("Cleanup complete. Goodbye.")

# ── CLI entry point ─────────────────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Display Brightness & Warmth Control",
    )
    parser.add_argument("--reset", action="store_true", help="Restore factory defaults and exit.")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    return parser.parse_args()

def main() -> None:
    args = _parse_args()
    if args.reset:
        cfg = AppConfig()
        cfg.save()
        reset_warmth()
        apply_brightness(NEUTRAL_BRIGHTNESS)
        print(f"{APP_NAME}: configuration reset to defaults.")
        return
        
    app = DuskRayApp()
    app.run()

if __name__ == "__main__":
    main()
