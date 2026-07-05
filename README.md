# ☀️ DuskRay — Display Brightness & Warmth Control App

A small Windows system tray tool to control screen **brightness** (10%–100%)
and **warmth** (in Kelvin, 2000K–6500K), with one-click enable/disable and a
"run at startup" option.

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

## ✨ Using it

- **Click the tray icon** → instantly toggles warmth on/off. This is the
  fastest way to disable/re-enable the effect.
- **Right-click the tray icon** for the full menu:
  - **Brightness** → 10% to 100%, in steps of 10.
  - **Warmth** → Kelvin values from 6500K (neutral) down to 2000K (very
    warm/orange). Lower K = warmer.
  - **Enabled** → same toggle as clicking the icon, shown as a checkbox.
  - **Run at startup** → adds/removes a registry entry so DuskRay launches
    automatically when you log in to Windows.
  - **Exit** → resets the display to normal color and closes the app.

Settings are saved automatically to `%APPDATA%\DuskRay\config.json`.

## 🔧 Building DuskRay.exe

Compiling to a standalone exe gets you a proper icon, a small file size, and
a correct name in Task Manager / Startup Apps (running the raw `.py` script
via `pythonw.exe` would otherwise just show "pythonw.exe" there, since that's
the actual program being launched, not the registry entry name).

1. Generate the icon (only needed once, or after changing the design):
   ```
   python generate_icon.py
   ```
   This creates `duskray.ico` from the same drawing code used by the tray
   icon, at multiple resolutions.

2. Install the build tools:
   ```
   pip install pyinstaller
   ```
   For a smaller exe, also install [UPX](https://github.com/upx/upx/releases)
   (grab the `-win64` build) and extract it somewhere, e.g. `C:\upx`.

3. Build:
   ```
   pyinstaller --onefile --noconsole --clean --strip --name "DuskRay" ^
       --icon=duskray.ico --version-file=version_info.txt ^
       --upx-dir=C:\upx ^
       --exclude-module tkinter --exclude-module unittest ^
       --exclude-module pdb --exclude-module doctest --exclude-module test ^
       duskray.py
   ```

4. The result is `dist\DuskRay.exe`. Run that instead of the `.py` file, and
   turn on "Run at startup" from its tray menu — the registry entry will
   point at `DuskRay.exe`, and Task Manager will correctly show "DuskRay" as
   both the process name and (from the embedded metadata) the description.

## 🧪 Notes on Brightness Control

`screen_brightness_control` uses Windows' built-in WMI brightness API, which
reliably controls **laptop built-in displays**. External monitor support
depends on the monitor/drivers supporting DDC/CI — some external monitors
won't respond to software brightness changes; warmth will still work.

## 🧪 How the Warmth Effect Works

DuskRay adjusts the monitor's gamma ramp — the same underlying Windows
mechanism used by Night Light/f.lux — using a standard color-temperature
(Kelvin) approximation to compute the tint, rather than an arbitrary
percentage. No overlay window is used. Turn off Windows Night Light if you
use DuskRay, since two gamma-ramp tools will fight each other.

## 📄 License

MIT
