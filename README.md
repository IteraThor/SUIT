<div align="center">
  <img src="https://github.com/user-attachments/assets/5071afba-adca-4fc4-87cb-10860e37698f" alt="SUIT Logo" width="150">
</div>

# SUIT - Setup Utilities by IteraThor

SUIT is a graphical toolkit designed to simplify the deployment and management of an **Autodarts system** on Linux, specifically optimized for Ubuntu and Wayland environments.

## Features

* 🎯 **Autodarts Management:** Dedicated menu to install, monitor, and control the Autodarts service (start, stop, or restart).
* 💡 **AutoGlow Manager:** Complete setup for the AutoGlow LED control script. Includes a live WebSocket log, ESP32 hardware detection, and an effect tester.
* 🖥️ **Kiosk Mode:** Configures Firefox to boot directly into fullscreen. Includes a Wayland "black screen" fix and an emergency exit via the power button.
* 🔄 **Screen & Touch Rotation:** Rotate your display and fix touch input coordinates using DBus and Udev rules.
* ⌨️ **On-Screen Keyboard:** Quick toggle to enable or disable the GNOME on-screen keyboard.

### 1. Install System Dependencies
Run the following command to install the necessary system packages:
```bash
sudo apt update
sudo apt install libdbus-1-dev libglib2.0-dev pkg-config python3 python3-tk python3-dev python3-venv python3-dbus git xterm -y

```

## Installation & Usage

1. **Clone the repository:**
```bash
git clone https://github.com/IteraThor/SUIT.git
cd SUIT

```
2. **Create a desktop Icon**
```bash
python3 create_launcher.py

```
3. **Launch from the Desktiop Icon or run the suit_test.py**
