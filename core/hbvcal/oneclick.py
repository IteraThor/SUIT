"""One-click workflow behind the Camera Optimizer page: support check, optimize and restore.

Safety: the tuning back door (XU unlock + memory writes) is only ever used on cameras that pass
`support()`: USB ID 0bdc:2710 (HBV OV2710 / Realtek bridge) AND a firmware build whose memory layout we
verified. Other cameras are never touched. The user must also confirm once that they use HBV OV2710 cameras.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from typing import Callable, List, Optional

from . import profiles, service
from .autocal import PREVIEW, calibrate_all, start_clean
from .capture import burst
from .daemon import cameras, is_streaming, tick
from .settings import Q_ADDR, Q_DEFAULT, Settings, apply_streaming, q_addr
from .v4l2 import Camera
from .xu import XU

HBV_USB_ID = profiles.HBV_USB_ID
BEFORE_AFTER = profiles.STORE.parent / "beforeafter"
AUTODARTS_START_TIMEOUT = 60

log = logging.getLogger("suit.hbvcal.oneclick")  # lands in suit.log inside SUIT


def support(dev: str) -> dict:
    """Is this camera one we can safely optimize? Never sends XU commands to non-HBV cameras."""
    uid = profiles.usb_id(dev)
    out = {"device": dev, "port": profiles.port_key(dev), "usb_id": uid, "supported": False}
    if uid != HBV_USB_ID:
        out["reason"] = f"not an HBV OV2710 camera (USB ID {uid}, expected {HBV_USB_ID})"
        return out
    try:
        with XU(dev) as xu:
            xu.unlock()
            build = xu.firmware_build()
    except OSError as e:
        out["reason"] = f"camera did not answer the tuning interface ({e})"
        return out
    out["firmware"] = build
    if build not in Q_ADDR:
        out["reason"] = f"unknown firmware build {build}: memory layout not verified"
        return out
    out["supported"] = True
    out["optimized"] = profiles.is_calibrated(profiles.get(dev))
    return out


def systemctl(action: str, unit: str = "autodarts") -> str:
    r = subprocess.run(["systemctl", "--user", action, unit], capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def autodarts_active() -> bool:
    return systemctl("is-active") == "active"


class Job:
    """A background task with named steps the page can show as a checklist."""

    def __init__(self):
        self.name = ""
        self.running = False
        self.steps: List[dict] = []
        self.log: List[str] = []
        self.result: dict = {}
        self.error = ""
        self.thread: Optional[threading.Thread] = None

    def start(self, name: str, fn: Callable, *args):
        if self.running:
            return False
        self.running, self.steps, self.log, self.result, self.error = True, [], [], {}, ""
        self.name = name

        def run():
            log.info("%s started", name)
            try:
                fn(self, *args)
                log.info("%s finished", name)
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                log.exception("%s failed", name)
                if self.steps and self.steps[-1]["state"] == "running":
                    self.steps[-1]["state"] = "failed"
            finally:
                self.running = False

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        return True

    def say(self, msg: str):
        """A detail line for the page's log (and suit.log)."""
        self.log.append(msg)
        log.info("%s: %s", self.name, msg)

    def step(self, title: str):
        if self.steps and self.steps[-1]["state"] == "running":
            self.steps[-1]["state"] = "done"
        self.steps.append({"title": title, "state": "running"})
        log.info("%s step: %s", self.name, title)

    def skip(self, note: str):
        """The current step did not work out and the job goes on (a compression level that does not fit, a missing
        picture): shown as a yellow note, not a failure. Real failures end the job (red)."""
        if self.steps and self.steps[-1]["state"] == "running":
            self.steps[-1]["state"] = "skipped"
            self.steps[-1]["title"] += f": {note}"
            log.info("%s step skipped: %s", self.name, self.steps[-1]["title"])

    def finish(self):
        if self.steps and self.steps[-1]["state"] == "running":
            self.steps[-1]["state"] = "done"

    def to_json(self) -> dict:
        return {"name": self.name, "running": self.running, "steps": self.steps,
                "log": self.log[-40:], "result": self.result, "error": self.error}


def _wait(cond: Callable[[], bool], timeout: float, poll: float = 1.0) -> bool:
    t_end = time.time() + timeout
    while time.time() < t_end:
        if cond():
            return True
        time.sleep(poll)
    return cond()


def _wait_cameras_free(devs: List[str], timeout: float = 20) -> bool:
    def free():
        for d in devs:
            try:
                with Camera(d) as cam:
                    cam.configure(1280, 960, "MJPG", 30)
                    cam.start()
                    cam.stop()
            except OSError:
                return False
        return True
    return _wait(free, timeout)


def _wait_streaming(devs: List[str], timeout: float) -> bool:
    def all_streaming():
        for d in devs:
            with XU(d) as xu:
                if not is_streaming(xu):
                    return False
        return True
    return _wait(all_streaming, timeout)


def snapshot_frame(dev: str, s: Settings = None, say: Callable[[str], None] = log.info) -> bytes:
    """One complete camera frame (raw JPEG). With `s`, the settings are applied first (as the keeper would);
    without, the camera keeps its current ones. If the camera only sends broken frames at that MJPEG Q, the picture
    is taken at the next coarser Q that works (and the camera keeps that Q)."""
    with XU(dev) as xu, Camera(dev) as cam:
        xu.unlock()
        q = s.mjpeg_q if s is not None and s.mjpeg_q is not None else xu.mem_read(q_addr(xu, 1280), 1)[0]
        start_clean(cam, xu, q, log=say)
        try:
            if s is not None:
                apply_streaming(cam, xu, s)
            return burst(cam, 1, discard=20).frames[-1].data
        finally:
            cam.stop()


def save_before_after(dev: str, which: str, data: bytes, note: str = "", root=BEFORE_AFTER) -> None:
    d = root / profiles.port_key(dev)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{which}.jpg").write_bytes(data)
    meta_path = d / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    meta[which] = {"time": time.strftime("%Y-%m-%d %H:%M"), "note": note}
    meta_path.write_text(json.dumps(meta))


def clear_before_after(dev: str, root=BEFORE_AFTER) -> None:
    shutil.rmtree(root / profiles.port_key(dev), ignore_errors=True)


def _pictures(dev: str, root, meta_q: dict) -> Optional[dict]:
    d = root / profiles.port_key(dev)
    if not ((d / "before.jpg").exists() and (d / "after.jpg").exists()):
        return None
    meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else {}
    return {"device": dev, "port": profiles.port_key(dev), "meta": meta, "dir": d,
            "q": (meta_q.get("previous", {}).get("mjpeg_q"), meta_q.get("mjpeg_q"))}


def before_after_info() -> list:
    """Pictures of a calibration waiting to be applied, else of the cameras that are optimized now (older
    pictures belong to an undone optimization). Each with its folder, Q before/after and `pending`."""
    pend = load_pending()
    out = []
    for dev in cameras():
        port = profiles.port_key(dev)
        if pend:
            info = _pictures(dev, PENDING_PICTURES, pend[port]["meta"]) if port in pend else None
        else:
            prof = profiles.get(dev)
            info = _pictures(dev, BEFORE_AFTER, prof["meta"]) if profiles.is_calibrated(prof) else None
        if info:
            out.append({**info, "pending": bool(pend)})
    return out


# --- a finished calibration waits for the user's decision (apply or discard) -------------------------------

PENDING = profiles.STORE.parent / "pending.json"  # {port: {"settings": ..., "meta": ...}}
PENDING_PICTURES = profiles.STORE.parent / "beforeafter-pending"


def load_pending() -> dict:
    try:
        return json.loads(PENDING.read_text()).get("cameras", {})
    except (OSError, ValueError):
        return {}


def discard() -> None:
    """Forget a calibration that was not applied (the cameras already run their previous settings)."""
    PENDING.unlink(missing_ok=True)
    shutil.rmtree(PENDING_PICTURES, ignore_errors=True)
    log.info("pending calibration discarded")


def _put_back(dev: str) -> None:
    """MJPEG Q of the previous profile (stock without one) for the next stream start: the calibration left its
    own Q in camera memory, and Autodarts may open the camera before the keeper's first pass."""
    prev = profiles.get_settings(dev)
    with XU(dev) as xu:
        xu.unlock()
        build = xu.firmware_build()
        if prev is not None and prev.mjpeg_q is not None and profiles.hidden_ok(dev):
            xu.mem_write(q_addr(xu, 1280), bytes([prev.mjpeg_q]))
        else:
            for cls, addr in Q_ADDR[build].items():
                xu.mem_write(addr, bytes([Q_DEFAULT[cls]]))


def optimize(job: Job, pause_live: Callable[[bool], None], fps: int = 30) -> None:
    """Calibrate and take before/after pictures. Nothing is kept: the result waits in PENDING for apply() or
    discard(), and the cameras go back to their previous settings meanwhile."""
    devs = [s["device"] for s in map(support, cameras()) if s["supported"]]
    if not devs:
        raise RuntimeError("no supported HBV OV2710 camera found")
    was_running = autodarts_active()
    service.migrate_legacy()
    keeper_was_on = service.is_enabled()
    service.stop()  # it would re-apply the old profile onto the calibration stream
    pause_live(True)
    PREVIEW.clear()
    discard()
    try:
        job.step("Stopping Autodarts")
        if was_running:
            systemctl("stop")
        if not _wait_cameras_free(devs):
            raise RuntimeError("cameras are still in use by another program")

        job.step("Taking 'before' pictures")
        before = {}
        for d in devs:  # cameras still hold the settings Autodarts gave them
            try:
                before[d] = snapshot_frame(d, say=job.say)
                PREVIEW.set(d, "Before: the settings Autodarts used", before[d])
            except (OSError, RuntimeError) as e:  # only a picture: the optimization does not depend on it
                job.say(f"no 'before' picture of {profiles.port_key(d)}: {e}")
        if len(before) < len(devs):
            job.skip("not every camera, see the log")

        results = calibrate_all(devs, fps=fps, save=False, log=job.say, step=job.step, skipped=job.skip)

        job.step("Taking 'after' pictures")
        for d, r in results.items():
            try:
                after = snapshot_frame(d, r.settings, say=job.say)
            except (OSError, RuntimeError) as e:
                job.say(f"no 'after' picture of {profiles.port_key(d)}: {e}")
                continue
            if d in before:
                save_before_after(d, "before", before[d], "settings before optimizing", PENDING_PICTURES)
            save_before_after(d, "after", after, "optimized", PENDING_PICTURES)
            PREVIEW.set(d, "After: optimized", after)
        PENDING.parent.mkdir(parents=True, exist_ok=True)
        PENDING.write_text(json.dumps({"cameras": {profiles.port_key(d): {"settings": r.settings.to_json(),
                                                                          "meta": r.meta}
                                                   for d, r in results.items()}}, indent=1))

        job.step("Previous settings back until you apply")
        for d in devs:
            _put_back(d)
        job.result = {d: {"port": profiles.port_key(d), "settings": r.settings.label(),
                          "white": r.meta["reference"]["white"], "noise": r.meta["reference"]["tnoise"],
                          "mjpeg_q": r.meta.get("mjpeg_q"), "pending": True} for d, r in results.items()}
        job.finish()
    finally:
        if keeper_was_on:
            service.start()  # the previous profiles
        if was_running and not autodarts_active():
            systemctl("start")
        pause_live(False)


def apply(job: Job, pause_live: Callable[[bool], None]) -> None:
    """Keep the pending calibration: save the profiles, save them as a preset that loads at every boot, and
    restart Autodarts so the cameras open with them (the MJPEG Q only takes effect at a stream start)."""
    pend = load_pending()
    devs = [d for d in cameras() if profiles.port_key(d) in pend]
    if not devs:
        raise RuntimeError("no calibration waiting to be applied (or its cameras are not connected)")
    from . import live  # live imports this module
    was_running = autodarts_active()
    pause_live(True)
    try:
        job.step("Saving the calibration")
        for d in devs:
            p = pend[profiles.port_key(d)]
            profiles.put(d, Settings.from_json(p["settings"]), p["meta"])
            clear_before_after(d)
            src = PENDING_PICTURES / profiles.port_key(d)
            if src.exists():
                BEFORE_AFTER.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(BEFORE_AFTER / profiles.port_key(d)))
        name = f"Optimized {time.strftime('%Y-%m-%d %H:%M')}"
        job.step(f"Saving as default preset “{name}”")
        live.save_preset(name)
        profiles.set_default_preset(name)
        job.result = {"preset": name}
        discard()

        job.step("Stopping Autodarts")
        if was_running:
            systemctl("stop")
        service.stop()
        _wait_cameras_free(devs)
        job.step("Applying the settings")
        tick({})  # cameras not streaming: writes the MJPEG Q for the next stream start
        service.enable()
        if was_running:
            job.step("Starting Autodarts")
            systemctl("start")
            if not _wait_streaming(devs, AUTODARTS_START_TIMEOUT):
                job.say("Autodarts did not open all cameras in time; the keeper will apply later")
        job.finish()
    finally:
        if was_running and not autodarts_active():
            systemctl("start")
        pause_live(False)


def restore(job: Job, pause_live: Callable[[bool], None]) -> None:
    """Back to the camera's stock JPEG quality and Autodarts' own control values."""
    devs = [s["device"] for s in map(support, cameras()) if s["supported"]]
    service.stop()
    service.migrate_legacy()
    was_running = autodarts_active()
    pause_live(True)
    try:
        job.step("Stopping Autodarts")
        if was_running:
            systemctl("stop")
        _wait_cameras_free(devs)
        job.step("Restoring stock JPEG quality and controls")
        for d in devs:
            with XU(d) as xu:
                xu.unlock()
                build = xu.firmware_build()
                for cls, addr in Q_ADDR[build].items():
                    xu.mem_write(addr, bytes([Q_DEFAULT[cls]]))
            prev = (profiles.get(d) or {}).get("meta", {}).get("previous")
            if prev:  # control values from before the optimization (Autodarts re-writes its own anyway)
                with Camera(d) as cam:
                    for name, v in Settings.from_json(prev).uvc.items():
                        try:
                            cam.set(name, v)
                        except OSError:
                            pass
            profiles.remove(d)
            clear_before_after(d)
        discard()
        if was_running:
            job.step("Starting Autodarts")
            systemctl("start")
        job.finish()
    finally:
        service.sync()  # other cameras may still have kept settings
        if was_running and not autodarts_active():
            systemctl("start")
        pause_live(False)


JOB = Job()  # the one optimize/apply/restore job of this process; pages poll it, it outlives them
