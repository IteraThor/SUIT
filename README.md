<div align="center">
  <img src="assets/icons/suit-icon.png" alt="SUIT for Fedora Logo" width="120">
  <h1>SUIT for Fedora</h1>
  <p><strong>Setup Utilities by IteraThor for Autodarts on Fedora Linux</strong></p>
</div>

---

## Quick Install

To install SUIT, copy and run this single command in your terminal:

```bash
git clone https://github.com/IteraThor/SUIT.git && cd SUIT && chmod +x install.sh && ./install.sh
```

Launch **SUIT for Fedora** directly from your application menu or Dash favorites once installed.

Since version 0.7.0 SUIT is written in Rust. An existing Python install switches over with the in-app **Update** button: the first start after the update downloads the new version, which usually takes under a minute.

---

## Uninstalling SUIT for Fedora

To remove SUIT for Fedora, its background services, autostarts, and desktop launchers:

```bash
cd SUIT && ./uninstall.sh
```
*(Optional: pass `./uninstall.sh --purge` to also delete saved camera profiles and display configurations).*

---

## Features & Modes

### 1. Main Dashboard
Modern Libadwaita touch menu providing rapid access to all system tools and live status.

<div align="center">
  <img src="docs/assets/screenshots/01-main-dashboard.png" alt="SUIT Main Dashboard" width="700">
</div>

* Large 44px+ touch targets optimized for touchscreens.
* Quick access to community resources, Discord, and 3D print models.
* Integrated one-click update checker.

---

### 2. System Utilities & Touchscreen Tweaks
One-click performance, display, and desktop optimizations for dartboard machines. The **Essentials** tab holds everything *Apply All* sets up; the **Extras** tab has the optional tweaks (touchscreen tweaks, minimal desktop, Fast Boot, hardware overlay, advanced tools).

<div align="center">
  <img src="docs/assets/screenshots/02-system-tweaks.png" alt="System Utilities and Touchscreen Tweaks" width="700">
</div>

* **Touchscreen Tweaks**: Enlarges GNOME top bar to 54px, expands button touch padding, enables On-Screen Keyboard (OSK), and restores window Minimize/Maximize buttons across Chromium and desktop apps.
* **Minimal Desktop**: One switch for a cleaner rig desktop: a dock with your favorite and running apps sits on the wallpaper (windows simply cover it), and top bar icons and quick settings a dart machine never needs (accessibility, microphone, power mode, night light, dark style, do not disturb) are hidden. When AutoGlow 2 is installed, an **Autoglow 2** button next to Wi-Fi opens its web interface.
* **Performance Profile**: Locks CPU governors to high-performance mode to minimize dart detection latency.
* **Display Keepalive & Auto-Login**: Bypasses screen blanking, sleep timeouts, and login password prompts.

---

### 3. Autodarts Setup
The official Autodarts v2 console, embedded and sized to fit, plus install and maintenance tools.

<div align="center">
  <img src="docs/assets/screenshots/03-autodarts-setup.png" alt="Autodarts Setup" width="700">
</div>

* Everything from the Autodarts console in one page: board info and sign-in, run as a service, start / stop / reset / calibrate, cameras, resolution, FPS, standby time and detection speed.
* The console scales its font to the window, so every section is visible without scrolling.
* **Tools and Maintenance**: restart the console, and reinstall or uninstall Autodarts.

---

### 4. Camera Focus Tool
Optical calibration wizard to achieve razor-sharp focus across all 3 Autodarts cameras.

* **Sweet-Spot HUD**: Projects a green target reticle directly onto the furthest double ring wire.
* **2.5x Magnifier Lens**: Live zoomed view centered on the sweet spot for fine optical lens adjustments.
* **Sharpness Score Meter**: Normalized 0–100 score gauge with real-time peak tracking and focus trend indicators.
* **Audio Tone Guidance**: Modulates pitch dynamically as focus improves, allowing you to focus lenses while standing at the board without looking at the screen.

---

### 5. Camera Optimizer (HBV cameras)
One-click image calibration for HBV cameras, found under **Camera Tools**.

| Model | USB ID | What the optimizer sets |
|---|---|---|
| HBV OV2710 | `0bdc:2710` | exposure, 1× sensor gain + matched digital gain, MJPEG quality |
| HBV OV9732 | `0bdc:8088` | exposure, matched sensor gain, MJPEG quality |
| HBV IMX179 | `1e45:8025` | exposure, matched gain (UVC controls only; its JPEG quality is fixed in the firmware) |

* **Resolution first**: pick the resolution before you start (every size all cameras offer, Autodarts' current one preselected). If the cameras can't stream it cleanly together, the run stops and suggests a lower one; applying switches Autodarts to it.
* **One click**: stops Autodarts, calibrates every camera against the empty board, matches them, and starts Autodarts again (about 2 minutes).
* **Hidden camera controls**: longest flicker-free exposure at 1× sensor gain, matched digital gain, low sharpening, and a high MJPEG quality picked by a bandwidth check across all cameras. About twice as visible darts in Autodarts' difference image on the reference rig.
* **Stays applied**: the `suit-camera-keeper` user service writes the settings before Autodarts opens the cameras and re-applies them after restarts and standby. Nothing is flashed; settings live in camera RAM.
* **Fine-tuning** afterwards on the **Camera Controls** page, with "Reset to calibrated" and a brightness check.
* **Before and after** comparison per camera, and **Restore original settings** to undo everything.

Hidden settings are only used on a known HBV model with a verified firmware build, after you confirm that model once; other cameras of a known model get their standard controls calibrated. A saved profile is never applied to a different camera plugged into the same port. Profiles live in `~/.config/hbvcal`.

<p align="center">
  <img src="docs/assets/screenshots/07-camera-before-after.png" alt="Camera Optimizer before (left) and after (right)" width="420">
</p>

---

### 6. Camera Controls
Live view and every setting of each camera, for any UVC camera, found under **Camera Tools**.

* **Every control** grouped by Exposure / Tone / Detail / Color / Stream; on confirmed HBV cameras also JPEG quality and sensor gain.
* **Keep these settings**: per camera, re-applied by the `suit-camera-keeper` service before Autodarts opens the cameras and after every restart or standby.
* **Presets**: save the settings of all cameras under a name (e.g. day / evening light) and load them in one tap.
* **Live view** that never takes a camera away from Autodarts: while Autodarts runs, it shows Autodarts' own picture.

---

### 7. Screen & Touch Manager
Wayland display orientation and touch coordinate matrix calibration. The page is a hub with **Display Rotation & Touch** and **Wireless Screen** (an Android or iOS tablet as the screen).

<div align="center">
  <img src="docs/assets/screenshots/05-screen-touch.png" alt="Screen and Touch Manager" width="700">
</div>

* Rotate displays seamlessly between 0° Normal, 90° Right, 180° Inverted, and 270° Left.
* Automatic touch matrix calibration (`LIBINPUT_CALIBRATION_MATRIX`) via udev rules to align touch input with rotated screens.
* Discovers connected displays and touchscreen controllers automatically.

**Wireless Screen** uses a tablet as the screen of the PC, also with no monitor connected. The wizard shows one simple screen at a time:
1. **Start**: one button, 1920 x 1200 by default (**Change** offers iPad and Android sizes). Start stays off without a free video port or internet. After you confirm, SUIT turns a free video port into a virtual screen (own EDID, dracut, kernel args via `grubby`; the rescue boot entry stays without it, kernel updates keep it) and installs [Sunshine](https://github.com/LizardByte/Sunshine) from its COPR with low-latency settings (KMS capture, software x264 `zerolatency`, LAN only, firewall ports). The desktop no longer blanks, locks or sleeps.
2. **Restart**: one button (asks first, save your work); SUIT opens the wizard again after the login.
3. **Connect**: QR codes for the free Moonlight app, then tap this PC in Moonlight; SUIT shows the PIN field as soon as Moonlight asks and tells you exactly what went wrong.
4. **Done**: open Moonlight → this PC → **Desktop**. It shows whether the tablet is connected, can switch to the tablet only, offers **Match my tablet** with the size the tablet asked for, and warns when the PC is on Wi-Fi. Settings: same picture or extended desktop with a monitor (the `suit-wireless-screen` user service), Sharp or Smooth picture, hide the cursor while streaming, tablet size, **Check my setup** (checklist with **Fix it**), add another tablet, **Copy details**, and **Remove everything**.

Without the GUI: `suit wireless-screen --remove` undoes everything (safe to run twice); `suit wireless-screen --help` lists the other options (`--status`, `--check --fix`, ...).

The picture is encoded by the processor (software H.264). Moonlight on iOS sends mouse input, not multi-touch.

---

### 8. Fullscreen Kiosk Mode
Run Autodarts as a dedicated full-screen appliance with customized browser controls.

<div align="center">
  <img src="docs/assets/screenshots/06-kiosk-controls.png" alt="Fullscreen Kiosk Mode with In-Page Controls" width="560">
</div>

* Boots Chromium directly into fullscreen mode without desktop distractions, optionally on login.
* Built-in Chromium extension adding Power, Restart, Exit and Board Configuration buttons directly into the Autodarts header.
* Optional dartboard light button for WLED, Tasmota or Shelly devices, with an On / Off test.
