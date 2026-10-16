"""Unit tests for DuskRay — run with:  python -m unittest discover -s tests -v

Device access is replaced by fakes, so the suite runs headless on any OS.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("PYSTRAY_BACKEND", "dummy")
_tmp_cfg = tempfile.mkdtemp(prefix="duskray-test-")
os.environ["DUSKRAY_CONFIG_DIR"] = _tmp_cfg
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duskray as dr  # noqa: E402

dr.init_logging._done = True  # keep expected warnings out of test output


# ── fakes ───────────────────────────────────────────────────────────────────
class FakeGamma:
    def __init__(self):
        self.calls = []
        self.refreshed = 0
        self.closed = False

    def apply(self, k):
        self.calls.append(dr.quantize_kelvin(k))
        return True

    def reset(self):
        return self.apply(dr.NEUTRAL_K)

    def refresh_devices(self):
        self.refreshed += 1

    def close(self):
        self.closed = True


class FakeOverlay:
    available = True

    def __init__(self):
        self.alphas = []
        self.stopped = False

    @property
    def alpha(self):
        return self.alphas[-1] if self.alphas else 0

    def start(self, timeout=0):
        pass

    def set_alpha(self, a):
        self.alphas.append(a)

    def stop(self):
        self.stopped = True


class FakeHW:
    def __init__(self, state=dr.HardwareBrightness.AVAILABLE):
        self.state = state
        self.requests = []
        self.final = None

    def request(self, pct, force=False):
        self.requests.append((pct, force))

    def stop(self, final_pct=None, timeout=0):
        self.final = final_pct


class FakeSBC:
    def __init__(self, delay=0.0, fail_get=False, fail_set=False, initial=50):
        self.delay, self.fail_get, self.fail_set = delay, fail_get, fail_set
        self.value = initial
        self.sets = []
        self.lock = threading.Lock()

    def get_brightness(self):
        if self.fail_get:
            raise RuntimeError("no monitors")
        return [self.value]

    def set_brightness(self, v):
        time.sleep(self.delay)
        if self.fail_set:
            raise RuntimeError("DDC timeout")
        with self.lock:
            self.sets.append(v)
            self.value = v


def wait_for(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


# ── configuration ───────────────────────────────────────────────────────────
class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "config.json"

    def test_defaults(self):
        c = dr.AppConfig()
        self.assertTrue(c.enabled and c.warmth_enabled)
        self.assertEqual((c.brightness, c.warmth_k), (100, 4000))
        self.assertEqual(c.click_action, dr.CLICK_TOGGLE_ALL)

    def test_roundtrip(self):
        c = dr.AppConfig(brightness=-20, warmth_k=3000, warmth_enabled=False)
        self.assertTrue(c.save(self.path))
        self.assertEqual(dr.AppConfig.load(self.path), c)
        self.assertFalse((self.dir / "config.tmp").exists())

    def test_old_config_without_new_keys_loads(self):
        old = {"enabled": False, "brightness": 40, "warmth_k": 3500, "run_at_startup": True,
               "smooth_transitions": True, "circadian_enabled": False, "circadian_day_k": 6500,
               "circadian_night_k": 3000, "circadian_day_hour": 7, "circadian_night_hour": 21}
        self.path.write_text(json.dumps(old))
        c = dr.AppConfig.load(self.path)
        self.assertEqual((c.enabled, c.brightness, c.warmth_k), (False, 40, 3500))
        self.assertTrue(c.warmth_enabled)  # new option defaults to on

    def test_invalid_field_only_resets_that_field(self):
        self.path.write_text(json.dumps({"brightness": "abc", "warmth_k": 3000,
                                         "enabled": "maybe", "click_action": "explode",
                                         "unknown_key": 1, "circadian_day_hour": float("nan")
                                         if False else 99}))
        c = dr.AppConfig.load(self.path)
        self.assertEqual(c.brightness, 100)
        self.assertEqual(c.warmth_k, 3000)
        self.assertTrue(c.enabled)
        self.assertEqual(c.click_action, dr.CLICK_TOGGLE_ALL)
        self.assertEqual(c.circadian_day_hour, 23)

    def test_clamping_and_coercion(self):
        c = dr.AppConfig(brightness=500, warmth_k=100, circadian_night_k=99999,
                         enabled="false", smooth_transitions=0, circadian_fade_minutes=-5)
        self.assertEqual((c.brightness, c.warmth_k, c.circadian_night_k), (100, 2000, 6500))
        self.assertFalse(c.enabled)
        self.assertFalse(c.smooth_transitions)
        self.assertEqual(c.circadian_fade_minutes, 0)
        self.assertEqual(dr.AppConfig(brightness=True).brightness, 100)  # bool is not a number

    def test_corrupt_file_is_backed_up(self):
        self.path.write_text("{not json")
        c = dr.AppConfig.load(self.path)
        self.assertEqual(c, dr.AppConfig())
        self.assertTrue((self.dir / "config.corrupt.json").exists())

    def test_non_object_root(self):
        self.path.write_text("[1,2,3]")
        self.assertEqual(dr.AppConfig.load(self.path), dr.AppConfig())

    def test_missing_file(self):
        self.assertEqual(dr.AppConfig.load(self.dir / "nope.json"), dr.AppConfig())

    def test_save_failure_does_not_raise(self):
        blocker = self.dir / "file"
        blocker.write_text("x")
        self.assertFalse(dr.AppConfig().save(blocker / "sub" / "config.json"))


# ── colour maths ────────────────────────────────────────────────────────────
class ColourTests(unittest.TestCase):
    def test_neutral_is_identity(self):
        self.assertEqual(dr.kelvin_factors(6500), (1.0, 1.0, 1.0))
        r, g, b = dr.build_ramp_channels(6500)
        self.assertEqual(r, [i * 257 for i in range(256)])
        self.assertEqual(g, r)
        self.assertEqual(b, r)

    def test_warmer_reduces_blue_then_green(self):
        prev = dr.kelvin_factors(6500)
        for k in dr.WARMTH_LEVELS_K[1:]:
            f = dr.kelvin_factors(k)
            self.assertEqual(f[0], 1.0)
            self.assertLessEqual(f[1], prev[1])
            self.assertLess(f[2], prev[2])
            prev = f

    def test_ramps_are_valid_and_monotonic(self):
        for k in range(2000, 6501, 10):
            for ch in dr.build_ramp_channels(k):
                self.assertEqual(len(ch), 256)
                self.assertTrue(all(0 <= v <= 65535 for v in ch))
                self.assertTrue(all(a <= b for a, b in zip(ch, ch[1:])))

    def test_quantize(self):
        self.assertEqual(dr.quantize_kelvin(3004), 3000)
        self.assertEqual(dr.quantize_kelvin(3006), 3010)
        self.assertEqual(dr.quantize_kelvin(10), dr.MIN_K)
        self.assertEqual(dr.quantize_kelvin(9000), dr.MAX_K)

    def test_ramp_cache_lru_bounded(self):
        cache = dr.RampCache(size=4)
        first = cache.get(3000)
        self.assertIs(cache.get(3003), first)  # same quantised key
        for k in (3100, 3200, 3300, 3400, 3500):
            cache.get(k)
        self.assertEqual(len(cache), 4)
        self.assertEqual(list(first.Red)[255], int(255 * 257))  # red channel untouched


# ── circadian maths ─────────────────────────────────────────────────────────
def original_target(day_h, night_h, day_k, night_k, hour):
    """Exact v1.2 logic, used to prove behaviour is preserved with fade=0."""
    if day_h < night_h:
        return day_k if day_h <= hour < night_h else night_k
    return night_k if night_h <= hour < day_h else day_k


class CircadianTests(unittest.TestCase):
    def test_matches_original_without_fade(self):
        for day_h in range(24):
            for night_h in range(24):
                for hour in range(24):
                    for minute in (0, 30, 59):
                        got = dr.circadian_kelvin(day_h, night_h, 6500, 3000, 0, hour * 60 + minute)
                        self.assertEqual(got, original_target(day_h, night_h, 6500, 3000, hour),
                                         (day_h, night_h, hour, minute))

    def test_fade(self):
        f = lambda m: dr.circadian_kelvin(7, 21, 6500, 3000, 30, m)  # noqa: E731
        self.assertEqual(f(7 * 60), 3000)            # fade starts at boundary
        self.assertEqual(f(7 * 60 + 15), 4750)       # halfway
        self.assertEqual(f(7 * 60 + 30), 6500)
        self.assertEqual(f(21 * 60 + 15), 4750)
        self.assertEqual(f(3 * 60), 3000)

    def test_fade_across_midnight(self):
        f = lambda m: dr.circadian_kelvin(6, 23, 6500, 2000, 60, m)  # noqa: E731
        self.assertEqual(f(23 * 60 + 30), 4250)
        self.assertEqual(f(0 * 60 + 30), 2000)

    def test_fade_clipped_to_period(self):
        # 1-hour night, 3-hour fade → fade lasts the whole night but never overshoots
        vals = [dr.circadian_kelvin(8, 7, 6500, 3000, 180, m) for m in range(7 * 60, 8 * 60)]
        self.assertTrue(all(3000 <= v <= 6500 for v in vals))
        self.assertEqual(dr.circadian_kelvin(8, 7, 6500, 3000, 180, 8 * 60 + 600), 6500)


# ── target computation (the warmth/brightness independence) ─────────────────
class TargetTests(unittest.TestCase):
    def test_warmth_disabled_keeps_brightness(self):
        c = dr.AppConfig(brightness=-20, warmth_k=3000, warmth_enabled=False)
        self.assertEqual(dr.compute_targets(c, 0), (-20, dr.NEUTRAL_K))

    def test_master_off_restores_everything(self):
        c = dr.AppConfig(enabled=False, brightness=30, warmth_k=3000)
        self.assertEqual(dr.compute_targets(c, 0), (100, 6500))

    def test_normal_and_override(self):
        c = dr.AppConfig(brightness=-30, warmth_k=3500)
        self.assertEqual(dr.compute_targets(c, 0), (-30, 3500))
        self.assertEqual(dr.compute_targets(c, 0, brightness_override=10), (10, 3500))

    def test_circadian_target(self):
        c = dr.AppConfig(circadian_enabled=True, circadian_fade_minutes=0)
        self.assertEqual(dr.compute_targets(c, 12 * 60)[1], 6500)
        self.assertEqual(dr.compute_targets(c, 23 * 60)[1], 3000)


# ── device mapping ──────────────────────────────────────────────────────────
class DisplayOutputTests(unittest.TestCase):
    def make(self, state=dr.HardwareBrightness.AVAILABLE):
        g, h, o = FakeGamma(), FakeHW(state), FakeOverlay()
        return dr.DisplayOutput(g, h, o), g, h, o

    def test_split(self):
        self.assertEqual(dr.DisplayOutput.split(-20, True), (0, 20))
        self.assertEqual(dr.DisplayOutput.split(40, True), (40, 0))
        self.assertEqual(dr.DisplayOutput.split(40, False), (None, 60))

    def test_dedup(self):
        out, g, h, o = self.make()
        out.apply(50.2, 3001)
        out.apply(49.8, 2999)   # same integer / quantum → no device calls
        self.assertEqual(h.requests, [(50, False)])
        self.assertEqual(g.calls, [3000])
        out.apply(50, 3000, force_brightness=True)
        self.assertEqual(h.requests[-1], (50, True))

    def test_negative_uses_overlay(self):
        out, g, h, o = self.make()
        out.apply(-30, 6500)
        self.assertEqual((h.requests[-1][0], o.alpha), (0, 30))

    def test_no_hardware_uses_overlay_for_full_range(self):
        out, g, h, o = self.make(dr.HardwareBrightness.UNAVAILABLE)
        out.apply(40, 6500)
        self.assertEqual(h.requests, [])
        self.assertEqual(o.alpha, 60)

    def test_refresh_only_reasserts_warm_ramps(self):
        out, g, h, o = self.make()
        out.apply(100, 6500)
        out.apply(100, 6500, refresh_gamma=True)
        self.assertEqual(g.calls, [6500])          # neutral not re-written
        out.apply(100, 3000)
        out.apply(100, 3000, refresh_gamma=True)
        self.assertEqual(g.calls, [6500, 3000, 3000])


# ── gamma floor fallback ────────────────────────────────────────────────────
class FloorGamma(dr.GammaController):
    def __init__(self, min_ok):
        super().__init__()
        self.min_ok = min_ok
        self.applied = []

    def _open_devices(self):
        return [("DISPLAY1", 1, True), ("DISPLAY2", 2, True)]

    def _close_devices(self, dcs):
        pass

    _sig = {}

    def _set_ramp(self, hdc, ramp):
        if not self._sig:  # signature (green[255], blue[255]) → kelvin
            for k in range(2000, 6501, 10):
                _r, g, b = dr.build_ramp_channels(k)
                self._sig.setdefault((g[255], b[255]), k)
        k = self._sig[(ramp.Green[255], ramp.Blue[255])]
        ok = k >= self.min_ok
        if ok:
            self.applied.append((hdc, k))
        return ok


class GammaControllerTests(unittest.TestCase):
    def test_applies_to_every_display(self):
        g = FloorGamma(2000)
        self.assertTrue(g.apply(3000))
        self.assertEqual(g.applied, [(1, 3000), (2, 3000)])

    def test_floor_search_and_reuse(self):
        g = FloorGamma(3400)
        self.assertTrue(g.apply(2000))
        self.assertEqual(g.floor_k, 3500)
        g.applied.clear()
        self.assertTrue(g.apply(2500))       # clamped straight to the floor
        self.assertEqual(g.applied, [(1, 3500), (2, 3500)])
        self.assertTrue(g.apply(5000))
        g.refresh_devices()
        self.assertEqual(g.floor_k, dr.MIN_K)  # re-probe after display change

    def test_neutral_failure(self):
        g = FloorGamma(9999)
        self.assertFalse(g.apply(3000))


# ── hardware brightness worker ──────────────────────────────────────────────
class HardwareTests(unittest.TestCase):
    def test_coalesces_and_never_blocks_caller(self):
        sbc = FakeSBC(delay=0.05)
        hw = dr.HardwareBrightness(backend=sbc, detect_delays=(0,))
        self.assertTrue(wait_for(lambda: hw.state == hw.AVAILABLE))
        t0 = time.monotonic()
        for v in range(100):
            hw.request(v)
        self.assertLess(time.monotonic() - t0, 0.05)
        self.assertTrue(wait_for(lambda: sbc.value == 99))
        self.assertLess(len(sbc.sets), 10)        # intermediate values dropped
        hw.stop(final_pct=100)
        self.assertEqual(sbc.sets[-1], 100)

    def test_skip_same_value_unless_forced(self):
        sbc = FakeSBC(initial=40)
        hw = dr.HardwareBrightness(backend=sbc, detect_delays=(0,))
        wait_for(lambda: hw.state == hw.AVAILABLE)
        hw.request(40)
        time.sleep(0.05)
        self.assertEqual(sbc.sets, [])
        hw.request(40, force=True)
        self.assertTrue(wait_for(lambda: sbc.sets == [40]))
        hw.stop()

    def test_unavailable_when_detection_fails(self):
        states = []
        hw = dr.HardwareBrightness(on_state_change=states.append,
                                   backend=FakeSBC(fail_get=True), detect_delays=(0, 0.01))
        self.assertTrue(wait_for(lambda: states == [hw.UNAVAILABLE]))
        hw.request(10)
        hw.stop(final_pct=100)

    def test_repeated_failures_mark_unavailable(self):
        states = []
        hw = dr.HardwareBrightness(on_state_change=states.append,
                                   backend=FakeSBC(fail_set=True), detect_delays=(0,))
        wait_for(lambda: hw.state == hw.AVAILABLE)
        for v in (10, 20, 30):
            hw.request(v)
            time.sleep(0.03)
        self.assertTrue(wait_for(lambda: hw.state == hw.UNAVAILABLE))
        self.assertEqual(states, [hw.AVAILABLE, hw.UNAVAILABLE])

    def test_stop_during_slow_detection_still_restores(self):
        sbc = FakeSBC(initial=10)
        hw = dr.HardwareBrightness(backend=sbc, detect_delays=(5.0,))  # long boot-time wait
        t0 = time.monotonic()
        hw.stop(final_pct=100, timeout=3)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(sbc.sets, [100])


# ── animation engine ────────────────────────────────────────────────────────
class EngineTests(unittest.TestCase):
    def setUp(self):
        self.frames = []
        self.lock = threading.Lock()

    def output(self, b, k, force_brightness=False, refresh_gamma=False):
        with self.lock:
            self.frames.append((b, k, force_brightness, refresh_gamma))

    def test_instant(self):
        e = dr.DisplayEngine(self.output)
        e.set_target(40, 3000, 0.0, force_brightness=True)
        self.assertTrue(e.wait_idle())
        self.assertEqual(self.frames, [(40.0, 3000.0, True, False)])
        e.stop()

    def test_animation_reaches_target_monotonically(self):
        e = dr.DisplayEngine(self.output, fps=100)
        e.set_target(0, 3000, 0.2)
        self.assertTrue(e.wait_idle())
        ks = [f[1] for f in self.frames]
        self.assertGreater(len(ks), 5)
        self.assertEqual(self.frames[-1][:2], (0.0, 3000.0))
        self.assertTrue(all(a >= b for a, b in zip(ks, ks[1:])))
        e.stop()

    def test_retarget_continues_from_current_frame(self):
        """v1.2 restarted every toggle from 6500 K → visible flash. Not anymore."""
        e = dr.DisplayEngine(self.output, fps=100, start_b=100, start_k=3000)
        e.set_target(100, 6500, 0.3)
        time.sleep(0.15)
        mid = e.current[1]
        e.set_target(100, 3000, 0.3)
        self.assertTrue(e.wait_idle())
        with self.lock:
            after = [f[1] for f in self.frames]
        idx = max(i for i, k in enumerate(after) if abs(k - mid) < 1e-6)
        self.assertTrue(all(k <= mid + 1e-6 for k in after[idx:]))
        self.assertEqual(after[-1], 3000.0)
        e.stop()

    def test_periodic_refresh_only_when_warm(self):
        e = dr.DisplayEngine(self.output, refresh_s=0.05)
        e.set_target(100, 6500)
        time.sleep(0.25)
        self.assertFalse(any(f[3] for f in self.frames))
        e.set_target(100, 3000)
        time.sleep(0.25)
        self.assertGreaterEqual(sum(1 for f in self.frames if f[3]), 2)
        e.stop()

    def test_output_exception_does_not_kill_engine(self):
        calls = []

        def bad(b, k, **kw):
            calls.append(k)
            if len(calls) == 1:
                raise RuntimeError("driver hiccup")
        e = dr.DisplayEngine(bad)
        e.set_target(100, 3000)
        e.wait_idle()
        e.set_target(100, 4000)
        self.assertTrue(wait_for(lambda: calls[-1] == 4000))
        e.stop()


# ── application + tray menu ─────────────────────────────────────────────────
class FakeIcon:
    def __init__(self):
        self.icon = None
        self.title = ""
        self.notified = None
        self.stopped = False

    def notify(self, message, title=None):
        self.notified = (message, title)

    def stop(self):
        self.stopped = True


def find(menu, *path):
    item = None
    for label in path:
        item = next(i for i in menu.items
                    if i.text == label or i.text.startswith(label + " "))
        menu = item.submenu
    return item


class AppTests(unittest.TestCase):
    def setUp(self):
        self.g, self.h, self.o = FakeGamma(), FakeHW(), FakeOverlay()
        out = dr.DisplayOutput(self.g, self.h, self.o)
        self.minute = 12 * 60
        self.app = dr.DuskRayApp(config=dr.AppConfig(brightness=40, warmth_k=3000),
                                 output=out, persist=False, clock=lambda: self.minute)
        self.app.start_pipeline()
        self.app._icon = FakeIcon()
        self.app.apply()
        self.app._engine.wait_idle()
        self.menu = self.app.build_menu()

    def tearDown(self):
        self.app.shutdown()

    def settle(self):
        self.assertTrue(self.app._engine.wait_idle())
        return self.app._output.current

    def click(self, *path):
        find(self.menu, *path)(self.app._icon)
        return self.settle()

    def test_initial_state(self):
        self.assertEqual(self.settle(), (40, 3000))

    def test_toggle_warmth_keeps_brightness(self):
        self.assertEqual(self.click("Warmth enabled"), (40, 6500))
        self.assertFalse(self.app.config.warmth_enabled)
        self.assertEqual(self.h.requests[-1][0], 40)
        self.assertIn("Warmth: Off", self.app.status_text())
        self.assertEqual(self.app._last_ui[0], "nowarm")
        self.assertEqual(self.click("Warmth enabled"), (40, 3000))

    def test_brightness_change_while_warmth_off(self):
        self.click("Warmth enabled")
        self.assertEqual(self.click("Brightness", "-10%"), (-10, 6500))
        self.assertEqual(self.o.alpha, 10)

    def test_master_toggle(self):
        self.assertEqual(self.click("Enabled"), (100, 6500))
        self.assertEqual(self.app._last_ui[0], "disabled")
        self.assertEqual(self.click("Enabled"), (40, 3000))

    def test_click_action_follows_setting(self):
        default = lambda: next(i for i in self.menu.items if i.default).text  # noqa: E731
        self.assertEqual(default(), "Enabled")
        self.menu(self.app._icon)                    # left click → toggle all
        self.assertEqual(self.settle(), (100, 6500))
        self.menu(self.app._icon)
        self.click("Icon click", "Toggle warmth only")
        self.assertEqual(default(), "Warmth enabled")
        self.menu(self.app._icon)                    # left click → warmth only
        self.assertEqual(self.settle(), (40, 6500))
        self.assertTrue(self.app.config.enabled)

    def test_turning_warmth_on_while_off_enables_app(self):
        self.click("Enabled")
        self.click("Warmth enabled")   # warmth was on → turns off, app stays off
        self.assertEqual(self.settle(), (100, 6500))
        self.click("Warmth enabled")   # turning warmth on implies on
        self.assertTrue(self.app.config.enabled)
        self.assertEqual(self.settle(), (40, 3000))

    def test_pick_warmth_disables_circadian_and_enables_warmth(self):
        self.click("Circadian mode")
        self.assertTrue(self.app._circadian.running)
        self.assertEqual(self.settle(), (40, 6500))     # noon → day temperature
        self.click("Warmth enabled")
        self.click("Warmth", "3500K")
        self.assertFalse(self.app.config.circadian_enabled)
        self.assertTrue(self.app.config.warmth_enabled)
        self.assertFalse(self.app._circadian.running)
        self.assertEqual(self.settle(), (40, 3500))

    def test_circadian_settings_apply_immediately(self):
        self.click("Circadian mode")
        self.minute = 23 * 60
        self.app._on_circadian_tick()
        self.assertEqual(self.settle(), (40, 3000))
        self.click("Circadian settings", "Night warmth", "2500K")
        self.assertEqual(self.settle(), (40, 2500))

    def test_circadian_tick_is_idempotent(self):
        self.click("Circadian mode")
        n = len(self.g.calls)
        for _ in range(5):
            self.app._on_circadian_tick()
        self.settle()
        self.assertEqual(len(self.g.calls), n)

    def test_radio_checks(self):
        self.assertTrue(find(self.menu, "Brightness", "40%").checked)
        self.assertTrue(find(self.menu, "Warmth", "3000K").checked)
        self.assertFalse(find(self.menu, "Warmth", "3500K").checked)

    def test_safe_startup_override(self):
        self.app._brightness_override = 10
        self.app.apply()
        self.assertEqual(self.settle(), (10, 3000))
        self.assertEqual(self.app.config.brightness, 40)   # preference preserved
        self.click("Brightness", "-20%")
        self.assertIsNone(self.app._brightness_override)
        self.assertEqual(self.settle(), (-20, 3000))

    def test_reset(self):
        self.click("Warmth enabled")
        self.click("Reset to defaults")
        self.assertEqual(self.app.config, dr.AppConfig())
        self.assertEqual(self.settle(), (100, 4000))
        self.assertTrue(find(self.menu, "Brightness", "100%").checked)

    def test_menu_actions_never_raise(self):
        self.app.set_brightness = lambda lvl: 1 / 0
        menu = self.app.build_menu()
        find(menu, "Brightness", "50%")(self.app._icon)  # logged, not raised

    def test_about_and_exit(self):
        self.click("About")
        msg, title = self.app._icon.notified
        self.assertIn(dr.APP_VERSION, title)
        self.click("Exit")
        self.assertTrue(self.app._icon.stopped)
        self.assertEqual(self.g.calls[-1], 6500)
        # Exit must not request a final hardware-brightness value.
        self.assertIsNone(self.h.final)
        self.assertTrue(self.o.stopped)
        self.app.shutdown()  # idempotent

    def test_persistence(self):
        path = Path(_tmp_cfg) / "config.json"
        app = dr.DuskRayApp(config=dr.AppConfig(), output=dr.DisplayOutput(FakeGamma(), FakeHW(), FakeOverlay()),
                            persist=True)
        app.start_pipeline()
        app.toggle_warmth()
        self.assertFalse(json.loads(path.read_text())["warmth_enabled"])
        app.shutdown()

    def test_thread_safety_under_load(self):
        stop = time.monotonic() + 0.5

        def hammer(fn):
            while time.monotonic() < stop:
                fn()
        threads = [threading.Thread(target=hammer, args=(f,)) for f in (
            self.app.toggle_warmth, lambda: self.app.set_brightness(-10),
            self.app._on_circadian_tick, self.app.toggle_enabled)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        b, k = self.settle()
        self.assertEqual((b, k), dr.compute_targets(self.app.config, self.minute))


class IconTests(unittest.TestCase):
    def test_all_states_render(self):
        for state in dr.ICON_STATES:
            img = dr.render_icon(state, 64)
            self.assertEqual(img.size, (64, 64))
            self.assertIsNotNone(img.getbbox())
        self.assertIs(dr.render_icon("active"), dr.render_icon("active"))

    def test_icon_state(self):
        c = dr.AppConfig()
        self.assertEqual(dr.icon_state(c, 50), "active")
        self.assertEqual(dr.icon_state(c, -10), "dim")
        c.warmth_enabled = False
        self.assertEqual(dr.icon_state(c, 50), "nowarm")
        c.enabled = False
        self.assertEqual(dr.icon_state(c, 50), "disabled")


class CliTests(unittest.TestCase):
    def test_args(self):
        a = dr.parse_args(["--startup", "--debug"])
        self.assertTrue(a.startup and a.debug and not a.reset)

    def test_non_windows_exit_code(self):
        if not dr.IS_WINDOWS:
            self.assertEqual(dr.main([]), 2)


if __name__ == "__main__":
    unittest.main()
