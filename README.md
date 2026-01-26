

# SUIT - Setup Utilities by IteraThor

SUIT is a graphical tool designed mainly to streamline the deployment of an **Autodarts system** on Linux (e.g., Raspberry Pi / Ubuntu).

I originally created this project to make setting up my own Linux PCs for Autodarts easier and faster. I am sharing it here for free for anyone in the community who might find it useful!

## Features

* 🎯 **Autodarts Management:** Menu to install, control (start/stop/restart), and uninstall the Autodarts service.
* 💡 **AutoGlow Manager:** Easy setup and management for my AutoGlow script (LED control).
* 🖥️ **Kiosk Mode:** Enable or disable Kiosk Mode to boot Firefox directly into fullscreen (includes a fix for the "black screen" issue on Wayland).
* 🔄 **Touch Input Hotfix:** A specific fix to rotate touch input coordinates (for Wayland/Ubuntu) if your touchscreen responds incorrectly.

## Requirements

The tool requires `Python` and `tkinter` for the graphical interface.

```bash
sudo apt update
sudo apt install python3 python3-tk python3-venv git

```

## Installation & Usage

1. Clone repository:
```bash
git clone https://github.com/IteraThor/SUIT.git
cd SUIT

```


2. Start app:
```bash
python3 suit_test.py

```
