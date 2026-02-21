<div align="center">
  <img src="https://github.com/user-attachments/assets/5071afba-adca-4fc4-87cb-10860e37698f" alt="SUIT Logo" width="150">
</div>

# SUIT - Setup Utilities by IteraThor

SUIT is a graphical toolkit designed to simplify the deployment and management of an **Autodarts system** on Linux, specifically optimized for Ubuntu and Wayland environments.

<div align="center">
  <img width="300" height="434" alt="Main Menu" src="https://github.com/user-attachments/assets/8b95ad84-579b-41c8-a201-5e79a3c0c6c7" />
</div>

## Features

* 🎯 **Autodarts Management:** Dedicated menu to install, monitor, and control the Autodarts service (start, stop, or restart).
* 💡 **AutoGlow Manager:** Complete setup for the [AutoGlow](https://github.com/IteraThor/AutoGlow) WLED control script. 
* 🖥️ **Kiosk Mode:** Configures Firefox or Chromium to boot directly into fullscreen. Includes a Wayland "black screen" fix and an emergency exit via the power button.
* 🔄 **Screen & Touch Rotation:** Rotate your display and fix touch input coordinates.

## Installation

The easiest way to install SUIT is to use the provided installation script:

```bash
git clone https://github.com/IteraThor/SUIT.git
cd SUIT
chmod +x install.sh
./install.sh
```

This script will:
1. Install all necessary system dependencies (`python3-tk`, `libdbus-1-dev`, etc.).
2. Create a Python virtual environment.
3. Install required Python packages.
4. Create a **Desktop Launcher** for easy access.

## Usage
Once installed, you can launch SUIT directly from your application menu or the desktop icon.

### Language Selection
SUIT is bilingual (English and German). You can toggle the language using the button in the top-right corner. Your preference will be saved automatically.

### Troubleshooting
If you encounter any issues, SUIT generates a `suit.log` file in its directory. This file contains technical details that can help with debugging.

## Screenshots
<details>
  <summary>Click to expand</summary>
  <img width="300" alt="Autodarts" src="https://github.com/user-attachments/assets/ffb895da-e75b-4e2d-a69c-7034b84637a1" />
  <img width="300" alt="AutoGlow" src="https://github.com/user-attachments/assets/aa2e6ea0-238e-47ca-92d0-871217b3a785" />
  <img width="300" alt="Kiosk" src="https://github.com/user-attachments/assets/d7b57b88-8939-414c-b1ba-ed13c01f0e06" />
  <img width="300" alt="Rotation" src="https://github.com/user-attachments/assets/1207e016-40c0-4eb5-b3bf-f95ea9596439" />
</details>
