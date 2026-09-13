"""
Dedicated modal dialog for the 1-Click Darts Scorer Installer.
Displays live progress percentage, stage description, and spinner.
"""
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib
from core.system_service import SystemService
from modules_gtk.async_utils import run_async


class DartsScorerInstallDialog(Adw.Window):
    """
    Dedicated modal dialog for the 1-Click Darts Scorer Installer.
    Displays live progress percentage, stage description, and spinner.
    """
    def __init__(self, parent_window, on_finished_callback):
        super().__init__()
        self.set_transient_for(parent_window)
        self.set_modal(True)
        self.set_title("Installing Darts Scorer")
        self.set_default_size(460, 220)
        self.on_finished = on_finished_callback
        self._is_running = True

        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        self.header = Adw.HeaderBar()
        self.header.set_show_end_title_buttons(False)
        toolbar_view.add_top_bar(self.header)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content_box.set_margin_top(16)
        content_box.set_margin_bottom(20)
        content_box.set_margin_start(24)
        content_box.set_margin_end(24)
        toolbar_view.set_content(content_box)

        # Title / Subtitle
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        lbl_heading = Gtk.Label(label="Darts Scorer Setup", xalign=0)
        lbl_heading.add_css_class("title-3")
        title_box.append(lbl_heading)

        self.lbl_stage = Gtk.Label(label="Starting setup...", xalign=0)
        self.lbl_stage.add_css_class("dim-label")
        title_box.append(self.lbl_stage)
        content_box.append(title_box)

        # Progress bar + Spinner
        progress_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.spinner = Gtk.Spinner()
        self.spinner.start()
        progress_row.append(self.spinner)

        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_hexpand(True)
        self.progress_bar.set_show_text(True)
        self.progress_bar.set_fraction(0.05)
        progress_row.append(self.progress_bar)
        content_box.append(progress_row)

        # Bottom Action Bar
        bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bottom_bar.set_halign(Gtk.Align.END)
        content_box.append(bottom_bar)

        self.btn_action = Gtk.Button(label="Installing...")
        self.btn_action.set_sensitive(False)
        self.btn_action.set_size_request(130, 38)
        bottom_bar.append(self.btn_action)

        self.connect("close-request", self._on_close_request)
        self._start_install()

    def _on_close_request(self, win):
        return self._is_running

    def _on_launch_clicked(self, btn):
        SystemService.launch_darts_scorer()
        self.close()

    def _start_install(self):
        def progress_cb(stage_name, fraction):
            def update_ui():
                self.lbl_stage.set_label(stage_name)
                self.progress_bar.set_fraction(min(1.0, max(0.0, fraction)))
                return False
            GLib.idle_add(update_ui)

        def worker():
            return SystemService.full_darts_scorer_setup(progress_callback=progress_cb)

        def on_done(res):
            self._is_running = False
            self.spinner.stop()
            self.spinner.set_visible(False)
            self.header.set_show_end_title_buttons(True)
            success, msg = res
            if success:
                self.lbl_stage.set_label("Pinned to Dash and ready to play.")
                self.progress_bar.set_fraction(1.0)
                self.btn_action.set_label("Launch Scorer")
                self.btn_action.add_css_class("suggested-action")
                self.btn_action.connect("clicked", self._on_launch_clicked)
            else:
                self.lbl_stage.set_label(f"Installation failed: {msg}")
                self.btn_action.set_label("Close")
                self.btn_action.add_css_class("destructive-action")
                self.btn_action.connect("clicked", lambda b: self.close())
            self.btn_action.set_sensitive(True)

            if self.on_finished:
                self.on_finished(success, msg)

        run_async(worker, on_done=on_done)
