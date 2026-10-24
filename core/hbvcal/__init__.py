"""Camera Optimizer for HBV OV2710 USB cameras (Realtek RTS5842 bridge, USB ID 0bdc:2710).

Uses the cameras' hidden runtime controls: the MJPEG quality byte, sensor analog gain and the standard UVC
controls, calibrated per camera against the board. Nothing is flashed: every change lives in camera RAM and
is gone after a power cycle; the keeper service (daemon.py, installed by service.py) re-applies it.

  xu.py         Realtek extension-unit mailbox: reset, tuning unlock, memory, sensor registers
  v4l2.py       minimal V4L2 capture with sequence numbers and controls
  settings.py   settings across all layers, applied in the order the firmware needs
  profiles.py   per-camera profile store (~/.config/hbvcal), keyed by USB port
  autocal.py    automatic calibration of one camera / all cameras
  oneclick.py   support check, optimize and restore jobs
  daemon.py     keeper: re-applies the profiles while Autodarts runs
  live.py       live view and manual adjustments for the GTK page
"""
