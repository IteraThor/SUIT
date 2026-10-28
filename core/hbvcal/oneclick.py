"""One-click workflow behind the Camera Optimizer page: support check, optimize and restore.

Safety: the tuning back door (XU unlock + memory writes) is only ever used on cameras that pass
`support()`: USB ID 0bdc:2710 (HBV OV2710 / Realtek bridge) AND a firmware build whose memory layout we
verified. Other cameras are never touched. The user must also confirm once that they use HBV OV2710 cameras.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from typing import Callable, List, Optional

from . import profiles, service
from .autocal import PREVIEW, calibrate_all
from .capture import START_DISCARD, burst
from .daemon import cameras, is_streaming, tick
from .settings import Q_ADDR, Q_DEFAULT, Settings, apply_prestream, apply_streaming
from .v4l2 import Camera
from .xu import XU

HBV_USB_ID = profiles.HBV_USB_ID
BEFORE_AFTER = profiles.STORE.parent / "beforeafter"
AUTODARTS_START_TIMEOUT = 60


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
            try:
                fn(self, *args)
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                if self.steps and self.steps[-1]["state"] == "running":
                    self.steps[-1]["state"] = "failed"
            finally:
                self.running = False

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        return True

    def step(self, title: str):
        if self.steps and self.steps[-1]["state"] == "running":
            self.steps[-1]["state"] = "done"
        self.steps.append({"title": title, "state": "running"})

    def fail(self, note: str):
        """The current step did not work out, but the job goes on (e.g. one compression level)."""
        if self.steps and self.steps[-1]["state"] == "running":
            self.steps[-1]["state"] = "failed"
            self.steps[-1]["title"] += f": {note}"

    def finish(self):
        if self.steps and self.steps[-1]["state"] == "running":
            self.steps[-1]["state"] = "done"

    def to_json(self) -> dict:
        return {"name": getattr(self, "name", ""), "running": self.running, "steps": self.steps,
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


def snapshot_frame(dev: str, s: Settings = None) -> bytes:
    """One complete camera frame (raw JPEG). With `s`, the settings are applied first (as the keeper would)."""
    with XU(dev) as xu, Camera(dev) as cam:
        if s is not None:
            apply_prestream(xu, s, 1280)
        cam.configure(1280, 960, "MJPG", 30)
        cam.start()
        try:
            burst(cam, 0, discard=START_DISCARD)
            if s is not None:
                apply_streaming(cam, xu, s)
            return burst(cam, 1, discard=20).frames[-1].data
        finally:
            cam.stop()


def save_before_after(dev: str, which: str, data: bytes, note: str = "") -> None:
    d = BEFORE_AFTER / profiles.port_key(dev)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{which}.jpg").write_bytes(data)
    meta_path = d / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    meta[which] = {"time": time.strftime("%Y-%m-%d %H:%M"), "note": note}
    meta_path.write_text(json.dumps(meta))


def clear_before_after(dev: str) -> None:
    shutil.rmtree(BEFORE_AFTER / profiles.port_key(dev), ignore_errors=True)


def before_after_info() -> list:
    """Pictures of the cameras that are optimized now (older pictures belong to an undone optimization)."""
    out = []
    for dev in cameras():
        d = BEFORE_AFTER / profiles.port_key(dev)
        if not profiles.is_calibrated(profiles.get(dev)):
            continue
        if (d / "before.jpg").exists() and (d / "after.jpg").exists():
            meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else {}
            out.append({"device": dev, "port": profiles.port_key(dev), "meta": meta})
    return out


def optimize(job: Job, pause_live: Callable[[bool], None], fps: int = 30) -> None:
    devs = [s["device"] for s in map(support, cameras()) if s["supported"]]
    if not devs:
        raise RuntimeError("no supported HBV OV2710 camera found")
    was_running = autodarts_active()
    service.migrate_legacy()
    keeper_was_on = service.is_enabled()
    service.stop()  # it would re-apply the old profile onto the calibration stream
    pause_live(True)
    PREVIEW.clear()
    ok = False
    try:
        job.step("Stopping Autodarts")
        if was_running:
            systemctl("stop")
        if not _wait_cameras_free(devs):
            raise RuntimeError("cameras are still in use by another program")

        job.step("Taking 'before' pictures")
        before = {}
        for d in devs:  # cameras still hold the settings Autodarts gave them
            before[d] = snapshot_frame(d)
            PREVIEW.set(d, "Before: the settings Autodarts used", before[d])

        results = calibrate_all(devs, fps=fps, log=job.log.append, step=job.step, failed=job.fail)

        job.step("Taking 'after' pictures")
        for d, r in results.items():
            save_before_after(d, "before", before[d], "settings before optimizing")
            after = snapshot_frame(d, r.settings)
            save_before_after(d, "after", after, "optimized")
            PREVIEW.set(d, "After: optimized", after)

        job.step("Saving profiles")  # calibrate_all saved them
        job.result = {d: {"port": profiles.port_key(d), "settings": r.settings.label(),
                          "white": r.meta["reference"]["white"], "noise": r.meta["reference"]["tnoise"],
                          "mjpeg_q": r.meta.get("mjpeg_q")} for d, r in results.items()}

        if was_running:
            job.step("Starting Autodarts")
            systemctl("start")
            job.step("Applying settings after Autodarts opened the cameras")
            if not _wait_streaming(devs, AUTODARTS_START_TIMEOUT):
                job.log.append("Autodarts did not open all cameras in time; the keeper will apply later")
            tick({})
        service.enable()
        ok = True
        job.finish()
    finally:
        if not ok:
            if keeper_was_on:
                service.start()  # back to the previous profiles
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
        if was_running:
            job.step("Starting Autodarts")
            systemctl("start")
        job.finish()
    finally:
        service.sync()  # other cameras may still have kept settings
        if was_running and not autodarts_active():
            systemctl("start")
        pause_live(False)


JOB = Job()  # the one optimize/restore job of this process; pages poll it, it outlives them
