# Changelog

## 1.3.0

### New
- **Warmth enabled** switch: turn warmth off without touching brightness (and back on).
- **Icon click** setting: left-click toggles everything (default) or warmth only;
  the bold menu item always shows what a click does.
- Circadian **Fade duration** (instant / 15 / 30 / 60 min) for gradual shifts.
- Gamma ramps now apply to **every attached monitor**, not only the primary one.
- Automatic re-apply after display changes, sleep/resume, unlock, and periodically
  while warm.
- Warmest-supported fallback when Windows rejects strong ramps, with a fix hint.
- Tray icon state for "warmth off" (blue sun). `--debug` flag.
- Unit-test suite, run in CI before every build.

### Performance
- Hardware brightness (WMI/DDC) moved to a background latest-value-wins worker —
  previously every animation frame blocked on slow monitor calls.
- One persistent animation thread instead of a new thread per change; the menu
  never blocks waiting for a previous animation to stop.
- Device calls are skipped when the value didn't change; warmth animates in
  10 K steps (smooth) using a bounded ramp cache.
- `screen_brightness_control` is imported and probed in the background, so the
  tray icon appears immediately.
- Size-capped rotating log at INFO level (was an unbounded DEBUG log).

### Fixes
- Toggling/animating always started from 6500 K (visible flash; disabling snapped
  instead of fading). Transitions now start from what's actually on screen.
- Circadian mode re-saved the config and restarted an animation every minute, and
  silently overrode a manually chosen warmth. Circadian settings now apply
  immediately.
- Safe-startup brightness could be saved permanently by any later setting change.
- 64-bit handle truncation: missing `argtypes`/`restype` for `GetDC`,
  `SetDeviceGammaRamp`, and `LRESULT` declared as 32-bit.
- Unreliable `GetLastError` in the single-instance check (now `use_last_error`).
- Possible cross-thread deadlock with the overlay window (now async window calls).
- Hardware detection at boot failed permanently if WMI wasn't ready (now retries).
- Repeated hardware failures now fall back to the overlay instead of warning forever.
- One bad config value reset *all* settings; now only that value is reset, and a
  corrupt file is backed up.
- "Reset to defaults" left the startup registry entry in place.
- About notification had title and message swapped.
- Cleanup could run twice on exit; display restored on Windows sign-out/shutdown.
- Overlay brush leaked; logging to a missing console under `pythonw`.
- `generate_icon.py` drew a different icon from the tray; it now uses the same code.
