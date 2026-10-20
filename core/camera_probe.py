"""Camera probe: measures the camera behaviour the Camera Optimizer relies on, for one camera.

The optimizer was built on measurements of HBV OV2710 cameras (vid 0bdc). Other brands can differ
in any of them, so this probe re-measures each one and saves the results to share:
  identity       USB ids, manufacturer, firmware (bcdDevice), controls with ranges, formats
  exposure       written-exposure response at minimum gain: does white saturate IN THE SENSOR below
                 255 (the "sensor ceiling" the knee search needs), and up to which exposure the stream
                 keeps its frame rate
  gain / gamma   response and noise at a fixed exposure
  latency        frames until a control change shows up (the optimizer flushes 4)
  auto exposure  level per backlight_compensation value, whether AE follows gain, what
                 exposure_time_absolute reads while AE runs
  AE lock        switching to manual without writing an exposure: does the picture hold, is it less
                 noisy than the written exposure of the same brightness, do gain/gamma writes and a
                 stream reopen keep it
The camera's controls are restored at the end, whatever happens.

Autodarts must be stopped (it holds the cameras). Empty board, lights on as for playing.
Run: python -m core.camera_probe [--device /dev/videoN] [--size 1280x960]
"""

from __future__ import annotations
import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from core.v4l2_device import V4L2Device

logger = logging.getLogger("suit.camera_probe")

FLUSH = 6  # frames dropped after a control change (latency is measured separately)
BURST = 8
AE_SETTLE_S = 4.0
LOCK_WATCH_S = 3.0


def _run(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except Exception as e:
        return f"<{cmd[0]} failed: {e}>"


def usb_identity(dev: str) -> Dict[str, str]:
    """vid/pid/strings of the USB device behind a /dev/videoN node, from sysfs."""
    node = os.path.basename(os.path.realpath(dev))
    p = Path(f"/sys/class/video4linux/{node}/device").resolve()
    for d in [p, *p.parents]:
        if (d / "idVendor").exists():
            def rd(name: str) -> str:
                try:
                    return (d / name).read_text().strip()
                except Exception:
                    return ""
            return {k: rd(k) for k in ("idVendor", "idProduct", "manufacturer", "product", "serial", "bcdDevice", "speed")} \
                | {"sysfs": str(d)}
    return {}


def ae_menu(dev: str) -> Dict[int, str]:
    out = _run("v4l2-ctl", "-d", dev, "--list-ctrls-menus")
    m = re.search(r"auto_exposure .*?\n((?:\s+\d+: .*\n)+)", out)
    return {int(k): v.strip() for k, v in re.findall(r"(\d+): (.*)", m.group(1))} if m else {}


class Probe:
    def __init__(self, dev: str, size: Tuple[int, int], out_dir: Path):
        self.dev = dev
        self.size = size
        self.out_dir = out_dir
        self.device = V4L2Device(dev)
        self.ctrls = self.device.query_controls()
        self.cap: Optional[cv2.VideoCapture] = None
        self.masks: Dict[str, np.ndarray] = {}
        self.images: List[Tuple[str, np.ndarray]] = []

    # -- stream ---------------------------------------------------------------------------------

    def open(self) -> bool:
        # No CAP_PROP_BUFFERSIZE=1: it makes reads lag the stream and fps readings wrong
        cap = cv2.VideoCapture(self.dev, cv2.CAP_V4L2)
        if not cap.isOpened():
            return False
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.size[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.size[1])
        self.cap = cap
        return self.read() is not None

    def close(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def read(self) -> Optional[np.ndarray]:
        ok, f = self.cap.read()
        return cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) if ok and f is not None else None

    def burst(self, n: int = BURST, flush: int = FLUSH) -> Optional[Dict]:
        for _ in range(flush):
            self.cap.grab()
        t0 = time.monotonic()
        frames = [self.read() for _ in range(n)]
        dt = time.monotonic() - t0
        frames = [f for f in frames if f is not None]
        if len(frames) < n:
            return None
        st = np.stack(frames).astype(np.float32)
        m = st.mean(axis=0)
        sd = st.std(axis=0, ddof=1)
        r = {"fps": round((n - 1) / dt if dt > 0 else 0.0, 1), "mean": round(float(m.mean()), 2),
             "p999": round(float(np.percentile(st, 99.9)), 1), "clip_pct": round(float((st >= 254).mean() * 100), 3)}
        for k, mask in self.masks.items():
            r[k] = round(float(np.median(m[mask])), 2)
            r[k + "_noise"] = round(float(sd[mask].mean()), 3)
        r["_frame"] = frames[0]
        return r

    def set(self, name: str, value: int) -> bool:
        return name in self.ctrls and self.device.set_control(name, int(value))

    def rng(self, name: str) -> Optional[Tuple[int, int, int]]:
        c = self.ctrls.get(name)
        return (c.min_val, c.max_val, c.default_val) if c else None

    def settle_ae(self) -> Optional[Dict]:
        """Wait for auto exposure: at least AE_SETTLE_S, then until two bursts agree within 1 %."""
        time.sleep(AE_SETTLE_S)
        prev = None
        for _ in range(12):
            b = self.burst(4, flush=2)
            if b is None:
                return None
            if prev is not None and abs(b["mean"] - prev["mean"]) <= 0.01 * max(prev["mean"], 1):
                b["settled"] = True
                return b
            prev = b
        prev["settled"] = False
        return prev

    @staticmethod
    def clean(b: Optional[Dict]) -> Optional[Dict]:
        return {k: v for k, v in b.items() if not k.startswith("_")} if b else None

    # -- measurements ----------------------------------------------------------------------------

    def run(self, log) -> Dict:
        res: Dict = {}
        auto_vals = [k for k in ae_menu(self.dev) if k != 1]
        ae_auto = 3 if 3 in auto_vals else (auto_vals[0] if auto_vals else None)
        res["ae_auto_value"] = ae_auto

        # Reference picture at the camera's own auto exposure: defines the bright / dark / mid pixels
        log("reference at auto exposure")
        for name in ("gain", "gamma", "contrast", "brightness", "sharpness", "backlight_compensation"):
            r = self.rng(name)
            if r:
                self.set(name, r[2])
        if ae_auto is not None:
            self.set("auto_exposure", ae_auto)
        ref = self.settle_ae()
        if ref is None:
            raise RuntimeError("no frames")
        g = ref["_frame"].astype(np.float32)
        lo, mid_lo, mid_hi, hi = np.percentile(g, [30, 40, 60, 85])
        self.masks = {"bright": g >= hi, "dark": g <= lo, "midtone": (g >= mid_lo) & (g <= mid_hi)}
        res["reference_auto"] = self.clean(self.burst())
        self.images.append(("reference_auto", ref["_frame"]))

        e = self.rng("exposure_time_absolute")
        gn = self.rng("gain")
        if e is None or "auto_exposure" not in self.ctrls:
            res["error"] = "no manual exposure control"
            return res

        # Written-exposure response at minimum gain
        log("exposure response")
        self.set("auto_exposure", 1)
        if gn:
            self.set("gain", gn[0])
        e_min, e_max = max(e[0], 1), e[1]
        steps = sorted({int(round(v)) for v in np.geomspace(max(e_min, 2), e_max, 16)})
        rows = []
        for ex in steps:
            self.set("exposure_time_absolute", ex)
            b = self.burst()
            if b is None:
                rows.append({"exposure": ex, "error": "no frames"})
                continue
            rows.append({"exposure": ex, **self.clean(b)})
            if ex in (steps[len(steps) // 2], steps[-1]):
                self.images.append((f"exposure_{ex}_gain_min", b["_frame"]))
            log(f"  exposure {ex}: bright {b['bright']:.1f} dark {b['dark']:.1f} fps {b['fps']}")
        res["exposure_response"] = rows
        ok = [r for r in rows if "bright" in r]
        # sensor ceiling: bright median stops growing (<2 %) while exposure still rises, below 8-bit clip
        # (from 40 DN up: near black the median also sits still, on the noise floor)
        ceiling = None
        for a, b in zip(ok, ok[1:]):
            if a["bright"] >= 40 and b["bright"] < 250 and b["bright"] - a["bright"] < 0.02 * max(a["bright"], 1) and b["exposure"] > a["exposure"] * 1.1:
                ceiling = {"dn": a["bright"], "from_exposure": a["exposure"]}
                break
        res["sensor_ceiling_at_min_gain"] = ceiling
        fps_full = max((r["fps"] for r in ok), default=0)
        res["max_exposure_full_fps"] = max((r["exposure"] for r in ok if r["fps"] >= 0.9 * fps_full), default=None)

        # A mid exposure for the remaining tests: the one with the bright pixels nearest 50 % of max level
        top = max((r["bright"] for r in ok), default=255)
        e_mid = min(ok, key=lambda r: abs(r["bright"] - 0.5 * top))["exposure"] if ok else e[2]
        res["test_exposure"] = e_mid

        log("control latency")
        self.set("exposure_time_absolute", e_mid)
        self.burst(4)
        self.set("exposure_time_absolute", max(e_min, e_mid // 3))
        levels = [float(self.read().mean()) for _ in range(10)]
        base = levels[-1]
        res["latency_frames_after_exposure_write"] = next((i for i, v in enumerate(levels) if abs(v - base) < 0.03 * max(base, 1)), None)
        res["latency_levels"] = [round(v, 1) for v in levels]

        if gn:
            log("gain response")
            self.set("exposure_time_absolute", e_mid)
            rows = []
            for gv in sorted({int(v) for v in np.linspace(gn[0], gn[1], 7)}):
                self.set("gain", gv)
                b = self.burst()
                if b:
                    rows.append({"gain": gv, **self.clean(b)})
            res["gain_response"] = rows
            self.set("gain", gn[0])

        gm = self.rng("gamma")
        if gm:
            log("gamma response")
            rows = []
            for gv in sorted({gm[0], gm[2], int(gm[2] + 0.75 * (gm[1] - gm[2])), gm[1]}):
                self.set("gamma", gv)
                b = self.burst()
                if b:
                    rows.append({"gamma": gv, **self.clean(b)})
            res["gamma_response"] = rows
            self.set("gamma", gm[2])

        if ae_auto is None:
            res["auto_exposure"] = "no auto mode in the menu"
            return res

        # Auto exposure per metering bias, and the lock test at each
        log("auto exposure and lock")
        blc = self.rng("backlight_compensation")
        blc_vals = list(range(blc[0], blc[1] + 1)) if blc and blc[1] - blc[0] <= 8 else ([blc[0], blc[2], blc[1]] if blc else [None])
        ae_rows = []
        for v in blc_vals:
            if v is not None:
                self.set("backlight_compensation", v)
            if gn:
                self.set("gain", gn[0])
            self.set("auto_exposure", ae_auto)
            b = self.settle_ae()
            if b is None:
                continue
            row = {"backlight_compensation": v, "auto": self.clean(b),
                   "exposure_reads_during_auto": self.device.get_control("exposure_time_absolute")}
            # lock: manual mode without writing an exposure
            self.set("auto_exposure", 1)
            watch = []
            t0 = time.monotonic()
            while time.monotonic() - t0 < LOCK_WATCH_S:
                lb = self.burst(4, flush=1)
                if lb:
                    watch.append(lb["mean"])
            lock = self.burst()
            row["lock"] = self.clean(lock)
            row["lock_level_drift"] = watch
            row["exposure_reads_after_lock"] = self.device.get_control("exposure_time_absolute")
            # gamma write while locked
            if gm and lock:
                self.set("gamma", gm[1])
                self.set("gamma", gm[2])
                after = self.burst()
                row["lock_after_gamma_write_mean"] = after["mean"] if after else None
            # written exposure of the same brightness, for the noise comparison
            if lock and ok:
                xs = np.array([r["exposure"] for r in ok], float)
                ys = np.maximum.accumulate(np.array([r["mean"] for r in ok], float))
                if ys[0] <= lock["mean"] <= ys[-1]:
                    e_eq = int(round(np.interp(lock["mean"], ys, xs)))
                    self.set("exposure_time_absolute", e_eq)
                    w = self.burst()
                    row["written_equivalent"] = {"exposure": e_eq, **(self.clean(w) or {})}
            ae_rows.append(row)
            log(f"  blc {v}: auto mean {b['mean']:.1f}, lock {lock['mean'] if lock else None}")
            if v == (blc[2] if blc else None) and lock:
                self.images.append(("ae_lock_default_blc", lock["_frame"]))
        res["auto_exposure"] = ae_rows

        # Does AE follow gain (meters after the gain stage) or ignore it?
        if gn:
            log("AE versus gain")
            rows = []
            if blc:
                self.set("backlight_compensation", blc[2])
            for gv in (gn[0], gn[2], gn[1]):
                self.set("gain", gv)
                self.set("auto_exposure", ae_auto)
                b = self.settle_ae()
                if b:
                    rows.append({"gain": gv, **self.clean(b)})
            res["ae_versus_gain"] = rows

        # Does a stream reopen keep a lock?
        log("lock across a stream reopen")
        self.set("auto_exposure", ae_auto)
        self.settle_ae()
        self.set("auto_exposure", 1)
        before = self.burst()
        self.close()
        time.sleep(0.5)
        self.open()
        after = self.burst()
        res["lock_across_reopen"] = {"before": before["mean"] if before else None, "after": after["mean"] if after else None,
                                     "auto_exposure_after": self.device.get_control("auto_exposure")}
        return res


def find_cameras() -> List[str]:
    out = []
    for p in sorted(Path("/dev").glob("video*"), key=lambda p: int(re.sub(r"\D", "", p.name) or 0)):
        info = _run("v4l2-ctl", "-d", str(p), "--all")
        if "Video Capture" in info and "Format Video Capture" in info:
            out.append(str(p))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", help="camera node, e.g. /dev/video0 (default: the first camera)")
    ap.add_argument("--size", default="1280x960")
    ap.add_argument("--out", default=str(Path.home()), help="folder for the result zip")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    dev = args.device or next(iter(find_cameras()), None)
    if not dev:
        print("No camera found.")
        return 1
    size = tuple(int(v) for v in args.size.lower().split("x"))
    ident = usb_identity(dev)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = re.sub(r"[^A-Za-z0-9]+", "-", f"{ident.get('manufacturer', '')}-{ident.get('idVendor', '')}{ident.get('idProduct', '')}").strip("-")
    out_dir = Path(args.out)
    zpath = out_dir / f"suit-camera-probe-{name or 'camera'}-{stamp}.zip"

    report = {"device": dev, "size": size, "time": stamp, "usb": ident,
              "v4l2_all": _run("v4l2-ctl", "-d", dev, "--all"),
              "controls": _run("v4l2-ctl", "-d", dev, "--list-ctrls-menus"),
              "formats": _run("v4l2-ctl", "-d", dev, "--list-formats-ext"),
              "lsusb": _run("lsusb", "-v", "-d", f"{ident.get('idVendor', '')}:{ident.get('idProduct', '')}")}
    print(f"Camera {dev}: {ident.get('manufacturer', '?')} / {ident.get('product', '?')} "
          f"({ident.get('idVendor', '?')}:{ident.get('idProduct', '?')}, firmware {ident.get('bcdDevice', '?')})")

    probe = Probe(dev, size, out_dir)
    probe.device.take_snapshot()
    try:
        if not probe.open():
            print("Could not open the camera stream. Is Autodarts still running? Stop it first:\n"
                  "  sudo systemctl stop autodarts")
            report["measurements"] = {"error": "stream busy or not available"}
        else:
            t0 = time.monotonic()
            report["measurements"] = probe.run(lambda s: print(f"[{time.monotonic() - t0:5.0f}s] {s}", flush=True))
    except Exception as e:
        logger.exception("probe failed")
        report.setdefault("measurements", {})["error"] = repr(e)
    finally:
        probe.close()
        probe.device.restore_snapshot()

    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("probe.json", json.dumps(report, indent=1, default=str))
        for label, img in probe.images:
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
            if ok:
                z.writestr(f"{label}.jpg", buf.tobytes())
    print(f"\nSaved {zpath}\nPlease send this file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
