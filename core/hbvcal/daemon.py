"""Keep the saved camera settings applied while Autodarts runs.

Runs as the user service suit-camera-keeper, ordered before autodarts.service (see service.py):
    python -m core.hbvcal.daemon [--interval 2]

At its first start of a boot it makes the default preset (if one is set) the kept profiles, so every boot begins
from it; changes made later in that session last until the next boot.

Per camera with a saved profile (matched by USB port):
  * HBV camera with hidden layers (optimized), only after the user confirmed HBV cameras:
    - as soon as the device appears: unlock + write the MJPEG Q byte (must happen BEFORE the stream starts;
      it lives in camera RAM, survives stream restarts, is lost on power-off/replug → re-sent on re-appearance)
    - while it is streaming: if any UVC control or sensor register differs from the profile, re-apply all
      (Autodarts applies its own controls when it opens a camera; a UVC exposure write resets sensor gain)
    - frame overflow guard: if Autodarts runs detection but gets 0 fps from this camera for STALL_S, its frames
      are too big for the camera's buffer at the saved Q (a busier scene than the optimizer saw). The saved Q is
      raised two levels and written; Autodarts reconnects a stalled camera every ~1.6 s and picks it up.
  * any other profile (any camera, standard controls only): whenever a UVC control differs, re-apply the
    standard controls. No extension-unit command is ever sent to these cameras.
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import signal
import sys
import time
from typing import Dict, List, Optional

from . import profiles
from .autodarts import autodarts_index, camera_fps
from .settings import Settings, apply_prestream, apply_streaming, apply_uvc, inactive, q_above, q_addr
from .v4l2 import Camera
from .xu import XU, XUError

log = logging.getLogger("hbvcal.daemon")

STALL_S = 8.0  # one camera at 0 fps this long while Autodarts runs detection: taken as an MJPEG frame overflow


def cameras():
    return sorted(glob.glob("/dev/v4l/by-path/*-usb-*-video-index0"))


def is_streaming(xu: XU) -> bool:
    """The sensor is only powered (and its ID readable) while some process streams."""
    try:
        return xu.sensor_read(0x300B) == 0x10 and xu.sensor_read(0x300A) == 0x27
    except OSError:
        return False


def differences(cam: Camera, xu: Optional[XU], s: Settings, skip=()) -> Dict[str, tuple]:
    diff = {}
    for name, want in s.uvc.items():
        if name in skip or inactive(s.uvc, name):
            continue
        try:
            have = cam.get(name)
        except (OSError, KeyError):
            continue
        if have != want:
            diff[name] = (have, want)
    if xu is None or s.uvc.get("auto_exposure", 1) != 1:  # auto exposure drives the sensor gain itself
        return diff
    for reg, want in s.sensor.items():
        have = xu.sensor_read(reg)
        if have != want:
            diff[f"sensor 0x{reg:04X}"] = (have, want)
    return diff


class Tracker:
    def __init__(self, dev: str):
        self.dev = dev
        self.inode = os.stat(dev).st_ino if os.path.exists(dev) else None
        self.q_done = False
        self.applies = 0
        self.refused: Dict[str, str] = {}  # standard controls this camera refused: not retried every tick
        self.stall_since: Optional[float] = None
        self.stall_warned = False


def stalled(dev: str, t: Tracker, fps: Optional[List[float]]) -> bool:
    """Has Autodarts been getting no frames from this camera for STALL_S (while it runs detection)?"""
    idx = autodarts_index(dev) if fps else None
    if idx is None or idx >= len(fps) or fps[idx] > 0:
        t.stall_since = None
        return False
    now = time.monotonic()
    if t.stall_since is None:
        t.stall_since = now
    if now - t.stall_since < STALL_S:
        return False
    t.stall_since = now  # give Autodarts' reconnect time to pick up a change before judging again
    return True


def tick(trackers: Dict[str, Tracker]) -> None:
    present = set(cameras())
    fps_cache: list = []

    def fps() -> Optional[List[float]]:  # one API read per tick, only if a camera needs it
        if not fps_cache:
            fps_cache.append(camera_fps())
        return fps_cache[0]

    for dev in list(trackers):
        if dev not in present:
            log.info("%s disappeared", dev)
            del trackers[dev]
    for dev in present:
        s = profiles.get_settings(dev)
        if s is None:
            continue
        t = trackers.get(dev)
        ino = os.stat(dev).st_ino
        if t is None or t.inode != ino:  # new or re-enumerated device
            t = trackers[dev] = Tracker(dev)
            log.info("%s appeared (profile %s)", dev, s.label())
        if not ((s.mjpeg_q is not None or s.sensor or s.isp) and profiles.hidden_ok(dev)):
            _keep_uvc(dev, t, s)
            continue
        try:
            with XU(dev) as xu:
                xu.unlock()
                width = int(profiles.get(dev).get("meta", {}).get("resolution", "1280x960").split("x")[0])
                if s.mjpeg_q is not None:
                    have = xu.mem_read(q_addr(xu, width), 1)[0]
                    if have != s.mjpeg_q:
                        streaming = is_streaming(xu)
                        apply_prestream(xu, s, width)
                        log.info("%s MJPEG Q %d -> %d%s", dev, have, s.mjpeg_q,
                                 " (camera already streaming: applies at next stream start)" if streaming else "")
                    if stalled(dev, t, fps()):
                        new = q_above(s.mjpeg_q, 2)
                        if new != s.mjpeg_q:
                            profiles.set_q(dev, new, f"Autodarts got no frames at Q{s.mjpeg_q} for {STALL_S:.0f} s")
                            apply_prestream(xu, Settings(mjpeg_q=new), width)
                            log.warning("%s: Autodarts gets no frames at MJPEG Q %d (frames too big for the camera); "
                                        "saved and wrote Q %d", dev, s.mjpeg_q, new)
                        elif not t.stall_warned:
                            t.stall_warned = True
                            log.warning("%s: Autodarts gets no frames even at stock MJPEG Q %d", dev, s.mjpeg_q)
                if not is_streaming(xu):
                    continue
                with Camera(dev) as cam:
                    d = differences(cam, xu, s)
                    if d:
                        apply_streaming(cam, xu, s)
                        t.applies += 1
                        log.info("%s re-applied settings (#%d), changed: %s", dev, t.applies,
                                 ", ".join(f"{k} {v[0]}->{v[1]}" for k, v in d.items()))
        except (OSError, XUError) as e:
            log.warning("%s: %s", dev, e)


def _keep_uvc(dev: str, t: Tracker, s: Settings) -> None:
    """Standard controls only: works on any UVC camera, streaming or not."""
    try:
        with Camera(dev) as cam:
            d = differences(cam, None, s, skip=t.refused)
            if not d:
                return
            uvc = {k: v for k, v in s.uvc.items() if k not in t.refused}
            refused = apply_uvc(cam, uvc, strict=False)
            t.applies += 1
            log.info("%s re-applied standard controls (#%d), changed: %s", dev, t.applies,
                     ", ".join(f"{k} {v[0]}->{v[1]}" for k, v in d.items()))
            for k, e in refused.items():
                log.warning("%s refuses %s=%s (%s): not retried until it reconnects", dev, k, s.uvc[k], e)
            t.refused.update(refused)
    except OSError as e:
        log.warning("%s: %s", dev, e)


def main(argv=None):
    p = argparse.ArgumentParser(prog="core.hbvcal.daemon")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--once", action="store_true", help="one pass, then exit")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    trackers: Dict[str, Tracker] = {}
    log.info("camera keeper started, profiles: %s", profiles.STORE)
    try:
        name = profiles.load_default_preset_once()
        if name:
            log.info("loaded the default preset %r for this boot", name)
    except (OSError, ValueError) as e:
        log.warning("default preset not loaded: %s", e)
    while not stop:
        tick(trackers)
        if a.once:
            break
        time.sleep(a.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
