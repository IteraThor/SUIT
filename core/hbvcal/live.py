"""Live view, readouts, manual adjustments and presets behind the Camera Controls page.

Works for every UVC camera. The hidden layers (MJPEG quality, sensor gain) are only used on HBV cameras after
the user confirmed them (profiles.hidden_ok). A camera's changes are kept (re-applied by the keeper service)
once it has a profile: from the optimizer, from "Keep these settings", or from a loaded preset.

Live view: the page streams the camera itself when it is free. While Autodarts holds the camera it shows
Autodarts' own current frame (http://127.0.0.1:3180/api/img/cams/<i>), and controls still work (V4L2/XU
access does not need the stream).
"""

from __future__ import annotations

import threading
import time
import tomllib
import urllib.request
from pathlib import Path
from typing import Dict, Optional

import cv2
import numpy as np

from . import board, oneclick, profiles, service
from .autocal import WHITE_TARGET
from .capture import START_DISCARD, luma, valid_jpeg
from .daemon import cameras, is_streaming
from .settings import Q_ADDR, Q_DEFAULT, Settings, apply_prestream, apply_streaming, apply_uvc, q_addr, set_uvc
from .v4l2 import Camera
from .xu import XU

AUTODARTS_API = "http://127.0.0.1:3180"
AUTODARTS_CONFIG = Path.home() / ".config" / "autodarts" / "config.toml"
Q_COMMITTED = 0xC102  # Q factor committed at the last stream start (same address on both firmware builds)
UVC_CONTROLS = ["exposure_time_absolute", "gain", "gamma", "sharpness", "contrast", "brightness", "saturation",
                "hue", "white_balance_temperature", "white_balance_automatic", "auto_exposure",
                "power_line_frequency", "backlight_compensation"]
IDLE_S = 15  # a live view nobody asked for frames in this long hands its camera back


def autodarts_index(dev: str) -> Optional[int]:
    """Position of this camera in Autodarts' camera list (its image endpoint index)."""
    try:
        devs = tomllib.loads(AUTODARTS_CONFIG.read_text()).get("cam", {}).get("devices", [])
    except (OSError, ValueError):
        return None
    port = profiles.port_key(dev)
    for i, uri in enumerate(devs):
        if f"location={port}" in uri:
            return i
    return None


hbv = profiles.hbv
hidden_ok = profiles.hidden_ok


# --- image readouts ---------------------------------------------------------------------------------

def laplacian_var(img: np.ndarray, mask: np.ndarray) -> float:
    lap = cv2.Laplacian(img.astype(np.float32), cv2.CV_32F, ksize=3)
    return float(lap[mask].var())


def blockiness(img: np.ndarray, mask: np.ndarray) -> float:
    """Mean |step| across 8×8 block boundaries divided by mean |step| inside blocks (1.0 = none)."""
    f = img.astype(np.float32)
    dh = np.abs(np.diff(f, axis=1))  # between column x and x+1
    dv = np.abs(np.diff(f, axis=0))
    mh = mask[:, 1:] & mask[:, :-1]
    mv = mask[1:, :] & mask[:-1, :]
    colb = (np.arange(dh.shape[1]) % 8) == 7
    rowb = (np.arange(dv.shape[0]) % 8) == 7
    bnd = np.concatenate([dh[:, colb][mh[:, colb]], dv[rowb, :][mv[rowb, :]]])
    inn = np.concatenate([dh[:, ~colb][mh[:, ~colb]], dv[~rowb, :][mv[~rowb, :]]])
    return float(bnd.mean() / max(inn.mean(), 1e-6))


class Live:
    """Latest frame of one camera: own stream if free, else Autodarts' image endpoint."""

    def __init__(self, dev: str):
        self.dev = dev
        self.frame: Optional[bytes] = None  # raw JPEG
        self.bgr: Optional[np.ndarray] = None
        self.frames = []  # recent decoded luma for scores
        self.sizes = []
        self.source = "none"
        self.last_request = 0.0
        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None
        self.pause = threading.Event()
        self.frame_count = 0

    def touch(self):
        self.last_request = time.time()
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def stop(self):
        """Hand the camera back now instead of after IDLE_S."""
        self.last_request = 0.0

    def _wanted(self) -> bool:
        return time.time() - self.last_request < IDLE_S

    def _push(self, data: bytes):
        bgr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            return
        y = luma(bgr)
        with self.lock:
            self.frame = data
            self.bgr = bgr
            self.frames = (self.frames + [y])[-10:]
            self.sizes = (self.sizes + [len(data)])[-30:]

    def _run(self):
        while self._wanted():
            if self.pause.is_set():
                time.sleep(0.5)
                continue
            # Never take a camera away from Autodarts: while its service is active (also during a restart,
            # when the cameras are briefly free) only show Autodarts' own picture.
            if oneclick.autodarts_active():
                self._autodarts_poll()
                continue
            try:
                self._own_stream()
            except OSError:
                self._autodarts_poll()
        self.source = "none"

    def _own_stream(self):
        s = profiles.get_settings(self.dev)
        width = 1280
        hidden = s is not None and hidden_ok(self.dev)
        if s is not None and not hidden:
            s = profiles.uvc_only(s)
        with XU(self.dev) as xu, Camera(self.dev) as cam:
            if hidden:
                apply_prestream(xu, s, width)
            cam.configure(width, 960, "MJPG", 30)
            cam.start()  # raises EBUSY when Autodarts streams
            try:
                self.source = "own stream"
                for _ in range(START_DISCARD):
                    cam.read()
                if hidden:  # a stream start resets sensor gain: re-apply the whole profile
                    apply_streaming(cam, xu, s)
                elif s is not None:
                    apply_uvc(cam, s.uvc, strict=False)
                while self._wanted() and not self.pause.is_set():
                    if self.frame_count % 30 == 0 and oneclick.autodarts_active():
                        break  # Autodarts is starting: hand the camera back immediately
                    self.frame_count += 1
                    f = cam.read()
                    if valid_jpeg(f.data):
                        self._push(f.data)
            finally:
                cam.stop()
        self.source = "none"

    def _autodarts_poll(self):
        idx = autodarts_index(self.dev)
        if idx is None:
            self.source = "busy (not an Autodarts camera)"
            time.sleep(1)
            return
        try:
            with urllib.request.urlopen(f"{AUTODARTS_API}/api/img/cams/{idx}", timeout=3) as r:
                self._push(r.read())
            self.source = "Autodarts"
        except OSError:
            self.source = "busy (no Autodarts image)"
        time.sleep(0.5)

    def scores(self) -> dict:
        with self.lock:
            fr = list(self.frames)
            sizes = list(self.sizes)
        if not fr:
            return {"source": self.source}
        y = np.stack(fr)
        H = board.load_homography(self.dev, y.shape[2], y.shape[1]) if y.shape[1:] == (960, 1280) else None
        mask = board.board_mask_image(H, y.shape[1:]) if H is not None else np.ones(y.shape[1:], bool)
        m = y.mean(0)[mask]
        out = {"source": self.source, "white": float(np.percentile(m, 97)), "black": float(np.percentile(m, 3)),
               "mean": float(m.mean()), "sharpness": laplacian_var(y[-1], mask),
               "blockiness": blockiness(y[-1], mask), "kb": float(np.mean(sizes) / 1024),
               "white_target": WHITE_TARGET, "board_mask": H is not None}
        if len(y) >= 4 and self.source == "own stream":
            out["tnoise"] = float(np.median(y.std(0, ddof=1)[mask]))
        out["clip_pct"] = float(100 * (y[-1][mask] >= 250).mean())
        return out


LIVE: Dict[str, Live] = {}


def live(dev: str) -> Live:
    if dev not in LIVE:
        LIVE[dev] = Live(dev)
    return LIVE[dev]


def pause_live(on: bool) -> None:
    for lv in LIVE.values():
        (lv.pause.set if on else lv.pause.clear)()


def stop_all() -> None:
    for lv in LIVE.values():
        lv.stop()


# --- controls ---------------------------------------------------------------------------------------

def camera_info(dev: str) -> dict:
    prof = profiles.get(dev)
    info = {"device": dev, "port": profiles.port_key(dev), "autodarts_index": autodarts_index(dev),
            "profile": prof, "controls": {}, "usb_id": profiles.usb_id(dev), "hbv": hbv(dev),
            "hidden": False, "optimized": profiles.is_calibrated(prof)}
    try:
        with Camera(dev) as cam:
            for n in UVC_CONTROLS:
                try:
                    q = cam.query(n)
                    info["controls"][n] = {**q, "value": cam.get(n)}
                except OSError:
                    pass
    except OSError as e:
        info["error"] = str(e)
        return info
    if prof:
        info["calibrated"] = prof.get("meta", {}).get("calibrated_settings", prof["settings"]) \
            if info["optimized"] or "calibrated_settings" in prof.get("meta", {}) else None
    if not hidden_ok(dev):
        return info
    try:
        with XU(dev) as xu:
            xu.unlock()
            info["firmware"] = xu.firmware_build()
            info["q"] = xu.mem_read(q_addr(xu, 1280), 1)[0]          # applies at the next stream start
            info["q_active"] = xu.mem_read(Q_COMMITTED, 1)[0]          # what the running stream uses
            info["streaming"] = is_streaming(xu)
            if info["streaming"]:
                info["sensor_gain"] = xu.sensor_read(0x350B)
            info["hidden"] = True
    except (OSError, RuntimeError) as e:  # RuntimeError: firmware build whose memory layout is not verified
        info["hidden_error"] = str(e)
    return info


def current_settings(dev: str) -> Settings:
    """What the camera runs now, in every layer this camera may use."""
    info = camera_info(dev)
    s = Settings(uvc={n: c["value"] for n, c in info["controls"].items()})
    if info["hidden"]:
        s.mjpeg_q = info.get("q")
        if s.uvc.get("auto_exposure", 1) != 1:
            pass  # auto exposure drives the sensor gain itself: keeping a value would fight it
        elif "sensor_gain" in info:
            s.sensor[0x350B] = info["sensor_gain"]
        elif info.get("profile"):
            s.sensor = Settings.from_json(info["profile"]["settings"]).sensor
    return s


def _save_edit(dev: str, prof: dict, s: Settings, **meta_extra) -> None:
    """Store an edited profile; the first edit of a calibrated profile keeps the calibration for "reset"."""
    meta = dict(prof.get("meta", {}))
    if profiles.is_calibrated(prof):
        meta.setdefault("calibrated_settings", prof["settings"])
    profiles.put(dev, s, {**meta, **meta_extra})


def set_control(dev: str, layer: str, name: str, value: int) -> dict:
    """Set one control now. If the camera has a profile, the change is saved into it, so the keeper keeps it
    (otherwise the keeper would restore the profile value within 2 s)."""
    if layer != "uvc" and not hidden_ok(dev):
        raise ValueError("JPEG quality and sensor gain are only used on confirmed HBV OV2710 cameras")
    out = {"ok": True}
    prof = profiles.get(dev)
    s = Settings.from_json(prof["settings"]) if prof else None
    if layer == "uvc":
        if s is not None:
            s.uvc[name] = value
        with Camera(dev) as cam:
            set_uvc(cam, name, value)
        if name in ("exposure_time_absolute", "auto_exposure") and s and s.sensor and hidden_ok(dev):
            with XU(dev) as xu:  # UVC exposure writes reset sensor gain
                if is_streaming(xu):
                    for r, v in s.sensor.items():
                        xu.sensor_write(r, v)
    else:
        with XU(dev) as xu:
            xu.unlock()
            if layer == "q":
                if s is not None:
                    s.mjpeg_q = value
                xu.mem_write(q_addr(xu, 1280), bytes([value]))
                lv = LIVE.get(dev)
                if lv and lv.source == "own stream":  # restart our own stream so the new Q applies
                    lv.pause.set()
                    time.sleep(1.0)
                    lv.pause.clear()
                    out["note"] = "Applied (live view restarted)"
                elif is_streaming(xu):
                    out["needs_restart"] = True
                    out["note"] = "Saved. Autodarts' running stream keeps its quality until Autodarts restarts"
            elif layer == "sensor":
                if s is not None:
                    s.sensor[int(name, 16)] = value
                xu.sensor_write(int(name, 16), value)
    if s is not None:
        _save_edit(dev, prof, s, manual_edit=time.strftime("%Y-%m-%d %H:%M:%S"))
        out["saved_to_profile"] = True
    return out


def keep(dev: str) -> None:
    """Keep this camera's current settings: the keeper re-applies them after Autodarts restarts."""
    profiles.put(dev, current_settings(dev), {"source": "kept", "kept": time.strftime("%Y-%m-%d %H:%M:%S")})
    service.sync()


def _write_stock_q(dev: str) -> None:
    with XU(dev) as xu:
        xu.unlock()
        build = xu.firmware_build()
        for cls, addr in Q_ADDR[build].items():
            xu.mem_write(addr, bytes([Q_DEFAULT[cls]]))


def forget(dev: str) -> dict:
    """Stop keeping this camera's settings. An optimized camera also gets its stock JPEG quality and its
    control values from before the optimization back (Autodarts writes its own when it restarts)."""
    prof = profiles.get(dev)
    out = {"ok": True}
    if not prof:
        return out
    s = Settings.from_json(prof["settings"])
    if s.mjpeg_q is not None and hidden_ok(dev):
        _write_stock_q(dev)
        out["needs_restart"] = True
    prev = prof.get("meta", {}).get("previous")
    if prev:
        with Camera(dev) as cam:
            apply_uvc(cam, Settings.from_json(prev).uvc, strict=False)
    profiles.remove(dev)
    oneclick.clear_before_after(dev)
    service.sync()
    return out


def reset_profile(dev: str) -> dict:
    """Back to the values the last optimization picked; the keeper applies them within 2 s."""
    prof = profiles.get(dev)
    if not prof:
        raise ValueError("this camera has no profile: run Optimize first")
    meta = dict(prof.get("meta", {}))
    calibrated = meta.pop("calibrated_settings", None)
    meta.pop("manual_edit", None)
    meta.pop("preset", None)
    if calibrated is None:
        return {"ok": True, "note": "Already at the calibrated values"}
    profiles.put(dev, Settings.from_json(calibrated), meta)
    out = {"ok": True}
    if calibrated.get("mjpeg_q") != prof["settings"].get("mjpeg_q"):
        out["needs_restart"] = True
    return out


def reset_defaults(dev: str) -> dict:
    """Standard controls back to the camera's own defaults (saved into its profile, if it has one)."""
    with Camera(dev) as cam:
        defaults = {}
        for n in UVC_CONTROLS:
            try:
                defaults[n] = cam.query(n)["default"]
            except OSError:
                pass
        refused = apply_uvc(cam, defaults, strict=False)
    prof = profiles.get(dev)
    if prof:
        s = Settings.from_json(prof["settings"])
        s.uvc.update({k: v for k, v in defaults.items() if k not in refused})
        _save_edit(dev, prof, s, manual_edit=time.strftime("%Y-%m-%d %H:%M:%S"))
    return {"ok": True, "refused": list(refused)}


# --- presets ----------------------------------------------------------------------------------------

DEFAULT_PRESET = "Camera defaults"  # built in: every camera on its own defaults, nothing kept


def _check_name(name: str) -> None:
    if name.strip().lower() == DEFAULT_PRESET.lower():
        raise ValueError(f"“{DEFAULT_PRESET}” is built in; pick another name")


def save_preset(name: str) -> None:
    """Every camera's kept profile (what the keeper enforces), or its current settings if nothing is kept."""
    _check_name(name)
    presets = profiles.load_presets()
    presets[name] = {}
    for d in cameras():
        s = profiles.get_settings(d)
        presets[name][profiles.port_key(d)] = (s if s is not None else current_settings(d)).to_json()
    profiles.save_presets(presets)


def load_defaults() -> dict:
    """The built-in preset: stop keeping anything (an optimized camera also gets its stock JPEG quality back)
    and put every camera's standard controls on its own defaults."""
    out = {"ok": True, "cameras": 0}
    for dev in cameras():
        if forget(dev).get("needs_restart"):
            out["needs_restart"] = True
        reset_defaults(dev)
        out["cameras"] += 1
    return out


def load_preset(name: str) -> dict:
    """Every connected camera in the preset gets its settings as its kept profile and applied now."""
    if name == DEFAULT_PRESET:
        return load_defaults()
    entry = profiles.load_presets()[name]
    out = {"ok": True, "cameras": 0}
    for dev in cameras():
        js = entry.get(profiles.port_key(dev))
        if js is None:
            continue
        s = Settings.from_json(js)
        hidden = hidden_ok(dev)
        if not hidden:
            s = profiles.uvc_only(s)
        prof = profiles.get(dev)
        if prof:
            _save_edit(dev, prof, s, preset=name)
        else:
            profiles.put(dev, s, {"source": "preset", "preset": name})
        with Camera(dev) as cam:
            apply_uvc(cam, s.uvc, strict=False)
        if hidden and s.mjpeg_q is not None:
            with XU(dev) as xu:
                apply_prestream(xu, s, 1280)
                if is_streaming(xu) and xu.mem_read(Q_COMMITTED, 1)[0] != s.mjpeg_q:
                    out["needs_restart"] = True
        out["cameras"] += 1
    service.sync()
    return out


def rename_preset(old: str, new: str) -> None:
    _check_name(new)
    presets = profiles.load_presets()
    if new in presets:
        raise ValueError(f"a preset named {new!r} already exists")
    presets[new] = presets.pop(old)
    profiles.save_presets(presets)


def delete_preset(name: str) -> None:
    presets = profiles.load_presets()
    presets.pop(name, None)
    profiles.save_presets(presets)


def recheck(dev: str, trim: bool) -> dict:
    """Compare the white level with the calibration reference; optionally trim digital gain to restore it."""
    lv = live(dev)
    lv.touch()
    for _ in range(40):
        if len(lv.frames) >= 4:
            break
        time.sleep(0.25)
    sc = lv.scores()
    prof = profiles.get(dev)
    if not profiles.is_calibrated(prof) or "white" not in sc:
        return {"error": "no calibration or no frames", **sc}
    ref = prof["meta"].get("reference", {}).get("white", WHITE_TARGET)
    dev_pct = 100 * (sc["white"] - ref) / ref
    out = {"white": sc["white"], "reference": ref, "deviation_pct": round(dev_pct, 1),
           "verdict": "ok" if abs(dev_pct) < 6 else ("recalibrate" if abs(dev_pct) > 20 else "trim gain")}
    if trim and out["verdict"] != "ok":
        g = profiles.get_settings(dev).uvc.get("gain", 44)
        new = int(round(min(128, max(0, g * ref / max(sc["white"], 1)))))
        set_control(dev, "uvc", "gain", new)  # saves it into the profile
        out["gain"] = (g, new)
    return out


def board_view(dev: str, path: Path) -> Optional[np.ndarray]:
    """A stored camera frame (BGR), warped to Autodarts' 1000x1000 board view when its calibration is known."""
    img = cv2.imread(str(path))
    if img is None:
        return None
    H = board.load_homography(dev, img.shape[1], img.shape[0])
    if H is not None:
        img = board.warp(img, H, cv2.INTER_CUBIC)
    return img
