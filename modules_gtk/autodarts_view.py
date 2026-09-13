import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib
from pathlib import Path

from core.logger import get_logger
from core.systemd_service import SystemdService
from core.autodarts_service import (
    DEFAULT_HOST, DEFAULT_PORT,
    fetch_telemetry,
    read_cam_config, save_cam_config, get_available_cameras, get_supported_resolutions,
    control_detection_start, control_detection_stop, control_detection_reset,
    install_autodarts, uninstall_autodarts
)
from modules_gtk.async_utils import run_async, open_browser_url
from modules_gtk.dialogs.board_setup_dialog import BoardSetupDialog
from modules_gtk.ui_helpers import create_button_with_icon

logger = get_logger("autodarts_view")

SERVICE_NAME = "autodarts.service"
FPS_OPTIONS = [15, 20, 25, 30]


class AutodartsView(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="Autodarts Setup", tag="autodarts")
        self.window = window
        self.poll_source_id = None
        self.is_busy = False
        self.cam_config_loaded = False
        self.cam_options = [{"path": "", "label": "Select camera"}]
        self.res_options = [(1280, 720)]

        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.set_kinetic_scrolling(True)
        self.set_child(scrolled)

        clamp = Adw.Clamp(maximum_size=880, tightening_threshold=660)
        clamp.set_margin_top(16)
        clamp.set_margin_bottom(24)
        clamp.set_margin_start(16)
        clamp.set_margin_end(16)
        scrolled.set_child(clamp)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        clamp.set_child(main_box)

        # 1. Top Header Banner Card
        header_card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        header_card.set_margin_bottom(2)

        icon_img = Gtk.Image.new_from_icon_name("darts-symbolic")
        icon_img.set_pixel_size(36)
        icon_img.set_valign(Gtk.Align.CENTER)
        header_card.append(icon_img)

        lbl_title = Gtk.Label(label="Autodarts Setup", xalign=0)
        lbl_title.add_css_class("title-1")
        lbl_title.set_valign(Gtk.Align.CENTER)
        lbl_title.set_hexpand(True)
        header_card.append(lbl_title)

        # Activity Spinner
        self.spinner = Gtk.Spinner()
        self.spinner.set_valign(Gtk.Align.CENTER)
        header_card.append(self.spinner)

        main_box.append(header_card)

        # 2. FULL-WIDTH Service Controls Card
        self.card_ctrl = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.card_ctrl.add_css_class("tile-card")
        main_box.append(self.card_ctrl)

        tc_top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        tc_icon = Gtk.Image.new_from_icon_name("system-run-symbolic")
        tc_icon.set_pixel_size(28)
        tc_top.append(tc_icon)

        tc_title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        tc_title_box.set_hexpand(True)
        tc_title = Gtk.Label(label="Service Controls", xalign=0)
        tc_title.add_css_class("title-2")
        tc_title_box.append(tc_title)

        self.lbl_web_info = Gtk.Label(label="Autodarts Engine: Offline", xalign=0)
        self.lbl_web_info.add_css_class("dim-label")
        tc_title_box.append(self.lbl_web_info)
        tc_top.append(tc_title_box)

        self.card_ctrl.append(tc_top)

        # Primary Service Actions (Start / Stop / Restart / Open Web UI)
        self.btn_action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.btn_action_box.set_margin_top(4)
        self.btn_action_box.set_homogeneous(True)

        self.btn_start = create_button_with_icon("media-playback-start-symbolic", "Start Service", "suggested-action", height=50, touch_btn=True)
        self.btn_start.connect("clicked", lambda b: self._do_service_action("start"))

        self.btn_stop = create_button_with_icon("media-playback-stop-symbolic", "Stop", "destructive-action", height=50, touch_btn=True)
        self.btn_stop.connect("clicked", lambda b: self._do_service_action("stop"))

        self.btn_restart = create_button_with_icon("view-refresh-symbolic", "Restart", height=50, touch_btn=True)
        self.btn_restart.connect("clicked", lambda b: self._do_service_action("restart"))

        self.btn_web = create_button_with_icon("web-browser-symbolic", "Open Web UI", "suggested-action", height=50, touch_btn=True)
        self.btn_web.connect("clicked", self._open_web_ui)

        self.card_ctrl.append(self.btn_action_box)

        # Visible Maintenance Actions Row (Reinstall / Uninstall)
        self.maint_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.maint_box.set_margin_top(2)
        self.maint_box.set_homogeneous(True)

        self.btn_reinstall = create_button_with_icon("software-update-available-symbolic", "Reinstall Autodarts", "secondary-btn", height=44, touch_btn=True)
        self.btn_reinstall.connect("clicked", self._on_install_clicked)
        self.maint_box.append(self.btn_reinstall)

        self.btn_uninstall = create_button_with_icon("user-trash-symbolic", "Uninstall Autodarts", "secondary-btn-destructive", height=44, touch_btn=True)
        self.btn_uninstall.connect("clicked", self._on_uninstall_clicked)
        self.maint_box.append(self.btn_uninstall)

        self.card_ctrl.append(self.maint_box)

        # 3. Cloud Board & Live Detection Controls Row
        self.row_board_section = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        main_box.append(self.row_board_section)

        # Left: Cloud Board Status Group
        self.board_group = Adw.PreferencesGroup()
        self.board_group.set_hexpand(True)
        self.row_board_section.append(self.board_group)

        self.row_board = Adw.ActionRow(
            title="Cloud Board Status",
            subtitle="Checking board configuration..."
        )
        self.board_icon = Gtk.Image.new_from_icon_name("network-wired-symbolic")
        self.board_icon.set_pixel_size(24)
        self.board_icon.add_css_class("status-icon")
        self.row_board.add_prefix(self.board_icon)

        self.btn_board_setup = Gtk.Button(label="Link Board")
        self.btn_board_setup.add_css_class("suggested-action")
        self.btn_board_setup.add_css_class("touch-btn")
        self.btn_board_setup.set_valign(Gtk.Align.CENTER)
        self.btn_board_setup.set_size_request(110, 44)
        self.btn_board_setup.connect("clicked", self._open_board_setup_dialog)
        self.row_board.add_suffix(self.btn_board_setup)

        self.board_group.add(self.row_board)

        # Right: Detection Engine Controls Card (PreferencesGroup matching Board Linked)
        self.detection_group = Adw.PreferencesGroup()
        self.detection_group.set_hexpand(True)
        self.detection_group.set_visible(False)
        self.row_board_section.append(self.detection_group)

        self.row_detection = Adw.ActionRow(
            title="Detection",
            subtitle="Stopped"
        )
        self.detection_icon = Gtk.Image.new_from_icon_name("media-playback-stop-symbolic")
        self.detection_icon.set_pixel_size(24)
        self.detection_icon.add_css_class("status-icon")
        self.row_detection.add_prefix(self.detection_icon)

        # Action Buttons (Start/Stop + Reset)
        det_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        det_btn_box.set_valign(Gtk.Align.CENTER)

        # Toggle Button (Start / Stop)
        self.btn_detection_toggle = Gtk.Button()
        self.btn_detection_toggle.add_css_class("suggested-action")
        self.btn_detection_toggle.add_css_class("compact-btn")
        self.btn_detection_toggle.set_size_request(86, 44)
        box_det_btn = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box_det_btn.set_halign(Gtk.Align.CENTER)
        self.img_detection = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        self.img_detection.set_pixel_size(16)
        box_det_btn.append(self.img_detection)
        self.lbl_detection = Gtk.Label(label="Start")
        box_det_btn.append(self.lbl_detection)
        self.btn_detection_toggle.set_child(box_det_btn)
        self.btn_detection_toggle.connect("clicked", self._on_detection_toggle_clicked)
        det_btn_box.append(self.btn_detection_toggle)

        # Reset Button
        self.btn_detection_reset = create_button_with_icon("view-refresh-symbolic", "Reset", "secondary-btn compact-btn", height=44, touch_btn=True)
        self.btn_detection_reset.set_size_request(86, 44)
        self.btn_detection_reset.connect("clicked", self._on_detection_reset_clicked)
        det_btn_box.append(self.btn_detection_reset)

        self.row_detection.add_suffix(det_btn_box)
        self.detection_group.add(self.row_detection)

        # 4. Compact Camera & Video Configuration Card
        self.card_cam = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.card_cam.add_css_class("tile-card-compact")
        main_box.append(self.card_cam)

        # Card Header: Title on Left, Apply & Restart button on Right
        cam_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        cam_hdr.set_valign(Gtk.Align.CENTER)

        hdr_left = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hdr_left.set_valign(Gtk.Align.CENTER)
        hdr_left.set_hexpand(True)

        cam_icon = Gtk.Image.new_from_icon_name("camera-web-symbolic")
        cam_icon.set_pixel_size(20)
        hdr_left.append(cam_icon)

        lbl_cam_hdr = Gtk.Label(label="Cameras & Video", xalign=0)
        lbl_cam_hdr.add_css_class("heading")
        hdr_left.append(lbl_cam_hdr)
        cam_hdr.append(hdr_left)

        self.btn_apply_cam = create_button_with_icon("emblem-ok-symbolic", "Apply & Restart", "suggested-action compact-btn", height=42, touch_btn=True)
        self.btn_apply_cam.connect("clicked", self._on_apply_cam_clicked)
        cam_hdr.append(self.btn_apply_cam)
        self.card_cam.append(cam_hdr)

        # 5-Column Full-Width Controls Row (Resolution, FPS, Camera 1, Camera 2, Camera 3)
        row_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row_controls.set_homogeneous(True)

        # Col 1: Resolution
        box_res = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        lbl_res = Gtk.Label(label="Resolution", xalign=0)
        lbl_res.add_css_class("dim-label")
        box_res.append(lbl_res)
        self.combo_res = Gtk.DropDown.new_from_strings(["1280x720"])
        self.combo_res.set_size_request(-1, 44)
        box_res.append(self.combo_res)
        row_controls.append(box_res)

        # Col 2: FPS
        box_fps = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        lbl_fps = Gtk.Label(label="FPS", xalign=0)
        lbl_fps.add_css_class("dim-label")
        box_fps.append(lbl_fps)
        self.combo_fps = Gtk.DropDown.new_from_strings(["15", "20", "25", "30"])
        self.combo_fps.set_size_request(-1, 44)
        box_fps.append(self.combo_fps)
        row_controls.append(box_fps)

        # Col 3: Camera 1
        box_c1 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        lbl_c1 = Gtk.Label(label="Camera 1", xalign=0)
        lbl_c1.add_css_class("dim-label")
        box_c1.append(lbl_c1)
        self.combo_c1 = Gtk.DropDown.new_from_strings(["Select camera"])
        self.combo_c1.set_size_request(-1, 44)
        box_c1.append(self.combo_c1)
        row_controls.append(box_c1)

        # Col 4: Camera 2
        box_c2 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        lbl_c2 = Gtk.Label(label="Camera 2", xalign=0)
        lbl_c2.add_css_class("dim-label")
        box_c2.append(lbl_c2)
        self.combo_c2 = Gtk.DropDown.new_from_strings(["Select camera"])
        self.combo_c2.set_size_request(-1, 44)
        box_c2.append(self.combo_c2)
        row_controls.append(box_c2)

        # Col 5: Camera 3
        box_c3 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        lbl_c3 = Gtk.Label(label="Camera 3", xalign=0)
        lbl_c3.add_css_class("dim-label")
        box_c3.append(lbl_c3)
        self.combo_c3 = Gtk.DropDown.new_from_strings(["Select camera"])
        self.combo_c3.set_size_request(-1, 44)
        box_c3.append(self.combo_c3)
        row_controls.append(box_c3)

        self.card_cam.append(row_controls)

        # 5. USB Bandwidth Optimizer Row
        self.grp_usb = Adw.PreferencesGroup()
        self.grp_usb.set_margin_top(6)
        main_box.append(self.grp_usb)

        self.row_usb = Adw.ActionRow(
            title="USB Bandwidth Optimizer",
            subtitle="Benchmark camera frame rates and test USB bus bandwidth under full load."
        )
        self.row_usb.set_activatable(True)
        self.row_usb.connect("activated", lambda r: self.window.open_usb_page())

        icon_usb = Gtk.Image.new_from_icon_name("drive-harddisk-usb-symbolic")
        icon_usb.set_pixel_size(24)
        self.row_usb.add_prefix(icon_usb)

        self.btn_open_usb = create_button_with_icon(
            "drive-harddisk-usb-symbolic",
            "Optimize Bandwidth",
            "secondary-btn compact-btn",
            height=40,
            touch_btn=True,
        )
        self.btn_open_usb.connect("clicked", lambda b: self.window.open_usb_page())
        self.row_usb.add_suffix(self.btn_open_usb)

        chevron_usb = Gtk.Image.new_from_icon_name("go-next-symbolic")
        chevron_usb.set_opacity(0.5)
        chevron_usb.set_valign(Gtk.Align.CENTER)
        self.row_usb.add_suffix(chevron_usb)

        self.grp_usb.add(self.row_usb)

        # Initial visibility based on whether Autodarts is installed
        installed_init = (
            Path("/etc/systemd/system/autodarts.service").exists()
            or Path("/usr/lib/systemd/system/autodarts.service").exists()
            or (Path.home() / ".local" / "opt" / "autodarts" / "autodarts").exists()
            or Path("/usr/local/bin/autodarts").exists()
        )
        self.maint_box.set_visible(installed_init)
        self.row_board_section.set_visible(installed_init)
        self.card_cam.set_visible(installed_init)
        self.grp_usb.set_visible(installed_init)

        # Connect page lifecycle
        self.connect("map", self._on_page_mapped)
        self.connect("unmap", self._on_page_unmapped)

    def _open_board_setup_dialog(self, btn):
        dlg = BoardSetupDialog(self.window, on_saved_cb=self.refresh)
        dlg.present()

    def _on_page_mapped(self, widget):
        self.refresh()
        self._load_cam_config()
        if not self.poll_source_id:
            self.poll_source_id = GLib.timeout_add_seconds(3, self._periodic_poll)

    def _on_page_unmapped(self, widget):
        if self.poll_source_id:
            GLib.source_remove(self.poll_source_id)
            self.poll_source_id = None

    def _periodic_poll(self):
        self.refresh()
        return True

    def refresh(self):
        if self.is_busy:
            return
        self.spinner.start()

        def worker():
            status = SystemdService.get_status(SERVICE_NAME)
            telem = fetch_telemetry()
            return {"status": status, "telem": telem}

        def on_done(res):
            self.spinner.stop()
            self._apply_state(res)

        run_async(worker, on_done=on_done)

    def _apply_state(self, data):
        status = data["status"]
        telem = data["telem"]

        active_state = status.get("active_state", "nofile")

        # Reset primary button box children
        while child := self.btn_action_box.get_first_child():
            self.btn_action_box.remove(child)

        board_id = telem.get("board_id", "")
        online = telem.get("online", False)

        if active_state == "active":
            self.btn_action_box.append(self.btn_stop)
            self.btn_action_box.append(self.btn_restart)
            self.btn_action_box.append(self.btn_web)
            self.btn_web.set_sensitive(True)

            ver = telem.get("version", "v1.0.7")
            self.lbl_web_info.set_text(f"Autodarts {ver}")

            self.btn_reinstall.set_sensitive(True)
            self.btn_uninstall.set_sensitive(True)

        elif active_state == "nofile":
            btn_install = create_button_with_icon("software-update-available-symbolic", "Install Autodarts", "suggested-action", height=50, touch_btn=True)
            btn_install.connect("clicked", self._on_install_clicked)
            self.btn_action_box.append(btn_install)

            self.lbl_web_info.set_text("Autodarts Engine: Not Installed")

            self.btn_reinstall.set_sensitive(False)
            self.btn_uninstall.set_sensitive(False)
        else:
            self.btn_action_box.append(self.btn_start)
            self.btn_action_box.append(self.btn_restart)
            self.btn_action_box.append(self.btn_web)
            self.btn_web.set_sensitive(False)

            self.lbl_web_info.set_text("Autodarts Engine: Stopped")

            self.btn_reinstall.set_sensitive(True)
            self.btn_uninstall.set_sensitive(True)

        # Visibility of sections based on whether Autodarts is installed
        is_installed = (active_state != "nofile")
        self.maint_box.set_visible(is_installed)
        self.row_board_section.set_visible(is_installed)
        self.card_cam.set_visible(is_installed)
        self.grp_usb.set_visible(is_installed)

        # Update cloud board row
        if board_id:
            self.row_board.set_title("Board Linked")
            self.row_board.set_subtitle(f"UUID: {board_id[:8]}...{board_id[-4:]}")
            self.board_icon.set_from_icon_name("emblem-default-symbolic")
            self.btn_board_setup.set_label("Edit Link")
            self.btn_board_setup.remove_css_class("suggested-action")
        else:
            self.row_board.set_title("No Board Linked")
            self.row_board.set_subtitle("Link a board to this device.")
            self.board_icon.set_from_icon_name("dialog-warning-symbolic")
            self.btn_board_setup.set_label("Link Board")
            self.btn_board_setup.add_css_class("suggested-action")

        # Dynamic 50/50 split: Show Start/Stop and Reset detection card when linked & active
        is_detection_available = bool(board_id) and (active_state == "active")
        if is_detection_available:
            self.row_board_section.set_homogeneous(True)
            self.detection_group.set_visible(True)
            is_running = telem.get("running", False)
            state_str = telem.get("state", "")
            event_str = telem.get("event", "")
            self._update_detection_buttons(is_running, state_str, event_str)
        else:
            self.row_board_section.set_homogeneous(False)
            self.detection_group.set_visible(False)

    def _update_detection_buttons(self, is_running: bool, status: str = "", event: str = ""):
        self.detection_running = is_running

        # 1. Update Start/Stop Button
        if is_running:
            self.img_detection.set_from_icon_name("media-playback-stop-symbolic")
            self.lbl_detection.set_text("Stop")
            self.btn_detection_toggle.remove_css_class("suggested-action")
            self.btn_detection_toggle.add_css_class("destructive-action")
        else:
            self.img_detection.set_from_icon_name("media-playback-start-symbolic")
            self.lbl_detection.set_text("Start")
            self.btn_detection_toggle.remove_css_class("destructive-action")
            self.btn_detection_toggle.add_css_class("suggested-action")

        # 2. Update Row Theming, Icon, and Subtitle Status
        for cls in ("detection-running", "detection-stopped", "detection-fault"):
            self.row_detection.remove_css_class(cls)

        status_lower = status.lower() if status else ""
        event_lower = event.lower() if event else ""

        if is_running:
            self.row_detection.add_css_class("detection-running")
            self.detection_icon.set_from_icon_name("media-playback-start-symbolic")
            self.row_detection.set_subtitle("Started")
            self.row_detection.set_tooltip_text("Detection engine is actively running.")
        elif "error" in status_lower or "fail" in status_lower or "fault" in status_lower or "error" in event_lower or "fail" in event_lower:
            self.row_detection.add_css_class("detection-fault")
            self.detection_icon.set_from_icon_name("dialog-warning-symbolic")
            err_sub = f"Fault: {event}" if (event and len(event) < 16) else "Faulted"
            self.row_detection.set_subtitle(err_sub)
            tip = f"Fault: {event}" if event else f"Status: {status}"
            self.row_detection.set_tooltip_text(tip)
        else:
            self.row_detection.add_css_class("detection-stopped")
            self.detection_icon.set_from_icon_name("media-playback-stop-symbolic")
            self.row_detection.set_subtitle("Stopped")
            self.row_detection.set_tooltip_text("Detection engine is stopped / idle.")

    def _on_detection_toggle_clicked(self, btn):
        if self.is_busy:
            return
        self.is_busy = True
        self.spinner.start()
        currently_running = getattr(self, "detection_running", False)
        action_name = "Stopping" if currently_running else "Starting"
        self.window.show_toast(f"{action_name} engine...")

        def worker():
            if currently_running:
                return control_detection_stop()
            else:
                return control_detection_start()

        def on_done(ok):
            self.is_busy = False
            self.spinner.stop()
            if ok:
                status_str = "stopped" if currently_running else "started"
                self.window.show_toast(f"Engine {status_str}.")
            else:
                self.window.show_toast("Detection action failed.")
            self.refresh()

        run_async(worker, on_done=on_done)

    def _on_detection_reset_clicked(self, btn):
        if self.is_busy:
            return
        self.is_busy = True
        self.spinner.start()
        self.window.show_toast("Resetting engine...")

        def worker():
            return control_detection_reset()

        def on_done(ok):
            self.is_busy = False
            self.spinner.stop()
            if ok:
                self.window.show_toast("Engine state reset.")
            else:
                self.window.show_toast("Engine reset failed.")
            self.refresh()

        run_async(worker, on_done=on_done)

    def _do_service_action(self, action):
        self.is_busy = True
        self.spinner.start()
        def worker():
            if action == "start":
                return SystemdService.start_unit(SERVICE_NAME)
            elif action == "stop":
                return SystemdService.stop_unit(SERVICE_NAME)
            elif action == "restart":
                return SystemdService.restart_unit(SERVICE_NAME)
            return False

        def on_done(ok):
            self.is_busy = False
            self.spinner.stop()
            action_name = action.capitalize()
            if ok:
                self.window.show_toast(f"Autodarts service: {action_name} completed.")
            else:
                self.window.show_toast(f"Autodarts service {action} failed.")
            self.refresh()

        run_async(worker, on_done=on_done)

    def _open_web_ui(self, btn):
        url = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
        try:
            if not open_browser_url(self.window, url):
                self.window.show_toast("Failed to open web browser.")
        except Exception:
            logger.exception("Failed opening web browser for Autodarts UI")
            self.window.show_toast("Failed to open web browser.")

    def _on_install_clicked(self, btn):
        self.window.show_toast("Installing Autodarts official release...")
        self.is_busy = True
        self.spinner.start()

        def on_done(res):
            self.is_busy = False
            self.spinner.stop()
            ok, msg = res
            self.window.show_toast(msg)
            self.refresh()

        run_async(install_autodarts, on_done=on_done)

    def _on_uninstall_clicked(self, btn):
        dialog = Adw.AlertDialog(
            heading="Uninstall Autodarts?",
            body="This will stop the service and delete Autodarts binaries and configurations. Are you sure?"
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("uninstall", "Uninstall")
        dialog.set_response_appearance("uninstall", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(d, response):
            if response == "uninstall":
                self._run_uninstall()

        dialog.connect("response", on_response)
        dialog.present(self.window)

    def _run_uninstall(self):
        self.window.show_toast("Uninstalling Autodarts...")
        self.is_busy = True
        self.spinner.start()

        def on_done(res):
            self.is_busy = False
            self.spinner.stop()
            ok, msg = res
            self.window.show_toast(msg)
            self.refresh()

        run_async(uninstall_autodarts, on_done=on_done)

    def _load_cam_config(self):
        def worker():
            cfg = read_cam_config()
            available = get_available_cameras()
            supported_res = get_supported_resolutions()
            return cfg, available, supported_res

        def on_done(res):
            cfg, available, supported_res = res
            self.cam_options = available
            self.res_options = supported_res

            # 1. Update Camera Device dropdowns
            labels = [c["label"] for c in available]
            for combo, saved_path in [
                (self.combo_c1, cfg["cams"][0]),
                (self.combo_c2, cfg["cams"][1]),
                (self.combo_c3, cfg["cams"][2]),
            ]:
                model = Gtk.StringList.new(labels)
                combo.set_model(model)
                sel = 0
                for idx, c in enumerate(available):
                    if c["path"] and c["path"] == saved_path:
                        sel = idx
                        break
                combo.set_selected(sel)
                if sel < len(available) and available[sel].get("full_name"):
                    combo.set_tooltip_text(available[sel]["full_name"])

            # 2. Update Resolution dropdown (strictly from camera firmware)
            res_labels = [f"{w}x{h}" for w, h in supported_res]
            self.combo_res.set_model(Gtk.StringList.new(res_labels))
            cur_res = (cfg.get("width", 1280), cfg.get("height", 720))
            res_sel = 0
            if cur_res in supported_res:
                res_sel = supported_res.index(cur_res)
            self.combo_res.set_selected(res_sel)

            # 3. Update FPS dropdown (15, 20, 25, 30)
            cur_fps = cfg.get("fps", 30)
            fps_sel = 3  # default 30
            if cur_fps in FPS_OPTIONS:
                fps_sel = FPS_OPTIONS.index(cur_fps)
            self.combo_fps.set_selected(fps_sel)

            self.cam_config_loaded = True

        run_async(worker, on_done=on_done)

    def _on_apply_cam_clicked(self, btn):
        res_idx = self.combo_res.get_selected()
        fps_idx = self.combo_fps.get_selected()
        c1_idx = self.combo_c1.get_selected()
        c2_idx = self.combo_c2.get_selected()
        c3_idx = self.combo_c3.get_selected()

        width, height = self.res_options[res_idx] if res_idx < len(self.res_options) else (1280, 720)
        fps = FPS_OPTIONS[fps_idx] if fps_idx < len(FPS_OPTIONS) else 30

        c1_path = self.cam_options[c1_idx]["path"] if c1_idx < len(self.cam_options) else ""
        c2_path = self.cam_options[c2_idx]["path"] if c2_idx < len(self.cam_options) else ""
        c3_path = self.cam_options[c3_idx]["path"] if c3_idx < len(self.cam_options) else ""

        self.is_busy = True
        self.spinner.start()
        self.window.show_toast(f"Applying {width}x{height} @ {fps} FPS and restarting...")

        def worker():
            save_cam_config([c1_path, c2_path, c3_path], width, height, fps)
            SystemdService.restart_unit(SERVICE_NAME)
            return True

        def on_done(ok):
            self.is_busy = False
            self.spinner.stop()
            self.window.show_toast(f"Cameras set to {width}x{height} @ {fps} FPS.")
            self.refresh()
            self._load_cam_config()

        run_async(worker, on_done=on_done)
