"""
DuskRay — Display Brightness & Warmth Control
=============================================

A lightweight Windows system-tray utility that controls screen brightness and
colour warmth (in Kelvin) with smooth animated transitions, an optional
circadian schedule with gradual fades, multi-monitor gamma support, sub-zero
overlay dimming (-10/-20/-30 %), safe-startup brightness, and an optional
"run at Windows startup" entry.

Warmth and brightness can be controlled independently: "Warmth enabled" turns
the colour shift off while keeping your brightness level, and the master
"Enabled" switch turns everything off.

INSTALL (once):
    pip install -r requirements.txt

RUN:
    pythonw duskray.py              (no console window — recommended)
    python  duskray.py --debug      (console + verbose log, for debugging)
    python  duskray.py --reset      (restore factory defaults and exit)
    python  duskray.py --startup    (launched by Windows at startup — safe brightness)

Architecture (top to bottom):
    pure helpers / config / colour maths    — platform independent, unit tested
    Win32 bindings                          — only defined on Windows
    GammaController                         — per-monitor gamma ramps (warmth)
    HardwareBrightness                      — background, latest-wins DDC/WMI worker
    DimOverlay                              — click-through veil + system events
    DisplayOutput                           — maps (brightness, kelvin) to devices
    DisplayEngine                           — one persistent animation thread
    CircadianScheduler                      — time-of-day warmth
    DuskRayApp                              — config, tray menu, lifecycle
"""
from __future__ import annotations

# ── Standard library ────────────────────────────────────────────────────────
import argparse
import atexit
import ctypes
import functools
import json
import logging
import logging.handlers
import math
import os
import signal
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

IS_WINDOWS: bool = sys.platform == "win32"

# ── Constants ───────────────────────────────────────────────────────────────
APP_NAME: str = "DuskRay"
APP_VERSION: str = "1.3.0"

CONFIG_DIR: Path = Path(
    os.environ.get("DUSKRAY_CONFIG_DIR")
    or Path(os.getenv("APPDATA") or (Path.home() / "AppData" / "Roaming")) / APP_NAME
)
CONFIG_PATH: Path = CONFIG_DIR / "config.json"
LOG_PATH: Path = CONFIG_DIR / "duskray.log"

# Negative brightness = hardware at 0 % plus a click-through black overlay.
BRIGHTNESS_LEVELS: tuple = (-30, -20, -10, 0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100)
WARMTH_LEVELS_K: tuple = (6500, 6000, 5500, 5000, 4500, 4000, 3500, 3000, 2500, 2000)
FADE_MINUTES_CHOICES: tuple = (0, 15, 30, 60)
HOURS_24: tuple = tuple(range(24))

CLICK_TOGGLE_ALL: str = "toggle_all"
CLICK_TOGGLE_WARMTH: str = "toggle_warmth"
CLICK_ACTIONS: tuple = (CLICK_TOGGLE_ALL, CLICK_TOGGLE_WARMTH)

NEUTRAL_K: int = 6500             # "no warmth" reference (daylight)
MIN_K: int = 2000
MAX_K: int = NEUTRAL_K
NEUTRAL_BRIGHTNESS: int = 100     # restored when DuskRay is disabled / exits
MIN_BRIGHTNESS: int = -30
MAX_BRIGHTNESS: int = 100
SAFE_STARTUP_BRIGHTNESS: int = 10

TRANSITION_TOGGLE_S: float = 0.6
TRANSITION_BRIGHTNESS_S: float = 0.3
TRANSITION_WARMTH_S: float = 0.4
TRANSITION_CIRCADIAN_S: float = 1.5
TRANSITION_FPS: int = 30

GAMMA_REFRESH_S: float = 10.0     # re-assert warm gamma (other apps / driver resets)
CIRCADIAN_TICK_S: float = 30.0
KELVIN_QUANTUM: int = 10          # ramps are cached per 10 K step
RAMP_CACHE_SIZE: int = 64         # ~1.5 KB each → <100 KB worst case
GAMMA_FLOOR_STEP_K: int = 250     # step used when Windows rejects a very warm ramp
HW_DETECT_DELAYS_S: tuple = (0.0, 2.0, 5.0, 10.0)   # WMI is often slow right after boot
HW_MAX_CONSECUTIVE_FAILURES: int = 3

WIN_RUN_KEY: str = r"Software\Microsoft\Windows\CurrentVersion\Run"
MUTEX_NAME: str = f"Local\\{APP_NAME}-Singleton"
MAX_OVERLAY_ALPHA_PCT: int = 95   # never fully black out the screen
OVERLAY_EDGE_INSET_PX: int = 2    # keep a sliver of every edge uncovered so an
                                  # auto-hide taskbar on any edge still reveals
                                  # on hover (a TOPMOST window that exactly
                                  # covers a monitor is treated as fullscreen).

log: logging.Logger = logging.getLogger(APP_NAME)
log.addHandler(logging.NullHandler())


# ════════════════════════════════════════════════════════════════════════════
#  Logging
# ════════════════════════════════════════════════════════════════════════════
def init_logging(debug: bool = False) -> None:
    """Rotating file log (bounded size) plus console output when one exists."""
    if getattr(init_logging, "_done", False):
        return
    init_logging._done = True  # type: ignore[attr-defined]

    log.setLevel(logging.DEBUG if debug else logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)-7s %(threadName)s: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=512 * 1024, backupCount=1, encoding="utf-8", delay=True)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        pass  # read-only profile etc. — keep running without a file log

    # pythonw.exe has no console: sys.stdout/sys.stderr are None there.
    if sys.stderr is not None:
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(logging.DEBUG if debug else logging.WARNING)
        ch.setFormatter(fmt)
        log.addHandler(ch)

    def _thread_hook(args: Any) -> None:
        log.error("Unhandled exception in thread %s",
                  getattr(args.thread, "name", "?"),
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = _thread_hook


# ════════════════════════════════════════════════════════════════════════════
#  Pure helpers
# ════════════════════════════════════════════════════════════════════════════
def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def clamp_int(v: Any, lo: int, hi: int) -> int:
    return int(clamp(int(round(float(v))), lo, hi))


def nearest(value: float, choices: Sequence[int]) -> int:
    return min(choices, key=lambda c: abs(c - value))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def ease_in_out(t: float) -> float:
    """Quadratic ease-in-out on [0, 1]."""
    return 2 * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 2 / 2


def quantize_kelvin(k: float) -> int:
    q = int(round(k / KELVIN_QUANTUM)) * KELVIN_QUANTUM
    return int(clamp(q, MIN_K, MAX_K))


# ════════════════════════════════════════════════════════════════════════════
#  Configuration
# ════════════════════════════════════════════════════════════════════════════
def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str) and v.strip().lower() in ("true", "false", "1", "0", "yes", "no", "on", "off"):
        return v.strip().lower() in ("true", "1", "yes", "on")
    raise ValueError(f"not a boolean: {v!r}")


def _as_int(v: Any) -> int:
    if isinstance(v, bool):
        raise ValueError(f"not a number: {v!r}")
    f = float(v)
    if not math.isfinite(f):
        raise ValueError(f"not finite: {v!r}")
    return int(round(f))


@dataclass
class AppConfig:
    """Validated, serialisable application configuration."""
    enabled: bool = True                 # master switch (brightness + warmth)
    warmth_enabled: bool = True          # warmth on/off, independent of brightness
    brightness: int = 100                # virtual brightness (-30 … 100)
    warmth_k: int = 4000                 # fixed warmth used when circadian is off
    run_at_startup: bool = False
    smooth_transitions: bool = True
    circadian_enabled: bool = False
    circadian_day_k: int = 6500
    circadian_night_k: int = 3000
    circadian_day_hour: int = 7
    circadian_night_hour: int = 21
    circadian_fade_minutes: int = 30     # gradual shift after each boundary
    click_action: str = CLICK_TOGGLE_ALL # what a left-click on the tray icon does

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Coerce every field into its legal range; invalid values → default."""
        for f in fields(self):
            try:
                setattr(self, f.name, self._coerce(f.name, getattr(self, f.name)))
            except (TypeError, ValueError, OverflowError) as exc:
                log.warning("Config '%s' invalid (%s); using default.", f.name, exc)
                setattr(self, f.name, f.default)

    @staticmethod
    def _coerce(name: str, v: Any) -> Any:
        if name in ("enabled", "warmth_enabled", "run_at_startup",
                    "smooth_transitions", "circadian_enabled"):
            return _as_bool(v)
        if name == "brightness":
            return int(clamp(_as_int(v), MIN_BRIGHTNESS, MAX_BRIGHTNESS))
        if name in ("warmth_k", "circadian_day_k", "circadian_night_k"):
            return int(clamp(_as_int(v), MIN_K, MAX_K))
        if name in ("circadian_day_hour", "circadian_night_hour"):
            return int(clamp(_as_int(v), 0, 23))
        if name == "circadian_fade_minutes":
            return int(clamp(_as_int(v), 0, 180))
        if name == "click_action":
            if v not in CLICK_ACTIONS:
                raise ValueError(f"unknown click action {v!r}")
            return v
        return v

    # ── persistence ─────────────────────────────────────────────────────────
    @classmethod
    def from_dict(cls, raw: dict) -> "AppConfig":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "AppConfig":
        path = path or CONFIG_PATH
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            log.info("No config yet; using defaults.")
            return cls()
        except OSError as exc:
            log.warning("Could not read config (%s); using defaults.", exc)
            return cls()
        try:
            raw = json.loads(text)
            if not isinstance(raw, dict):
                raise ValueError("config root must be an object")
        except ValueError as exc:
            log.warning("Config is corrupt (%s); backing it up and using defaults.", exc)
            try:
                os.replace(path, path.with_suffix(".corrupt.json"))
            except OSError:
                pass
            return cls()
        cfg = cls.from_dict(raw)
        log.info("Configuration loaded from %s", path)
        return cfg

    def save(self, path: Optional[Path] = None) -> bool:
        """Atomic write (temp file + replace). Never raises."""
        path = path or CONFIG_PATH
        tmp = path.with_suffix(".tmp")
        data = json.dumps(asdict(self), indent=2)
        for attempt in range(3):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(data, encoding="utf-8")
                os.replace(tmp, path)  # may fail briefly if AV/indexer holds the file
                return True
            except OSError as exc:
                if attempt == 2:
                    log.error("Could not save config: %s", exc)
                else:
                    time.sleep(0.05)
        return False

    def copy(self) -> "AppConfig":
        return AppConfig(**asdict(self))


# ════════════════════════════════════════════════════════════════════════════
#  Colour maths
# ════════════════════════════════════════════════════════════════════════════
def kelvin_to_rgb(kelvin: float) -> tuple:
    """Tanner Helland black-body approximation → (r, g, b) in 0…255."""
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
    return clamp(red, 0, 255), clamp(green, 0, 255), clamp(blue, 0, 255)


_REF_RGB: tuple = kelvin_to_rgb(NEUTRAL_K)


def kelvin_factors(kelvin: float) -> tuple:
    """Per-channel multipliers relative to neutral (6500 K → 1, 1, 1)."""
    r, g, b = kelvin_to_rgb(kelvin)
    return (min(1.0, r / _REF_RGB[0]), min(1.0, g / _REF_RGB[1]), min(1.0, b / _REF_RGB[2]))


def build_ramp_channels(kelvin: float) -> tuple:
    """Three 256-entry lists of 16-bit gamma values for the given temperature."""
    out = []
    for f in kelvin_factors(kelvin):
        scale = 257.0 * f  # identity ramp is i * 257 (0 → 0, 255 → 65535)
        out.append([min(65535, int(i * scale)) for i in range(256)])
    return tuple(out)


_WORD_ARRAY = ctypes.c_ushort * 256


class GammaRamp(ctypes.Structure):
    _fields_ = [("Red", _WORD_ARRAY), ("Green", _WORD_ARRAY), ("Blue", _WORD_ARRAY)]


class RampCache:
    """Bounded LRU of ready-to-use ctypes gamma ramps, keyed by quantised K."""
    __slots__ = ("_cache", "_size", "_lock")

    def __init__(self, size: int = RAMP_CACHE_SIZE) -> None:
        self._cache: "OrderedDict[int, GammaRamp]" = OrderedDict()
        self._size = size
        self._lock = threading.Lock()

    def get(self, kelvin: float) -> GammaRamp:
        key = quantize_kelvin(kelvin)
        with self._lock:
            ramp = self._cache.get(key)
            if ramp is not None:
                self._cache.move_to_end(key)
                return ramp
        ramp = GammaRamp()
        r, g, b = build_ramp_channels(key)
        ramp.Red[:], ramp.Green[:], ramp.Blue[:] = r, g, b
        with self._lock:
            self._cache[key] = ramp
            while len(self._cache) > self._size:
                self._cache.popitem(last=False)
        return ramp

    def __len__(self) -> int:
        return len(self._cache)


# ════════════════════════════════════════════════════════════════════════════
#  Circadian maths & target computation
# ════════════════════════════════════════════════════════════════════════════
def circadian_kelvin(day_hour: int, night_hour: int, day_k: int, night_k: int,
                     fade_minutes: int, minute_of_day: float) -> int:
    """Warmth for a time of day. After each boundary the temperature fades
    linearly over *fade_minutes* (clipped to the length of the period)."""
    if day_hour == night_hour:
        return day_k
    day_start, night_start = day_hour * 60, night_hour * 60
    since_day = (minute_of_day - day_start) % 1440
    since_night = (minute_of_day - night_start) % 1440
    if since_day < since_night:             # most recent boundary was sunrise
        frm, to, elapsed = night_k, day_k, since_day
        period = (night_start - day_start) % 1440
    else:
        frm, to, elapsed = day_k, night_k, since_night
        period = (day_start - night_start) % 1440
    fade = min(fade_minutes, period)
    if fade > 0 and elapsed < fade:
        return int(round(lerp(frm, to, elapsed / fade)))
    return to


def minute_of_day_now() -> float:
    t = time.localtime()
    return t.tm_hour * 60 + t.tm_min + t.tm_sec / 60.0


def effective_kelvin(cfg: AppConfig, minute_of_day: float) -> int:
    """Warmth the user asked for, ignoring the on/off switches."""
    if cfg.circadian_enabled:
        return circadian_kelvin(cfg.circadian_day_hour, cfg.circadian_night_hour,
                                cfg.circadian_day_k, cfg.circadian_night_k,
                                cfg.circadian_fade_minutes, minute_of_day)
    return cfg.warmth_k


def compute_targets(cfg: AppConfig, minute_of_day: float,
                    brightness_override: Optional[int] = None) -> tuple:
    """(virtual brightness, kelvin) that should be on screen right now."""
    if not cfg.enabled:
        return NEUTRAL_BRIGHTNESS, NEUTRAL_K
    b = cfg.brightness if brightness_override is None else brightness_override
    k = effective_kelvin(cfg, minute_of_day) if cfg.warmth_enabled else NEUTRAL_K
    return b, k


# ════════════════════════════════════════════════════════════════════════════
#  Win32 bindings (Windows only)
# ════════════════════════════════════════════════════════════════════════════
if IS_WINDOWS:
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _wtsapi32: Optional[ctypes.WinDLL]
    try:
        _wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
    except OSError:  # pragma: no cover
        _wtsapi32 = None

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)

    class _WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
        ]

    class _DISPLAY_DEVICEW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
            ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
            ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128),
        ]

    def _proto(fn: Any, restype: Any, *argtypes: Any) -> None:
        fn.restype = restype
        fn.argtypes = list(argtypes)

    H, U, I = wintypes.HANDLE, wintypes.UINT, ctypes.c_int
    _proto(_user32.DefWindowProcW, LRESULT, wintypes.HWND, U, wintypes.WPARAM, wintypes.LPARAM)
    _proto(_user32.RegisterClassW, wintypes.ATOM, ctypes.POINTER(_WNDCLASSW))
    _proto(_user32.UnregisterClassW, wintypes.BOOL, wintypes.LPCWSTR, wintypes.HINSTANCE)
    _proto(_user32.CreateWindowExW, wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR,
           wintypes.LPCWSTR, wintypes.DWORD, I, I, I, I, wintypes.HWND, wintypes.HMENU,
           wintypes.HINSTANCE, wintypes.LPVOID)
    _proto(_user32.DestroyWindow, wintypes.BOOL, wintypes.HWND)
    _proto(_user32.SetLayeredWindowAttributes, wintypes.BOOL, wintypes.HWND,
           wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD)
    _proto(_user32.ShowWindowAsync, wintypes.BOOL, wintypes.HWND, I)
    _proto(_user32.SetWindowPos, wintypes.BOOL, wintypes.HWND, wintypes.HWND, I, I, I, I, U)
    _proto(_user32.SetWindowRgn, I, wintypes.HWND, wintypes.HRGN, wintypes.BOOL)
    _proto(_user32.GetMessageW, wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND, U, U)
    _proto(_user32.TranslateMessage, wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
    _proto(_user32.DispatchMessageW, LRESULT, ctypes.POINTER(wintypes.MSG))
    _proto(_user32.PostMessageW, wintypes.BOOL, wintypes.HWND, U, wintypes.WPARAM, wintypes.LPARAM)
    _proto(_user32.PostQuitMessage, None, I)
    _proto(_user32.GetSystemMetrics, I, I)
    _proto(_user32.GetDC, wintypes.HDC, wintypes.HWND)
    _proto(_user32.ReleaseDC, I, wintypes.HWND, wintypes.HDC)
    _proto(_user32.EnumDisplayDevicesW, wintypes.BOOL, wintypes.LPCWSTR, wintypes.DWORD,
           ctypes.POINTER(_DISPLAY_DEVICEW), wintypes.DWORD)
    _proto(_gdi32.CreateDCW, wintypes.HDC, wintypes.LPCWSTR, wintypes.LPCWSTR,
           wintypes.LPCWSTR, wintypes.LPVOID)
    _proto(_gdi32.DeleteDC, wintypes.BOOL, wintypes.HDC)
    _proto(_gdi32.SetDeviceGammaRamp, wintypes.BOOL, wintypes.HDC, wintypes.LPVOID)
    _proto(_gdi32.CreateSolidBrush, wintypes.HBRUSH, wintypes.COLORREF)
    _proto(_gdi32.CreateRectRgn, wintypes.HRGN, I, I, I, I)
    _proto(_gdi32.DeleteObject, wintypes.BOOL, wintypes.HGDIOBJ)
    _proto(_kernel32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)
    _proto(_kernel32.CreateMutexW, H, wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    _proto(_kernel32.CloseHandle, wintypes.BOOL, H)
    if _wtsapi32 is not None:
        _proto(_wtsapi32.WTSRegisterSessionNotification, wintypes.BOOL, wintypes.HWND, wintypes.DWORD)
        _proto(_wtsapi32.WTSUnRegisterSessionNotification, wintypes.BOOL, wintypes.HWND)
    del H, U, I

# Win32 constants (harmless on other platforms)
WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x00080000, 0x00000020
WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_EX_NOACTIVATE = 0x00000080, 0x00000008, 0x08000000
SWP_NOACTIVATE, SWP_ASYNCWINDOWPOS = 0x0010, 0x4000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
LWA_ALPHA = 0x0002
WM_DESTROY, WM_ENDSESSION, WM_DISPLAYCHANGE = 0x0002, 0x0016, 0x007E
WM_NCHITTEST, WM_POWERBROADCAST, WM_WTSSESSION_CHANGE = 0x0084, 0x0218, 0x02B1
WM_APP_CLOSE = 0x8000 + 1
PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC = 0x0007, 0x0012
WTS_CONSOLE_CONNECT, WTS_SESSION_LOGON, WTS_SESSION_UNLOCK = 0x1, 0x5, 0x8
HTTRANSPARENT = -1
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x00000001
ERROR_ALREADY_EXISTS, ERROR_CLASS_ALREADY_EXISTS = 183, 1410


def enable_dpi_awareness() -> None:
    """Per-monitor DPI awareness so virtual-screen metrics are physical pixels."""
    if not IS_WINDOWS:
        return
    try:  # Windows 10 1703+: PER_MONITOR_AWARE_V2
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


# ════════════════════════════════════════════════════════════════════════════
#  Gamma (warmth) — every attached monitor
# ════════════════════════════════════════════════════════════════════════════
class GammaController:
    """Applies gamma ramps to every monitor attached to the desktop.

    * Device contexts are opened once and re-opened on display changes.
    * Windows rejects ramps that deviate too far from identity unless the
      ``GdiIcmGammaRange`` registry tweak is set. When that happens we search
      upwards for the warmest temperature the system accepts (the *floor*)
      and clamp to it, instead of silently doing nothing.
    """

    def __init__(self, cache: Optional[RampCache] = None) -> None:
        self._cache = cache or RampCache()
        self._lock = threading.Lock()
        self._dcs: list = []           # (name, hdc, owned)
        self._floor_k: int = MIN_K
        self._hint_logged = False
        self._opened = False

    # — device handling (overridable for tests) —
    def _open_devices(self) -> list:
        dcs: list = []
        if not IS_WINDOWS:
            return dcs
        dd = _DISPLAY_DEVICEW()
        i = 0
        while i < 32:
            dd.cb = ctypes.sizeof(dd)
            if not _user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
                break
            i += 1
            if not dd.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
                continue
            name = dd.DeviceName
            hdc = _gdi32.CreateDCW(None, name, None, None) or _gdi32.CreateDCW(name, None, None, None)
            if hdc:
                dcs.append((name, hdc, True))
            else:
                log.warning("CreateDC failed for %s (err=%d)", name, ctypes.get_last_error())
        if not dcs:  # fall back to the primary screen DC
            hdc = _user32.GetDC(None)
            if hdc:
                dcs.append(("primary", hdc, False))
        log.info("Gamma devices: %s", ", ".join(n for n, _, _ in dcs) or "none")
        return dcs

    def _close_devices(self, dcs: list) -> None:
        if not IS_WINDOWS:
            return
        for _name, hdc, owned in dcs:
            if owned:
                _gdi32.DeleteDC(hdc)
            else:
                _user32.ReleaseDC(None, hdc)

    def _set_ramp(self, hdc: Any, ramp: GammaRamp) -> bool:
        return bool(_gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(ramp)))

    # — public API —
    def refresh_devices(self) -> None:
        """Re-enumerate monitors (call after WM_DISPLAYCHANGE)."""
        with self._lock:
            old, self._dcs = self._dcs, []
            self._close_devices(old)
            self._dcs = self._open_devices()
            self._opened = True
            self._floor_k = MIN_K  # a new display/driver may accept warmer ramps

    def _apply_locked(self, kelvin: int) -> bool:
        ok_any = False
        ramp = self._cache.get(kelvin)
        for _name, hdc, _owned in self._dcs:
            if self._set_ramp(hdc, ramp):
                ok_any = True
        return ok_any

    def apply(self, kelvin: float) -> bool:
        with self._lock:
            if not self._opened:
                self._dcs = self._open_devices()
                self._opened = True
            if not self._dcs:
                return False
            k = max(quantize_kelvin(kelvin), self._floor_k)
            if self._apply_locked(k):
                return True
            if k >= NEUTRAL_K:
                log.warning("SetDeviceGammaRamp failed even for a neutral ramp.")
                return False
            # Find the warmest temperature Windows accepts and remember it.
            probe = k
            while probe < NEUTRAL_K:
                probe = min(NEUTRAL_K, probe + GAMMA_FLOOR_STEP_K)
                if self._apply_locked(probe):
                    self._floor_k = probe
                    if not self._hint_logged:
                        self._hint_logged = True
                        log.warning(
                            "Windows rejected %d K; the warmest accepted is %d K. To allow "
                            "warmer values set HKLM\\SOFTWARE\\Microsoft\\Windows NT\\"
                            "CurrentVersion\\ICM\\GdiIcmGammaRange (DWORD) = 256 and sign out.",
                            k, probe)
                    return True
            return False

    def reset(self) -> bool:
        return self.apply(NEUTRAL_K)

    @property
    def floor_k(self) -> int:
        return self._floor_k

    def close(self) -> None:
        with self._lock:
            self._close_devices(self._dcs)
            self._dcs = []
            self._opened = False


# ════════════════════════════════════════════════════════════════════════════
#  Hardware brightness — background worker, latest request wins
# ════════════════════════════════════════════════════════════════════════════
class HardwareBrightness:
    """Talks to screen_brightness_control on its own thread.

    WMI/DDC-CI calls can take 50 ms–2 s each. Running them inline used to stall
    every animation frame; here callers just post the latest wanted value and
    the worker coalesces intermediate values away.
    """
    UNKNOWN, AVAILABLE, UNAVAILABLE = "unknown", "available", "unavailable"

    def __init__(self, on_state_change: Optional[Callable[[str], None]] = None,
                 backend: Any = None, detect_delays: Sequence[float] = HW_DETECT_DELAYS_S) -> None:
        self._on_state_change = on_state_change
        self._backend = backend
        self._detect_delays = tuple(detect_delays)
        self._cond = threading.Condition()
        self._pending: Optional[int] = None
        self._force = False
        self._stopping = False
        self._last: Optional[int] = None
        self._failures = 0
        self.state = self.UNKNOWN
        self._thread = threading.Thread(target=self._loop, name="DuskRay-Brightness", daemon=True)
        self._thread.start()

    def request(self, pct: int, force: bool = False) -> None:
        with self._cond:
            if self._stopping:
                return
            self._pending = int(clamp(pct, 0, 100))
            self._force = self._force or force
            self._cond.notify()

    def stop(self, final_pct: Optional[int] = None, timeout: float = 3.0) -> None:
        with self._cond:
            if final_pct is not None:
                self._pending = int(clamp(final_pct, 0, 100))
                self._force = True
            self._stopping = True
            self._cond.notify()
        self._thread.join(timeout)

    # — internals —
    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            log.info("Hardware brightness: %s", state)
            if self._on_state_change:
                try:
                    self._on_state_change(state)
                except Exception:
                    log.exception("brightness state callback failed")

    def _load_backend(self) -> Any:
        if self._backend is not None:
            return self._backend
        try:
            import screen_brightness_control as sbc  # heavy import — done lazily
            return sbc
        except Exception as exc:
            log.info("screen_brightness_control unavailable (%s)", exc)
            return None

    def _detect(self, backend: Any) -> bool:
        """Retry with back-off; when stopping, make one last immediate try so
        a final restore (e.g. on exit / --reset) still gets through."""
        for delay in self._detect_delays:
            with self._cond:
                if delay and not self._stopping:
                    self._cond.wait(delay)
                stopping = self._stopping
            try:
                vals = backend.get_brightness()
                if vals:
                    first = vals[0] if isinstance(vals, (list, tuple)) else vals
                    self._last = int(first)
                    return True
            except Exception as exc:
                log.debug("Brightness detection attempt failed: %s", exc)
            if stopping:
                return False
        return False

    def _loop(self) -> None:
        backend = self._load_backend()
        if backend is None or not self._detect(backend):
            self._set_state(self.UNAVAILABLE)
            return
        self._set_state(self.AVAILABLE)
        while True:
            with self._cond:
                while self._pending is None and not self._stopping:
                    self._cond.wait()
                if self._pending is None:
                    return  # stopping and drained
                pct, force = self._pending, self._force
                self._pending, self._force = None, False
            if not force and pct == self._last:
                continue
            try:
                backend.set_brightness(pct)
                self._last = pct
                self._failures = 0
            except Exception as exc:
                self._failures += 1
                log.warning("Hardware brightness failed (%s) [%d/%d]",
                            exc, self._failures, HW_MAX_CONSECUTIVE_FAILURES)
                if self._failures >= HW_MAX_CONSECUTIVE_FAILURES:
                    self._set_state(self.UNAVAILABLE)
                    return


# ════════════════════════════════════════════════════════════════════════════
#  Dim overlay + system-event window
# ════════════════════════════════════════════════════════════════════════════
def _overlay_bounds() -> tuple:
    """Virtual-screen bounds, inset on every side (see OVERLAY_EDGE_INSET_PX)."""
    inset = OVERLAY_EDGE_INSET_PX
    x = _user32.GetSystemMetrics(SM_XVIRTUALSCREEN) + inset
    y = _user32.GetSystemMetrics(SM_YVIRTUALSCREEN) + inset
    w = _user32.GetSystemMetrics(SM_CXVIRTUALSCREEN) - 2 * inset
    h = _user32.GetSystemMetrics(SM_CYVIRTUALSCREEN) - 2 * inset
    return x, y, max(1, w), max(1, h)


def _apply_overlay_region(hwnd: Any, w: int, h: int) -> None:
    rgn = _gdi32.CreateRectRgn(1, 1, max(2, w - 1), max(2, h - 1))
    if rgn and not _user32.SetWindowRgn(hwnd, rgn, True):
        _gdi32.DeleteObject(rgn)  # ownership transfers only on success


class DimOverlay:
    """Click-through, topmost, layered black window across the virtual screen.

    It dims below the hardware minimum (or replaces hardware brightness when
    that is unavailable). Its hidden window also receives system broadcasts,
    which are forwarded to *on_event*: "display", "resume", "unlock", "end".
    Callbacks run on the overlay thread and must not block.
    """
    _CLASS = "DuskRayOverlayClass"

    def __init__(self, on_event: Optional[Callable[[str], None]] = None) -> None:
        self._on_event = on_event
        self._hwnd: Any = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._failed = not IS_WINDOWS
        self._alpha_pct = 0
        self._visible = False
        self._wndproc_ref: Any = None   # keep the ctypes callback alive
        self._brush: Any = None

    def start(self, timeout: float = 3.0) -> None:
        if self._failed or (self._thread and self._thread.is_alive()):
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="DuskRay-Overlay", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            log.warning("Overlay window did not become ready in time.")

    @property
    def available(self) -> bool:
        return not self._failed and bool(self._hwnd)

    @property
    def alpha(self) -> int:
        return self._alpha_pct

    def set_alpha(self, alpha_pct: int) -> None:
        alpha_pct = int(clamp(alpha_pct, 0, MAX_OVERLAY_ALPHA_PCT))
        if alpha_pct == self._alpha_pct and (alpha_pct > 0) == self._visible:
            return
        self._alpha_pct = alpha_pct
        hwnd = self._hwnd
        if not hwnd:
            return
        _user32.SetLayeredWindowAttributes(hwnd, 0, int(alpha_pct * 255 / 100), LWA_ALPHA)
        want_visible = alpha_pct > 0
        if want_visible != self._visible:
            # Async variants never block on the overlay thread → no deadlocks.
            # Hidden at 0 % so an invisible TOPMOST window never sits above
            # the taskbar and breaks auto-hide.
            _user32.ShowWindowAsync(hwnd, SW_SHOWNOACTIVATE if want_visible else SW_HIDE)
            self._visible = want_visible

    def stop(self) -> None:
        hwnd = self._hwnd
        if hwnd:
            _user32.PostMessageW(hwnd, WM_APP_CLOSE, 0, 0)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    def _emit(self, event: str) -> None:
        if self._on_event:
            try:
                self._on_event(event)
            except Exception:
                log.exception("overlay event handler failed (%s)", event)

    def _run(self) -> None:
        hinst = _kernel32.GetModuleHandleW(None)
        registered = False
        try:
            self._wndproc_ref = WNDPROC(self._wndproc)
            self._brush = _gdi32.CreateSolidBrush(0x000000)
            wc = _WNDCLASSW()
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = hinst
            wc.hbrBackground = self._brush
            wc.lpszClassName = self._CLASS
            if _user32.RegisterClassW(ctypes.byref(wc)):
                registered = True
            elif ctypes.get_last_error() != ERROR_CLASS_ALREADY_EXISTS:
                raise OSError(f"RegisterClassW failed (err={ctypes.get_last_error()})")

            x, y, w, h = _overlay_bounds()
            ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE
            hwnd = _user32.CreateWindowExW(ex, self._CLASS, "DuskRayOverlay", WS_POPUP,
                                           x, y, w, h, None, None, hinst, None)
            if not hwnd:
                raise OSError(f"CreateWindowExW failed (err={ctypes.get_last_error()})")
            _apply_overlay_region(hwnd, w, h)
            _user32.SetLayeredWindowAttributes(hwnd, 0, 0, LWA_ALPHA)
            if _wtsapi32 is not None:
                _wtsapi32.WTSRegisterSessionNotification(hwnd, 0)  # NOTIFY_FOR_THIS_SESSION
            self._hwnd = hwnd
            log.info("Overlay window ready (%dx%d @ %d,%d)", w, h, x, y)
        except Exception:
            log.exception("Overlay unavailable")
            self._failed = True
            self._ready.set()
            self._cleanup(hinst, registered)
            return
        self._ready.set()

        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))
        self._cleanup(hinst, registered)
        log.info("Overlay window destroyed.")

    def _cleanup(self, hinst: Any, registered: bool) -> None:
        hwnd, self._hwnd = self._hwnd, None
        if hwnd:
            if _wtsapi32 is not None:
                _wtsapi32.WTSUnRegisterSessionNotification(hwnd)
            _user32.DestroyWindow(hwnd)
        if registered:
            _user32.UnregisterClassW(self._CLASS, hinst)
        if self._brush:
            _gdi32.DeleteObject(self._brush)
            self._brush = None

    def _wndproc(self, hwnd: Any, msg: int, wparam: int, lparam: int) -> int:
        try:
            if msg == WM_NCHITTEST:
                return HTTRANSPARENT
            if msg == WM_DISPLAYCHANGE:
                x, y, w, h = _overlay_bounds()
                _user32.SetWindowPos(hwnd, ctypes.c_void_p(-1), x, y, w, h,
                                     SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS)
                _apply_overlay_region(hwnd, w, h)
                self._emit("display")
                return 0
            if msg == WM_POWERBROADCAST:
                if wparam in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                    self._emit("resume")
                return 1  # TRUE
            if msg == WM_WTSSESSION_CHANGE:
                if wparam in (WTS_SESSION_UNLOCK, WTS_SESSION_LOGON, WTS_CONSOLE_CONNECT):
                    self._emit("unlock")
                return 0
            if msg == WM_ENDSESSION:
                if wparam:
                    self._emit("end")
                return 0
            if msg == WM_APP_CLOSE:
                if _wtsapi32 is not None:
                    _wtsapi32.WTSUnRegisterSessionNotification(hwnd)
                _user32.DestroyWindow(hwnd)
                self._hwnd = None
                return 0
            if msg == WM_DESTROY:
                _user32.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("overlay wndproc error")
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)


class NullOverlay:
    """Stand-in when no overlay is possible (non-Windows / tests)."""
    available = False
    alpha = 0

    def start(self, timeout: float = 0) -> None: ...
    def set_alpha(self, alpha_pct: int) -> None: ...
    def stop(self) -> None: ...


# ════════════════════════════════════════════════════════════════════════════
#  Display output: (virtual brightness, kelvin) → devices
# ════════════════════════════════════════════════════════════════════════════
class DisplayOutput:
    """Maps the virtual state onto overlay, hardware and gamma — and skips
    every device call whose value did not actually change.

    Virtual brightness range -30 … 100:
      * hardware available : hw = max(0, v), overlay = max(0, -v)
      * no hardware        : overlay = 100 - v (capped at MAX_OVERLAY_ALPHA_PCT)
    """

    def __init__(self, gamma: Any, hardware: Any, overlay: Any) -> None:
        self.gamma = gamma
        self.hardware = hardware
        self.overlay = overlay
        self._last_b: Optional[int] = None
        self._last_k: Optional[int] = None
        self._lock = threading.Lock()

    @staticmethod
    def split(virtual: int, hw_usable: bool) -> tuple:
        """(hardware %, overlay %) for a virtual brightness."""
        if hw_usable:
            return max(0, virtual), max(0, -virtual)
        return None, max(0, 100 - virtual)

    def apply(self, b: float, k: float, force_brightness: bool = False,
              refresh_gamma: bool = False) -> None:
        vb = int(clamp(int(round(b)), MIN_BRIGHTNESS, MAX_BRIGHTNESS))
        vk = quantize_kelvin(k)
        with self._lock:
            if force_brightness or vb != self._last_b:
                self._last_b = vb
                hw_usable = self.hardware is not None and self.hardware.state != HardwareBrightness.UNAVAILABLE
                hw, ov = self.split(vb, hw_usable)
                if hw is not None:
                    self.hardware.request(hw, force=force_brightness)
                self.overlay.set_alpha(ov)
            # A forced refresh only re-asserts *warm* ramps: re-writing a
            # neutral ramp while idle would fight Night Light & co.
            if vk != self._last_k or (refresh_gamma and vk < NEUTRAL_K):
                self._last_k = vk
                self.gamma.apply(vk)

    @property
    def current(self) -> tuple:
        return self._last_b, self._last_k


# ════════════════════════════════════════════════════════════════════════════
#  Display engine — one persistent animation thread
# ════════════════════════════════════════════════════════════════════════════
class DisplayEngine:
    """Animates from wherever the screen *currently* is to the latest target.

    * A single long-lived thread (no thread per transition, no blocking joins
      on the UI thread).
    * Re-targeting mid-animation continues smoothly from the current frame.
    * While warm, periodically re-asserts the gamma ramp, because Windows,
      drivers, UAC prompts, games and lock screens can silently reset it.
    """

    def __init__(self, output: Callable[..., None], fps: int = TRANSITION_FPS,
                 refresh_s: float = GAMMA_REFRESH_S,
                 start_b: float = NEUTRAL_BRIGHTNESS, start_k: float = NEUTRAL_K) -> None:
        self._output = output
        self._frame = 1.0 / max(1, fps)
        self._refresh_s = refresh_s
        self._cond = threading.Condition()
        self._cur_b, self._cur_k = float(start_b), float(start_k)
        self._tgt_b, self._tgt_k = float(start_b), float(start_k)
        self._from_b, self._from_k = self._cur_b, self._cur_k
        self._t0 = 0.0
        self._duration = 0.0
        self._dirty = False
        self._animating = False
        self._force_b = False
        self._refresh_g = False
        self._stop = False
        self._thread = threading.Thread(target=self._loop, name="DuskRay-Engine", daemon=True)
        self._thread.start()

    def set_target(self, b: float, k: float, duration: float = 0.0,
                   force_brightness: bool = False) -> None:
        with self._cond:
            self._tgt_b, self._tgt_k = float(b), float(k)
            self._duration = max(0.0, duration)
            self._force_b = self._force_b or force_brightness
            self._dirty = True
            self._cond.notify()

    def reapply(self, gamma: bool = True, brightness: bool = False) -> None:
        with self._cond:
            self._refresh_g = self._refresh_g or gamma
            self._force_b = self._force_b or brightness
            self._cond.notify()

    @property
    def target(self) -> tuple:
        return self._tgt_b, self._tgt_k

    @property
    def current(self) -> tuple:
        return self._cur_b, self._cur_k

    def is_idle(self) -> bool:
        with self._cond:
            return not (self._dirty or self._animating or self._force_b or self._refresh_g)

    def wait_idle(self, timeout: float = 5.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.is_idle():
                return True
            time.sleep(0.01)
        return self.is_idle()

    def stop(self, timeout: float = 2.0) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout)

    def _loop(self) -> None:
        next_refresh = time.monotonic() + self._refresh_s
        while True:
            with self._cond:
                while not (self._stop or self._dirty or self._animating
                           or self._force_b or self._refresh_g):
                    remaining = next_refresh - time.monotonic()
                    if remaining <= 0:
                        if self._cur_k < NEUTRAL_K:   # only fight resets while warm
                            self._refresh_g = True
                        next_refresh = time.monotonic() + self._refresh_s
                        continue
                    self._cond.wait(remaining)
                if self._stop:
                    return
                now = time.monotonic()
                if self._dirty:
                    self._dirty = False
                    self._from_b, self._from_k = self._cur_b, self._cur_k
                    self._t0 = now
                    changed = (self._from_b, self._from_k) != (self._tgt_b, self._tgt_k)
                    self._animating = changed and self._duration > 0
                if self._animating:
                    p = min(1.0, (now - self._t0) / self._duration)
                    e = ease_in_out(p)
                    b = lerp(self._from_b, self._tgt_b, e)
                    k = lerp(self._from_k, self._tgt_k, e)
                    if p >= 1.0:
                        self._animating = False
                        b, k = self._tgt_b, self._tgt_k
                else:
                    b, k = self._tgt_b, self._tgt_k
                self._cur_b, self._cur_k = b, k
                force_b, refresh_g = self._force_b, self._refresh_g
                self._force_b = self._refresh_g = False
                animating = self._animating
            try:
                self._output(b, k, force_brightness=force_b, refresh_gamma=refresh_g)
            except Exception:
                log.exception("display output failed")
            next_refresh = time.monotonic() + self._refresh_s
            if animating:
                with self._cond:
                    if not self._stop and not self._dirty:
                        self._cond.wait(self._frame)


# ════════════════════════════════════════════════════════════════════════════
#  Circadian scheduler
# ════════════════════════════════════════════════════════════════════════════
class CircadianScheduler:
    """Calls *on_tick* every CIRCADIAN_TICK_S seconds (and immediately)."""

    def __init__(self, on_tick: Callable[[], None], interval: float = CIRCADIAN_TICK_S) -> None:
        self._on_tick = on_tick
        self._interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="DuskRay-Circadian", daemon=True)
        self._thread.start()
        log.info("Circadian scheduler started.")

    def stop(self) -> None:
        if not self._thread:
            return
        self._stop.set()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None
        log.info("Circadian scheduler stopped.")

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._on_tick()
            except Exception:
                log.exception("circadian tick failed")


# ════════════════════════════════════════════════════════════════════════════
#  Windows startup registry & single instance
# ════════════════════════════════════════════════════════════════════════════
def startup_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --startup'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = str(pythonw) if pythonw.exists() else sys.executable
    return f'"{interpreter}" "{os.path.abspath(__file__)}" --startup'


def set_startup(enabled: bool) -> bool:
    """Create/remove the HKCU Run entry. Returns True on success."""
    if not IS_WINDOWS:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
            if enabled:
                cmd = startup_command()
                try:
                    current, _ = winreg.QueryValueEx(key, APP_NAME)
                except FileNotFoundError:
                    current = None
                if current != cmd:
                    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
                    log.info("Startup entry set: %s", cmd)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                    log.info("Startup entry removed.")
                except FileNotFoundError:
                    pass
        return True
    except OSError as exc:
        log.error("Failed to modify startup registry: %s", exc)
        return False


_singleton_handle: Any = None


def ensure_single_instance() -> bool:
    global _singleton_handle
    if not IS_WINDOWS:
        return True
    handle = _kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        log.warning("CreateMutexW failed (err=%d); continuing.", ctypes.get_last_error())
        return True
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        log.warning("Another instance of %s is already running.", APP_NAME)
        return False
    _singleton_handle = handle  # held for the process lifetime
    return True


# ════════════════════════════════════════════════════════════════════════════
#  Tray icon artwork (also used by generate_icon.py)
# ════════════════════════════════════════════════════════════════════════════
ICON_STATES: tuple = ("active", "dim", "nowarm", "disabled")
_ICON_COLOURS = {
    "active": ((255, 160, 40, 255), (255, 190, 60, 255)),
    "dim":    ((200, 120, 30, 255), (220, 150, 50, 255)),
    "nowarm": ((120, 175, 255, 255), (160, 200, 255, 255)),   # brightness only
}


@functools.lru_cache(maxsize=16)
def render_icon(state: str, size: int = 64) -> Any:
    """Anti-aliased sun (active / dim / nowarm) or moon (disabled)."""
    from PIL import Image, ImageDraw
    scale = 4
    big = size * scale
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = big / 2
    if state == "disabled":
        r_out, r_cut, off = big * 0.34, big * 0.28, big * 0.12
        d.ellipse((c - r_out, c - r_out, c + r_out, c + r_out), fill=(150, 150, 165, 255))
        d.ellipse((c - r_cut + off, c - r_cut - off, c + r_cut + off, c + r_cut - off),
                  fill=(0, 0, 0, 0))
    else:
        body, ray = _ICON_COLOURS.get(state, _ICON_COLOURS["active"])
        r_body = big * 0.28
        d.ellipse((c - r_body, c - r_body, c + r_body, c + r_body), fill=body)
        r_in, r_out = big * 0.36, big * 0.47
        width = max(2, int(big * 0.06))
        for angle in range(0, 360, 45):
            rad = math.radians(angle)
            ca, sa = math.cos(rad), math.sin(rad)
            d.line((c + ca * r_in, c + sa * r_in, c + ca * r_out, c + sa * r_out),
                   fill=ray, width=width)
    resample = getattr(Image, "Resampling", Image).LANCZOS
    return img.resize((size, size), resample)


def icon_state(cfg: AppConfig, brightness: int) -> str:
    if not cfg.enabled:
        return "disabled"
    if brightness < 0:
        return "dim"
    if not cfg.warmth_enabled:
        return "nowarm"
    return "active"


# ════════════════════════════════════════════════════════════════════════════
#  Application
# ════════════════════════════════════════════════════════════════════════════
def _pystray() -> Any:
    import pystray  # imported lazily so the module is importable headless
    return pystray


class DuskRayApp:
    """Owns config, display pipeline, scheduler and tray icon."""

    def __init__(self, config: Optional[AppConfig] = None, output: Optional[DisplayOutput] = None,
                 persist: bool = True, clock: Callable[[], float] = minute_of_day_now) -> None:
        self.config = config if config is not None else AppConfig.load()
        self._persist = persist
        self._clock = clock
        self._lock = threading.RLock()
        self._icon: Any = None
        self._brightness_override: Optional[int] = None
        self._shutdown_done = False
        self._last_ui: Optional[tuple] = None
        self._display_timer: Optional[threading.Timer] = None
        self._output = output
        self._engine: Optional[DisplayEngine] = None
        self._circadian = CircadianScheduler(self._on_circadian_tick)

    # ── pipeline ────────────────────────────────────────────────────────────
    def _build_output(self) -> DisplayOutput:
        overlay: Any = DimOverlay(on_event=self._on_system_event) if IS_WINDOWS else NullOverlay()
        overlay.start()
        hardware = HardwareBrightness(on_state_change=self._on_hw_state)
        return DisplayOutput(GammaController(), hardware, overlay)

    def start_pipeline(self) -> None:
        if self._output is None:
            self._output = self._build_output()
        if self._engine is None:
            self._engine = DisplayEngine(self._output.apply)

    def _on_hw_state(self, _state: str) -> None:
        # Brightness mapping changes (overlay takes over) → re-apply.
        if self._engine:
            self._engine.reapply(gamma=False, brightness=True)

    def _on_system_event(self, event: str) -> None:
        """Runs on the overlay thread — keep it non-blocking."""
        log.info("System event: %s", event)
        if event == "end":
            self.shutdown()
            return
        if event == "display" and self._output is not None:
            # Display changes arrive in bursts; debounce and let the driver settle.
            with self._lock:
                if self._display_timer is not None:
                    self._display_timer.cancel()
                self._display_timer = threading.Timer(0.75, self._after_display_change)
                self._display_timer.daemon = True
                self._display_timer.start()
            return
        if self._engine:
            self._engine.reapply(gamma=True, brightness=(event == "resume"))

    def _after_display_change(self) -> None:
        if self._shutdown_done:
            return
        try:
            self._output.gamma.refresh_devices()  # type: ignore[union-attr]
        except Exception:
            log.exception("gamma device refresh failed")
        if self._engine:
            self._engine.reapply(gamma=True, brightness=True)

    # ── state → screen ──────────────────────────────────────────────────────
    def targets(self) -> tuple:
        with self._lock:
            return compute_targets(self.config, self._clock(), self._brightness_override)

    def apply(self, duration: float = 0.0, force_brightness: bool = False) -> None:
        with self._lock:
            b, k = self.targets()
            smooth = self.config.smooth_transitions
        if self._engine:
            self._engine.set_target(b, k, duration if smooth else 0.0, force_brightness)
        self._refresh_ui()

    def _update(self, **changes: Any) -> None:
        with self._lock:
            for name, value in changes.items():
                setattr(self.config, name, value)
            self.config.validate()
            if self._persist:
                self.config.save()

    # ── user actions ────────────────────────────────────────────────────────
    def set_brightness(self, level: int) -> None:
        with self._lock:
            self._brightness_override = None   # explicit choice beats safe-startup
            self._update(brightness=level)
        self.apply(TRANSITION_BRIGHTNESS_S, force_brightness=True)

    def set_warmth(self, kelvin: int) -> None:
        """Pick a fixed warmth. Turns circadian mode off (it would otherwise
        override the choice on its next tick) and turns warmth on."""
        with self._lock:
            self._update(warmth_k=kelvin, circadian_enabled=False, warmth_enabled=True)
        self._circadian.stop()
        self.apply(TRANSITION_WARMTH_S)

    def toggle_enabled(self) -> None:
        self._update(enabled=not self.config.enabled)
        self.apply(TRANSITION_TOGGLE_S, force_brightness=True)

    def toggle_warmth(self) -> None:
        """Warmth on/off while keeping brightness exactly as it is."""
        with self._lock:
            changes: dict = {"warmth_enabled": not self.config.warmth_enabled}
            if changes["warmth_enabled"] and not self.config.enabled:
                changes["enabled"] = True     # turning warmth on implies "on"
            self._update(**changes)
        self.apply(TRANSITION_TOGGLE_S, force_brightness="enabled" in changes)

    def on_click(self) -> None:
        if self.config.click_action == CLICK_TOGGLE_WARMTH:
            self.toggle_warmth()
        else:
            self.toggle_enabled()

    def set_click_action(self, action: str) -> None:
        self._update(click_action=action)

    def toggle_smooth(self) -> None:
        self._update(smooth_transitions=not self.config.smooth_transitions)

    def toggle_circadian(self) -> None:
        turning_on = not self.config.circadian_enabled
        changes: dict = {"circadian_enabled": turning_on}
        if turning_on:
            changes["warmth_enabled"] = True   # a schedule only makes sense with warmth on
        self._update(**changes)
        self._sync_circadian()
        self.apply(TRANSITION_WARMTH_S)

    def set_circadian(self, **changes: Any) -> None:
        self._update(**changes)
        if self.config.circadian_enabled:
            self.apply(TRANSITION_WARMTH_S)

    def toggle_startup(self) -> None:
        new_state = not self.config.run_at_startup
        if set_startup(new_state) or not IS_WINDOWS:
            self._update(run_at_startup=new_state)

    def reset_to_defaults(self) -> None:
        with self._lock:
            self.config = AppConfig()
            self._brightness_override = None
            if self._persist:
                self.config.save()
        set_startup(False)
        self._sync_circadian()
        self.apply(TRANSITION_TOGGLE_S, force_brightness=True)

    # ── circadian ───────────────────────────────────────────────────────────
    def _sync_circadian(self) -> None:
        if self.config.circadian_enabled:
            self._circadian.start()
        else:
            self._circadian.stop()

    def _on_circadian_tick(self) -> None:
        if not (self.config.enabled and self.config.warmth_enabled and self.config.circadian_enabled):
            return
        b, k = self.targets()
        if self._engine and quantize_kelvin(self._engine.target[1]) == quantize_kelvin(k):
            return  # nothing to do — no redundant work, no config writes
        self.apply(TRANSITION_CIRCADIAN_S)

    # ── labels / icon / tooltip ─────────────────────────────────────────────
    def status_text(self) -> str:
        with self._lock:
            cfg = self.config
            b = cfg.brightness if self._brightness_override is None else self._brightness_override
            if not cfg.enabled:
                return f"Off  (brightness {b}%, warmth {'on' if cfg.warmth_enabled else 'off'})"
            if not cfg.warmth_enabled:
                warmth = "Off"
            else:
                warmth = f"{effective_kelvin(cfg, self._clock())}K"
                if cfg.circadian_enabled:
                    warmth += " (auto)"
            return f"Brightness: {b}%   |   Warmth: {warmth}"

    def _refresh_ui(self) -> None:
        icon = self._icon
        if icon is None:
            return
        with self._lock:
            b = self.config.brightness if self._brightness_override is None else self._brightness_override
            state = icon_state(self.config, b)
            title = f"{APP_NAME} — {self.status_text()}"[:127]
        if self._last_ui == (state, title):
            return
        self._last_ui = (state, title)
        try:
            icon.icon = render_icon(state)
            icon.title = title
        except Exception:
            log.exception("tray icon update failed")

    # ── tray menu ───────────────────────────────────────────────────────────
    def _act(self, fn: Callable[..., None], *args: Any, **kwargs: Any) -> Callable:
        """Menu callback wrapper: exceptions are logged, never fatal."""
        def _action(icon: Any = None, item: Any = None) -> None:
            try:
                fn(*args, **kwargs)
            except Exception:
                log.exception("menu action %s failed", getattr(fn, "__name__", fn))
        return _action

    def build_menu(self) -> Any:
        pystray = _pystray()
        Item, Menu = pystray.MenuItem, pystray.Menu
        cfg = lambda: self.config  # noqa: E731 — always the *current* config object

        def radio(label: str, action: Callable, is_checked: Callable[[], bool]) -> Any:
            return Item(label, action, checked=lambda _i: is_checked(), radio=True)

        brightness = Menu(*(
            radio(f"{lvl}%" + ("  (dim overlay)" if lvl < 0 else ""),
                  self._act(self.set_brightness, lvl),
                  lambda lvl=lvl: cfg().brightness == lvl)
            for lvl in BRIGHTNESS_LEVELS))
        warmth = Menu(*(
            radio(f"{k}K" + ("  (neutral)" if k == NEUTRAL_K else ""),
                  self._act(self.set_warmth, k),
                  lambda k=k: cfg().warmth_enabled and not cfg().circadian_enabled and cfg().warmth_k == k)
            for k in WARMTH_LEVELS_K))

        def hours(field_name: str) -> Any:
            return Menu(*(
                radio(f"{h:02d}:00", self._act(self.set_circadian, **{field_name: h}),
                      lambda h=h: getattr(cfg(), field_name) == h)
                for h in HOURS_24))

        def kelvins(field_name: str) -> Any:
            return Menu(*(
                radio(f"{k}K", self._act(self.set_circadian, **{field_name: k}),
                      lambda k=k: getattr(cfg(), field_name) == k)
                for k in WARMTH_LEVELS_K))

        fade = Menu(*(
            radio("Instant" if m == 0 else f"{m} minutes",
                  self._act(self.set_circadian, circadian_fade_minutes=m),
                  lambda m=m: cfg().circadian_fade_minutes == m)
            for m in FADE_MINUTES_CHOICES))

        circadian_settings = Menu(
            Item("Day start", hours("circadian_day_hour")),
            Item("Night start", hours("circadian_night_hour")),
            Menu.SEPARATOR,
            Item("Day warmth", kelvins("circadian_day_k")),
            Item("Night warmth", kelvins("circadian_night_k")),
            Menu.SEPARATOR,
            Item("Fade duration", fade),
        )
        click = Menu(
            radio("Toggle everything", self._act(self.set_click_action, CLICK_TOGGLE_ALL),
                  lambda: cfg().click_action == CLICK_TOGGLE_ALL),
            radio("Toggle warmth only", self._act(self.set_click_action, CLICK_TOGGLE_WARMTH),
                  lambda: cfg().click_action == CLICK_TOGGLE_WARMTH),
        )

        return Menu(
            Item(lambda _i: self.status_text(), self._act(lambda: None), enabled=False),
            Menu.SEPARATOR,
            Item("Brightness", brightness),
            Item("Warmth", warmth),
            Menu.SEPARATOR,
            # The bold (default) item is the one a left-click on the icon runs.
            Item("Enabled", self._act(self.toggle_enabled),
                 checked=lambda _i: cfg().enabled,
                 default=lambda _i: cfg().click_action == CLICK_TOGGLE_ALL),
            Item("Warmth enabled", self._act(self.toggle_warmth),
                 checked=lambda _i: cfg().warmth_enabled,
                 default=lambda _i: cfg().click_action == CLICK_TOGGLE_WARMTH),
            Item("Smooth transitions", self._act(self.toggle_smooth),
                 checked=lambda _i: cfg().smooth_transitions),
            Item("Circadian mode", self._act(self.toggle_circadian),
                 checked=lambda _i: cfg().circadian_enabled),
            Item("Circadian settings", circadian_settings),
            Menu.SEPARATOR,
            Item("Icon click", click),
            Item("Run at startup", self._act(self.toggle_startup),
                 checked=lambda _i: cfg().run_at_startup),
            Item("Reset to defaults", self._act(self.reset_to_defaults)),
            Item("About", self._act(self._about)),
            Menu.SEPARATOR,
            Item("Exit", self._act(self.quit)),
        )

    def _about(self) -> None:
        if not self._icon:
            return
        c = self.config
        onoff = lambda v: "On" if v else "Off"  # noqa: E731
        try:
            self._icon.notify(
                f"{self.status_text()}\n"
                f"Circadian: {onoff(c.circadian_enabled)}  ·  Smooth: {onoff(c.smooth_transitions)}\n"
                f"Startup: {onoff(c.run_at_startup)}",
                f"{APP_NAME} v{APP_VERSION}")
        except Exception:
            log.exception("notify failed")

    # ── lifecycle ───────────────────────────────────────────────────────────
    def run(self, startup_mode: bool = False) -> None:
        log.info("%s v%s starting (Python %s)", APP_NAME, APP_VERSION, sys.version.split()[0])
        if startup_mode and self.config.brightness < SAFE_STARTUP_BRIGHTNESS:
            log.info("Safe startup: brightness %d%% → %d%% (preference kept).",
                     self.config.brightness, SAFE_STARTUP_BRIGHTNESS)
            self._brightness_override = SAFE_STARTUP_BRIGHTNESS

        if self.config.run_at_startup:
            set_startup(True)  # keep the path current if the exe moved

        enable_dpi_awareness()
        self.start_pipeline()
        # Clear any ramp left over from a crash, then apply the real state.
        self._output.gamma.reset()  # type: ignore[union-attr]
        self.apply(0.0, force_brightness=True)
        self._sync_circadian()

        pystray = _pystray()
        b = self.config.brightness if self._brightness_override is None else self._brightness_override
        self._icon = pystray.Icon(APP_NAME, render_icon(icon_state(self.config, b)),
                                  APP_NAME, self.build_menu())
        self._refresh_ui()

        atexit.register(self.shutdown)
        try:
            signal.signal(signal.SIGINT, lambda *_: self.quit())
            signal.signal(signal.SIGTERM, lambda *_: self.quit())
        except (ValueError, OSError):
            pass
        log.info("Tray icon active.")
        try:
            self._icon.run()
        finally:
            self.shutdown()

    def quit(self) -> None:
        log.info("Exit requested.")
        self.shutdown()
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                log.exception("icon stop failed")

    def shutdown(self) -> None:
        """Restore a normal display. Idempotent and safe from any thread."""
        with self._lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
            timer, self._display_timer = self._display_timer, None
        if timer is not None:
            timer.cancel()
        self._circadian.stop()
        if self._engine:
            self._engine.stop()
        out = self._output
        if out is not None:
            try:
                out.gamma.reset()
            except Exception:
                log.exception("gamma reset failed")
            try:
                out.overlay.set_alpha(0)
                out.overlay.stop()
            except Exception:
                log.exception("overlay stop failed")
            if out.hardware is not None:
                out.hardware.stop(final_pct=NEUTRAL_BRIGHTNESS)
            try:
                out.gamma.close()
            except Exception:
                pass
        log.info("Cleanup complete. Goodbye.")


# ════════════════════════════════════════════════════════════════════════════
#  CLI
# ════════════════════════════════════════════════════════════════════════════
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog=APP_NAME, description="Display Brightness & Warmth Control")
    p.add_argument("--reset", action="store_true", help="Restore factory defaults and exit.")
    p.add_argument("--startup", action="store_true",
                   help="Launched at OS startup (enforces a safe minimum brightness).")
    p.add_argument("--debug", action="store_true", help="Verbose logging.")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    return p.parse_args(argv)


def reset_everything() -> None:
    AppConfig().save()
    set_startup(False)
    gamma = GammaController()
    gamma.reset()
    gamma.close()
    hw = HardwareBrightness(detect_delays=(0.0,))
    hw.request(NEUTRAL_BRIGHTNESS, force=True)
    hw.stop(final_pct=NEUTRAL_BRIGHTNESS, timeout=5.0)
    print(f"{APP_NAME}: configuration reset to defaults.")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    init_logging(debug=args.debug)
    if not IS_WINDOWS:
        print(f"{APP_NAME} requires Windows.", file=sys.stderr)
        return 2
    try:
        if args.reset:
            reset_everything()
            return 0
        if not ensure_single_instance():
            print(f"{APP_NAME}: another instance is already running.")
            return 1
        DuskRayApp().run(startup_mode=args.startup)
        return 0
    except Exception:
        log.exception("Fatal error")
        raise


if __name__ == "__main__":
    sys.exit(main())
