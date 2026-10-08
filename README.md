# ☀️ DuskRay — Display Brightness & Warmth Control App

A small, fast Windows system-tray tool that controls screen **brightness**
(-30 % to 100 %) and **warmth** (2000 K–6500 K) with smooth transitions, an
optional circadian schedule, multi-monitor support, and a "run at startup"
option.

Brightness and warmth are **independent**: you can turn warmth off and keep
your brightness level, or turn everything off at once.

## ☄️ How to Install (Easy Way)

### 🪟 Windows

1. [Download the latest release.](https://github.com/ErfanNamira/DuskRay/releases/latest)
2. Run `DuskRay.exe`.

## 💻 Setup (From Source)

1. Install [Python 3.9+](https://www.python.org/downloads/) — check **"Add python.exe to PATH"** during install.
2. Get the code, either:
   - **Clone with git:**
     ```
     git clone https://github.com/ErfanNamira/DuskRay.git
     cd DuskRay
     ```
   - **Or download the ZIP:** go to the [DuskRay repo](https://github.com/ErfanNamira/DuskRay), click **Code → Download ZIP**, then extract it and open a terminal inside that folder.
3. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
4. Start the app:
   ```
   pythonw duskray.py
   ```

Command-line options: `--debug` (verbose log + console output), `--reset`
(restore factory defaults, reset the display, remove the startup entry, then exit),
`--version`.

## ✨ Using it

- **Left-click the tray icon** → runs the **bold** menu item. By default that
  is **Enabled** (everything on/off). Change it under **Icon click** to
  **Toggle warmth only**.
- **Right-click the tray icon** for the full menu:
  - **Brightness** → -30 % to 100 %. Values below 0 % add a click-through
    dimming overlay on top of the hardware minimum.
  - **Warmth** → 6500 K (neutral) down to 2000 K (very warm). Lower K = warmer.
    Picking a value turns warmth on, and switches Circadian mode off so your
    choice sticks.
  - **Enabled** → master switch: off restores 100 % brightness and neutral colour.
  - **Warmth enabled** → turns the colour shift off/on **without changing
    brightness**.
  - **Smooth transitions** → animate changes instead of snapping.
  - **Circadian mode** → automatic day/night warmth.
  - **Circadian settings** → day/night start hour, day/night warmth, and the
    **Fade duration** (instant, 15, 30 or 60 min) used after each boundary.
  - **Icon click** → choose what a left-click does.
  - **Run at startup** → adds/removes a registry entry so DuskRay launches when
    you log in. At startup, brightness is temporarily raised to at least 10 %
    so you never boot into a black screen (your saved setting is kept).
  - **Reset to defaults**, **About**, **Exit** (exit restores the display).

The tray icon shows the state: orange sun (active), darker sun (dim overlay),
blue sun (warmth off, brightness only), grey moon (disabled).

Settings are saved to `%APPDATA%\DuskRay\config.json`; the log (size-capped)
is `%APPDATA%\DuskRay\duskray.log`. Older config files are upgraded
automatically, and an invalid value only resets that one setting.

## 🔧 Building DuskRay.exe

1. Generate the icon (only needed after changing the artwork — it uses the
   same drawing code as the tray icon):
   ```
   python generate_icon.py
   ```
2. Install the build tools:
   ```
   pip install pyinstaller
   ```
   For a smaller exe, also install [UPX](https://github.com/upx/upx/releases)
   (grab the `-win64` build) and extract it somewhere, e.g. `C:\upx`.
3. Build:
   ```
   pyinstaller --onefile --noconsole --clean --name "DuskRay" ^
       --icon=duskray.ico --version-file=version_info.txt ^
       --upx-dir=C:\upx ^
       --exclude-module tkinter --exclude-module unittest ^
       --exclude-module pdb --exclude-module doctest --exclude-module test ^
       duskray.py
   ```
4. The result is `dist\DuskRay.exe`. Turn on "Run at startup" from its tray
   menu — the registry entry will point at `DuskRay.exe`, and Task Manager
   will show "DuskRay" as the process name and description.

## 🧪 Tests

```
python -m unittest discover -s tests -v
```

The suite uses fake devices, so it runs anywhere (including CI) without
touching your display.

## 🧪 Notes on Brightness Control

`screen_brightness_control` uses Windows' WMI brightness API (laptop panels)
and DDC/CI (external monitors that support it). These calls can be slow, so
DuskRay runs them on a background worker that always applies only the latest
value — the UI and animations never wait for the monitor. If hardware control
is unavailable (or keeps failing), DuskRay falls back to the dimming overlay
automatically; warmth works either way.

## 🧪 How the Warmth Effect Works

DuskRay sets a gamma ramp on **every attached monitor** — the same Windows
mechanism used by Night Light/f.lux — computed from a standard
colour-temperature (Kelvin) approximation. It re-applies the ramp after
display changes, sleep/resume and unlock, and periodically while warm, because
Windows, drivers and games can silently reset it. When warmth is off it leaves
the gamma alone. Turn off Windows Night Light if you use DuskRay, since two
gamma tools will fight each other.

**Very warm values have no effect?** By default Windows rejects strong gamma
ramps. DuskRay detects this, uses the warmest value Windows accepts, and logs
a hint. To unlock the full range, create the DWORD
`HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ICM\GdiIcmGammaRange` = `256`
(admin required), then sign out and back in.

## 📄 License

MIT
