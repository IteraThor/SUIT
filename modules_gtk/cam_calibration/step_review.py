"""Review and A/B comparison step for CamCalibrationView: magnified ROI and ISP filter evaluation."""

from __future__ import annotations
import logging
from typing import TYPE_CHECKING, Dict, Optional, Tuple
import cv2
import numpy as np

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from core.cv_utils import apply_edge_contrast_filter, apply_difference_filter
from core.systemd_service import SystemdManager
from core.v4l2_device import V4L2Device
from core.camera_manual_service import CameraManualService
from modules_gtk.cv_gtk_utils import extract_magnified_pixbuf, frame_to_pixbuf

if TYPE_CHECKING:
    from modules_gtk.cam_calibration_view import CamCalibrationView

logger = logging.getLogger("cam_librator.review")


class ReviewStep:
    """Manages the A/B comparison review, magnifier details, and filter inspection modes."""

    def __init__(self, view: CamCalibrationView):
        self.view = view
        self.review_selected_cam: Optional[str] = None
        self.review_zoom_zone: str = "center"
        self.review_custom_coord: Optional[Tuple[int, int]] = None
        self.review_zoom_factor: float = 1.0
        self.active_review_mode: str = "after"
        self.active_review_filter: str = "normal"
        self._magnifier_crop_coords: Dict[str, Tuple[int, int, int, int]] = {}

        self.review_cam_widgets: Dict[str, dict] = {}
        self.rev_cam_nav_buttons: Dict[Optional[str], Gtk.Button] = {}
        self.ov_filt_buttons: Dict[str, Gtk.Button] = {}
        self.det_filt_buttons: Dict[str, Gtk.Button] = {}
        self.rev_zone_buttons: Dict[str, Gtk.Button] = {}
        self.rev_zoom_buttons: Dict[float, Gtk.Button] = {}

    def build_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(6)
        box.set_margin_bottom(8)
        box.set_margin_start(10)
        box.set_margin_end(10)

        # Unified Header & Controls Top Bar
        top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        top_bar.set_valign(Gtk.Align.CENTER)

        top_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title = Gtk.Label(label="Step 4 of 4: Review & Comparison", xalign=0.0)
        title.add_css_class("heading")
        top_txt.append(title)
        self.review_sub_lbl = Gtk.Label(
            label="Direct A/B comparison and magnified inspection.",
            xalign=0.0,
        )
        self.review_sub_lbl.add_css_class("dim-label")
        top_txt.append(self.review_sub_lbl)
        top_bar.append(top_txt)

        spacer = Gtk.Box(hexpand=True)
        top_bar.append(spacer)

        # Camera Selector Tabs (All Cameras vs Cam 1 vs Cam 2 vs Cam 3)
        self.rev_cam_selector_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.rev_cam_selector_box.set_valign(Gtk.Align.CENTER)
        top_bar.append(self.rev_cam_selector_box)

        # Overview A/B Toggle & Filter Controls (compact in top bar)
        self.ov_ctrls_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.ov_ctrls_box.set_valign(Gtk.Align.CENTER)

        # A/B Toggle Box
        ov_ab_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.rev_btn_after = Gtk.Button(label="After")
        self.rev_btn_after.add_css_class("compact-btn")
        self.rev_btn_after.set_tooltip_text("Show calibrated settings")
        self.rev_btn_after.connect("clicked", lambda _: self.set_review_mode("after"))
        ov_ab_box.append(self.rev_btn_after)

        self.rev_btn_before = Gtk.Button(label="Before")
        self.rev_btn_before.add_css_class("compact-btn")
        self.rev_btn_before.set_tooltip_text("Show default hardware settings")
        self.rev_btn_before.connect("clicked", lambda _: self.set_review_mode("before"))
        ov_ab_box.append(self.rev_btn_before)
        self.ov_ctrls_box.append(ov_ab_box)

        # Filter Buttons (Normal vs Edges vs Heatmap)
        ov_filt_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.ov_filt_buttons.clear()
        for f_val, f_lbl in [
            ("normal", "Normal"),
            ("edge", "Edges"),
            ("diff", "Heatmap"),
        ]:
            btn = Gtk.Button(label=f_lbl)
            btn.add_css_class("compact-btn")
            btn.connect("clicked", self._make_filter_handler(f_val))
            ov_filt_box.append(btn)
            self.ov_filt_buttons[f_val] = btn
        self.ov_ctrls_box.append(ov_filt_box)

        top_bar.append(self.ov_ctrls_box)
        box.append(top_bar)

        # View Stack: Switch between "overview" (3 columns) and "detail" (side-by-side magnified)
        self.review_view_stack = Gtk.Stack()
        self.review_view_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.review_view_stack.set_vexpand(True)
        self.review_view_stack.set_hexpand(True)

        # -------------------------------------------------------------
        # 1. OVERVIEW VIEW (3 Columns)
        # -------------------------------------------------------------
        self.review_overview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.review_overview_box.set_vexpand(True)
        self.review_overview_box.set_hexpand(True)

        # 3 Columns Container
        self.review_columns_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, homogeneous=True)
        self.review_columns_box.set_vexpand(True)
        self.review_columns_box.set_hexpand(True)
        self.review_overview_box.append(self.review_columns_box)
        self.review_view_stack.add_named(self.review_overview_box, "overview")

        # -------------------------------------------------------------
        # 2. DETAIL VIEW (Side-by-Side Magnifier Before vs After)
        # -------------------------------------------------------------
        self.review_detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.review_detail_box.set_vexpand(True)
        self.review_detail_box.set_hexpand(True)

        # Detail Toolbar (Zoom & Filter)
        detail_tb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        detail_tb.set_halign(Gtk.Align.CENTER)
        detail_tb.set_valign(Gtk.Align.CENTER)

        # Zoom Factor Group
        zoom_lbl = Gtk.Label(label="View:")
        zoom_lbl.add_css_class("dim-label")
        detail_tb.append(zoom_lbl)

        zoom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.rev_zoom_buttons.clear()
        for zoom_f, z_lbl in [(1.0, "Full Board"), (1.5, "1.5x"), (2.5, "2.5x")]:
            zf_btn = Gtk.Button(label=z_lbl)
            zf_btn.add_css_class("compact-btn")
            zf_btn.connect("clicked", self._make_zoom_factor_handler(zoom_f))
            zoom_box.append(zf_btn)
            self.rev_zoom_buttons[zoom_f] = zf_btn
        detail_tb.append(zoom_box)

        sep_d = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        detail_tb.append(sep_d)

        # Filter Group in Detail Mode (Normal vs Edges)
        det_filt_lbl = Gtk.Label(label="Filter:")
        det_filt_lbl.add_css_class("dim-label")
        detail_tb.append(det_filt_lbl)

        det_filt_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.det_filt_buttons.clear()
        for f_val, f_lbl in [
            ("normal", "Normal"),
            ("edge", "Edges"),
            ("diff", "Heatmap"),
        ]:
            btn = Gtk.Button(label=f_lbl)
            btn.add_css_class("compact-btn")
            btn.connect("clicked", self._make_filter_handler(f_val))
            det_filt_box.append(btn)
            self.det_filt_buttons[f_val] = btn
        detail_tb.append(det_filt_box)

        self.review_detail_box.append(detail_tb)

        # Side-by-Side Dual Column Box
        side_by_side_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, homogeneous=True)
        side_by_side_box.set_vexpand(True)
        side_by_side_box.set_hexpand(True)

        # Left Column: Before (Default) Card
        self.rev_detail_card_before = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.rev_detail_card_before.add_css_class("cam-column-card")
        self.rev_detail_card_before.set_vexpand(True)
        self.rev_detail_card_before.set_hexpand(True)

        hdr_before = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
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
        self.rev_detail_pic_before.set_hexpand(True)
        self.rev_detail_pic_before.set_vexpand(True)
        click_before = Gtk.GestureClick()
        click_before.connect("pressed", lambda g, n, x, y: self._on_detail_picture_clicked(g, n, x, y, "before"))
        self.rev_detail_pic_before.add_controller(click_before)
        self.rev_detail_card_before.append(self.rev_detail_pic_before)

        self.rev_detail_lbl_before = Gtk.Label(label="Default values", xalign=0.5)
        self.rev_detail_lbl_before.add_css_class("caption")
        self.rev_detail_card_before.append(self.rev_detail_lbl_before)
        side_by_side_box.append(self.rev_detail_card_before)

        # Right Column: After (Calibrated) Card
        self.rev_detail_card_after = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.rev_detail_card_after.add_css_class("cam-column-card")
        self.rev_detail_card_after.set_vexpand(True)
        self.rev_detail_card_after.set_hexpand(True)

        hdr_after = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
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
        self.rev_detail_pic_after.set_hexpand(True)
        self.rev_detail_pic_after.set_vexpand(True)
        click_after = Gtk.GestureClick()
        click_after.connect("pressed", lambda g, n, x, y: self._on_detail_picture_clicked(g, n, x, y, "after"))
        self.rev_detail_pic_after.add_controller(click_after)
        self.rev_detail_card_after.append(self.rev_detail_pic_after)

        self.rev_detail_lbl_after = Gtk.Label(label="Calibrated parameters", xalign=0.5)
        self.rev_detail_lbl_after.add_css_class("caption")
        self.rev_detail_lbl_after.add_css_class("accent")
        self.rev_detail_card_after.append(self.rev_detail_lbl_after)
        side_by_side_box.append(self.rev_detail_card_after)

        self.review_detail_box.append(side_by_side_box)
        self.review_view_stack.add_named(self.review_detail_box, "detail")
        box.append(self.review_view_stack)

        # Bottom Bar: Discard vs Apply
        bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        bottom_bar.set_margin_top(4)

        discard_btn = Gtk.Button(label="Discard")
        discard_btn.add_css_class("touch-btn")
        discard_btn.connect("clicked", lambda _: self.view._go_to_dashboard())
        bottom_bar.append(discard_btn)

        apply_btn = Gtk.Button(label="Save & Apply Settings")
        apply_btn.add_css_class("suggested-action")
        apply_btn.add_css_class("touch-btn")
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
            b = Gtk.Button(label=f"Cam {cam.index}")
            b.add_css_class("compact-btn")
            b.connect("clicked", self._make_cam_select_handler(cam.dev_node))
            self.rev_cam_selector_box.append(b)
            self.rev_cam_nav_buttons[cam.dev_node] = b

        # 2. Rebuild 3 Columns Overview Cards
        while child := self.review_columns_box.get_first_child():
            self.review_columns_box.remove(child)

        self.review_cam_widgets.clear()

        for cam in self.view.cameras:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            card.add_css_class("cam-column-card")
            card.set_vexpand(True)
            card.set_hexpand(True)

            hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            title = Gtk.Label(label=f"Camera {cam.index}", xalign=0.0)
            title.add_css_class("heading")
            hdr.append(title)

            badge = Gtk.Label(label="Optimized")
            badge.set_css_classes(["status-pill-ok"])
            badge.set_hexpand(True)
            badge.set_xalign(1.0)
            hdr.append(badge)
            card.append(hdr)

            # Picture container (Expanding to fill card)
            pic = Gtk.Picture()
            pic.set_can_shrink(True)
            pic.set_hexpand(True)
            pic.set_vexpand(True)
            card.append(pic)

            # Compact Parameter Delta Summary (2x2 grid)
            res = self.view.results.get(cam.dev_node)
            grid = Gtk.Grid()
            grid.set_column_spacing(6)
            grid.set_row_spacing(2)
            grid.set_margin_top(2)

            def add_param(col: int, row: int, name: str, val_before: str, val_after: str):
                nl = Gtk.Label(label=name, xalign=0.0)
                nl.add_css_class("caption")
                grid.attach(nl, col * 2, row, 1, 1)

                vl = Gtk.Label(label=f"{val_before}→{val_after}", xalign=1.0)
                vl.set_hexpand(True)
                vl.add_css_class("caption")
                vl.add_css_class("accent")
                grid.attach(vl, col * 2 + 1, row, 1, 1)

            if res:
                s_i = res.initial_settings
                s_o = res.optimized_settings
                add_param(0, 0, "Exp:", f"{s_i.exposure_absolute * 0.1:.1f}ms", f"{s_o.exposure_absolute * 0.1:.1f}ms")
                add_param(1, 0, "Gain:", str(s_i.gain), str(s_o.gain))
                add_param(0, 1, "Contrast:", str(s_i.contrast), str(s_o.contrast))
                add_param(1, 1, "Sharp:", str(s_i.sharpness), str(s_o.sharpness))
            card.append(grid)

            # Clicking card zooms into details for that camera
            card_click = Gtk.GestureClick()
            card_click.connect("pressed", lambda g, n, x, y, node=cam.dev_node: self.select_review_camera(node))
            card.add_controller(card_click)

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

        self._update_zone_button_styles()
        self._update_zoom_button_styles()
        self._set_review_filter(self.active_review_filter)

    def _make_cam_select_handler(self, dev_node: Optional[str]):
        return lambda _: self.select_review_camera(dev_node)

    def select_review_camera(self, dev_node: Optional[str]):
        self.review_selected_cam = dev_node
        self.review_custom_coord = None
        self.review_zoom_zone = "center"

        for node, btn in self.rev_cam_nav_buttons.items():
            if node == dev_node:
                btn.add_css_class("suggested-action")
            else:
                btn.remove_css_class("suggested-action")

        if dev_node is None:
            self.review_view_stack.set_visible_child_name("overview")
            self.review_sub_lbl.set_text("Direct A/B comparison across all cameras.")
            if hasattr(self, "ov_ctrls_box"):
                self.ov_ctrls_box.set_visible(True)
        else:
            self.review_view_stack.set_visible_child_name("detail")
            cam = next((c for c in self.view.cameras if c.dev_node == dev_node), None)
            cam_title = f"Camera {cam.index}" if cam else dev_node
            self.review_sub_lbl.set_text(f"Magnified Side-by-Side Inspection ({cam_title}).")
            if hasattr(self, "ov_ctrls_box"):
                self.ov_ctrls_box.set_visible(False)

        self._update_zone_button_styles()
        self.refresh_review_pictures()

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
        for z_val, btn in self.rev_zone_buttons.items():
            if z_val == self.review_zoom_zone:
                btn.add_css_class("suggested-action")
            else:
                btn.remove_css_class("suggested-action")

    def _make_zoom_factor_handler(self, factor: float):
        return lambda _: self.set_zoom_factor(factor)

    def set_zoom_factor(self, factor: float):
        self.review_zoom_factor = factor
        self._update_zoom_button_styles()
        self.refresh_review_pictures()

    def _update_zoom_button_styles(self):
        for z_val, btn in self.rev_zoom_buttons.items():
            if abs(z_val - self.review_zoom_factor) < 0.1:
                btn.add_css_class("suggested-action")
            else:
                btn.remove_css_class("suggested-action")

    def set_review_mode(self, mode: str):
        self.active_review_mode = mode
        if hasattr(self, "rev_btn_after"):
            if mode == "after":
                self.rev_btn_after.add_css_class("suggested-action")
                self.rev_btn_before.remove_css_class("suggested-action")
            else:
                self.rev_btn_before.add_css_class("suggested-action")
                self.rev_btn_after.remove_css_class("suggested-action")
        self.refresh_review_pictures()

    def _make_filter_handler(self, filter_name: str):
        return lambda _: self._set_review_filter(filter_name)

    def _set_review_filter(self, filter_name: str):
        self.active_review_filter = filter_name
        for f_val, btn in self.ov_filt_buttons.items():
            if f_val == filter_name:
                btn.add_css_class("suggested-action")
            else:
                btn.remove_css_class("suggested-action")
        for f_val, btn in self.det_filt_buttons.items():
            if f_val == filter_name:
                btn.add_css_class("suggested-action")
            else:
                btn.remove_css_class("suggested-action")
        self.refresh_review_pictures()

    def _on_detail_picture_clicked(self, gesture, n_press, x, y, pane_type: str):
        """Map click coordinates inside magnified picture back to original image space."""
        if pane_type not in self._magnifier_crop_coords:
            return
        cx, cy, cw, ch = self._magnifier_crop_coords[pane_type]
        pic_widget = self.rev_detail_pic_before if pane_type == "before" else self.rev_detail_pic_after
        alloc_w = pic_widget.get_width()
        alloc_h = pic_widget.get_height()
        if alloc_w <= 0 or alloc_h <= 0:
            return

        rel_x = float(x) / float(alloc_w)
        rel_y = float(y) / float(alloc_h)
        full_x = int(cx + (rel_x * cw))
        full_y = int(cy + (rel_y * ch))

        dev_node = self.review_selected_cam
        res = self.view.results.get(dev_node) if dev_node else None
        base_frame = self.view.baseline_frames.get(dev_node) if dev_node else None
        ref = res.optimized_frame if (res and res.optimized_frame is not None) else base_frame
        if ref is not None and ref.size > 0:
            fh, fw = ref.shape[:2]
            full_x = max(0, min(fw - 1, full_x))
            full_y = max(0, min(fh - 1, full_y))
            self.review_custom_coord = (full_x, full_y)
            self.review_zoom_zone = "custom"
            self._update_zone_button_styles()
            self.refresh_review_pictures()

    def refresh_review_pictures(self):
        GLib.idle_add(self._update_review_display)

    def _update_review_display(self):
        # 1. Update Overview View
        for cam in self.view.cameras:
            if cam.dev_node not in self.review_cam_widgets:
                continue
            res = self.view.results.get(cam.dev_node)
            base_frame = self.view.baseline_frames.get(cam.dev_node)

            if self.active_review_mode == "after" and res:
                frame = res.optimized_frame
                badge_txt, badge_cls = res.badge()
            else:
                frame = res.initial_frame if res else base_frame
                badge_txt = "Default"
                badge_cls = "status-pill-warn"

            if frame is None:
                continue

            if self.active_review_filter == "edge":
                filtered = apply_edge_contrast_filter(frame)
            elif self.active_review_filter == "diff" and base_frame is not None:
                filtered = apply_difference_filter(base_frame, frame)
            else:
                filtered = frame

            pix = frame_to_pixbuf(filtered, max_w=640, max_h=440)
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

            before_frame = res.initial_frame if (res and res.initial_frame is not None) else base_frame
            after_frame = res.optimized_frame if (res and res.optimized_frame is not None) else base_frame

            ref_frame = after_frame if after_frame is not None else before_frame
            if ref_frame is not None and ref_frame.size > 0:
                fh, fw = ref_frame.shape[:2]

                # Determine center coordinate
                if self.review_zoom_zone == "custom" and self.review_custom_coord:
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
                        max_w=640,
                        max_h=440,
                        filter_mode=self.active_review_filter,
                        show_reticle=True,
                        diff_reference_frame=base_frame,
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
                        max_w=640,
                        max_h=440,
                        filter_mode=self.active_review_filter,
                        show_reticle=True,
                        diff_reference_frame=base_frame,
                    )
                    if pix_a:
                        self.rev_detail_pic_after.set_pixbuf(pix_a)
                    self._magnifier_crop_coords["after"] = box_a

                # Update Labels and Badges
                if res:
                    s_init = res.initial_settings
                    s_opt = res.optimized_settings

                    self.rev_detail_badge_before.set_text("Default")
                    self.rev_detail_badge_before.set_css_classes(["status-pill-warn"])
                    self.rev_detail_lbl_before.set_text(
                        f"Exposure: {s_init.exposure_absolute * 0.1:.1f}ms | Gain: {s_init.gain} | Contrast: {s_init.contrast} | Sharpness: {s_init.sharpness}"
                    )

                    badge_txt, badge_cls = res.badge()
                    self.rev_detail_badge_after.set_text(badge_txt)
                    self.rev_detail_badge_after.set_css_classes([badge_cls])
                    self.rev_detail_lbl_after.set_text(
                        f"Exposure: {s_opt.exposure_absolute * 0.1:.1f}ms | Gain: {s_opt.gain} | Contrast: {s_opt.contrast} | Sharpness: {s_opt.sharpness}"
                    )
                else:
                    self.rev_detail_lbl_before.set_text("Default values unchanged")
                    self.rev_detail_lbl_after.set_text("Calibrated values pending")
        return False

    def _apply_and_finish(self):
        """Save calibrated values directly to camera hardware and enable CameraManualService boot unit."""
        applied_nodes = []
        kept_nodes = []
        for dev_node, res in self.view.results.items():
            cam = next((c for c in self.view.cameras if c.dev_node == dev_node), None)
            if cam and not res.is_improvement:
                logger.info("Keeping current settings on %s: no measurable gain (%.1f%%)", dev_node, res.improvement_percentage)
                kept_nodes.append(dev_node)
                continue
            if cam:
                applied_nodes.append(cam.dev_node)
                try:
                    vdev = V4L2Device(dev_node)
                    vdev.apply_settings(res.optimized_settings)
                    # Also persist to manual service presets
                    CameraManualService.save_preset(dev_node, res.optimized_settings.to_dict())
                except Exception as e:
                    logger.error("Failed to apply settings to %s: %s", dev_node, e)

        # Persist across reboots via the single unified SUIT camera boot service
        all_nodes = [c.dev_node for c in self.view.cameras] if self.view.cameras else applied_nodes
        if applied_nodes and all_nodes:
            CameraManualService.enable_boot_service(all_nodes)

        if getattr(self.view, "_autodarts_was_running", False) or SystemdManager._autodarts_paused_by_suit:
            SystemdManager.resume_autodarts()
            self.view._autodarts_was_running = False

        if hasattr(self.view, "toast_overlay"):
            if applied_nodes:
                msg = f"Applied calibrated settings to {len(applied_nodes)} camera(s)"
                if kept_nodes:
                    msg += f"; kept {len(kept_nodes)} unchanged (no gain)"
            else:
                msg = "No camera improved – current settings kept"
            self.view.toast_overlay.add_toast(Adw.Toast(title=msg))

        self.view._go_to_dashboard()

        # Open Manual Controls Menu
        if hasattr(self.view, "on_open_manual") and callable(self.view.on_open_manual):
            self.view.on_open_manual()
        elif hasattr(self.view, "window") and hasattr(self.view.window, "open_camera_manual_page"):
            self.view.window.open_camera_manual_page()
