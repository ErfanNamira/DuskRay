"""
DuskRay — Display Brightness & Warmth Control App
A lightweight Windows system-tray utility to control screen brightness and
colour warmth (in Kelvin), with smooth animated transitions, an optional
circadian-rhythm scheduler, multi-monitor awareness, one-click enable/disable,
sub-zero overlay dimming (–10/–20/–30 %), safe-startup brightness, and the
option to run automatically at Windows startup.

INSTALL (once):
    pip install pystray Pillow screen_brightness_control

RUN:
    pythonw duskray.py              (no console window — recommended)
    python  duskray.py              (with console, for debugging)
    python  duskray.py --reset      (restore factory defaults and exit)
    python  duskray.py --startup    (launched by Windows at startup — safe brightness)

BUILD standalone DuskRay.exe:
    pip install pyinstaller
    pyinstaller --onefile --noconsole --clean --strip --name "DuskRay" ^
        --icon=duskray.ico --version-file=version_info.txt ^
        --exclude-module tkinter --exclude-module unittest ^
        --exclude-module pdb --exclude-module doctest --exclude-module test ^
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
from typing import Any, Optional

# ── Third-party ─────────────────────────────────────────────────────────────
from PIL import Image, ImageDraw
import pystray

try:
    import screen_brightness_control as sbc
    _SBC_AVAILABLE: bool = True
except ImportError:  # pragma: no cover
    sbc = None  # type: ignore[assignment]
    _SBC_AVAILABLE = False

# ── Constants ───────────────────────────────────────────────────────────────
APP_NAME: str = "DuskRay"
APP_VERSION: str = "1.2.0"
CONFIG_DIR: Path = Path(os.getenv("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
CONFIG_PATH: Path = CONFIG_DIR / "config.json"
LOG_PATH: Path = CONFIG_DIR / "duskray.log"

# Brightness levels: negative values use a click-through overlay on top of 0 % hardware.
BRIGHTNESS_LEVELS: tuple[int, ...] = (-30, -20, -10, 0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100)
WARMTH_LEVELS_K: tuple[int, ...] = (6500, 6000, 5500, 5000, 4500, 4000, 3500, 3000, 2500, 2000)
HOURS_24: tuple[int, ...] = tuple(range(24))

NEUTRAL_K: int = 6500           # "no warmth" reference (daylight)
NEUTRAL_BRIGHTNESS: int = 100   # restored when DuskRay is disabled
MIN_BRIGHTNESS: int = -30
MAX_BRIGHTNESS: int = 100
SAFE_STARTUP_BRIGHTNESS: int = 10
TRANSITION_DURATION: float = 0.6
TRANSITION_FPS: int = 30
WIN_RUN_KEY: str = r"Software\Microsoft\Windows\CurrentVersion\Run"
MUTEX_NAME: str = f"Global\\{APP_NAME}-Singleton-v2"
MAX_OVERLAY_ALPHA_PCT: int = 95  # never fully hide the tray icon

# ── Logging ─────────────────────────────────────────────────────────────────
def _init_logging() -> logging.Logger:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:
        fmt = logging.Formatter(
            "[%(asctime)s] %(levelname)-7s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
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
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ── Win32 API bindings ──────────────────────────────────────────────────────
_gdi32 = ctypes.windll.gdi32
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

# Window-style constants
WS_POPUP            = 0x80000000
WS_VISIBLE          = 0x10000000
WS_EX_LAYERED       = 0x00080000
WS_EX_TRANSPARENT   = 0x00000020
WS_EX_TOOLWINDOW    = 0x00000080
WS_EX_TOPMOST       = 0x00000008
WS_EX_NOACTIVATE    = 0x08000000

CS_HREDRAW          = 0x0002
CS_VREDRAW          = 0x0001

HWND_TOPMOST        = ctypes.c_void_p(-1)
SWP_NOACTIVATE      = 0x0010
SWP_NOMOVE          = 0x0002
SWP_NOSIZE          = 0x0001
SWP_SHOWWINDOW      = 0x0040

SW_SHOWNOACTIVATE   = 4
SW_HIDE             = 0

LWA_ALPHA           = 0x0002

WM_DESTROY          = 0x0002
WM_QUIT             = 0x0012
WM_DISPLAYCHANGE    = 0x007E
WM_NCHITTEST        = 0x0084
WM_USER_CLOSE       = 0x0400 + 1

HTTRANSPARENT       = -1

SM_XVIRTUALSCREEN   = 76
SM_YVIRTUALSCREEN   = 77
SM_CXVIRTUALSCREEN  = 78
SM_CYVIRTUALSCREEN  = 79

ERROR_ALREADY_EXISTS = 183

# Callback type for WindowProc
WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long,       # LRESULT  (return values are small)
    ctypes.c_void_p,     # HWND
    ctypes.c_uint,       # UINT
    ctypes.c_size_t,     # WPARAM
    ctypes.c_ssize_t,    # LPARAM
)

class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style",          ctypes.c_uint),
        ("lpfnWndProc",    WNDPROC),
        ("cbClsExtra",     ctypes.c_int),
        ("cbWndExtra",     ctypes.c_int),
        ("hInstance",      ctypes.c_void_p),
        ("hIcon",          ctypes.c_void_p),
        ("hCursor",        ctypes.c_void_p),
        ("hbrBackground",  ctypes.c_void_p),
        ("lpszMenuName",   ctypes.c_wchar_p),
        ("lpszClassName",  ctypes.c_wchar_p),
    ]

class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd",     ctypes.c_void_p),
        ("message",  ctypes.c_uint),
        ("wParam",   ctypes.c_size_t),
        ("lParam",   ctypes.c_ssize_t),
        ("time",     ctypes.c_uint),
        ("pt",       ctypes.c_long * 2),
    ]

# Function prototypes — set once at import
_user32.DefWindowProcW.restype   = ctypes.c_long
_user32.DefWindowProcW.argtypes  = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]

_user32.RegisterClassW.restype   = ctypes.c_uint
_user32.RegisterClassW.argtypes  = [ctypes.POINTER(_WNDCLASSW)]

_user32.UnregisterClassW.restype  = ctypes.c_int
_user32.UnregisterClassW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]

_user32.CreateWindowExW.restype  = ctypes.c_void_p
_user32.CreateWindowExW.argtypes = [
    ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
]

_user32.SetLayeredWindowAttributes.restype = ctypes.c_int
_user32.SetLayeredWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_ubyte, ctypes.c_uint]

_user32.ShowWindow.restype   = ctypes.c_int
_user32.ShowWindow.argtypes  = [ctypes.c_void_p, ctypes.c_int]

_user32.GetMessageW.restype  = ctypes.c_int
_user32.GetMessageW.argtypes = [ctypes.POINTER(_MSG), ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]

_user32.TranslateMessage.restype  = ctypes.c_int
_user32.TranslateMessage.argtypes = [ctypes.POINTER(_MSG)]

_user32.DispatchMessageW.restype  = ctypes.c_long
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(_MSG)]

_user32.GetSystemMetrics.restype  = ctypes.c_int
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]

_user32.DestroyWindow.restype  = ctypes.c_int
_user32.DestroyWindow.argtypes = [ctypes.c_void_p]

_user32.PostMessageW.restype   = ctypes.c_int
_user32.PostMessageW.argtypes  = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]

_user32.SetWindowPos.restype   = ctypes.c_int
_user32.SetWindowPos.argtypes  = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_uint]

_user32.PostQuitMessage.restype = None
_user32.PostQuitMessage.argtypes = [ctypes.c_int]

_kernel32.GetModuleHandleW.restype  = ctypes.c_void_p
_kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]

_kernel32.CreateMutexW.restype   = ctypes.c_void_p
_kernel32.CreateMutexW.argtypes  = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]

_kernel32.GetLastError.restype = ctypes.c_uint

_gdi32.CreateSolidBrush.restype  = ctypes.c_void_p
_gdi32.CreateSolidBrush.argtypes = [ctypes.c_uint]


# ── Configuration ───────────────────────────────────────────────────────────
def _clamp(v: float, lo: float = 0.0, hi: float = 255.0) -> float:
    return max(lo, min(hi, v))

def _clamp_int(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(v)))

def _nearest(value: int, choices: Sequence[int]) -> int:
    return min(choices, key=lambda c: abs(c - value))

def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


@dataclass(slots=True)
class AppConfig:
    """Validated, serialisable application configuration."""
    enabled: bool = True
    brightness: int = 100          # virtual brightness (–30 … 100)
    warmth_k: int = 4000
    run_at_startup: bool = False
    smooth_transitions: bool = True
    circadian_enabled: bool = False
    circadian_day_k: int = 6500
    circadian_night_k: int = 3000
    circadian_day_hour: int = 7
    circadian_night_hour: int = 21

    def __post_init__(self) -> None:
        self.brightness = _clamp_int(self.brightness, MIN_BRIGHTNESS, MAX_BRIGHTNESS)
        self.warmth_k = _nearest(self.warmth_k, WARMTH_LEVELS_K)
        self.circadian_day_k = _nearest(self.circadian_day_k, WARMTH_LEVELS_K)
        self.circadian_night_k = _nearest(self.circadian_night_k, WARMTH_LEVELS_K)
        self.circadian_day_hour = _clamp_int(self.circadian_day_hour, 0, 23)
        self.circadian_night_hour = _clamp_int(self.circadian_night_hour, 0, 23)

    @classmethod
    def load(cls) -> "AppConfig":
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
        tmp.replace(CONFIG_PATH)
        log.debug("Configuration saved.")

    def reset(self) -> None:
        self.__init__()  # type: ignore[misc]
        self.save()
        log.info("Configuration reset to defaults.")


# ── Kelvin → RGB (Tanner Helland blackbody approximation) ──────────────────
def kelvin_to_rgb(kelvin: int) -> tuple[float, float, float]:
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


# ── Gamma Ramp ──────────────────────────────────────────────────────────────
_WORD = ctypes.c_ushort
_GammaRampArray = _WORD * 256

class _GammaRamp(ctypes.Structure):
    _fields_ = [
        ("Red",   _GammaRampArray),
        ("Green", _GammaRampArray),
        ("Blue",  _GammaRampArray),
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
        rf, gf, bf = r / _REF_R, g / _REF_G, b / _REF_B
        ramp = _GammaRamp()
        for i in range(256):
            base = i * 257  # identity: 0→0, 255→65535
            ramp.Red[i]   = int(_clamp(base * rf, 0, 65535))
            ramp.Green[i] = int(_clamp(base * gf, 0, 65535))
            ramp.Blue[i]  = int(_clamp(base * bf, 0, 65535))
        return ramp

_ramp_cache = _RampCache()
for _k in (*WARMTH_LEVELS_K, NEUTRAL_K):
    _ramp_cache.get(_k)


# ── Display DC (for gamma ramp) ────────────────────────────────────────────
class _DisplayDC:
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


# ── Dim overlay (click-through topmost black window) ────────────────────────
class DimOverlay:
    """A click-through, topmost, layered window that covers the entire virtual
    screen.  Used to dim the display *below* the hardware's minimum brightness
    (–10 %, –20 %, –30 %) by painting a semi-transparent black veil."""

    __slots__ = (
        "_hwnd", "_thread", "_ready", "_failed",
        "_alpha_pct", "_wndproc_ref",
    )

    def __init__(self) -> None:
        self._hwnd: int = 0
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._failed: bool = False
        self._alpha_pct: int = 0
        self._wndproc_ref: Optional[WNDPROC] = None  # keep callback alive

    # ── public API ──────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="DuskRay-Overlay",
        )
        self._thread.start()
        self._ready.wait(timeout=3.0)

    def set_alpha(self, alpha_pct: int) -> None:
        alpha_pct = _clamp_int(alpha_pct, 0, MAX_OVERLAY_ALPHA_PCT)
        self._alpha_pct = alpha_pct
        if self._failed or not self._hwnd:
            return
        alpha_byte = int(alpha_pct * 255 / 100)
        _user32.SetLayeredWindowAttributes(self._hwnd, 0, alpha_byte, LWA_ALPHA)

    @property
    def alpha(self) -> int:
        return self._alpha_pct

    @property
    def available(self) -> bool:
        return not self._failed and bool(self._hwnd)

    def stop(self) -> None:
        if self._hwnd:
            _user32.PostMessageW(self._hwnd, WM_USER_CLOSE, 0, 0)
            if self._thread:
                self._thread.join(timeout=2.0)
            self._hwnd = 0

    # ── internal ────────────────────────────────────────────────────────────
    def _run(self) -> None:
        try:
            hinst = _kernel32.GetModuleHandleW(None)

            # Keep a strong reference so the GC does not free the callback
            self._wndproc_ref = WNDPROC(self._wndproc)

            wc = _WNDCLASSW()
            wc.style = CS_HREDRAW | CS_VREDRAW
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = hinst
            wc.hbrBackground = _gdi32.CreateSolidBrush(0x00000000)  # black
            wc.lpszClassName = "DuskRayOverlayClass"

            atom = _user32.RegisterClassW(ctypes.byref(wc))
            if not atom:
                err = _kernel32.GetLastError()
                log.error("RegisterClassW failed (err=%d)", err)
                self._failed = True
                self._ready.set()
                return

            x = _user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
            y = _user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
            w = _user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
            h = _user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)

            ex_style = (WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
                        | WS_EX_TOPMOST | WS_EX_NOACTIVATE)

            hwnd = _user32.CreateWindowExW(
                ex_style, "DuskRayOverlayClass", "DuskRayOverlay",
                WS_POPUP, x, y, w, h, None, None, hinst, None,
            )
            if not hwnd:
                err = _kernel32.GetLastError()
                log.error("CreateWindowExW failed (err=%d)", err)
                self._failed = True
                self._ready.set()
                return

            self._hwnd = hwnd
            _user32.SetLayeredWindowAttributes(hwnd, 0, 0, LWA_ALPHA)
            _user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)

            log.info("Overlay window created (%dx%d @ %d,%d)", w, h, x, y)
            self._ready.set()

            # Message pump
            msg = _MSG()
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))

            # Pump exited — destroy window if still alive
            if self._hwnd:
                _user32.DestroyWindow(self._hwnd)
                self._hwnd = 0

            _user32.UnregisterClassW("DuskRayOverlayClass", hinst)
            log.info("Overlay window destroyed.")

        except Exception:
            log.exception("Overlay thread crashed")
            self._failed = True
            self._ready.set()

    def _wndproc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == WM_DISPLAYCHANGE:
            # Monitor layout changed — resize overlay to cover all displays
            x = _user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
            y = _user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
            w = _user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
            h = _user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
            _user32.SetWindowPos(
                hwnd, HWND_TOPMOST, x, y, w, h,
                SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
            log.debug("Overlay resized after display change (%dx%d)", w, h)
            return 0

        if msg == WM_NCHITTEST:
            return HTTRANSPARENT  # click-through

        if msg == WM_USER_CLOSE:
            _user32.DestroyWindow(hwnd)
            return 0

        if msg == WM_DESTROY:
            _user32.PostQuitMessage(0)
            return 0

        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)


# ── Brightness controller (hardware + overlay) ──────────────────────────────
class BrightnessController:
    """Unified controller for hardware brightness and overlay dimming.

    Virtual brightness range: –30 … 100
      * ≥ 0 : set hardware brightness to *virtual* %, overlay = 0
      * < 0 : set hardware to 0 %, overlay alpha = |virtual| %
    """

    __slots__ = ("_overlay", "_hw_available", "_current_virtual")

    def __init__(self) -> None:
        self._overlay = DimOverlay()
        self._overlay.start()
        self._hw_available = self._detect_hardware()
        self._current_virtual: int = NEUTRAL_BRIGHTNESS
        log.info(
            "Brightness controller ready — hardware control: %s, overlay: %s",
            "available" if self._hw_available else "unavailable",
            "available" if self._overlay.available else "unavailable",
        )

    def _detect_hardware(self) -> bool:
        if not _SBC_AVAILABLE:
            return False
        try:
            vals = sbc.get_brightness()  # type: ignore[union-attr]
            return bool(vals)
        except Exception:
            return False

    def apply(self, virtual: int) -> None:
        virtual = _clamp_int(virtual, MIN_BRIGHTNESS, MAX_BRIGHTNESS)
        self._current_virtual = virtual

        overlay_pct = 0

        if self._hw_available:
            hw_pct = max(0, virtual)
            overlay_pct = max(0, -virtual)
            try:
                sbc.set_brightness(hw_pct)  # type: ignore[union-attr]
            except Exception as exc:
                log.warning("Hardware brightness failed (%s); using overlay.", exc)
                overlay_pct = max(0, 100 - virtual)
        else:
            # No hardware control — use overlay for the full range
            overlay_pct = max(0, 100 - virtual)

        if self._overlay.available:
            self._overlay.set_alpha(overlay_pct)

    @property
    def current_virtual(self) -> int:
        return self._current_virtual

    def get_hardware_brightness(self) -> int:
        if not self._hw_available:
            return NEUTRAL_BRIGHTNESS
        try:
            vals = sbc.get_brightness()  # type: ignore[union-attr]
            return int(vals[0]) if vals else NEUTRAL_BRIGHTNESS
        except Exception:
            return NEUTRAL_BRIGHTNESS

    def shutdown(self) -> None:
        self._overlay.set_alpha(0)
        self._overlay.stop()
        if self._hw_available:
            try:
                sbc.set_brightness(NEUTRAL_BRIGHTNESS)  # type: ignore[union-attr]
            except Exception:
                pass


# Module-level singleton (initialised in main)
_brightness_ctrl: Optional[BrightnessController] = None

def init_brightness_controller() -> None:
    global _brightness_ctrl
    if _brightness_ctrl is None:
        _brightness_ctrl = BrightnessController()

def apply_brightness(virtual: int) -> None:
    if _brightness_ctrl:
        _brightness_ctrl.apply(virtual)

def get_current_brightness() -> int:
    if _brightness_ctrl:
        return _brightness_ctrl.current_virtual
    return NEUTRAL_BRIGHTNESS


# ── Smooth transition engine ───────────────────────────────────────────────
class _TransitionEngine:
    """Animate brightness and/or warmth from current → target values."""
    __slots__ = ("_lock", "_thread", "_cancel")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()

    def animate(self, fb: int, tb: int, fk: int, tk: int,
                duration: float = TRANSITION_DURATION,
                fps: int = TRANSITION_FPS) -> None:
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

    def _run(self, fb: int, tb: int, fk: int, tk: int,
             duration: float, fps: int) -> None:
        steps = max(1, int(duration * fps))
        interval = duration / steps
        start_time = time.perf_counter()

        for step in range(1, steps + 1):
            if self._cancel.is_set():
                return

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
        self._thread: Optional[threading.Thread] = None

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
        return f'"{sys.executable}" --startup'
    pythonw = Path(sys.executable).parent / "pythonw.exe"
    interpreter = str(pythonw) if pythonw.exists() else sys.executable
    script = os.path.abspath(__file__)
    return f'"{interpreter}" "{script}" --startup'

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


# ── Single-instance guard ──────────────────────────────────────────────────
_singleton_mutex: Optional[int] = None

def _ensure_single_instance() -> bool:
    global _singleton_mutex
    _singleton_mutex = _kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if _kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        log.warning("Another instance of %s is already running.", APP_NAME)
        return False
    return True


# ── Tray icon image generator ──────────────────────────────────────────────
_ICON_CACHE: dict[str, Image.Image] = {}

def _generate_icon_image(state: str, size: int = 64) -> Image.Image:
    """Render an anti-aliased sun (active), dim sun (sub-zero), or moon (disabled)."""
    scale = 2
    big = size * scale
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, cy = big // 2, big // 2

    if state == "disabled":
        body_color = (150, 150, 165, 255)
        r_outer = int(big * 0.34)
        r_cut = int(big * 0.28)
        offset = int(big * 0.12)
        d.ellipse((cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer), fill=body_color)
        d.ellipse((cx - r_cut + offset, cy - r_cut - offset,
                   cx + r_cut + offset, cy + r_cut - offset), fill=(0, 0, 0, 0))
    else:
        if state == "dim":
            body_color = (200, 120, 30, 255)
            ray_color = (220, 150, 50, 255)
        else:
            body_color = (255, 160, 40, 255)
            ray_color = (255, 190, 60, 255)

        r_body = int(big * 0.28)
        d.ellipse((cx - r_body, cy - r_body, cx + r_body, cy + r_body), fill=body_color)

        ray_inner = int(big * 0.34)
        ray_outer = int(big * 0.46)
        ray_width = max(2, scale * 2)

        for angle in range(0, 360, 45):
            rad = math.radians(angle)
            cos_a, sin_a = math.cos(rad), math.sin(rad)
            x1 = cx + cos_a * ray_inner
            y1 = cy + sin_a * ray_inner
            x2 = cx + cos_a * ray_outer
            y2 = cy + sin_a * ray_outer
            d.line((x1, y1, x2, y2), fill=ray_color, width=ray_width)

    return img.resize((size, size), Image.LANCZOS)

def get_icon_image(enabled: bool, brightness: int) -> Image.Image:
    if enabled:
        state = "dim" if brightness < 0 else "active"
    else:
        state = "disabled"
    if state not in _ICON_CACHE:
        _ICON_CACHE[state] = _generate_icon_image(state)
    return _ICON_CACHE[state]


# ── Main application class ─────────────────────────────────────────────────
class DuskRayApp:
    """Orchestrates config, display control, transitions, and the tray icon."""
    __slots__ = ("config", "_lock", "_icon", "_circadian", "_menu_cache")

    def __init__(self) -> None:
        self.config = AppConfig.load()
        self._lock = threading.Lock()
        self._icon: Optional[pystray.Icon] = None
        self._circadian: Optional[CircadianScheduler] = None
        self._menu_cache: Optional[pystray.Menu] = None

    # ── apply state to display ──────────────────────────────────────────────
    def apply_all(self, animate: bool = True) -> None:
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

    # ── user actions ────────────────────────────────────────────────────────
    def set_brightness(self, level: int) -> None:
        old = self.config.brightness
        self._update_config(brightness=level)
        if self.config.enabled and self.config.smooth_transitions:
            _transition.animate(old, level, self.config.warmth_k, self.config.warmth_k, duration=0.3)
        else:
            self.apply_all(animate=False)
        self._refresh_icon()
        self._update_tooltip()

    def set_warmth(self, kelvin: int) -> None:
        old = self.config.warmth_k
        self._update_config(warmth_k=kelvin)
        if self.config.enabled and self.config.smooth_transitions:
            _transition.animate(
                self.config.brightness, self.config.brightness, old, kelvin, duration=0.4,
            )
        else:
            self.apply_all(animate=False)
        self._update_tooltip()

    def toggle_enabled(self) -> None:
        new_state = not self.config.enabled
        self._update_config(enabled=new_state)
        self.apply_all()
        self._refresh_icon()
        self._update_tooltip()

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

    def reset_to_defaults(self) -> None:
        self.config.reset()
        self.apply_all(animate=False)
        self._refresh_icon()
        self._update_tooltip()
        if self._icon:
            self._icon.update_menu()

    # ── circadian ───────────────────────────────────────────────────────────
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

    # ── icon & tooltip ─────────────────────────────────────────────────────
    def _refresh_icon(self) -> None:
        if self._icon:
            self._icon.icon = get_icon_image(self.config.enabled, self.config.brightness)

    def _update_tooltip(self) -> None:
        if self._icon:
            state = "ON" if self.config.enabled else "OFF"
            self._icon.title = (
                f"{state}  |  "
                f"Brightness {self.config.brightness}%  |  "
                f"Warmth {self.config.warmth_k}K"
            )

    # ── menu callbacks ──────────────────────────────────────────────────────
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

    def _cb_reset(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self.reset_to_defaults()

    def _cb_about(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        icon.notify(
            f"{APP_NAME} v{APP_VERSION}",
            f"Brightness: {self.config.brightness}%\n"
            f"Warmth: {self.config.warmth_k}K\n"
            f"Circadian: {'On' if self.config.circadian_enabled else 'Off'}\n"
            f"Smooth: {'On' if self.config.smooth_transitions else 'Off'}\n"
            f"Startup: {'On' if self.config.run_at_startup else 'Off'}",
        )

    def _cb_quit(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        log.info("Shutting down…")
        self._stop_circadian()
        reset_warmth()
        if _brightness_ctrl:
            _brightness_ctrl.shutdown()
        icon.stop()

    def _cb_noop(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        pass

    # ── build tray menu ─────────────────────────────────────────────────────
    def _build_menu(self) -> pystray.Menu:
        if self._menu_cache:
            return self._menu_cache

        brightness_items = tuple(
            pystray.MenuItem(
                f"{lvl}%",
                self._cb_brightness(lvl),
                checked=lambda item, lvl=lvl: self.config.brightness == lvl,
                radio=True,
            )
            for lvl in BRIGHTNESS_LEVELS
        )
        warmth_items = tuple(
            pystray.MenuItem(
                f"{k}K",
                self._cb_warmth(k),
                checked=lambda item, k=k: self.config.warmth_k == k,
                radio=True,
            )
            for k in WARMTH_LEVELS_K
        )
        day_hour_items = tuple(
            pystray.MenuItem(
                f"{h:02d}:00",
                self._cb_circadian_day_hour(h),
                checked=lambda item, h=h: self.config.circadian_day_hour == h,
                radio=True,
            )
            for h in HOURS_24
        )
        night_hour_items = tuple(
            pystray.MenuItem(
                f"{h:02d}:00",
                self._cb_circadian_night_hour(h),
                checked=lambda item, h=h: self.config.circadian_night_hour == h,
                radio=True,
            )
            for h in HOURS_24
        )
        circ_day_k_items = tuple(
            pystray.MenuItem(
                f"{k}K",
                self._cb_circadian_day_k(k),
                checked=lambda item, k=k: self.config.circadian_day_k == k,
                radio=True,
            )
            for k in WARMTH_LEVELS_K
        )
        circ_night_k_items = tuple(
            pystray.MenuItem(
                f"{k}K",
                self._cb_circadian_night_k(k),
                checked=lambda item, k=k: self.config.circadian_night_k == k,
                radio=True,
            )
            for k in WARMTH_LEVELS_K
        )

        circadian_settings_menu = pystray.Menu(
            pystray.MenuItem("Day Start",   pystray.Menu(*day_hour_items)),
            pystray.MenuItem("Night Start", pystray.Menu(*night_hour_items)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Day Warmth",   pystray.Menu(*circ_day_k_items)),
            pystray.MenuItem("Night Warmth", pystray.Menu(*circ_night_k_items)),
        )

        self._menu_cache = pystray.Menu(
            # Status line (dynamic, non-clickable)
            pystray.MenuItem(
                lambda item: (
                    f"Brightness: {self.config.brightness}%   |   "
                    f"Warmth: {self.config.warmth_k}K"
                ),
                self._cb_noop,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Brightness", pystray.Menu(*brightness_items)),
            pystray.MenuItem("Warmth",     pystray.Menu(*warmth_items)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Enabled",
                self._cb_toggle_enabled,
                checked=lambda item: self.config.enabled,
                default=True,
            ),
            pystray.MenuItem(
                "Smooth transitions",
                self._cb_toggle_smooth,
                checked=lambda item: self.config.smooth_transitions,
            ),
            pystray.MenuItem(
                "Circadian mode",
                self._cb_toggle_circadian,
                checked=lambda item: self.config.circadian_enabled,
            ),
            pystray.MenuItem("Circadian Settings", circadian_settings_menu),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Run at startup",
                self._cb_toggle_startup,
                checked=lambda item: self.config.run_at_startup,
            ),
            pystray.MenuItem("Reset to defaults", self._cb_reset),
            pystray.MenuItem("About", self._cb_about),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self._cb_quit),
        )
        return self._menu_cache

    # ── run ─────────────────────────────────────────────────────────────────
    def run(self, startup_mode: bool = False) -> None:
        log.info("%s v%s starting…", APP_NAME, APP_VERSION)

        # Safe-startup: ensure at least 10 % brightness when launched at OS boot
        if startup_mode and self.config.brightness < SAFE_STARTUP_BRIGHTNESS:
            log.info(
                "Startup safe-mode: brightness was %d%% — bumping to %d%% for visibility.",
                self.config.brightness,
                SAFE_STARTUP_BRIGHTNESS,
            )
            self.config.brightness = SAFE_STARTUP_BRIGHTNESS
            # Note: intentionally NOT saved — user's preference is preserved.

        if self.config.run_at_startup:
            set_startup(True)

        # Initialise brightness controller (hardware + overlay)
        init_brightness_controller()

        if self.config.circadian_enabled:
            self._start_circadian()

        self.apply_all(animate=False)

        self._icon = pystray.Icon(
            APP_NAME,
            get_icon_image(self.config.enabled, self.config.brightness),
            f"{APP_NAME} v{APP_VERSION}",
            self._build_menu(),
        )
        self._update_tooltip()

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
        if _brightness_ctrl:
            _brightness_ctrl.shutdown()
        log.info("Cleanup complete. Goodbye.")


# ── CLI entry point ─────────────────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Display Brightness & Warmth Control",
    )
    parser.add_argument("--reset", action="store_true",
                        help="Restore factory defaults and exit.")
    parser.add_argument("--startup", action="store_true",
                        help="Indicates the app was launched at OS startup "
                             "(enforces safe minimum brightness).")
    parser.add_argument("--version", action="version",
                        version=f"{APP_NAME} {APP_VERSION}")
    return parser.parse_args()

def main() -> None:
    try:
        args = _parse_args()

        if args.reset:
            cfg = AppConfig()
            cfg.save()
            # Also restore display
            init_brightness_controller()
            reset_warmth()
            apply_brightness(NEUTRAL_BRIGHTNESS)
            if _brightness_ctrl:
                _brightness_ctrl.shutdown()
            print(f"{APP_NAME}: configuration reset to defaults.")
            return

        if not _ensure_single_instance():
            print(f"{APP_NAME}: another instance is already running.")
            return

        app = DuskRayApp()
        app.run(startup_mode=args.startup)

    except Exception:
        log.exception("Fatal error in main()")
        raise


if __name__ == "__main__":
    main()
