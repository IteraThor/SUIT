import os
import sys
import glob
import json
import time
import subprocess
from pathlib import Path
import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib
from core.logger import get_logger

logger = get_logger("display")

DEFAULT_CONFIG_PATH = Path.home() / ".suit_rotation_config.json"
DEFAULT_AUTOSTART_PATH = Path.home() / ".config" / "autostart" / "suit-rotation.desktop"
UDEV_RULE_PATH = "/etc/udev/rules.d/99-suit-touch.rules"

def normalize_rotation(rot: int | str | None) -> int:
    if rot is None:
        return 0
    if isinstance(rot, str):
        mapping = {"normal": 0, "right": 1, "inverted": 2, "left": 3}
        return mapping.get(rot.lower().strip(), 0)
    try:
        return int(rot)
    except (ValueError, TypeError):
        return 0

def get_rotation_matrix(rotation_int: int | str) -> str:
    rot_val = normalize_rotation(rotation_int)
    matrices = {
        0: "1 0 0 0 1 0 0 0 1",       # Normal (0)
        1: "0 -1 1 1 0 0 0 0 1",      # Right (90)
        2: "-1 0 1 0 -1 1 0 0 1",     # Inverted (180)
        3: "0 1 0 -1 0 1 0 0 1"       # Left (270)
    }
    return matrices.get(rot_val, matrices[0])

def mult3x3(A: list[float], B: list[float]) -> list[float]:
    C = [0.0] * 9
    for i in range(3):
        for j in range(3):
            C[i * 3 + j] = (
                A[i * 3 + 0] * B[0 * 3 + j] +
                A[i * 3 + 1] * B[1 * 3 + j] +
                A[i * 3 + 2] * B[2 * 3 + j]
            )
    return C

def calculate_affine_matrix(
    logical_monitors: list,
    monitors: list,
    connector_name: str,
    target_trans: int | None = None
) -> str | None:
    total_w = 0
    total_h = 0
    target_lm = None

    for lm in logical_monitors:
        x, y, scale, trans, is_primary, phys_monitors = lm[:6]
        for pm in phys_monitors:
            p_name = pm[0]
            for m_info in monitors:
                if m_info[0][0] == p_name:
                    for mode in m_info[1]:
                        if "is-current" in mode[6]:
                            w, h = int(mode[1]), int(mode[2])
                            eff_trans = trans
                            if p_name == connector_name and target_trans is not None:
                                eff_trans = target_trans

                            if eff_trans in [1, 3]:
                                w, h = h, w

                            total_w = max(total_w, int(x) + w)
                            total_h = max(total_h, int(y) + h)

                            if p_name == connector_name:
                                target_lm = {
                                    "x": int(x),
                                    "y": int(y),
                                    "w": w,
                                    "h": h,
                                    "trans": eff_trans
                                }

    if not target_lm or total_w == 0 or total_h == 0:
        logger.warning(f"Could not determine geometry for matrix calculation: {connector_name}")
        return None

    x, y, w, h, trans = (
        target_lm["x"], target_lm["y"], target_lm["w"], target_lm["h"], target_lm["trans"]
    )
    wf, hf = float(w) / float(total_w), float(h) / float(total_h)
    xf, yf = float(x) / float(total_w), float(y) / float(total_h)

    # Libinput rotation matrices (Mutter: 1=90 Right, 2=180, 3=270 Left)
    if trans == 1:   # screen 90 Right -> touch matrix for Right
        rot = [0, -1, 1, 1, 0, 0, 0, 0, 1]
    elif trans == 2: # screen 180 (inverted)   -> 180 (self-inverse)
        rot = [-1, 0, 1, 0, -1, 1, 0, 0, 1]
    elif trans == 3: # screen 270 Left -> touch matrix for Left
        rot = [0, 1, 0, -1, 0, 1, 0, 0, 1]
    else:            # Normal (0) / default
        rot = [1, 0, 0, 0, 1, 0, 0, 0, 1]

    s = [wf, 0, xf, 0, hf, yf, 0, 0, 1]
    matrix = mult3x3(s, rot)
    return " ".join([f"{v:.6f}" for v in matrix[:6]])

class DisplayService:
    @staticmethod
    def _get_mutter_proxy(retries: int = 1, delay: float = 0.5) -> Gio.DBusProxy | None:
        for attempt in range(retries):
            try:
                bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
                proxy = Gio.DBusProxy.new_sync(
                    bus,
                    Gio.DBusProxyFlags.NONE,
                    None,
                    "org.gnome.Mutter.DisplayConfig",
                    "/org/gnome/Mutter/DisplayConfig",
                    "org.gnome.Mutter.DisplayConfig",
                    None
                )
                if proxy:
                    return proxy
            except Exception:
                if attempt < retries - 1:
                    time.sleep(delay)
                else:
                    logger.debug("Mutter DisplayConfig DBus interface unavailable")
        return None

    @classmethod
    def get_current_mutter_state(cls, retries: int = 1) -> tuple | None:
        proxy = cls._get_mutter_proxy(retries=retries)
        if not proxy:
            return None
        try:
            state = proxy.call_sync("GetCurrentState", None, Gio.DBusCallFlags.NONE, 2000, None)
            return state.unpack()
        except Exception:
            logger.exception("Error querying Mutter DisplayConfig GetCurrentState")
            return None

    @classmethod
    def get_monitors(cls) -> list[dict]:
        results = []
        state = cls.get_current_mutter_state()
        if not state:
            return results

        serial, monitors, logical_monitors, properties = state
        for lm in logical_monitors:
            x, y, scale, trans, is_primary, phys_monitors = lm[:6]
            for pm in phys_monitors:
                name = pm[0]
                res = "Unknown"
                w, h = 0, 0
                for m_info in monitors:
                    if m_info[0][0] == name:
                        for mode in m_info[1]:
                            if "is-current" in mode[6]:
                                w, h = int(mode[1]), int(mode[2])
                                res = f"{w}x{h}"
                                break
                results.append({
                    "name": name,
                    "res": res,
                    "w": w, "h": h,
                    "x": int(x), "y": int(y),
                    "is_primary": bool(is_primary),
                    "rotation": int(trans)
                })
        return results

    @classmethod
    def build_monitors_variant(
        cls,
        serial: int,
        method: int,
        connector_name: str,
        rotation_int: int,
        logical_monitors: list,
        monitors: list
    ) -> GLib.Variant:
        new_lms = []
        for lm in logical_monitors:
            x, y, scale, trans, is_primary, phys_monitors = lm[:6]
            phys_configs = []
            for pm in phys_monitors:
                m_name = pm[0]
                mode_id = ""
                for m_info in monitors:
                    if m_info[0][0] == m_name:
                        for mode in m_info[1]:
                            if "is-current" in mode[6]:
                                mode_id = str(mode[0])
                                break
                if m_name == connector_name:
                    trans = int(rotation_int)
                phys_configs.append((m_name, mode_id, {}))
            new_lms.append((int(x), int(y), float(scale), int(trans), bool(is_primary), phys_configs))

        return GLib.Variant("(uua(iiduba(ssa{sv}))a{sv})", (int(serial), int(method), new_lms, {}))

    @classmethod
    def apply_rotation(cls, connector_name: str, rotation_int: int, method: int = 1) -> bool:
        proxy = cls._get_mutter_proxy()
        if not proxy:
            return False
        try:
            state = proxy.call_sync("GetCurrentState", None, Gio.DBusCallFlags.NONE, 2000, None)
            serial, monitors, logical_monitors, properties = state.unpack()

            variant = cls.build_monitors_variant(
                serial=serial,
                method=method,
                connector_name=connector_name,
                rotation_int=rotation_int,
                logical_monitors=logical_monitors,
                monitors=monitors
            )

            proxy.call_sync(
                "ApplyMonitorsConfig",
                variant,
                Gio.DBusCallFlags.NONE,
                3000,
                None
            )
            logger.info(f"Applied rotation {rotation_int} to {connector_name} (method {method})")
            return True
        except Exception:
            logger.exception(f"Failed applying rotation to {connector_name}")
            return False

    @staticmethod
    def get_touchscreens() -> list[str]:
        touchscreens = set()

        # Fast path: scan already-parsed /run/udev/data/ for event nodes.
        # Much faster than udevadm info --export-db which dumps the full DB.
        try:
            for entry_path in glob.glob("/run/udev/data/c13:*"):
                try:
                    with open(entry_path, "r") as f:
                        content = f.read()
                    if "ID_INPUT_TOUCHSCREEN=1" not in content:
                        continue
                    for line in content.splitlines():
                        # Prefer E:ID_MODEL (udev property), fallback to E:NAME
                        if line.startswith("E:ID_MODEL=") or line.startswith("E:NAME="):
                            # Format may be E:KEY=VALUE or E: KEY=VALUE
                            val = line.split("=", 1)[1].strip().strip('"')
                            vendor_line = next(
                                (l for l in content.splitlines() if l.startswith("E:ID_VENDOR=")),
                                ""
                            )
                            vendor = vendor_line.split("=", 1)[1].strip() if vendor_line else ""
                            full_name = f"{vendor} {val}".strip() if vendor else val
                            if full_name:
                                touchscreens.add(full_name)
                            break
                except Exception:
                    continue
        except Exception:
            logger.exception("Failed scanning /run/udev/data/ for touchscreens")

        # Fallback: read device names from sysfs, using INPUT_PROP_DIRECT (0x2)
        # to identify touchscreens without spawning any subprocess.
        if not touchscreens:
            for name_file in glob.glob("/sys/class/input/event*/device/name"):
                try:
                    props_file = name_file.replace("/name", "/properties")
                    if not Path(props_file).exists():
                        continue
                    with open(props_file, "r", encoding="utf-8") as f_props:
                        props_val = int(f_props.read().strip(), 16)
                    # INPUT_PROP_DIRECT (bit 1) = physically attached touchscreen
                    if props_val & 0x2:
                        with open(name_file, "r", encoding="utf-8") as f_name:
                            dev_name = f_name.read().strip()
                        if dev_name:
                            touchscreens.add(dev_name)
                except Exception:
                    continue

        return sorted(list(touchscreens))

    @staticmethod
    def write_udev_rule(touch_device_name: str | None, matrix_str: str | None) -> bool:
        try:
            if not touch_device_name or touch_device_name == "None" or not matrix_str:
                subprocess.run(["sudo", "-n", "rm", "-f", UDEV_RULE_PATH], capture_output=True, check=True)
                subprocess.run(["sudo", "-n", "udevadm", "control", "--reload-rules"], capture_output=True, check=True)
                # Trigger input subsystem re-evaluation so stale LIBINPUT_CALIBRATION_MATRIX
                # entries in /run/udev/data/ are cleared for already-connected devices.
                subprocess.run(
                    ["sudo", "-n", "udevadm", "trigger", "--subsystem-match=input"],
                    capture_output=True,
                    check=True,
                )
                logger.info("Cleared touch calibration udev rule")
                return True

            udev_rule = (
                f'ACTION=="add|change", KERNEL=="event*", '
                f'ATTRS{{name}}=="{touch_device_name}", '
                f'ENV{{LIBINPUT_CALIBRATION_MATRIX}}="{matrix_str}"\n'
            )
            subprocess.run(
                ["sudo", "-n", "tee", UDEV_RULE_PATH],
                input=udev_rule,
                text=True,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["sudo", "-n", "udevadm", "control", "--reload-rules"],
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["sudo", "-n", "udevadm", "trigger", "--subsystem-match=input"],
                capture_output=True,
                check=True,
            )
            logger.info(f"Wrote touch calibration rule for {touch_device_name}: {matrix_str}")
            return True
        except Exception:
            logger.exception("Failed writing udev touch rule")
            return False

    @staticmethod
    def rebind_usb_touch(touch_device_name: str) -> bool:
        """Force libinput to re-read LIBINPUT_CALIBRATION_MATRIX by USB rebind.

        libinput only reads LIBINPUT_CALIBRATION_MATRIX when the device is first
        opened. udevadm trigger --action=change updates the DB but has no effect
        on already-open devices. USB unbind/bind forces libinput to close and
        reopen the device, picking up the new udev property.

        Call this AFTER the Mutter rotation has been applied so the display
        transition completes before the touch device momentarily disconnects.
        """
        if not touch_device_name or touch_device_name == "None":
            return False
        try:
            bus_id = None
            for name_file in glob.glob("/sys/class/input/input*/name"):
                try:
                    with open(name_file, "r") as f:
                        if touch_device_name.strip().lower() in f.read().strip().lower():
                            cur = Path(name_file).resolve().parent
                            while cur != Path("/"):
                                if (cur / "idVendor").exists() and (cur / "busnum").exists():
                                    bus_id = cur.name
                                    break
                                cur = cur.parent
                            if bus_id:
                                break
                except Exception:
                    continue

            if bus_id:
                logger.info(f"Rebinding USB touch device {bus_id} to apply calibration matrix")
                subprocess.run(
                    ["sudo", "-n", "tee", "/sys/bus/usb/drivers/usb/unbind"],
                    input=f"{bus_id}\n", text=True, capture_output=True, check=True,
                )
                subprocess.run(
                    ["sudo", "-n", "tee", "/sys/bus/usb/drivers/usb/bind"],
                    input=f"{bus_id}\n", text=True, capture_output=True, check=True,
                )
                return True
            else:
                logger.debug(f"Could not locate USB bus ID for touch device: {touch_device_name}")
                return False
        except Exception:
            logger.exception("Failed rebinding USB touch device")
            return False

    @classmethod
    def apply_touch_calibration(
        cls,
        touch_device_name: str,
        connector_name: str,
        rotation_int: int | None = None,
        prev_touch_device_name: str | None = None
    ) -> bool:
        if not touch_device_name or touch_device_name == "None" or normalize_rotation(rotation_int) == 0:
            cls.write_udev_rule(None, None)
            target_dev = touch_device_name if touch_device_name and touch_device_name != "None" else prev_touch_device_name
            if target_dev and target_dev not in ("None", "", None):
                cls.rebind_usb_touch(target_dev)
            return True

        state = cls.get_current_mutter_state()
        if not state:
            logger.warning("Cannot calculate touch matrix without active Mutter state")
            return False

        serial, monitors, logical_monitors, properties = state
        matrix_str = calculate_affine_matrix(
            logical_monitors,
            monitors,
            connector_name,
            target_trans=rotation_int
        )
        if not matrix_str:
            return False

        ok = cls.write_udev_rule(touch_device_name, matrix_str)
        if ok:
            cls.rebind_usb_touch(touch_device_name)
        return ok

    @classmethod
    def load_config(cls, config_path: Path | None = None) -> dict:
        target = config_path or DEFAULT_CONFIG_PATH
        if target.exists():
            try:
                with open(target, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                logger.exception(f"Failed loading rotation config from {target}")
        return {}

    @classmethod
    def save_config(
        cls,
        config: dict,
        config_path: Path | None = None,
        autostart_path: Path | None = None
    ) -> bool:
        target = config_path or DEFAULT_CONFIG_PATH
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
            cls.ensure_autostart(config=config, autostart_path=autostart_path)
            return True
        except Exception:
            logger.exception(f"Failed saving rotation config to {target}")
            return False

    @classmethod
    def ensure_autostart(
        cls,
        config: dict | None = None,
        autostart_path: Path | None = None
    ) -> bool:
        target = autostart_path or DEFAULT_AUTOSTART_PATH
        cfg = config if config is not None else cls.load_config()
        # Also ensure autostart entry is configured if config exists
        has_config = bool(cfg)

        try:
            if has_config:
                target.parent.mkdir(parents=True, exist_ok=True)
                suit_root = Path(__file__).resolve().parent.parent
                py_bin = suit_root / "venv" / "bin" / "python"
                if not py_bin.exists():
                    py_bin = Path(sys.executable)

                exec_cmd = f"{py_bin} -m core.display_service --apply-boot"
                desktop_entry = f"""[Desktop Entry]
Type=Application
Name=SUIT Display & Touch Rotation
Exec=bash -c "cd {suit_root} && {exec_cmd}"
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
Comment=Maintain screen and touch rotation on boot
"""
                target.write_text(desktop_entry, encoding="utf-8")
                target.chmod(0o755)
                logger.info(f"Created rotation autostart file: {target}")
            else:
                if target.exists():
                    target.unlink()
                    logger.info(f"Removed rotation autostart file: {target}")
            return True
        except Exception:
            logger.exception("Failed configuring rotation autostart")
            return False

    @classmethod
    def write_monitors_xml(
        cls,
        connector_name: str,
        rotation_int: int,
        xml_path: Path | None = None
    ) -> bool:
        """Write ~/.config/monitors.xml directly so GNOME Mutter boots natively in this rotation."""
        target = xml_path or (Path.home() / ".config" / "monitors.xml")
        state = cls.get_current_mutter_state()
        if not state:
            return False

        serial, monitors, logical_monitors, properties = state
        rot_map = {0: "normal", 1: "left", 2: "upside_down", 3: "right"}
        target_rot_val = normalize_rotation(rotation_int)
        rot_str = rot_map.get(target_rot_val, "normal")

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            lines = [
                '<monitors version="2">',
                '  <configuration>',
                '    <layoutmode>logical</layoutmode>'
            ]

            for lm in logical_monitors:
                x, y, scale, trans, is_primary, phys_monitors = lm[:6]
                for pm in phys_monitors:
                    p_name = pm[0]
                    p_vendor, p_prod, p_serial = "unknown", "unknown", "unknown"
                    mode_w, mode_h, mode_rate = 1920, 1080, 60.0
                    for m_info in monitors:
                        if m_info[0][0] == p_name:
                            p_vendor, p_prod, p_serial = m_info[0][1], m_info[0][2], m_info[0][3]
                            for mode in m_info[1]:
                                if "is-current" in mode[6]:
                                    mode_w = int(mode[1])
                                    mode_h = int(mode[2])
                                    mode_rate = float(mode[3])
                                    break

                    eff_rot = rot_str if p_name == connector_name else rot_map.get(int(trans), "normal")
                    lines.extend([
                        '    <logicalmonitor>',
                        f'      <x>{int(x)}</x>',
                        f'      <y>{int(y)}</y>',
                        f'      <scale>{scale}</scale>',
                        f'      <primary>{"yes" if is_primary else "no"}</primary>',
                        '      <transform>',
                        f'        <rotation>{eff_rot}</rotation>',
                        '        <flipped>no</flipped>',
                        '      </transform>',
                        '      <monitor>',
                        '        <monitorspec>',
                        f'          <connector>{p_name}</connector>',
                        f'          <vendor>{p_vendor}</vendor>',
                        f'          <product>{p_prod}</product>',
                        f'          <serial>{p_serial}</serial>',
                        '        </monitorspec>',
                        '        <mode>',
                        f'          <width>{mode_w}</width>',
                        f'          <height>{mode_h}</height>',
                        f'          <rate>{mode_rate:.3f}</rate>',
                        '        </mode>',
                        '      </monitor>',
                        '    </logicalmonitor>'
                    ])

            lines.extend([
                '  </configuration>',
                '</monitors>\n'
            ])

            target.write_text("\n".join(lines), encoding="utf-8")
            logger.info(f"Wrote persistent monitors.xml for {connector_name} (rotation: {rot_str})")
            return True
        except Exception:
            logger.exception("Failed writing monitors.xml")
            return False

    @classmethod
    def persist_display_config(
        cls,
        connector_name: str,
        rotation_int: int,
        touch_device_name: str | None = None,
        config_path: Path | None = None,
        autostart_path: Path | None = None
    ) -> bool:
        """Persist confirmed display orientation and touch device to configuration and Mutter."""
        # 1. Write ~/.config/monitors.xml so Mutter loads this orientation on boot
        cls.write_monitors_xml(connector_name, rotation_int)

        # 2. Tell Mutter via DBus to apply persistently (method=2)
        cls.apply_rotation(connector_name, rotation_int, method=2)

        # 3. Update touch calibration rule for the confirmed rotation
        if touch_device_name and touch_device_name != "None":
            cls.apply_touch_calibration(touch_device_name, connector_name, rotation_int=rotation_int)
        else:
            cls.write_udev_rule(None, None)

        # 4. Save to SUIT configuration and ensure autostart
        config = cls.load_config(config_path=config_path)
        config[connector_name] = {
            "rotation": int(rotation_int),
            "touch_device": touch_device_name or "None"
        }
        return cls.save_config(config, config_path=config_path, autostart_path=autostart_path)

    @classmethod
    def apply_display_and_touch(
        cls,
        connector_name: str,
        rotation_int: int,
        touch_device_name: str | None = None,
        method: int | None = None,
        persist: bool = True,
        prev_touch_device_name: str | None = None
    ) -> bool:
        logger.info(f"Applying display rotation ({rotation_int}) and touch ({touch_device_name}) to {connector_name} (persist={persist})")

        touch_dev = touch_device_name if touch_device_name != "None" else None
        rebind_dev = touch_dev or prev_touch_device_name  # device whose calibration needs refresh

        # 1. Write udev rule with new calibration matrix (does NOT yet affect libinput).
        #    At 0° (normal), clear any existing rule so libinput uses its native default.
        if touch_dev and normalize_rotation(rotation_int) != 0:
            state = cls.get_current_mutter_state()
            if state:
                serial, monitors, logical_monitors, properties = state
                matrix_str = calculate_affine_matrix(
                    logical_monitors, monitors, connector_name, target_trans=rotation_int
                )
                if matrix_str:
                    cls.write_udev_rule(touch_dev, matrix_str)
                    logger.info(f"Wrote touch calibration rule for {touch_dev}: {matrix_str}")
        else:
            cls.write_udev_rule(None, None)

        # 2. Rotate display via Mutter DBus (method 1=temporary, 2=persistent)
        actual_method = method if method is not None else (2 if persist else 1)
        rot_ok = cls.apply_rotation(connector_name, rotation_int, method=actual_method)

        # 3. USB rebind — forces libinput to close+reopen the device so it picks
        #    up the new LIBINPUT_CALIBRATION_MATRIX. Short sleep lets Mutter finish
        #    the rotation animation before the input device disconnects momentarily.
        if rebind_dev and rebind_dev not in ("None", ""):
            time.sleep(0.6)
            cls.rebind_usb_touch(rebind_dev)

        # 4. Persist configuration if requested
        if persist:
            cls.persist_display_config(connector_name, rotation_int, touch_device_name)

        return rot_ok

    @classmethod
    def clear_config(
        cls,
        config_path: Path | None = None,
        autostart_path: Path | None = None,
        clear_udev: bool = True
    ) -> bool:
        """Remove SUIT rotation configuration, autostart entries, and touch calibration rules."""
        cfg_target = config_path or DEFAULT_CONFIG_PATH
        auto_target = autostart_path or DEFAULT_AUTOSTART_PATH
        mon_xml = Path.home() / ".config" / "monitors.xml"
        success = True

        try:
            if cfg_target.exists():
                cfg_target.unlink()
                logger.info(f"Removed rotation config: {cfg_target}")
        except Exception:
            logger.exception(f"Failed removing rotation config: {cfg_target}")
            success = False

        try:
            if auto_target.exists():
                auto_target.unlink()
                logger.info(f"Removed rotation autostart: {auto_target}")
        except Exception:
            logger.exception(f"Failed removing rotation autostart: {auto_target}")
            success = False

        try:
            if mon_xml.exists():
                mon_xml.unlink()
                logger.info(f"Removed monitors.xml: {mon_xml}")
        except Exception:
            logger.exception(f"Failed removing monitors.xml: {mon_xml}")
            success = False

        if clear_udev:
            try:
                cls.write_udev_rule(None, None)
            except Exception:
                logger.exception("Failed clearing touch udev rule")
                success = False

        return success

    @staticmethod
    def open_display_settings() -> bool:
        """Launch the default GNOME Display Settings panel."""
        try:
            subprocess.Popen(["gnome-control-center", "display"])
            return True
        except Exception:
            logger.exception("Failed to open gnome-control-center display settings")
            return False

    @classmethod
    def apply_boot_config(cls) -> bool:
        logger.info("Applying saved display and touch configuration from boot autostart")
        config = cls.load_config()
        if not config:
            return True

        # Wait for Mutter session to be available
        state = cls.get_current_mutter_state(retries=15)
        if not state:
            logger.warning("Mutter DisplayConfig not available during boot application")
            return False

        serial, monitors, logical_monitors, properties = state
        needs_refresh = False

        for mon_name, settings in config.items():
            target_rot = normalize_rotation(settings.get("rotation", 0))
            current_rot = None
            for lm in logical_monitors:
                for pm in lm[5]:
                    if pm[0] == mon_name:
                        current_rot = int(lm[3])
                        break
            if current_rot is not None and current_rot != target_rot:
                logger.info(f"Aligning {mon_name} from {current_rot} to {target_rot}")
                cls.apply_rotation(mon_name, target_rot, method=2)
                needs_refresh = True

        # Check if udev rule is in place without unbinding USB unless rule was missing
        udev_path = Path(UDEV_RULE_PATH)
        for mon_name, settings in config.items():
            touch = settings.get("touch_device")
            target_rot = normalize_rotation(settings.get("rotation", 0))
            if touch and touch != "None" and target_rot != 0:
                if not udev_path.exists():
                    logger.info("Udev rule missing on boot; writing and rebinding")
                    cls.apply_touch_calibration(touch, mon_name, rotation_int=target_rot)
            else:
                if udev_path.exists():
                    logger.info("Udev rule found when none expected; clearing")
                    cls.write_udev_rule(None, None)
            break

        return True

if __name__ == "__main__":
    if "--apply-boot" in sys.argv:
        DisplayService.apply_boot_config()
    else:
        print("Monitors:", DisplayService.get_monitors())
        print("Touchscreens:", DisplayService.get_touchscreens())
