<div align="center">
  <img src="https://github.com/user-attachments/assets/5071afba-adca-4fc4-87cb-10860e37698f" alt="SUIT Logo" width="150">
</div>

# SUIT - Setup Utilities by IteraThor

SUIT is a graphical toolkit designed to simplify the deployment and management of an **Autodarts system** on Linux, specifically optimized for Ubuntu and Wayland environments.

## Features

* 🎯 **Autodarts Management:** Dedicated menu to install, monitor, and control the Autodarts service (start, stop, or restart).
* 💡 **AutoGlow Manager:** Complete setup for the [AutoGlow](https://github.com/IteraThor/AutoGlow) WLED control script. 
* 🖥️ **Kiosk Mode:** Configures Firefox to boot directly into fullscreen. Includes a Wayland "black screen" fix and an emergency exit via the power button.
* 🔄 **Screen & Touch Rotation:** Rotate your display and fix touch input coordinates using.

### 1. Install System Dependencies
Run the following command to install the necessary system packages:
```bash
sudo apt update
sudo apt install python3-tk python3-dbus git
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
<details><summary>Screenshots</summary>
<img width="300" height="434" alt="image" src="https://github.com/user-attachments/assets/8b95ad84-579b-41c8-a201-5e79a3c0c6c7" />
<img width="300" height="492" alt="image" src="https://github.com/user-attachments/assets/ffb895da-e75b-4e2d-a69c-7034b84637a1" />
<img width="300" height="486" alt="image" src="https://github.com/user-attachments/assets/aa2e6ea0-238e-47ca-92d0-871217b3a785" />
<img width="300" height="521" alt="image" src="https://github.com/user-attachments/assets/d7b57b88-8939-414c-b1ba-ed13c01f0e06" />
<img width="300" height="354" alt="image" src="https://github.com/user-attachments/assets/1207e016-40c0-4eb5-b3bf-f95ea9596439" />
<details>
