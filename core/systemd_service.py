from pathlib import Path
import subprocess
import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib
from core.logger import get_logger

logger = get_logger("systemd")

class SystemdService:
    @staticmethod
    def _get_manager_proxy(scope: str = "system") -> Gio.DBusProxy | None:
        try:
            bus_type = Gio.BusType.SESSION if scope == "user" else Gio.BusType.SYSTEM
            bus = Gio.bus_get_sync(bus_type, None)
            return Gio.DBusProxy.new_sync(
                bus,
                Gio.DBusProxyFlags.NONE,
                None,
                "org.freedesktop.systemd1",
                "/org/freedesktop/systemd1",
                "org.freedesktop.systemd1.Manager",
                None
            )
        except Exception:
            logger.exception(f"Failed to connect to systemd DBus Manager on {scope} bus")
            return None

    @classmethod
    def get_status(cls, unit_name: str, scope: str = "auto") -> dict:
        if scope == "auto":
            user_status = cls._get_status_scoped(unit_name, scope="user")
            if user_status.get("active_state") != "nofile":
                return user_status
            return cls._get_status_scoped(unit_name, scope="system")
        return cls._get_status_scoped(unit_name, scope=scope)

    @classmethod
    def _get_status_scoped(cls, unit_name: str, scope: str = "system") -> dict:
        result = {"active_state": "nofile", "sub_state": "dead", "unit_file_state": "missing"}
        proxy = cls._get_manager_proxy(scope=scope)
        if not proxy:
            return result

        try:
            unit_path_var = proxy.call_sync(
                "GetUnit",
                GLib.Variant("(s)", (unit_name,)),
                Gio.DBusCallFlags.NONE,
                1000,
                None
            )
            unit_path = unit_path_var.unpack()[0]
        except GLib.GError as ge:
            # Unit not loaded, try LoadUnit
            try:
                unit_path_var = proxy.call_sync(
                    "LoadUnit",
                    GLib.Variant("(s)", (unit_name,)),
                    Gio.DBusCallFlags.NONE,
                    1000,
                    None
                )
                unit_path = unit_path_var.unpack()[0]
            except Exception:
                # Unit doesn't exist on system
                return result
        except Exception:
            return result

        try:
            bus_type = Gio.BusType.SESSION if scope == "user" else Gio.BusType.SYSTEM
            bus = Gio.bus_get_sync(bus_type, None)
            unit_proxy = Gio.DBusProxy.new_sync(
                bus,
                Gio.DBusProxyFlags.NONE,
                None,
                "org.freedesktop.systemd1",
                unit_path,
                "org.freedesktop.systemd1.Unit",
                None
            )
            
            active = unit_proxy.get_cached_property("ActiveState")
            sub = unit_proxy.get_cached_property("SubState")
            unit_file = unit_proxy.get_cached_property("UnitFileState")
            load_state = unit_proxy.get_cached_property("LoadState")
            
            ls = load_state.unpack() if load_state else "not-found"
            if ls == "not-found":
                result["active_state"] = "nofile"
                result["sub_state"] = "dead"
                result["unit_file_state"] = "missing"
            else:
                result["active_state"] = active.unpack() if active else "inactive"
                result["sub_state"] = sub.unpack() if sub else "dead"
                result["unit_file_state"] = unit_file.unpack() if unit_file else "unknown"
        except Exception:
            logger.exception(f"Failed querying properties for {unit_name} on {scope} bus")

        return result

    @classmethod
    def _execute_unit_action(cls, action: str, unit_name: str, mode: str = "replace", scope: str = "auto") -> bool:
        target_scope = scope
        if scope == "auto":
            status_user = cls._get_status_scoped(unit_name, scope="user")
            if status_user.get("active_state") != "nofile":
                target_scope = "user"
            else:
                target_scope = "system"

        proxy = cls._get_manager_proxy(scope=target_scope)
        dbus_method = {"start": "StartUnit", "stop": "StopUnit", "restart": "RestartUnit"}.get(action)
        if proxy and dbus_method:
            try:
                proxy.call_sync(
                    dbus_method,
                    GLib.Variant("(ss)", (unit_name, mode)),
                    Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION,
                    3000,
                    None
                )
                logger.info(f"Successfully executed {action} for {unit_name} via {target_scope} DBus")
                return True
            except Exception as e:
                logger.warning(f"DBus {action} failed for {unit_name} ({target_scope}): {e}. Trying CLI fallback...")

        # Fallback to systemctl CLI
        try:
            if target_scope == "user":
                cmd = ["systemctl", "--user", action, unit_name]
            else:
                cmd = ["sudo", "systemctl", action, unit_name]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                logger.info(f"Successfully executed {action} for {unit_name} via {target_scope} systemctl")
                return True
            else:
                logger.error(f"{target_scope} systemctl {action} {unit_name} failed: {res.stderr.strip()}")
                return False
        except Exception:
            logger.exception(f"Failed to execute {action} for {unit_name} via {target_scope} systemctl")
            return False

    @classmethod
    def start_unit(cls, unit_name: str, mode: str = "replace", scope: str = "auto") -> bool:
        return cls._execute_unit_action("start", unit_name, mode, scope=scope)

    @classmethod
    def stop_unit(cls, unit_name: str, mode: str = "replace", scope: str = "auto") -> bool:
        return cls._execute_unit_action("stop", unit_name, mode, scope=scope)

    @classmethod
    def restart_unit(cls, unit_name: str, mode: str = "replace", scope: str = "auto") -> bool:
        return cls._execute_unit_action("restart", unit_name, mode, scope=scope)

    @classmethod
    def is_unit_active(cls, unit_name: str, scope: str = "auto") -> bool:
        """Check if a systemd unit is currently in active state."""
        status = cls.get_status(unit_name, scope=scope)
        return status.get("active_state") == "active"

