"""Unified SystemService facade combining modular subsystem services via composition."""

from pathlib import Path
from core.power_service import PowerService, DEFAULT_GDM_CONFIG
from core.keyring_service import KeyringService
from core.debloat_service import DebloatService
from core.tailscale_service import TailscaleService
from core.waydroid_service import WaydroidService
from core.desktop_service import DesktopService
from core.remote_desktop_service import RemoteDesktopService


class SystemService:
    """
    Unified facade for SUIT system utilities using explicit composition.
    Delegates seamlessly to specialized domain services:
    - PowerService (Tuned, GDM auto-login, screen sleep)
    - KeyringService (GNOME Keyring, Seahorse, PAM)
    - DebloatService (Bloatware removal, DNF, Dash favorites, Chromium)
    - TailscaleService (VPN status, auth, connect/disconnect)
    - WaydroidService (Waydroid container, libndk, apkeep, Darts Scorer)
    - DesktopService (GNOME dark mode, wallpaper, app grid folders)
    - RemoteDesktopService (GNOME RDP desktop sharing, remote control)
    """

    # Sub-service class references for direct composition access
    power = PowerService
    keyring = KeyringService
    debloat = DebloatService
    tailscale = TailscaleService
    waydroid = WaydroidService
    desktop = DesktopService
    remote_desktop = RemoteDesktopService

    # DebloatService constants
    BLOATWARE_PACKAGES = DebloatService.BLOATWARE_PACKAGES
    APP_FRIENDLY_NAMES = DebloatService.APP_FRIENDLY_NAMES

    # WaydroidService constants
    AURORA_APK_URL = WaydroidService.AURORA_APK_URL
    APKEEP_BINARY_URL = WaydroidService.APKEEP_BINARY_URL

    # DesktopService constants
    EXTENSION_UUID = DesktopService.EXTENSION_UUID
    EXTENSION_SOURCE_DIR = DesktopService.EXTENSION_SOURCE_DIR
    USER_EXTENSION_DIR = DesktopService.USER_EXTENSION_DIR
    GTK4_CSS_PATH = DesktopService.GTK4_CSS_PATH
    GTK3_CSS_PATH = DesktopService.GTK3_CSS_PATH
    SUIT_TOUCH_MARKER_START = DesktopService.SUIT_TOUCH_MARKER_START
    SUIT_TOUCH_MARKER_END = DesktopService.SUIT_TOUCH_MARKER_END
    TOUCH_CSS_BLOCK = DesktopService.TOUCH_CSS_BLOCK

    def __init__(self):
        self.power = PowerService()
        self.keyring = KeyringService()
        self.debloat = DebloatService()
        self.tailscale = TailscaleService()
        self.waydroid = WaydroidService()
        self.desktop = DesktopService()
        self.remote_desktop = RemoteDesktopService()

    # --- PowerService delegation ---
    @staticmethod
    def set_screen_blanking(disable_blanking: bool) -> bool:
        return PowerService.set_screen_blanking(disable_blanking)

    @staticmethod
    def is_blanking_disabled() -> bool:
        return PowerService.is_blanking_disabled()

    @staticmethod
    def is_performance_profile_active(profile: str = "throughput-performance") -> bool:
        return PowerService.is_performance_profile_active(profile)

    @staticmethod
    def set_performance_profile(profile: str = "throughput-performance") -> bool:
        return PowerService.set_performance_profile(profile)

    @staticmethod
    def is_power_and_sleep_optimized() -> bool:
        return PowerService.is_power_and_sleep_optimized()

    @staticmethod
    def set_power_and_sleep(disable_sleep: bool = True) -> bool:
        return PowerService.set_power_and_sleep(disable_sleep)

    @staticmethod
    def generate_gdm_config(config_path: Path = DEFAULT_GDM_CONFIG, enabled: bool = True, user: str = "pi") -> str:
        return PowerService.generate_gdm_config(config_path, enabled, user)

    @classmethod
    def get_step1_status(cls, user: str) -> dict:
        return PowerService.get_step1_status.__func__(cls, user)

    @classmethod
    def apply_all_system_energy_and_login(cls, user: str) -> bool:
        return PowerService.apply_all_system_energy_and_login.__func__(cls, user)

    @classmethod
    def set_auto_login(cls, enabled: bool, user: str, config_path: Path | None = None) -> bool:
        return PowerService.set_auto_login.__func__(cls, enabled, user, config_path=config_path)

    @classmethod
    def is_auto_login_enabled(cls, config_path: Path | None = None) -> tuple[bool, str]:
        return PowerService.is_auto_login_enabled.__func__(cls, config_path=config_path)

    # --- KeyringService delegation ---
    @staticmethod
    def is_keyring_blank_or_unlocked(keyring_path: Path | None = None) -> tuple[bool, str]:
        return KeyringService.is_keyring_blank_or_unlocked(keyring_path=keyring_path)

    @classmethod
    def reset_keyring_to_blank(cls, keyring_path: Path | None = None) -> tuple[bool, str]:
        return KeyringService.reset_keyring_to_blank.__func__(cls, keyring_path=keyring_path)

    @staticmethod
    def is_seahorse_installed() -> bool:
        return KeyringService.is_seahorse_installed()

    @staticmethod
    def install_seahorse() -> bool:
        return KeyringService.install_seahorse()

    @staticmethod
    def launch_seahorse() -> bool:
        return KeyringService.launch_seahorse()

    @staticmethod
    def fix_keyring_pam() -> bool:
        return KeyringService.fix_keyring_pam()

    # --- DebloatService delegation ---
    @classmethod
    def get_installed_bloatware_details(cls, include_all: bool = False) -> list[dict]:
        return DebloatService.get_installed_bloatware_details.__func__(cls, include_all=include_all)

    @classmethod
    def get_installed_bloatware(cls, package_list: list[str] | None = None) -> list[str]:
        return DebloatService.get_installed_bloatware.__func__(cls, package_list=package_list)

    @staticmethod
    def clean_dash_favorites() -> bool:
        return DebloatService.clean_dash_favorites()

    @staticmethod
    def move_seahorse_to_utilities() -> bool:
        return DebloatService.move_seahorse_to_utilities()

    @classmethod
    def debloat_packages(cls, packages: list[str], progress_callback=None) -> tuple[bool, str]:
        return DebloatService.debloat_packages.__func__(cls, packages, progress_callback=progress_callback)

    @classmethod
    def debloat_system(cls, package_list: list[str] | None = None, progress_callback=None) -> tuple[bool, str]:
        return DebloatService.debloat_system.__func__(cls, package_list=package_list, progress_callback=progress_callback)

    @staticmethod
    def add_favorite_app(desktop_file: str) -> bool:
        return DebloatService.add_favorite_app(desktop_file)

    @staticmethod
    def is_app_installed(app_name: str) -> bool:
        return DebloatService.is_app_installed(app_name)

    @classmethod
    def get_software_status(cls) -> dict:
        return DebloatService.get_software_status.__func__(cls)

    @classmethod
    def install_chromium(cls) -> tuple[bool, str]:
        return DebloatService.install_chromium.__func__(cls)

    # --- TailscaleService delegation ---
    @staticmethod
    def install_tailscale() -> tuple[bool, str]:
        return TailscaleService.install_tailscale()

    @classmethod
    def get_tailscale_status(cls) -> dict:
        return TailscaleService.get_tailscale_status.__func__(cls)

    @classmethod
    def get_tailscale_auth_url(cls) -> str | None:
        return TailscaleService.get_tailscale_auth_url.__func__(cls)

    @staticmethod
    def disconnect_tailscale() -> tuple[bool, str]:
        return TailscaleService.disconnect_tailscale()

    @classmethod
    def connect_tailscale(cls) -> tuple[bool, str]:
        return TailscaleService.connect_tailscale.__func__(cls)

    @classmethod
    def launch_tailscale_up(cls) -> bool:
        return TailscaleService.launch_tailscale_up.__func__(cls)

    # --- WaydroidService delegation ---
    @staticmethod
    def is_waydroid_installed() -> bool:
        return WaydroidService.is_waydroid_installed()

    @staticmethod
    def is_waydroid_initialized() -> bool:
        return WaydroidService.is_waydroid_initialized()

    @staticmethod
    def is_aurora_store_installed() -> bool:
        return WaydroidService.is_aurora_store_installed()

    @staticmethod
    def is_darts_scorer_installed() -> bool:
        return WaydroidService.is_darts_scorer_installed()

    @staticmethod
    def is_arm_translation_installed() -> bool:
        return WaydroidService.is_arm_translation_installed()

    @classmethod
    def get_waydroid_status(cls) -> dict:
        return WaydroidService.get_waydroid_status.__func__(cls)

    @staticmethod
    def get_system_locale() -> str:
        return WaydroidService.get_system_locale()

    @classmethod
    def install_waydroid(cls, locale: str | None = None) -> tuple[bool, str]:
        return WaydroidService.install_waydroid.__func__(cls, locale=locale)

    @classmethod
    def waydroid_debloat_and_seamless(cls) -> tuple[bool, str]:
        return WaydroidService.waydroid_debloat_and_seamless.__func__(cls)

    @classmethod
    def install_aurora_store(cls) -> tuple[bool, str]:
        return WaydroidService.install_aurora_store.__func__(cls)

    @classmethod
    def full_waydroid_setup(cls) -> tuple[bool, str]:
        return WaydroidService.full_waydroid_setup.__func__(cls)

    @classmethod
    def install_libndk(cls) -> tuple[bool, str]:
        return WaydroidService.install_libndk.__func__(cls)

    @classmethod
    def ensure_apkeep(cls) -> Path | None:
        return WaydroidService.ensure_apkeep.__func__(cls)

    @classmethod
    def wait_for_android_package_service(cls, timeout: int = 60) -> bool:
        return WaydroidService.wait_for_android_package_service.__func__(cls, timeout=timeout)

    @classmethod
    def install_darts_scorer(cls, progress_callback=None) -> tuple[bool, str]:
        return WaydroidService.install_darts_scorer.__func__(cls, progress_callback=progress_callback)

    @staticmethod
    def get_primary_screen_resolution() -> tuple[int, int]:
        return WaydroidService.get_primary_screen_resolution()

    @classmethod
    def pin_darts_scorer_to_dash(cls) -> bool:
        return WaydroidService.pin_darts_scorer_to_dash.__func__(cls)

    @classmethod
    def ensure_darts_scorer_launcher(cls) -> Path:
        return WaydroidService.ensure_darts_scorer_launcher.__func__(cls)

    @classmethod
    def full_darts_scorer_setup(cls, progress_callback=None) -> tuple[bool, str]:
        return WaydroidService.full_darts_scorer_setup.__func__(cls, progress_callback=progress_callback)

    @staticmethod
    def is_virtual_machine() -> tuple[bool, str]:
        return WaydroidService.is_virtual_machine()

    @classmethod
    def launch_aurora_store(cls) -> tuple[bool, str]:
        return WaydroidService.launch_aurora_store.__func__(cls)

    @classmethod
    def launch_darts_scorer(cls) -> tuple[bool, str]:
        return WaydroidService.launch_darts_scorer.__func__(cls)

    # --- DesktopService delegation ---
    @staticmethod
    def is_dark_mode_enabled() -> bool:
        return DesktopService.is_dark_mode_enabled()

    @staticmethod
    def is_suit_wallpaper_set() -> bool:
        return DesktopService.is_suit_wallpaper_set()

    @staticmethod
    def is_map_wallpaper_set() -> bool:
        return DesktopService.is_map_wallpaper_set()

    @staticmethod
    def is_system_app_folder_configured() -> bool:
        return DesktopService.is_system_app_folder_configured()

    @classmethod
    def is_touch_scaling_enabled(cls, gtk4_path: Path | None = None, ext_uuid: str | None = None) -> bool:
        return DesktopService.is_touch_scaling_enabled.__func__(cls, gtk4_path=gtk4_path, ext_uuid=ext_uuid)

    @staticmethod
    def is_osk_enabled() -> bool:
        return DesktopService.is_osk_enabled()

    @staticmethod
    def set_osk(enabled: bool):
        return DesktopService.set_osk(enabled)

    @staticmethod
    def get_button_layout() -> str:
        return DesktopService.get_button_layout()

    @staticmethod
    def set_button_layout(layout: str):
        return DesktopService.set_button_layout(layout)

    @classmethod
    def set_touch_scaling(
        cls,
        enabled: bool,
        source_dir: Path | None = None,
        target_dir: Path | None = None,
        gtk4_path: Path | None = None,
        gtk3_path: Path | None = None,
        ext_uuid: str | None = None,
        scale_factor: float = 1.2,
    ) -> tuple[bool, str]:
        return DesktopService.set_touch_scaling.__func__(
            cls,
            enabled,
            source_dir=source_dir,
            target_dir=target_dir,
            gtk4_path=gtk4_path,
            gtk3_path=gtk3_path,
            ext_uuid=ext_uuid,
            scale_factor=scale_factor,
        )

    @classmethod
    def get_visuals_status(cls) -> dict:
        return DesktopService.get_visuals_status.__func__(cls)

    @classmethod
    def apply_desktop_visuals(cls) -> tuple[bool, str]:
        return DesktopService.apply_desktop_visuals.__func__(cls)

    # --- RemoteDesktopService delegation ---
    @staticmethod
    def get_local_ip() -> str:
        return RemoteDesktopService.get_local_ip()

    @classmethod
    def get_remote_desktop_status(cls) -> dict:
        return RemoteDesktopService.get_remote_desktop_status.__func__(cls)

    @classmethod
    def set_desktop_sharing(cls, enabled: bool) -> tuple[bool, str]:
        return RemoteDesktopService.set_desktop_sharing.__func__(cls, enabled)

    @classmethod
    def set_remote_control(cls, enabled: bool) -> tuple[bool, str]:
        return RemoteDesktopService.set_remote_control.__func__(cls, enabled)

    @classmethod
    def set_remote_credentials(cls, username: str, password: str) -> tuple[bool, str]:
        return RemoteDesktopService.set_remote_credentials.__func__(cls, username, password)

    @classmethod
    def open_gnome_sharing_settings(cls) -> bool:
        return RemoteDesktopService.open_gnome_sharing_settings.__func__(cls)

    # --- Facade composite operations ---
    @classmethod
    def get_recommended_status(cls, user: str) -> dict:
        s1 = cls.get_step1_status(user)
        is_blank, _ = cls.is_keyring_blank_or_unlocked()
        vis = cls.get_visuals_status()
        has_chrom = cls.is_app_installed("chromium")

        profile_ok = s1.get("profile_ok", False)
        power_ok = s1.get("power_ok", False)
        autologin_ok = s1.get("autologin_ok", False)
        keyring_ok = bool(is_blank)
        visuals_ok = bool(vis.get("all_ok", False))
        chromium_ok = bool(has_chrom)

        all_ok = (
            profile_ok
            and power_ok
            and autologin_ok
            and keyring_ok
            and visuals_ok
            and chromium_ok
        )

        return {
            "profile_ok": profile_ok,
            "power_ok": power_ok,
            "autologin_ok": autologin_ok,
            "keyring_ok": keyring_ok,
            "visuals_ok": visuals_ok,
            "chromium_ok": chromium_ok,
            "all_ok": all_ok,
        }

    @classmethod
    def apply_all_recommended(cls, user: str) -> tuple[bool, str]:
        results = []

        # 1. Performance profile, sleep, auto-login
        ok_energy = cls.apply_all_system_energy_and_login(user)
        results.append(ok_energy)

        # 2. Keyring password reset if needed
        is_blank, _ = cls.is_keyring_blank_or_unlocked()
        if not is_blank:
            ok_keyring, _ = cls.reset_keyring_to_blank()
            results.append(ok_keyring)

        # 3. Desktop visuals & app grid
        ok_vis, _ = cls.apply_desktop_visuals()
        results.append(ok_vis)

        # 4. Install Chromium if not present
        if not cls.is_app_installed("chromium"):
            ok_chrom, _ = cls.install_chromium()
            results.append(ok_chrom)

        all_success = all(results)
        msg = "All recommended optimizations applied." if all_success else "Some optimizations had warnings."
        return all_success, msg

    @classmethod
    def apply_all_system_and_apps(
        cls,
        user: str,
        include_touch: bool = False,
        step_callback=None
    ) -> tuple[bool, str, list[dict]]:
        """
        Sequentially executes all system optimizations, theme/visuals, optional touch tweaks,
        bloatware removal, and Chromium installation. Excludes Advanced Users.
        Errors on individual steps do not abort subsequent steps.
        """
        step_definitions = [
            ("Power Profile", "performance", "Locking CPU to maximum performance"),
            ("Display Always On", True, "Disabling screen blanking and sleep"),
            ("Automatic Login", True, "Configuring unattended auto-login"),
            ("Keyring Password", "blank", "Unlocking GNOME Keyring for passwordless boot"),
            ("Appearance & Theme", "theme", "Applying dark theme and organizing app grid"),
        ]
        if include_touch:
            step_definitions.append(("Touchscreen Tweaks", True, "Enlarging UI elements and touchscreen controls"))
        step_definitions.append(("Remove Unused Apps", "debloat", "Removing default bloatware packages"))
        step_definitions.append(("Chromium Browser", "chromium", "Installing Chromium web browser"))

        total_steps = len(step_definitions)
        results = []

        for idx, (title, action_type, default_desc) in enumerate(step_definitions, 1):
            if step_callback:
                step_callback(idx, total_steps, title, default_desc)

            success = False
            msg = ""

            try:
                if action_type == "performance":
                    success = cls.set_performance_profile("throughput-performance")
                    msg = "Power profile set to performance." if success else "Failed to set power profile."
                elif title == "Display Always On":
                    success = cls.set_screen_blanking(True)
                    msg = "Display always-on configured." if success else "Failed configuring display sleep."
                elif title == "Automatic Login":
                    success = cls.set_auto_login(True, user)
                    msg = "Automatic login enabled." if success else "Failed setting automatic login."
                elif action_type == "blank":
                    is_blank, _ = cls.is_keyring_blank_or_unlocked()
                    if is_blank:
                        success, msg = True, "Keyring is already unlocked."
                    else:
                        success, msg = cls.reset_keyring_to_blank()
                elif action_type == "theme":
                    success, msg = cls.apply_desktop_visuals()
                elif title == "Touchscreen Tweaks":
                    success, msg = cls.set_touch_scaling(True)
                elif action_type == "debloat":
                    bloat = cls.get_installed_bloatware()
                    if not bloat:
                        success, msg = True, "No default bloatware detected."
                    else:
                        def _debloat_cb(cur, tot, detail_str):
                            if step_callback:
                                step_callback(idx, total_steps, title, detail_str)
                        success, msg = cls.debloat_packages(bloat, progress_callback=_debloat_cb)
                elif action_type == "chromium":
                    if cls.is_app_installed("chromium"):
                        success, msg = True, "Chromium is already installed."
                    else:
                        if step_callback:
                            step_callback(idx, total_steps, title, "Downloading and installing Chromium via DNF...")
                        success, msg = cls.install_chromium()
            except Exception as e:
                logger.exception("Error executing step %s during apply_all", title)
                success = False
                msg = f"Error: {e}"

            results.append({"step": title, "success": success, "message": msg})

        all_success = all(r["success"] for r in results)
        if all_success:
            summary_msg = "All system configurations applied successfully."
        else:
            failed = [r["step"] for r in results if not r["success"]]
            summary_msg = f"Completed with warnings on: {', '.join(failed)}."

        return all_success, summary_msg, results


__all__ = [
    "SystemService",
    "PowerService",
    "KeyringService",
    "DebloatService",
    "TailscaleService",
    "WaydroidService",
    "DesktopService",
    "RemoteDesktopService",
    "DEFAULT_GDM_CONFIG",
]
