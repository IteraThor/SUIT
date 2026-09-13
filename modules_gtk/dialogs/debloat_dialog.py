"""
Granular review dialog for bloatware removal with checkboxes and real-time progress.
Allows users to easily select or deselect apps they wish to remove or keep.
"""
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib
from core.system_service import SystemService
from modules_gtk.async_utils import run_async


class DebloatReviewDialog(Adw.Window):
    """
    Granular review dialog for bloatware removal with checkboxes and real-time progress.
    Allows users to easily deselect apps they wish to keep on their system.
    """
    def __init__(self, parent_window, app_details, on_finished_callback):
        super().__init__()
        self.set_transient_for(parent_window)
        self.set_modal(True)
        self.set_title("Debloat Fedora System")
        self.set_default_size(560, 580)
        self.on_finished = on_finished_callback
        self.app_details = app_details
        self._is_removing = False
        self.connect("close-request", self._on_close_request)

        installed_items = [item for item in self.app_details if item.get("is_installed")]
        uninstalled_items = [item for item in self.app_details if not item.get("is_installed")]

        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content_box.set_margin_top(12)
        content_box.set_margin_bottom(16)
        content_box.set_margin_start(16)
        content_box.set_margin_end(16)
        toolbar_view.set_content(content_box)

        # Scrolled container
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_min_content_height(300)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        clamp = Adw.Clamp(maximum_size=540)
        scrolled.set_child(clamp)

        inner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        clamp.set_child(inner_box)

        self.check_buttons = []

        if installed_items:
            lbl_subtitle = Gtk.Label(
                label="Select applications to remove. Deselect any app you want to keep on your system.",
                wrap=True,
                xalign=0
            )
            lbl_subtitle.add_css_class("dim-label")
            content_box.append(lbl_subtitle)

            # Selection controls toolbar
            top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            self.btn_select_all = Gtk.Button(label="Select All")
            self.btn_select_all.add_css_class("flat")
            self.btn_select_all.connect("clicked", lambda b: self._set_all_selected(True))
            top_bar.append(self.btn_select_all)

            self.btn_deselect_all = Gtk.Button(label="Deselect All")
            self.btn_deselect_all.add_css_class("flat")
            self.btn_deselect_all.connect("clicked", lambda b: self._set_all_selected(False))
            top_bar.append(self.btn_deselect_all)

            self.lbl_selected_count = Gtk.Label(label="", xalign=1)
            self.lbl_selected_count.set_hexpand(True)
            self.lbl_selected_count.add_css_class("dim-label")
            top_bar.append(self.lbl_selected_count)
            content_box.append(top_bar)

            pref_group = Adw.PreferencesGroup()
            pref_group.set_title(f"Installed Default Applications ({len(installed_items)})")
            pref_group.set_description("Checked applications will be uninstalled. Uncheck any app to keep it.")
            inner_box.append(pref_group)

            for item in installed_items:
                row = Adw.ActionRow(
                    title=item["title"],
                    subtitle=item["description"]
                )
                icon_name = item.get("icon", "application-x-executable-symbolic")
                icon_img = Gtk.Image.new_from_icon_name(icon_name)
                icon_img.set_pixel_size(24)
                row.add_prefix(icon_img)

                chk = Gtk.CheckButton()
                chk.set_active(True)
                chk.set_valign(Gtk.Align.CENTER)
                chk.connect("toggled", self._on_check_toggled)
                row.add_suffix(chk)
                row.set_activatable_widget(chk)

                pref_group.add(row)
                self.check_buttons.append((chk, item["package"]))
        else:
            status_page = Adw.StatusPage()
            status_page.set_icon_name("emblem-ok-symbolic")
            status_page.set_title("System is Clean")
            status_page.set_description("0 bloatware packages found. All default bloatware apps have been removed from this system.")
            inner_box.append(status_page)

        if uninstalled_items:
            clean_group = Adw.PreferencesGroup()
            expander = Adw.ExpanderRow(
                title=f"Already Removed / Clean ({len(uninstalled_items)})",
                subtitle="Applications not installed on this system"
            )
            expander.set_expanded(not installed_items)
            for item in uninstalled_items:
                sub_row = Adw.ActionRow(
                    title=item["title"],
                    subtitle=item["description"]
                )
                sub_icon = Gtk.Image.new_from_icon_name(item.get("icon", "application-x-executable-symbolic"))
                sub_icon.set_pixel_size(20)
                sub_row.add_prefix(sub_icon)

                pill = Gtk.Label(label="Clean")
                pill.add_css_class("status-pill-running")
                pill.set_valign(Gtk.Align.CENTER)
                sub_row.add_suffix(pill)

                expander.add_row(sub_row)
            clean_group.add(expander)
            inner_box.append(clean_group)

        content_box.append(scrolled)

        # Live Progress Section (Hidden initially)
        self.progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.progress_box.set_visible(False)
        content_box.append(self.progress_box)

        self.lbl_progress_status = Gtk.Label(label="Starting package removal...", xalign=0)
        self.lbl_progress_status.add_css_class("caption")
        self.progress_box.append(self.lbl_progress_status)

        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_show_text(True)
        self.progress_box.append(self.progress_bar)

        # Bottom Action Bar
        bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bottom_bar.set_halign(Gtk.Align.END)
        content_box.append(bottom_bar)

        if installed_items:
            self.btn_cancel = Gtk.Button(label="Cancel")
            self.btn_cancel.set_size_request(80, 38)
            self.btn_cancel.connect("clicked", lambda b: self.close())
            bottom_bar.append(self.btn_cancel)

            self.btn_confirm = Gtk.Button(label="Remove Selected")
            self.btn_confirm.add_css_class("destructive-action")
            self.btn_confirm.set_size_request(160, 38)
            self.btn_confirm.connect("clicked", self._on_confirm_clicked)
            bottom_bar.append(self.btn_confirm)

            self._update_count()
        else:
            self.btn_clean_dash = Gtk.Button(label="Clean Dash Favorites")
            self.btn_clean_dash.add_css_class("secondary-btn")
            self.btn_clean_dash.set_size_request(160, 38)
            self.btn_clean_dash.connect("clicked", self._on_clean_dash_clicked)
            bottom_bar.append(self.btn_clean_dash)

            self.btn_close = Gtk.Button(label="Close")
            self.btn_close.set_size_request(80, 38)
            self.btn_close.connect("clicked", lambda b: self.close())
            bottom_bar.append(self.btn_close)

    def _on_close_request(self, win):
        return self._is_removing

    def _set_all_selected(self, state: bool):
        for chk, _ in self.check_buttons:
            chk.set_active(state)
        self._update_count()

    def _on_check_toggled(self, chk):
        self._update_count()

    def _update_count(self):
        selected = sum(1 for chk, _ in self.check_buttons if chk.get_active())
        total = len(self.check_buttons)
        self.lbl_selected_count.set_label(f"{selected} of {total} selected")
        self.btn_confirm.set_sensitive(selected > 0)
        self.btn_confirm.set_label(f"Remove Selected ({selected})")

    def _on_clean_dash_clicked(self, btn):
        btn.set_sensitive(False)
        def worker():
            SystemService.clean_dash_favorites()
            SystemService.move_seahorse_to_utilities()
            return True, "Dash favorites and app grid cleaned."
        def on_done(res):
            self.close()
            if self.on_finished:
                self.on_finished(True, res[1])
        run_async(worker, on_done=on_done)

    def _on_confirm_clicked(self, btn):
        to_remove = [pkg for chk, pkg in self.check_buttons if chk.get_active()]
        if not to_remove:
            return

        self._is_removing = True
        self.btn_confirm.set_sensitive(False)
        self.btn_cancel.set_sensitive(False)
        self.btn_select_all.set_sensitive(False)
        self.btn_deselect_all.set_sensitive(False)
        for chk, _ in self.check_buttons:
            chk.set_sensitive(False)

        self.progress_box.set_visible(True)
        self.progress_bar.set_fraction(0.0)

        def progress_cb(cur, tot, msg):
            def update_ui():
                frac = (cur / tot) if tot > 0 else 0.0
                self.progress_bar.set_fraction(min(1.0, max(0.0, frac)))
                self.lbl_progress_status.set_label(msg)
                return False
            GLib.idle_add(update_ui)

        def worker():
            return SystemService.debloat_packages(to_remove, progress_callback=progress_cb)

        def on_done(res):
            self._is_removing = False
            success, msg = res
            self.close()
            if self.on_finished:
                self.on_finished(success, msg)

        run_async(worker, on_done=on_done)
