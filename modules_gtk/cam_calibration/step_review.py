"""Review and A/B comparison step for CamCalibrationView: magnified ROI & dart testing."""

from __future__ import annotations
import logging
import threading
import time
from typing import TYPE_CHECKING, Dict, Optional, Tuple
import cv2
import numpy as np

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from core.cv_utils import (
    apply_difference_filter,
    apply_edge_contrast_filter,
    apply_noise_inspector_filter,
    detect_dart_tip,
)
from core.systemd_service import SystemdManager
from core.v4l2_device import V4L2Device
from modules_gtk.cv_gtk_utils import extract_magnified_pixbuf, frame_to_pixbuf

if TYPE_CHECKING:
    from modules_gtk.cam_calibration_view import CamCalibrationView

logger = logging.getLogger("cam_librator.review")


class ReviewStep:
    """Manages the A/B comparison review, magnifier details, filter modes, and dart insertion testing."""

    def __init__(self, view: CamCalibrationView):
        self.view = view
        self.review_selected_cam: Optional[str] = None
        self.review_source_mode: str = "empty"
        self.review_zoom_zone: str = "sweet_spot"
        self.review_custom_coord: Optional[Tuple[int, int]] = None
        self.review_zoom_factor: float = 2.5
        self.active_review_mode: str = "after"
        self.active_review_filter: str = "normal"
        self._magnifier_crop_coords: Dict[str, Tuple[int, int, int, int]] = {}

        self.dart_frames_before: Dict[str, np.ndarray] = {}
        self.dart_frames_after: Dict[str, np.ndarray] = {}
        self.dart_tips: Dict[str, Tuple[int, int]] = {}
        self.dart_capturing: bool = False

        self.review_cam_widgets: Dict[str, dict] = {}
        self.rev_cam_nav_buttons: Dict[Optional[str], Gtk.Button] = {}
        self.ov_filt_buttons: Dict[str, Gtk.Button] = {}
        self.det_filt_buttons: Dict[str, Gtk.Button] = {}
        self.rev_zone_buttons: Dict[str, Gtk.Button] = {}
        self.rev_zoom_buttons: Dict[float, Gtk.Button] = {}

    def build_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(12)
        box.set_margin_start(16)
        box.set_margin_end(16)

        # Header & Controls Row
        top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)

        top_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label="Step 5 of 5: Review & Comparison", xalign=0.0)
        title.add_css_class("wizard-title")
        top_txt.append(title)
        self.review_sub_lbl = Gtk.Label(
            label="Direct A/B comparison and magnified inspection.",
            xalign=0.0,
        )
        self.review_sub_lbl.set_wrap(True)
        self.review_sub_lbl.add_css_class("wizard-subtitle")
        top_txt.append(self.review_sub_lbl)
        top_bar.append(top_txt)

        # Top Right: Camera Selector Buttons (Overview vs Cam 1 vs Cam 2 vs Cam 3)
        self.rev_cam_selector_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.rev_cam_selector_box.set_hexpand(True)
        self.rev_cam_selector_box.set_halign(Gtk.Align.END)
        self.rev_cam_selector_box.set_valign(Gtk.Align.CENTER)
        top_bar.append(self.rev_cam_selector_box)
        box.append(top_bar)

        # View Stack: Switch between "overview" (3 columns) and "detail" (side-by-side magnified)
        self.review_view_stack = Gtk.Stack()
        self.review_view_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.review_view_stack.set_vexpand(True)

        # -------------------------------------------------------------
        # 1. OVERVIEW VIEW (3 Columns)
        # -------------------------------------------------------------
        self.review_overview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        # Overview Top Controls (Source + A/B Toggle + Filters)
        ov_ctrls_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        ov_ctrls_box.set_halign(Gtk.Align.FILL)

        ov_row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        ov_row1.set_halign(Gtk.Align.END)
        ov_ctrls_box.append(ov_row1)

        ov_src_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.rev_src_empty_btn = Gtk.Button(label="Empty Board")
        self.rev_src_empty_btn.add_css_class("compact-btn")
        self.rev_src_empty_btn.connect("clicked", lambda _: self.set_review_source("empty"))
        ov_src_box.append(self.rev_src_empty_btn)

        self.rev_src_dart_btn = Gtk.Button(label="With Dart")
        self.rev_src_dart_btn.add_css_class("compact-btn")
        self.rev_src_dart_btn.connect("clicked", lambda _: self.set_review_source("dart"))
        ov_src_box.append(self.rev_src_dart_btn)
        ov_row1.append(ov_src_box)

        ov_sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        ov_row1.append(ov_sep)

        ov_ab_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.rev_btn_after = Gtk.Button(label="After (Calibrated)")
        self.rev_btn_after.add_css_class("compact-btn")
        self.rev_btn_after.connect("clicked", lambda _: self.set_review_mode("after"))
        ov_ab_box.append(self.rev_btn_after)

        self.rev_btn_before = Gtk.Button(label="Before (Default)")
        self.rev_btn_before.add_css_class("compact-btn")
        self.rev_btn_before.connect("clicked", lambda _: self.set_review_mode("before"))
        ov_ab_box.append(self.rev_btn_before)
        ov_row1.append(ov_ab_box)

        # Filter Buttons in Overview
        ov_row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        ov_row2.set_halign(Gtk.Align.END)
        self.ov_filt_buttons.clear()
        for f_val, f_lbl in [
            ("normal", "Normal"),
            ("edge", "Edges & Wire"),
            ("diff", "CV Difference"),
            ("noise", "Noise Analysis"),
        ]:
            btn = Gtk.Button(label=f_lbl)
            btn.add_css_class("compact-btn")
            btn.connect("clicked", self._make_filter_handler(f_val))
            ov_row2.append(btn)
            self.ov_filt_buttons[f_val] = btn
        ov_ctrls_box.append(ov_row2)
        self.review_overview_box.append(ov_ctrls_box)

        # 3 Columns Container
        self.review_columns_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, homogeneous=True)
        self.review_columns_box.set_vexpand(True)
        self.review_overview_box.append(self.review_columns_box)
        self.review_view_stack.add_named(self.review_overview_box, "overview")

        # -------------------------------------------------------------
        # 2. DETAIL VIEW (Side-by-Side Magnifier Before vs After)
        # -------------------------------------------------------------
        self.review_detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.review_detail_box.set_vexpand(True)

        # Detail Toolbar (Source, Zones, Zoom Level, Filters)
        detail_tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        detail_tb.set_valign(Gtk.Align.CENTER)

        detail_row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        detail_row1.set_halign(Gtk.Align.CENTER)
        detail_tb.append(detail_row1)

        # Target Zone Group
        zone_lbl = Gtk.Label(label="Target:")
        zone_lbl.add_css_class("caption")
        detail_row1.append(zone_lbl)

        zone_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.rev_zone_buttons.clear()
        for z_val, z_lbl in [
            ("dart_tip", "Dart Tip"),
            ("sweet_spot", "Sweet Spot"),
            ("bullseye", "Bullseye"),
            ("treble", "Treble Ring"),
            ("center", "Full Board"),
        ]:
            z_btn = Gtk.Button(label=z_lbl)
            z_btn.add_css_class("compact-btn")
            z_btn.connect("clicked", self._make_zone_handler(z_val))
            zone_box.append(z_btn)
            self.rev_zone_buttons[z_val] = z_btn
        detail_row1.append(zone_box)

        sep_d1 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        detail_row1.append(sep_d1)

        # Zoom Factor Group
        zoom_lbl = Gtk.Label(label="Zoom:")
        zoom_lbl.add_css_class("caption")
        detail_row1.append(zoom_lbl)

        zoom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.rev_zoom_buttons.clear()
        for zoom_f, z_lbl in [(1.5, "1.5x"), (2.5, "2.5x"), (4.0, "4.0x")]:
            zf_btn = Gtk.Button(label=z_lbl)
            zf_btn.add_css_class("compact-btn")
            zf_btn.connect("clicked", self._make_zoom_factor_handler(zoom_f))
            zoom_box.append(zf_btn)
            self.rev_zoom_buttons[zoom_f] = zf_btn
        detail_row1.append(zoom_box)

        detail_row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        detail_row2.set_halign(Gtk.Align.CENTER)
        detail_tb.append(detail_row2)

        # Source Selector in Detail Mode
        det_src_lbl = Gtk.Label(label="Source:")
        det_src_lbl.add_css_class("caption")
        detail_row2.append(det_src_lbl)

        det_src_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.det_src_empty_btn = Gtk.Button(label="Empty Board")
        self.det_src_empty_btn.add_css_class("compact-btn")
        self.det_src_empty_btn.connect("clicked", lambda _: self.set_review_source("empty"))
        det_src_box.append(self.det_src_empty_btn)

        self.det_src_dart_btn = Gtk.Button(label="With Dart")
        self.det_src_dart_btn.add_css_class("compact-btn")
        self.det_src_dart_btn.connect("clicked", lambda _: self.set_review_source("dart"))
        det_src_box.append(self.det_src_dart_btn)
        detail_row2.append(det_src_box)

        sep_d2 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        detail_row2.append(sep_d2)

        # Filter Group in Detail Mode
        det_filt_lbl = Gtk.Label(label="Filter:")
        det_filt_lbl.add_css_class("caption")
        detail_row2.append(det_filt_lbl)

        det_filt_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.det_filt_buttons.clear()
        for f_val, f_lbl in [
            ("normal", "Normal"),
            ("edge", "Edges & Wire"),
            ("diff", "CV Difference"),
            ("noise", "Noise Analysis"),
        ]:
            btn = Gtk.Button(label=f_lbl)
            btn.add_css_class("compact-btn")
            btn.connect("clicked", self._make_filter_handler(f_val))
            det_filt_box.append(btn)
            self.det_filt_buttons[f_val] = btn
        detail_row2.append(det_filt_box)

        self.review_detail_box.append(detail_tb)

        # Side-by-Side Dual Column Box
        side_by_side_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, homogeneous=True)
        side_by_side_box.set_vexpand(True)

        # Left Column: Before (Default) Card
        self.rev_detail_card_before = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.rev_detail_card_before.add_css_class("cam-column-card")

        hdr_before = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_before = Gtk.Label(label="Before (Default)", xalign=0.0)
        title_before.add_css_class("heading")
        hdr_before.append(title_before)

        self.rev_detail_badge_before = Gtk.Label(label="Default")
        self.rev_detail_badge_before.set_css_classes(["status-pill-warn"])
        self.rev_detail_badge_before.set_hexpand(True)
        self.rev_detail_badge_before.set_xalign(1.0)
        hdr_before.append(self.rev_detail_badge_before)
        self.rev_detail_card_before.append(hdr_before)

        self.rev_detail_pic_before = Gtk.Picture()
        self.rev_detail_pic_before.set_can_shrink(True)
        self.rev_detail_pic_before.set_size_request(160, 110)
        self.rev_detail_pic_before.set_hexpand(True)
        self.rev_detail_pic_before.set_vexpand(True)
        click_before = Gtk.GestureClick()
        click_before.connect("pressed", lambda g, n, x, y: self._on_detail_picture_clicked(g, n, x, y, "before"))
        self.rev_detail_pic_before.add_controller(click_before)
        self.rev_detail_card_before.append(self.rev_detail_pic_before)

        self.rev_detail_lbl_before = Gtk.Label(label="Default values", xalign=0.0)
        self.rev_detail_lbl_before.add_css_class("caption")
        self.rev_detail_card_before.append(self.rev_detail_lbl_before)
        side_by_side_box.append(self.rev_detail_card_before)

        # Right Column: After (Calibrated) Card
        self.rev_detail_card_after = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.rev_detail_card_after.add_css_class("cam-column-card")

        hdr_after = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.rev_detail_title_after = Gtk.Label(label="After (Calibrated)", xalign=0.0)
        self.rev_detail_title_after.add_css_class("heading")
        hdr_after.append(self.rev_detail_title_after)

        self.rev_detail_badge_after = Gtk.Label(label="Calibrated")
        self.rev_detail_badge_after.set_css_classes(["status-pill-ok"])
        self.rev_detail_badge_after.set_hexpand(True)
        self.rev_detail_badge_after.set_xalign(1.0)
        hdr_after.append(self.rev_detail_badge_after)
        self.rev_detail_card_after.append(hdr_after)

        self.rev_detail_pic_after = Gtk.Picture()
        self.rev_detail_pic_after.set_can_shrink(True)
        self.rev_detail_pic_after.set_size_request(160, 110)
        self.rev_detail_pic_after.set_hexpand(True)
        self.rev_detail_pic_after.set_vexpand(True)
        click_after = Gtk.GestureClick()
        click_after.connect("pressed", lambda g, n, x, y: self._on_detail_picture_clicked(g, n, x, y, "after"))
        self.rev_detail_pic_after.add_controller(click_after)
        self.rev_detail_card_after.append(self.rev_detail_pic_after)

        self.rev_detail_lbl_after = Gtk.Label(label="Calibrated parameters", xalign=0.0)
        self.rev_detail_lbl_after.add_css_class("caption")
        self.rev_detail_lbl_after.add_css_class("accent")
        self.rev_detail_card_after.append(self.rev_detail_lbl_after)
        side_by_side_box.append(self.rev_detail_card_after)

        self.review_detail_box.append(side_by_side_box)

        # Helper note
        hint_lbl = Gtk.Label(label="Click anywhere on the image to pan the magnifying glass.", xalign=0.5)
        hint_lbl.set_wrap(True)
        hint_lbl.add_css_class("caption")
        hint_lbl.set_opacity(0.7)
        self.review_detail_box.append(hint_lbl)

        self.review_view_stack.add_named(self.review_detail_box, "detail")
        box.append(self.review_view_stack)

        # Bottom Bar: Discard vs Test with Dart vs Apply
        bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        bottom_bar.set_margin_top(10)

        discard_btn = Gtk.Button(label="Discard")
        discard_btn.add_css_class("compact-btn")
        discard_btn.connect("clicked", lambda _: self.view._go_to_dashboard())
        bottom_bar.append(discard_btn)

        self.rev_btn_test_dart = Gtk.Button(label="Test with Dart")
        self.rev_btn_test_dart.add_css_class("compact-btn")
        self.rev_btn_test_dart.connect("clicked", lambda _: self._show_dart_test_prompt())
        bottom_bar.append(self.rev_btn_test_dart)

        apply_btn = Gtk.Button(label="Save & Apply Settings")
        apply_btn.add_css_class("suggested-action")
        apply_btn.add_css_class("compact-btn")
        apply_btn.set_hexpand(True)
        apply_btn.set_halign(Gtk.Align.END)
        apply_btn.connect("clicked", lambda _: self._apply_and_finish())
        bottom_bar.append(apply_btn)
        box.append(bottom_bar)

        return box

    def setup_review_columns(self):
        # 1. Rebuild Top Camera Selector
        while child := self.rev_cam_selector_box.get_first_child():
            self.rev_cam_selector_box.remove(child)

        self.rev_cam_nav_buttons.clear()

        btn_all = Gtk.Button(label="All Cameras")
        btn_all.add_css_class("compact-btn")
        btn_all.connect("clicked", lambda _: self.select_review_camera(None))
        self.rev_cam_selector_box.append(btn_all)
        self.rev_cam_nav_buttons[None] = btn_all

        for cam in self.view.cameras:
            c_btn = Gtk.Button(label=f"Camera {cam.index}")
            c_btn.add_css_class("compact-btn")
            c_btn.connect("clicked", self._make_cam_select_handler(cam.dev_node))
            self.rev_cam_selector_box.append(c_btn)
            self.rev_cam_nav_buttons[cam.dev_node] = c_btn

        # 2. Rebuild 3 Overview Column Cards
        while child := self.review_columns_box.get_first_child():
            self.review_columns_box.remove(child)

        self.review_cam_widgets.clear()

        for cam in self.view.cameras:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            card.add_css_class("cam-column-card")

            hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            title = Gtk.Label(label=f"Camera {cam.index}", xalign=0.0)
            title.add_css_class("heading")
            hdr.append(title)

            badge = Gtk.Label(label="Calibrated")
            badge.set_css_classes(["status-pill-ok"])
            badge.set_hexpand(True)
            badge.set_xalign(1.0)
            hdr.append(badge)
            card.append(hdr)

            pic = Gtk.Picture()
            pic.set_can_shrink(True)
            pic.set_size_request(160, 110)
            pic.set_hexpand(True)
            pic.set_vexpand(True)
            card.append(pic)

            # Metric Box under picture
            metric_grid = Gtk.Grid()
            metric_grid.set_column_spacing(12)
            metric_grid.set_row_spacing(4)

            res = self.view.results.get(cam.dev_node)
            if res:
                s_opt = res.optimized_settings
                m_init = res.initial_metrics
                m_opt = res.optimized_metrics
                exp_lbl = Gtk.Label(label=f"Exposure: {s_opt.exposure_absolute * 0.1:.1f}ms", xalign=0.0)
                exp_lbl.add_css_class("caption")
                gain_lbl = Gtk.Label(label=f"Gain: {s_opt.gain}", xalign=0.0)
                gain_lbl.add_css_class("caption")

                noise_delta = ((m_init.temporal_noise - m_opt.temporal_noise) / max(0.01, m_init.temporal_noise)) * 100.0 if m_init else 0.0
                noise_sign = "-" if noise_delta >= 0 else "+"
                noise_lbl = Gtk.Label(label=f"Noise: {noise_sign}{abs(noise_delta):.0f}%", xalign=0.0)
                noise_lbl.add_css_class("caption")

                contrast_sign = "+" if res.improvement_percentage >= 0 else ""
                contrast_lbl = Gtk.Label(label=f"Wire Contrast: {contrast_sign}{res.improvement_percentage:.0f}%", xalign=0.0)
                contrast_lbl.add_css_class("caption")
                contrast_lbl.add_css_class("accent")

                metric_grid.attach(exp_lbl, 0, 0, 1, 1)
                metric_grid.attach(gain_lbl, 1, 0, 1, 1)
                metric_grid.attach(noise_lbl, 0, 1, 1, 1)
                metric_grid.attach(contrast_lbl, 1, 1, 1, 1)
            else:
                info = Gtk.Label(label="Default values unchanged", xalign=0.0)
                info.add_css_class("caption")
                metric_grid.attach(info, 0, 0, 2, 1)

            card.append(metric_grid)

            inspect_btn = Gtk.Button(label="Inspect Magnified")
            inspect_btn.add_css_class("compact-btn")
            inspect_btn.add_css_class("suggested-action")
            inspect_btn.connect("clicked", self._make_cam_select_handler(cam.dev_node))
            card.append(inspect_btn)

            self.review_cam_widgets[cam.dev_node] = {
                "badge": badge,
                "picture": pic,
            }
            self.review_columns_box.append(card)

        # Default to overview or previously selected
        if self.review_selected_cam is None:
            self.select_review_camera(None)
        else:
            self.select_review_camera(self.review_selected_cam)

        self._update_source_button_styles()
        self._update_zone_button_styles()
        self._update_zoom_button_styles()
        self._set_review_filter(self.active_review_filter)

    def _make_cam_select_handler(self, dev_node: Optional[str]):
        return lambda _: self.select_review_camera(dev_node)

    def select_review_camera(self, dev_node: Optional[str]):
        self.review_selected_cam = dev_node
        self.review_custom_coord = None
        if self.review_source_mode == "dart" and len(self.dart_frames_after) > 0:
            self.review_zoom_zone = "dart_tip"
        else:
            self.review_zoom_zone = "sweet_spot"

        for node, btn in self.rev_cam_nav_buttons.items():
            if node == dev_node:
                btn.add_css_class("suggested-action")
            else:
                btn.remove_css_class("suggested-action")

        if dev_node is None:
            self.review_view_stack.set_visible_child_name("overview")
            self.review_sub_lbl.set_text("Direct A/B comparison across all cameras.")
        else:
            self.review_view_stack.set_visible_child_name("detail")
            cam = next((c for c in self.view.cameras if c.dev_node == dev_node), None)
            cam_title = f"Camera {cam.index}" if cam else dev_node
            self.review_sub_lbl.set_text(f"Magnified Side-by-Side Inspection ({cam_title}).")

        self._update_zone_button_styles()
        self.refresh_review_pictures()

    def set_review_source(self, source: str):
        if source == "dart" and not self.dart_frames_after:
            self._show_dart_test_prompt()
            return
        self.review_source_mode = source
        if source == "dart":
            self.review_zoom_zone = "dart_tip"
        elif self.review_zoom_zone == "dart_tip":
            self.review_zoom_zone = "sweet_spot"
        self._update_source_button_styles()
        self._update_zone_button_styles()
        self.refresh_review_pictures()

    def _update_source_button_styles(self):
        is_dart = self.review_source_mode == "dart"
        if hasattr(self, "rev_src_empty_btn"):
            if not is_dart:
                self.rev_src_empty_btn.add_css_class("suggested-action")
                self.rev_src_dart_btn.remove_css_class("suggested-action")
            else:
                self.rev_src_dart_btn.add_css_class("suggested-action")
                self.rev_src_empty_btn.remove_css_class("suggested-action")
        if hasattr(self, "det_src_empty_btn"):
            if not is_dart:
                self.det_src_empty_btn.add_css_class("suggested-action")
                self.det_src_dart_btn.remove_css_class("suggested-action")
            else:
                self.det_src_dart_btn.add_css_class("suggested-action")
                self.det_src_empty_btn.remove_css_class("suggested-action")

    def _show_dart_test_prompt(self):
        root = self.view.get_root()
        dialog = Adw.MessageDialog(
            transient_for=root if isinstance(root, Gtk.Window) else None,
            heading="Insert Dart for Verification",
            body="Place or throw a dart into any board sector (e.g. Treble 20 or Bullseye). SUIT will capture before vs after snapshots across all cameras to verify dart tip edge contrast and CV difference signal.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("capture", "Capture Snapshot")
        dialog.set_response_appearance("capture", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._on_dart_dialog_response)
        dialog.present()

    def _on_dart_dialog_response(self, dialog, response: str):
        if response == "capture":
            self._start_dart_capture()

    def _start_dart_capture(self):
        if self.dart_capturing:
            return
        self.dart_capturing = True
        if hasattr(self, "rev_btn_test_dart"):
            self.rev_btn_test_dart.set_sensitive(False)
        self.review_sub_lbl.set_text("Capturing dart test frames across all cameras...")
        thread = threading.Thread(target=self._dart_capture_worker, daemon=True)
        thread.start()

    def _dart_capture_worker(self):
        try:
            for cam in self.view.cameras:
                res = self.view.results.get(cam.dev_node)
                vdev = V4L2Device(cam.dev_node)

                # 1. Capture Before Frame with initial settings
                if res and res.initial_settings:
                    vdev.apply_settings(res.initial_settings)
                time.sleep(0.12)

                frame_before = None
                for _ in range(4):
                    frame_before = vdev.grab_single_frame()
                    time.sleep(0.04)

                # 2. Capture After Frame with optimized settings
                if res and res.optimized_settings:
                    vdev.apply_settings(res.optimized_settings)
                time.sleep(0.12)

                frame_after = None
                for _ in range(4):
                    frame_after = vdev.grab_single_frame()
                    time.sleep(0.04)

                if frame_before is not None:
                    self.dart_frames_before[cam.dev_node] = frame_before
                if frame_after is not None:
                    self.dart_frames_after[cam.dev_node] = frame_after

                # Detect dart tip coordinate
                empty_frame = (
                    res.initial_frame
                    if (res and res.initial_frame is not None)
                    else self.view.baseline_frames.get(cam.dev_node)
                )
                if empty_frame is not None and frame_after is not None:
                    roi = res.roi if res else None
                    tip = detect_dart_tip(empty_frame, frame_after, roi=roi)
                    if tip:
                        self.dart_tips[cam.dev_node] = tip
        except Exception as e:
            logger.exception("Error capturing dart frames: %s", e)
        finally:
            GLib.idle_add(self._on_dart_capture_complete)

    def _on_dart_capture_complete(self):
        self.dart_capturing = False
        if hasattr(self, "rev_btn_test_dart"):
            self.rev_btn_test_dart.set_sensitive(True)
        self.review_source_mode = "dart"
        self.review_zoom_zone = "dart_tip"
        self._update_source_button_styles()
        self._update_zone_button_styles()
        self._set_review_filter("diff")
        self.refresh_review_pictures()

        if self.review_selected_cam:
            cam = next((c for c in self.view.cameras if c.dev_node == self.review_selected_cam), None)
            cam_title = f"Camera {cam.index}" if cam else self.review_selected_cam
            self.review_sub_lbl.set_text(f"Dart Test Inspection ({cam_title}) - Dart tip locked.")
        else:
            self.review_sub_lbl.set_text("Dart test captured. Inspect magnified dart tip and difference signal.")
        return False

    def _make_zone_handler(self, zone: str):
        return lambda _: self.set_zoom_zone(zone)

    def set_zoom_zone(self, zone: str):
        self.review_zoom_zone = zone
        self.review_custom_coord = None
        if zone == "center":
            self.review_zoom_factor = 1.0
        elif self.review_zoom_factor < 1.1:
            self.review_zoom_factor = 2.5
        self._update_zone_button_styles()
        self._update_zoom_button_styles()
        self.refresh_review_pictures()

    def _update_zone_button_styles(self):
        has_dart = len(self.dart_frames_after) > 0
        if "dart_tip" in self.rev_zone_buttons:
            self.rev_zone_buttons["dart_tip"].set_sensitive(has_dart)

        for z_val, btn in self.rev_zone_buttons.items():
            if z_val == self.review_zoom_zone:
                btn.add_css_class("suggested-action")
            else:
                btn.remove_css_class("suggested-action")

    def _make_zoom_factor_handler(self, factor: float):
        return lambda _: self.set_zoom_factor(factor)

    def set_zoom_factor(self, factor: float):
        self.review_zoom_factor = factor
        if self.review_zoom_zone == "center" and factor > 1.05:
            self.review_zoom_zone = "sweet_spot"
            self._update_zone_button_styles()
        self._update_zoom_button_styles()
        self.refresh_review_pictures()

    def _update_zoom_button_styles(self):
        for z_f, btn in self.rev_zoom_buttons.items():
            if abs(z_f - self.review_zoom_factor) < 0.1:
                btn.add_css_class("suggested-action")
            else:
                btn.remove_css_class("suggested-action")

    def _on_detail_picture_clicked(self, gesture, n_press, x, y, frame_key: str):
        widget = gesture.get_widget()
        ww = widget.get_width()
        wh = widget.get_height()
        if ww <= 0 or wh <= 0:
            return
        crop_box = self._magnifier_crop_coords.get(frame_key)
        if not crop_box:
            return
        x1, y1, cw, ch = crop_box
        if cw <= 0 or ch <= 0:
            return
        rel_x = max(0.0, min(1.0, x / ww))
        rel_y = max(0.0, min(1.0, y / wh))
        target_x = int(x1 + rel_x * cw)
        target_y = int(y1 + rel_y * ch)
        self.review_custom_coord = (target_x, target_y)
        self.review_zoom_zone = "custom"
        self._update_zone_button_styles()
        self.refresh_review_pictures()

    def set_review_mode(self, mode: str):
        self.active_review_mode = mode
        if mode == "after":
            self.rev_btn_after.add_css_class("suggested-action")
            self.rev_btn_before.remove_css_class("suggested-action")
        else:
            self.rev_btn_before.add_css_class("suggested-action")
            self.rev_btn_after.remove_css_class("suggested-action")
        self.refresh_review_pictures()

    def _make_filter_handler(self, f_val: str):
        return lambda _: self._set_review_filter(f_val)

    def _set_review_filter(self, f_val: str):
        self.active_review_filter = f_val
        for k, b in self.ov_filt_buttons.items():
            if k == f_val:
                b.add_css_class("suggested-action")
            else:
                b.remove_css_class("suggested-action")
        for k, b in self.det_filt_buttons.items():
            if k == f_val:
                b.add_css_class("suggested-action")
            else:
                b.remove_css_class("suggested-action")
        self.refresh_review_pictures()

    def refresh_review_pictures(self):
        """Render and display frames according to active view (overview vs detail)."""
        # 1. Update Overview Columns
        for cam in self.view.cameras:
            if cam.dev_node not in self.review_cam_widgets:
                continue

            res = self.view.results.get(cam.dev_node)
            base_frame = self.view.baseline_frames.get(cam.dev_node)

            if self.review_source_mode == "dart":
                frame = (
                    self.dart_frames_after.get(cam.dev_node)
                    if self.active_review_mode == "after"
                    else self.dart_frames_before.get(cam.dev_node)
                )
                if frame is None:
                    frame = res.optimized_frame if (self.active_review_mode == "after" and res) else (res.initial_frame if res else base_frame)
                badge_txt = "Dart Snapshot"
                badge_cls = "status-pill-ok" if self.active_review_mode == "after" else "status-pill-warn"
                empty_ref = self.view.baseline_frames.get(cam.dev_node) or (res.initial_frame if res else None)
            else:
                if self.active_review_mode == "after" and res:
                    frame = res.optimized_frame
                    badge_sign = "+" if res.improvement_percentage >= 0 else ""
                    badge_txt = f"{badge_sign}{res.improvement_percentage:.0f}% Sharper"
                    badge_cls = "status-pill-ok" if res.improvement_percentage >= 0 else "status-pill-warn"
                else:
                    frame = res.initial_frame if res else base_frame
                    badge_txt = "Default"
                    badge_cls = "status-pill-warn"
                empty_ref = None

            if frame is None:
                continue

            if self.active_review_filter == "diff" and empty_ref is not None:
                filtered = apply_difference_filter(empty_ref, frame)
            elif self.active_review_filter == "edge":
                filtered = apply_edge_contrast_filter(frame)
            elif self.active_review_filter == "noise":
                filtered = apply_noise_inspector_filter(frame)
            else:
                filtered = frame

            pix = frame_to_pixbuf(filtered, max_w=480, max_h=300)
            w = self.review_cam_widgets[cam.dev_node]
            if pix:
                w["picture"].set_pixbuf(pix)
            w["badge"].set_text(badge_txt)
            w["badge"].set_css_classes([badge_cls])

        # 2. Update Detail Side-by-Side View if active
        if self.review_selected_cam:
            dev_node = self.review_selected_cam
            res = self.view.results.get(dev_node)
            base_frame = self.view.baseline_frames.get(dev_node)

            if self.review_source_mode == "dart":
                before_frame = self.dart_frames_before.get(dev_node)
                after_frame = self.dart_frames_after.get(dev_node)
                empty_ref = self.view.baseline_frames.get(dev_node) or (res.initial_frame if res else None)
            else:
                before_frame = res.initial_frame if (res and res.initial_frame is not None) else base_frame
                after_frame = res.optimized_frame if (res and res.optimized_frame is not None) else base_frame
                empty_ref = None

            ref_frame = after_frame if after_frame is not None else before_frame
            if ref_frame is not None and ref_frame.size > 0:
                fh, fw = ref_frame.shape[:2]

                # Determine center coordinate
                if self.review_zoom_zone == "dart_tip":
                    if dev_node in self.dart_tips:
                        center = self.dart_tips[dev_node]
                    elif res and res.roi and res.roi.target_roi:
                        tx, ty, tw, th = res.roi.target_roi
                        center = (tx + tw // 2, ty + th // 2)
                    else:
                        center = (fw // 2, int(fh * 0.25))
                elif self.review_zoom_zone == "custom" and self.review_custom_coord:
                    center = self.review_custom_coord
                elif self.review_zoom_zone == "bullseye":
                    if res and res.roi and res.roi.center:
                        center = res.roi.center
                    else:
                        center = (fw // 2, fh // 2)
                elif self.review_zoom_zone == "treble":
                    if res and res.roi and res.roi.center:
                        center = (res.roi.center[0], max(0, res.roi.center[1] - int(res.roi.radius * 0.55)))
                    else:
                        center = (fw // 2, int(fh * 0.35))
                elif self.review_zoom_zone == "center":
                    center = (fw // 2, fh // 2)
                else:  # "sweet_spot"
                    if res and res.roi and res.roi.target_roi:
                        tx, ty, tw, th = res.roi.target_roi
                        center = (tx + tw // 2, ty + th // 2)
                    else:
                        center = (fw // 2, int(fh * 0.25))

                # Render Before
                if before_frame is not None:
                    pix_b, box_b = extract_magnified_pixbuf(
                        before_frame,
                        center=center,
                        zoom_factor=self.review_zoom_factor,
                        max_w=580,
                        max_h=380,
                        filter_mode=self.active_review_filter,
                        show_reticle=True,
                        diff_reference_frame=empty_ref,
                    )
                    if pix_b:
                        self.rev_detail_pic_before.set_pixbuf(pix_b)
                    self._magnifier_crop_coords["before"] = box_b

                # Render After
                if after_frame is not None:
                    pix_a, box_a = extract_magnified_pixbuf(
                        after_frame,
                        center=center,
                        zoom_factor=self.review_zoom_factor,
                        max_w=580,
                        max_h=380,
                        filter_mode=self.active_review_filter,
                        show_reticle=True,
                        diff_reference_frame=empty_ref,
                    )
                    if pix_a:
                        self.rev_detail_pic_after.set_pixbuf(pix_a)
                    self._magnifier_crop_coords["after"] = box_a

                # Update Labels and Badges
                if res:
                    s_init = res.initial_settings
                    s_opt = res.optimized_settings
                    m_init = res.initial_metrics
                    m_opt = res.optimized_metrics
                    sign = "+" if res.improvement_percentage >= 0 else ""

                    noise_delta = ((m_init.temporal_noise - m_opt.temporal_noise) / max(0.01, m_init.temporal_noise)) * 100.0 if m_init else 0.0
                    noise_sign = "-" if noise_delta >= 0 else "+"

                    self.rev_detail_badge_before.set_text("Default")
                    self.rev_detail_badge_before.set_css_classes(["status-pill-warn"])
                    self.rev_detail_lbl_before.set_text(
                        f"Exposure: {s_init.exposure_absolute * 0.1:.1f}ms | Gain: {s_init.gain} | Contrast: {s_init.contrast} | Sharpness: {s_init.sharpness}"
                    )

                    self.rev_detail_badge_after.set_text(f"{sign}{res.improvement_percentage:.0f}% Sharper")
                    self.rev_detail_badge_after.set_css_classes(["status-pill-ok" if res.improvement_percentage >= 0 else "status-pill-warn"])
                    self.rev_detail_lbl_after.set_text(
                        f"Exposure: {s_opt.exposure_absolute * 0.1:.1f}ms | Gain: {s_opt.gain} | Contrast: {s_opt.contrast} | Sharpness: {s_opt.sharpness} | Noise: {noise_sign}{abs(noise_delta):.0f}%"
                    )
                else:
                    self.rev_detail_lbl_before.set_text("Default values unchanged")
                    self.rev_detail_lbl_after.set_text("Calibrated values pending")

    def _apply_and_finish(self):
        """Save calibrated values to persistence, apply to cameras, and return to dashboard."""
        for dev_node, res in self.view.results.items():
            cam = next((c for c in self.view.cameras if c.dev_node == dev_node), None)
            if cam:
                self.view.persistence.save_calibration(cam.by_path, res.optimized_settings)
                try:
                    vdev = V4L2Device(dev_node)
                    vdev.apply_settings(res.optimized_settings)
                except Exception as e:
                    logger.error("Failed to apply settings to %s: %s", dev_node, e)

        self.view.persistence.set_calibration_enabled(True)
        if getattr(self.view, "_autodarts_was_running", False):
            SystemdManager.start_autodarts()
            self.view._autodarts_was_running = False
        self.view._go_to_dashboard()
