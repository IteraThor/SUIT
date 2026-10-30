import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
from core.system_service import SystemService
from modules_gtk.async_utils import run_async
from modules_gtk.ui_helpers import (
    PAGE_MAX_WIDTH, PAGE_TIGHTEN_AT, build_page_shell, create_button_with_icon,
)
from core.i18n import tr

__all__ = ["FastBootView", "summarize_fast_boot", "format_boot_phases"]

PHASE_LABELS = [("firmware", "BIOS"), ("loader", "Boot Loader"), ("kernel", "Kernel"),
                ("initrd", "Early Boot"), ("userspace", "System")]
RATING_TEXT = {"good": "Fast", "ok": "Could be faster", "slow": "Slow", "unknown": "Not measured"}


def summarize_fast_boot(applied: int, total: int, boot_time: float | None) -> str:
    """One-line state for the System page row, e.g. '4 of 11 tweaks on · Last boot 33.3 s'."""
    if applied:
        text = tr("{applied} of {total} tweaks on").format(applied=applied, total=total)
    else:
        text = tr("Off. Pick boot-time tweaks one by one.")
    if boot_time:
        text += " · " + tr("Last boot {seconds:.1f} s").format(seconds=boot_time)
    return text


def format_boot_phases(report: dict | None) -> str:
    if not report:
        return tr("Boot time not available.")
    return " · ".join(f"{tr(label)} {report[key]:.1f} s" for key, label in PHASE_LABELS if key in report)


class FastBootView(Adw.NavigationPage):
    """Fast Boot submenu: one switch per boot tweak with its estimated saving, and a BIOS guide."""

    def __init__(self, window, on_changed=None):
        super().__init__(title=tr("Fast Boot"), tag="fast_boot")
        self.window = window
        self._on_changed = on_changed
        self._updating = False
        self._busy = False
        self.tweaks = []
        self.tweak_rows = {}
        self.estimate_labels = {}

        # Last boot and the bulk actions stay pinned above the two tabs
        self.grp_summary = Adw.PreferencesGroup(title=tr("Last Boot"))
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.btn_revert_all = create_button_with_icon("edit-undo-symbolic", "Revert All",
                                                      "secondary-btn compact-btn", height=36)
        self.btn_revert_all.connect("clicked", lambda b: self._run_bulk(False))
        self.btn_recommended = create_button_with_icon("system-run-symbolic", "Apply Recommended",
                                                       "suggested-action compact-btn", height=36)
        self.btn_recommended.connect("clicked", lambda b: self._run_bulk(True))
        actions.append(self.btn_revert_all)
        actions.append(self.btn_recommended)
        self.grp_summary.set_header_suffix(actions)

        self.row_total = Adw.ActionRow(title=tr("Checking..."), subtitle="")
        self.grp_summary.add(self.row_total)

        top = Adw.Clamp(maximum_size=PAGE_MAX_WIDTH, tightening_threshold=PAGE_TIGHTEN_AT)
        top.set_margin_top(12)
        top.set_margin_start(16)
        top.set_margin_end(16)
        top.set_child(self.grp_summary)

        scrolled_tweaks, tweaks_box = build_page_shell()
        scrolled_bios, self.bios_box = build_page_shell()
        self.stack = Adw.ViewStack(vexpand=True)
        self.stack.add_titled(scrolled_tweaks, "tweaks", tr("Tweaks"))
        self.stack.add_titled(scrolled_bios, "bios", "BIOS")

        switcher = Adw.InlineViewSwitcher(stack=self.stack, display_mode=Adw.InlineViewSwitcherDisplayMode.LABELS)
        switcher.set_halign(Gtk.Align.CENTER)
        switcher.set_margin_top(4)

        page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page_box.append(top)
        page_box.append(switcher)
        page_box.append(self.stack)
        self.set_child(page_box)

        self.grp_login = Adw.PreferencesGroup(
            title=tr("Faster to the Desktop"),
            description=tr("Login waits for these. Estimates come from your last boot."))
        self.grp_background = Adw.PreferencesGroup(
            title=tr("Less Background Load"),
            description=tr("These start alongside login and compete for CPU and disk."))
        tweaks_box.append(self.grp_login)
        tweaks_box.append(self.grp_background)

        # BIOS tab: one group, rebuilt on every refresh since findings and steps depend on the machine
        self.btn_setup = create_button_with_icon("system-reboot-symbolic", "Open BIOS Setup",
                                                 "secondary-btn compact-btn", height=36)
        self.btn_setup.set_valign(Gtk.Align.CENTER)
        self.btn_setup.connect("clicked", self._on_open_setup_clicked)
        self.grp_bios = None

    # ------------------------------------------------------------------ state

    def refresh(self):
        def worker():
            report = SystemService.get_boot_report()
            return SystemService.get_boot_tweaks(), report, SystemService.analyze_firmware(report)

        def on_done(res):
            self.tweaks, report, firmware = res
            self._load_summary(report)
            self._load_tweaks()
            self._load_firmware(firmware)

        run_async(worker, on_done=on_done)

    def _load_summary(self, report):
        if report:
            self.row_total.set_title(tr("{seconds:.1f} s from power-on to desktop").format(seconds=report["total"]))
        else:
            self.row_total.set_title(tr("Boot time not available"))
        pending = sum(t["estimate_s"] for t in self.tweaks
                      if not t["applied"] and not t["unavailable"])
        phases = format_boot_phases(report)
        if pending >= 0.1:
            phases += "\n" + tr("Up to ≈ {seconds:.1f} s more available below.").format(seconds=pending)
        self.row_total.set_subtitle(phases)
        has_recommended = any(t["recommended"] and not t["applied"] and not t["unavailable"]
                              for t in self.tweaks)
        self.btn_recommended.set_visible(has_recommended)
        self.btn_revert_all.set_visible(any(t["applied"] for t in self.tweaks))

    def _load_tweaks(self):
        self._updating = True
        try:
            for t in self.tweaks:
                row = self.tweak_rows.get(t["id"])
                if row is None:
                    row = Adw.SwitchRow(title=tr(t["title"]))
                    # Fixed width so every title lines up whatever the estimate says
                    label = Gtk.Label(valign=Gtk.Align.CENTER, width_chars=10)
                    label.add_css_class("status-pill-checking")
                    row.add_prefix(label)
                    row.connect("notify::active", self._on_tweak_toggled, t["id"])
                    (self.grp_login if t["blocks_login"] else self.grp_background).add(row)
                    self.tweak_rows[t["id"]] = row
                    self.estimate_labels[t["id"]] = label

                subtitle = tr(t["summary"])
                if t["unavailable"]:
                    subtitle += f"\n{tr(t['unavailable'])}"
                elif t["warning"]:
                    subtitle += f"\n⚠ {tr(t['warning'])}"
                row.set_subtitle(subtitle)
                row.set_active(t["applied"])
                row.set_sensitive(not self._busy and (t["applied"] or not t["unavailable"]))

                label = self.estimate_labels[t["id"]]
                if t["applied"]:
                    label.set_label(tr("saves {seconds:.1f} s").format(seconds=t["estimate_s"]))
                else:
                    label.set_label(f"≈ {t['estimate_s']:.1f} s")
                for cls in ("status-pill-checking", "status-pill-running"):
                    label.remove_css_class(cls)
                label.add_css_class("status-pill-running" if t["applied"] else "status-pill-checking")
        finally:
            self._updating = False

    def _load_firmware(self, fw):
        if self.grp_bios is not None:
            self.grp_bios.set_header_suffix(None)
            self.bios_box.remove(self.grp_bios)

        took = f"{fw['firmware_s']:.1f} s · " if fw["firmware_s"] is not None else ""
        machine = fw["model"] if fw["model"].startswith(fw["vendor"]) else f"{fw['vendor']} {fw['model']}"
        self.grp_bios = Adw.PreferencesGroup(
            title=f"BIOS · {took}{tr(RATING_TEXT[fw['rating']])}",
            description=tr("{machine}: press {key} at power-on. "
                           "Change one thing at a time and check it still boots.").format(
                machine=machine.strip() or tr("This machine"), key=tr(fw["setup_key"])))
        self.grp_bios.set_header_suffix(self.btn_setup)

        for text in fw["findings"]:
            row = Adw.ActionRow(title=tr(text), title_lines=1)
            row.add_css_class("property")
            row.add_prefix(Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
            self.grp_bios.add(row)

        for i, step in enumerate(fw["steps"], 1):
            row = Adw.ActionRow(title=f"{i}. {tr(step['title'])}", subtitle=tr(step["where"] or step["detail"]),
                                title_lines=1, subtitle_lines=1)
            row.set_tooltip_text(tr(step["detail"]))
            self.grp_bios.add(row)
        self.bios_box.append(self.grp_bios)

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.btn_recommended.set_sensitive(not busy)
        self.btn_revert_all.set_sensitive(not busy)
        for row in self.tweak_rows.values():
            if busy:
                row.set_sensitive(False)
        if hasattr(self.window, "btn_back"):
            self.window.btn_back.set_sensitive(not busy)

    def _finish(self, msg: str):
        self._set_busy(False)
        self.window.show_toast(msg)
        self.refresh()
        if self._on_changed:
            self._on_changed()

    # ------------------------------------------------------------------ actions

    def _on_tweak_toggled(self, row, param, tweak_id):
        if self._updating or self._busy:
            return
        active = row.get_active()
        tweak = next(t for t in self.tweaks if t["id"] == tweak_id)
        if active and tweak["warning"]:
            dialog = Adw.AlertDialog(heading=tr("Turn on {title}?").format(title=tr(tweak["title"])),
                                     body=f"{tr(tweak['summary'])}\n\n{tr(tweak['warning'])}")
            dialog.add_response("cancel", tr("Cancel"))
            dialog.add_response("apply", tr("Turn On"))
            dialog.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)

            def on_response(d, resp):
                if resp == "apply":
                    self._run_tweak(tweak_id, True)
                else:
                    self._updating = True
                    row.set_active(False)
                    self._updating = False

            dialog.connect("response", on_response)
            dialog.present(self.window)
            return
        self._run_tweak(tweak_id, active)

    def _run_tweak(self, tweak_id: str, active: bool):
        self._set_busy(True)
        if tweak_id == "splash":
            self.window.show_toast(tr("Rebuilding the boot image, this takes about a minute..."))

        def on_done(res):
            self._finish(res[1])

        def on_error(err):
            self._finish(tr("Fast Boot error: {err}").format(err=err))

        run_async(lambda: SystemService.set_boot_tweak(tweak_id, active), on_done=on_done, on_error=on_error)

    def _run_bulk(self, enable: bool):
        self._set_busy(True)
        self.window.show_toast(tr("Applying recommended tweaks...") if enable else tr("Reverting all tweaks..."))

        def on_done(res):
            self._finish(res[1])

        def on_error(err):
            self._finish(tr("Fast Boot error: {err}").format(err=err))

        run_async(lambda: SystemService.set_fast_boot(enable), on_done=on_done, on_error=on_error)

    def _on_open_setup_clicked(self, btn):
        dialog = Adw.AlertDialog(
            heading=tr("Restart into BIOS Setup?"),
            body=tr("The computer restarts straight into the BIOS. Save your work first. "
                 "Autodarts stops until you leave the BIOS again."))
        dialog.add_response("cancel", tr("Cancel"))
        dialog.add_response("restart", tr("Restart Now"))
        dialog.set_response_appearance("restart", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(d, resp):
            if resp != "restart":
                return
            run_async(SystemService.reboot_to_firmware, on_done=lambda res: self.window.show_toast(res[1]))

        dialog.connect("response", on_response)
        dialog.present(self.window)
