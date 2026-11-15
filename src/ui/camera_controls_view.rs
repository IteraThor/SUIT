//! Camera Controls page: live view and every control of each camera, for any UVC camera (core::hbvcal::live).
//!
//! Camera strip on top (shared with the Camera Optimizer), live view with metric chips and overlays on the left,
//! collapsible control sections on the right. Every row has - / + steppers, a tappable value and a revert button.
//! Changes apply immediately; a camera's settings are kept once it has a profile. On confirmed HBV OV2710 cameras
//! the hidden JPEG quality and sensor gain are adjustable too.
use super::camera_widgets::{self as cw, ask_rename, caption, chip, port_no, segmented, set_chip, to_texture, AutodartsStatus, CameraStrip, StripEntry, ThumbPoller, THUMB_PX};
use super::helpers::{alert, ask, esc, run_async};
use super::{Page, Win};
use crate::core::cvx;
use crate::core::hbvcal::daemon::cameras;
use crate::core::hbvcal::{board, live, oneclick, profiles};
use crate::core::i18n::{tr, trf};
use adw::prelude::*;
use gtk::{glib, pango};
use opencv::core::{Mat, Scalar};
use opencv::imgproc;
use opencv::prelude::*;
use serde_json::Value;
use std::cell::{Cell, RefCell};
use std::collections::{HashMap, HashSet};
use std::rc::Rc;
use std::time::Instant;

/// Sensor analog gain register 0x350B: bits 4-6 = x2 steps, bits 0-3 = n/16
const SENSOR_STEPS: [i64; 10] = [0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18, 0x30, 0x38, 0x70];
const GROUPS: [(&str, &str); 5] = [("exposure", "Exposure"), ("tone", "Tone"), ("detail", "Detail"), ("color", "Color"), ("stream", "Stream")];
/// a select with this many options or fewer is shown as one-tap segments, else as a dropdown
const SEGMENT_MAX: usize = 4;
const UNDO_MAX: usize = 20;
/// a camera takes these first, so the dependent controls accept their values
const APPLY_FIRST: [&str; 2] = ["auto_exposure", "white_balance_automatic"];

#[derive(Clone, Copy, PartialEq)]
enum Kind {
    Select,
    Range,
    Sensor,
    Switch,
    Q,
}

struct Spec {
    name: &'static str,
    group: &'static str,
    label: &'static str,
    kind: Kind,
    options: &'static [(i64, &'static str)],
}

const SPEC: [Spec; 15] = [
    Spec { name: "auto_exposure", group: "exposure", label: "Mode", kind: Kind::Select, options: &[(1, "Manual"), (3, "Automatic")] },
    Spec { name: "exposure_time_absolute", group: "exposure", label: "Exposure time", kind: Kind::Range, options: &[] },
    Spec { name: "gain", group: "exposure", label: "Digital gain", kind: Kind::Range, options: &[] },
    Spec { name: "sensor_gain", group: "exposure", label: "Sensor gain", kind: Kind::Sensor, options: &[] },
    Spec { name: "brightness", group: "tone", label: "Brightness", kind: Kind::Range, options: &[] },
    Spec { name: "contrast", group: "tone", label: "Contrast", kind: Kind::Range, options: &[] },
    Spec { name: "gamma", group: "tone", label: "Gamma", kind: Kind::Range, options: &[] },
    Spec { name: "saturation", group: "tone", label: "Saturation", kind: Kind::Range, options: &[] },
    Spec { name: "hue", group: "tone", label: "Hue", kind: Kind::Range, options: &[] },
    Spec { name: "sharpness", group: "detail", label: "Sharpening", kind: Kind::Range, options: &[] },
    Spec { name: "backlight_compensation", group: "detail", label: "Backlight", kind: Kind::Range, options: &[] },
    Spec { name: "white_balance_automatic", group: "color", label: "Auto white balance", kind: Kind::Switch, options: &[] },
    Spec { name: "white_balance_temperature", group: "color", label: "Temperature", kind: Kind::Range, options: &[] },
    Spec { name: "q", group: "stream", label: "JPEG compression", kind: Kind::Q, options: &[] },
    Spec { name: "power_line_frequency", group: "stream", label: "Anti-flicker", kind: Kind::Select, options: &[(0, "Off"), (1, "50 Hz"), (2, "60 Hz")] },
];

fn spec(n: &str) -> Option<&'static Spec> {
    SPEC.iter().find(|s| s.name == n)
}

/// why a control is greyed out
fn off_reason(n: &str) -> Option<&'static str> {
    match n {
        "exposure_time_absolute" => Some("Set Mode to Manual to change the exposure time."),
        "sensor_gain" => Some("Set Mode to Manual to change the sensor gain."),
        "white_balance_temperature" => Some("Turn off Auto white balance to set a temperature."),
        _ => None,
    }
}

fn disabled(n: &str, vals: &HashMap<String, i64>) -> bool {
    match n {
        "exposure_time_absolute" | "sensor_gain" => vals.get("auto_exposure") != Some(&1),
        "white_balance_temperature" => vals.get("white_balance_automatic") == Some(&1),
        _ => false,
    }
}

pub fn sensor_gain(code: i64) -> f64 {
    let c = (code >> 4) & 7;
    (1i64 << c.count_ones()) as f64 * (1.0 + (code & 15) as f64 / 16.0)
}

pub fn gain_label(code: i64) -> String {
    let s = format!("{:.2}", sensor_gain(code));
    format!("{}×", s.trim_end_matches('0').trim_end_matches('.'))
}

/// Ask for a name and save every camera's settings as a preset (also used after an optimization).
pub fn ask_save_preset(win: &Win, name: &str) {
    ask_save_preset_then(win, name, None);
}

pub fn ask_save_preset_then(win: &Win, name: &str, on_saved: Option<Rc<dyn Fn()>>) {
    let d = adw::AlertDialog::new(
        Some(&tr("Save as Preset")),
        Some(&tr("Stores the settings of every camera under this name. Load it later from Camera Controls → Presets.")),
    );
    let e = gtk::Entry::new();
    e.set_text(name);
    e.set_activates_default(true);
    e.set_placeholder_text(Some(&tr("e.g. Evening light")));
    d.set_extra_child(Some(&e));
    d.add_response("cancel", &tr("Cancel"));
    d.add_response("save", &tr("Save"));
    d.set_response_appearance("save", adw::ResponseAppearance::Suggested);
    d.set_default_response(Some("save"));
    let w = win.clone();
    d.connect_response(None, move |_, r| {
        let new = e.text().trim().to_string();
        if r != "save" || new.is_empty() {
            return;
        }
        let replaced = profiles::load_presets().contains_key(&new);
        let (w2, cb, n2) = (w.clone(), on_saved.clone(), new.clone());
        run_async(move || live::save_preset(&n2).map_err(|e| e.to_string()), move |res| match res {
            Ok(_) => {
                w2.show_toast(&trf(if replaced { "Preset “{name}” overwritten" } else { "Preset “{name}” saved" }, &[("name", &new)]));
                if let Some(cb) = cb {
                    cb();
                }
            }
            Err(e) => w2.show_toast(&e),
        });
    });
    d.present(Some(&win.window));
}

fn calibrated_value(info: &Value, n: &str) -> Option<i64> {
    let k = &info["calibrated"];
    if !k.is_object() {
        return None;
    }
    match n {
        "q" => k["mjpeg_q"].as_i64(),
        "sensor_gain" => k["sensor"]["0x350B"].as_i64(),
        _ => k["uvc"][n].as_i64(),
    }
}

fn is_hbv(c: &Value) -> bool {
    c["hbv"].as_bool().unwrap_or(false)
}

fn exposure_text(c: &Value, v: i64) -> String {
    if is_hbv(c) { format!("{:.1} ms", v as f64 * 0.05) } else { v.to_string() } // HBV: 50 µs units (measured)
}

type PendingArgs = (String, String, i64, Rc<Cell<i64>>);

pub struct CameraControlsPage {
    nav: adw::NavigationPage,
    win: Win,
    cams: RefCell<Vec<Value>>,
    cur: Cell<usize>,
    building: Cell<bool>,
    pending: RefCell<HashMap<String, (glib::SourceId, PendingArgs)>>,
    origin: RefCell<HashMap<String, i64>>,
    undo: RefCell<Vec<(String, String, String, i64)>>,
    diffs: RefCell<HashMap<String, bool>>,
    sec_dots: RefCell<HashMap<String, gtk::Label>>,
    open: RefCell<HashSet<String>>,
    live_timer: RefCell<Option<glib::SourceId>>,
    live_tick: Cell<u64>,
    scores_busy: Rc<Cell<bool>>,
    shown_seq: Cell<u64>,
    painted: RefCell<Option<Mat>>,
    frozen: Cell<bool>,
    ident: Cell<u64>,
    rate: Cell<(u64, u64, Instant, f64)>,
    overlays: RefCell<HashMap<&'static str, bool>>,
    masks: RefCell<HashMap<(String, i32, i32), Option<Mat>>>,
    strip: Rc<CameraStrip>,
    autodarts: Rc<AutodartsStatus>,
    thumbs: RefCell<Option<Rc<ThumbPoller>>>,
    live_picture: gtk::Picture,
    lbl_tag: gtk::Label,
    chips: gtk::FlowBox,
    chip_white: gtk::Label,
    chip_clip: gtk::Label,
    chip_noise: gtk::Label,
    chip_kb: gtk::Label,
    chip_note: gtk::Label,
    check_box: gtk::Box,
    lbl_check: gtk::Label,
    btn_trim: gtk::Button,
    btn_check: gtk::Button,
    panel: gtk::Box,
    lbl_cam: gtk::Label,
    btn_copy: gtk::Button,
    sw_keep: gtk::Switch,
    lbl_keep: gtk::Label,
    ctl_scroll: gtk::ScrolledWindow,
    controls_box: gtk::Box,
    lbl_marks: gtk::Label,
    btn_undo: gtk::Button,
    btn_reset: gtk::Button,
}

impl Page for CameraControlsPage {
    fn nav(&self) -> &adw::NavigationPage {
        &self.nav
    }
    fn on_opened(self: Rc<Self>) {
        self.load();
        self.autodarts.refresh();
        if let Some(t) = self.thumbs.borrow().as_ref() {
            t.start();
        }
    }
    fn on_closed(self: Rc<Self>) {
        self.stop_live();
        if let Some(t) = self.thumbs.borrow().as_ref() {
            t.stop();
        }
    }
}

fn btn_content(icon: &str, label: &str) -> adw::ButtonContent {
    let c = adw::ButtonContent::new();
    c.set_icon_name(icon);
    c.set_label(&tr(label));
    c
}

impl CameraControlsPage {
    pub fn new(win: Win) -> Rc<dyn Page> {
        let outer = gtk::Box::new(gtk::Orientation::Vertical, 8);
        outer.set_margin_top(8);
        outer.set_margin_bottom(10);
        outer.set_margin_start(12);
        outer.set_margin_end(12);
        let nav = adw::NavigationPage::builder().title(tr("Camera Controls")).tag("camera_controls").child(&outer).build();
        let head = gtk::CenterBox::new();
        head.set_vexpand(false);
        head.set_valign(gtk::Align::Start);
        let strip = CameraStrip::new(win.clone());
        head.set_start_widget(Some(&strip.widget));
        let side = gtk::Box::new(gtk::Orientation::Vertical, 6);
        side.set_halign(gtk::Align::End);
        let navb = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        navb.set_halign(gtk::Align::End);
        let btn_presets = gtk::Button::new();
        btn_presets.set_child(Some(&btn_content("view-list-symbolic", "Presets")));
        btn_presets.set_tooltip_text(Some(&tr("Save or load the settings of all cameras at once")));
        navb.append(&btn_presets);
        let btn_opt = gtk::Button::new();
        btn_opt.set_child(Some(&btn_content("applications-science-symbolic", "Optimizer")));
        btn_opt.set_tooltip_text(Some(&tr("One-click calibration of all cameras")));
        let w = win.clone();
        btn_opt.connect_clicked(move |_| w.open_camera_optimizer_page());
        navb.append(&btn_opt);
        side.append(&navb);
        let autodarts = AutodartsStatus::new(win.clone());
        autodarts.widget.set_halign(gtk::Align::End);
        side.append(&autodarts.widget);
        head.set_end_widget(Some(&side));
        outer.append(&head);
        let cols = gtk::Box::new(gtk::Orientation::Horizontal, 12);
        cols.set_vexpand(true);
        outer.append(&cols);
        // left: live view
        let left = gtk::Box::new(gtk::Orientation::Vertical, 6);
        left.set_hexpand(true);
        let live_picture = gtk::Picture::new();
        live_picture.set_content_fit(gtk::ContentFit::Contain);
        live_picture.set_can_shrink(true);
        live_picture.set_vexpand(true);
        live_picture.set_size_request(280, 200);
        live_picture.add_css_class("camopt-frame");
        let ov = gtk::Overlay::new();
        ov.set_child(Some(&live_picture));
        ov.set_vexpand(true);
        let lbl_tag = gtk::Label::new(None);
        lbl_tag.set_halign(gtk::Align::Start);
        lbl_tag.set_valign(gtk::Align::Start);
        lbl_tag.add_css_class("camopt-tag");
        lbl_tag.set_margin_start(10);
        lbl_tag.set_margin_top(10);
        ov.add_overlay(&lbl_tag);
        let tools = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        tools.set_halign(gtk::Align::End);
        tools.set_valign(gtk::Align::Start);
        tools.set_margin_end(10);
        tools.set_margin_top(10);
        let pop_box = gtk::Box::new(gtk::Orientation::Vertical, 2);
        for m in [6] {
            pop_box.set_margin_top(m);
            pop_box.set_margin_bottom(m);
            pop_box.set_margin_start(m);
            pop_box.set_margin_end(m);
        }
        let mut overlay_checks = vec![];
        for (k, l, t) in [
            ("zebra", "Zebra: mark clipped areas", "Pure white areas are marked in red"),
            ("hist", "Histogram", "Brightness distribution of the picture"),
            ("mask", "Board mask", "Dim everything outside the board area used for the scores"),
        ] {
            let cb = gtk::CheckButton::with_label(&tr(l));
            cb.set_tooltip_text(Some(&tr(t)));
            pop_box.append(&cb);
            overlay_checks.push((k, cb));
        }
        let pop = gtk::Popover::new();
        pop.set_child(Some(&pop_box));
        let btn_ov = gtk::MenuButton::new();
        btn_ov.set_label(&tr("Overlays"));
        btn_ov.set_popover(Some(&pop));
        btn_ov.add_css_class("osd");
        btn_ov.add_css_class("compact-btn");
        btn_ov.set_tooltip_text(Some(&tr("Zebra, histogram and board mask on the live picture")));
        tools.append(&btn_ov);
        let btn_freeze = gtk::ToggleButton::new();
        btn_freeze.set_icon_name("media-playback-pause-symbolic");
        btn_freeze.set_tooltip_text(Some(&tr("Freeze the picture")));
        btn_freeze.add_css_class("osd");
        btn_freeze.add_css_class("compact-btn");
        tools.append(&btn_freeze);
        let btn_wide = gtk::ToggleButton::new();
        btn_wide.set_icon_name("view-fullscreen-symbolic");
        btn_wide.set_tooltip_text(Some(&tr("Hide the controls: bigger picture")));
        btn_wide.add_css_class("osd");
        btn_wide.add_css_class("compact-btn");
        tools.append(&btn_wide);
        ov.add_overlay(&tools);
        // the picture's own size must not decide the page height
        let lsw = gtk::ScrolledWindow::new();
        lsw.set_child(Some(&ov));
        lsw.set_vexpand(true);
        lsw.set_hscrollbar_policy(gtk::PolicyType::Never);
        lsw.set_vscrollbar_policy(gtk::PolicyType::Never);
        left.append(&lsw);
        let info = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        let chips = gtk::FlowBox::new();
        chips.set_selection_mode(gtk::SelectionMode::None);
        chips.set_hexpand(true);
        chips.set_max_children_per_line(8);
        chips.set_column_spacing(6);
        chips.set_row_spacing(4);
        chips.set_valign(gtk::Align::Center);
        let mkchip = || {
            let c = chip("", "neutral");
            c.set_visible(false);
            chips.append(&c);
            c
        };
        let (chip_white, chip_clip, chip_noise, chip_kb, chip_note) = (mkchip(), mkchip(), mkchip(), mkchip(), mkchip());
        info.append(&chips);
        let check_box = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        check_box.set_valign(gtk::Align::Center);
        let lbl_check = caption("", &[], false);
        check_box.append(&lbl_check);
        let btn_trim = gtk::Button::with_label(&tr("Fix"));
        btn_trim.set_visible(false);
        btn_trim.set_valign(gtk::Align::Center);
        btn_trim.add_css_class("compact-btn");
        btn_trim.set_tooltip_text(Some(&tr("Trim the digital gain back to the calibrated brightness")));
        check_box.append(&btn_trim);
        let btn_check = gtk::Button::with_label(&tr("Check brightness"));
        btn_check.set_valign(gtk::Align::Center);
        btn_check.set_tooltip_text(Some(&tr("Compare the brightness with the calibration")));
        btn_check.add_css_class("compact-btn");
        check_box.append(&btn_check);
        info.append(&check_box);
        left.append(&info);
        cols.append(&left);
        // right: panel
        let panel = gtk::Box::new(gtk::Orientation::Vertical, 0);
        panel.set_size_request(500, -1);
        panel.set_hexpand(false);
        panel.add_css_class("card");
        let ph = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        ph.set_margin_top(10);
        ph.set_margin_bottom(6);
        ph.set_margin_start(16);
        ph.set_margin_end(10);
        let lbl_cam = gtk::Label::new(None);
        lbl_cam.set_xalign(0.0);
        lbl_cam.set_ellipsize(pango::EllipsizeMode::End);
        lbl_cam.add_css_class("title-4");
        ph.append(&lbl_cam);
        let btn_name = gtk::Button::from_icon_name("document-edit-symbolic");
        btn_name.set_tooltip_text(Some(&tr("Name this camera")));
        btn_name.add_css_class("flat");
        btn_name.set_valign(gtk::Align::Center);
        ph.append(&btn_name);
        let spx = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        spx.set_hexpand(true);
        ph.append(&spx);
        let btn_copy = gtk::Button::with_label(&tr("Copy to all"));
        btn_copy.set_valign(gtk::Align::Center);
        btn_copy.add_css_class("compact-btn");
        btn_copy.set_tooltip_text(Some(&tr("Give the other cameras the same settings")));
        ph.append(&btn_copy);
        panel.append(&ph);
        let kb = gtk::Box::new(gtk::Orientation::Horizontal, 10);
        kb.set_margin_start(16);
        kb.set_margin_end(16);
        kb.set_margin_bottom(8);
        let sw_keep = gtk::Switch::new();
        sw_keep.set_valign(gtk::Align::Center);
        kb.append(&sw_keep);
        let kt = gtk::Box::new(gtk::Orientation::Vertical, 0);
        let kl = gtk::Label::new(Some(&tr("Keep these settings")));
        kl.set_xalign(0.0);
        kl.add_css_class("camctl-label");
        kt.append(&kl);
        let lbl_keep = caption("", &[], false);
        kt.append(&lbl_keep);
        kb.append(&kt);
        panel.append(&kb);
        panel.append(&gtk::Separator::new(gtk::Orientation::Horizontal));
        let ctl_scroll = gtk::ScrolledWindow::new();
        ctl_scroll.set_hscrollbar_policy(gtk::PolicyType::Never);
        ctl_scroll.set_vexpand(true);
        ctl_scroll.set_overlay_scrolling(false);
        ctl_scroll.set_kinetic_scrolling(true);
        let controls_box = gtk::Box::new(gtk::Orientation::Vertical, 0);
        ctl_scroll.set_child(Some(&controls_box));
        panel.append(&ctl_scroll);
        panel.append(&gtk::Separator::new(gtk::Orientation::Horizontal));
        let foot = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        foot.set_margin_top(8);
        foot.set_margin_bottom(8);
        foot.set_margin_start(16);
        foot.set_margin_end(10);
        let lbl_marks = caption("", &[], false);
        lbl_marks.set_hexpand(true);
        foot.append(&lbl_marks);
        let btn_undo = gtk::Button::with_label(&tr("Undo"));
        btn_undo.set_sensitive(false);
        btn_undo.add_css_class("compact-btn");
        btn_undo.set_tooltip_text(Some(&tr("Take back the last change")));
        foot.append(&btn_undo);
        let btn_reset = gtk::Button::with_label(&tr("Reset to defaults"));
        btn_reset.add_css_class("compact-btn");
        foot.append(&btn_reset);
        panel.append(&foot);
        cols.append(&panel);

        let me = Rc::new(CameraControlsPage {
            nav,
            win: win.clone(),
            cams: RefCell::new(vec![]),
            cur: Cell::new(0),
            building: Cell::new(false),
            pending: RefCell::new(HashMap::new()),
            origin: RefCell::new(HashMap::new()),
            undo: RefCell::new(vec![]),
            diffs: RefCell::new(HashMap::new()),
            sec_dots: RefCell::new(HashMap::new()),
            open: RefCell::new(["exposure".to_string()].into_iter().collect()),
            live_timer: RefCell::new(None),
            live_tick: Cell::new(0),
            scores_busy: Rc::new(Cell::new(false)),
            shown_seq: Cell::new(u64::MAX),
            painted: RefCell::new(None),
            frozen: Cell::new(false),
            ident: Cell::new(0),
            rate: Cell::new((0, 0, Instant::now(), 0.0)),
            overlays: RefCell::new([("zebra", false), ("hist", false), ("mask", false)].into_iter().collect()),
            masks: RefCell::new(HashMap::new()),
            strip,
            autodarts,
            thumbs: RefCell::new(None),
            live_picture,
            lbl_tag,
            chips,
            chip_white,
            chip_clip,
            chip_noise,
            chip_kb,
            chip_note,
            check_box,
            lbl_check,
            btn_trim,
            btn_check,
            panel,
            lbl_cam,
            btn_copy,
            sw_keep,
            lbl_keep,
            ctl_scroll,
            controls_box,
            lbl_marks,
            btn_undo,
            btn_reset,
        });
        let (m1, m2) = (Rc::downgrade(&me), Rc::downgrade(&me));
        me.strip.set_callbacks(
            Rc::new(move || {
                if let Some(m) = m1.upgrade() {
                    m.on_cam_pick();
                }
            }),
            Some(Rc::new(move || {
                if let Some(m) = m2.upgrade() {
                    let c = m.cur_cam();
                    m.lbl_cam.set_label(&c.map(|c| cw::cam_name(c["port"].as_str().unwrap_or(""))).unwrap_or_default());
                }
            })),
        );
        let m = Rc::downgrade(&me);
        let devs = Rc::new(move || {
            m.upgrade()
                .map(|m| m.cams.borrow().iter().map(|c| (c["port"].as_str().unwrap_or("").to_string(), c["device"].as_str().unwrap_or("").to_string())).collect())
                .unwrap_or_default()
        });
        let m = Rc::downgrade(&me);
        let skip = Rc::new(move || m.upgrade().and_then(|m| m.cur_cam().and_then(|c| c["port"].as_str().map(String::from))));
        *me.thumbs.borrow_mut() = Some(ThumbPoller::new(me.strip.clone(), devs, skip));
        let m = me.clone();
        btn_presets.connect_clicked(move |_| m.show_presets());
        for (k, cb) in overlay_checks {
            let m = me.clone();
            cb.connect_toggled(move |b| m.set_overlay(k, b.is_active()));
        }
        let m = me.clone();
        btn_freeze.connect_toggled(move |b| {
            m.frozen.set(b.is_active());
            m.shown_seq.set(u64::MAX);
        });
        let m = me.clone();
        btn_wide.connect_toggled(move |b| m.panel.set_visible(!b.is_active()));
        let m = me.clone();
        me.btn_trim.connect_clicked(move |_| m.recheck(true));
        let m = me.clone();
        me.btn_check.connect_clicked(move |_| m.recheck(false));
        let m = me.clone();
        btn_name.connect_clicked(move |_| {
            if let Some(c) = m.cur_cam() {
                let s = m.strip.clone();
                ask_rename(&m.win, c["port"].as_str().unwrap_or(""), Rc::new(move || s.refresh_names()));
            }
        });
        let m = me.clone();
        me.btn_copy.connect_clicked(move |_| m.ask_copy_to_all());
        let m = me.clone();
        me.sw_keep.connect_active_notify(move |s| m.on_keep(s.is_active()));
        let m = me.clone();
        me.btn_undo.connect_clicked(move |_| m.undo_last());
        let m = me.clone();
        me.btn_reset.connect_clicked(move |_| m.reset_cam());
        me
    }

    fn cur_cam(&self) -> Option<Value> {
        self.cams.borrow().get(self.cur.get()).cloned()
    }

    fn cur_dev(&self) -> Option<String> {
        self.cur_cam().and_then(|c| c["device"].as_str().map(String::from))
    }

    fn load(self: &Rc<Self>) {
        let m = self.clone();
        run_async(
            || cameras().iter().map(|d| live::camera_info(d)).collect::<Vec<Value>>(),
            move |cams| {
                let prev = m.cur_dev();
                let entries: Vec<StripEntry> = cams
                    .iter()
                    .map(|c| StripEntry {
                        port: c["port"].as_str().unwrap_or("").into(),
                        kind: (if c.get("error").is_some() { "bad" } else if c["profile"].is_object() { "ok" } else { "neutral" }).into(),
                    })
                    .collect();
                *m.cams.borrow_mut() = cams;
                m.strip.sync(&entries);
                let active = m.strip.active();
                let idx = m.cams.borrow().iter().position(|c| c["port"].as_str() == active.as_deref()).unwrap_or(0);
                m.cur.set(idx);
                if m.cur_dev() != prev {
                    // another camera: its first picture shows even when frozen
                    m.shown_seq.set(u64::MAX);
                    *m.painted.borrow_mut() = None;
                }
                m.render_controls();
                m.start_live();
                if let Some(t) = m.thumbs.borrow().as_ref() {
                    t.kick();
                }
            },
        );
    }

    fn later(self: &Rc<Self>, ms: u64) {
        let m = self.clone();
        super::helpers::once(ms, move || m.load());
    }

    fn on_cam_pick(self: &Rc<Self>) {
        self.flush_pending(); // slider moves still waiting belong to the camera shown so far
        self.lbl_check.set_label("");
        self.btn_trim.set_visible(false);
        self.live_picture.set_paintable(None::<&gtk::gdk::Paintable>);
        self.shown_seq.set(u64::MAX);
        *self.painted.borrow_mut() = None;
        self.undo.borrow_mut().clear();
        self.origin.borrow_mut().clear();
        self.sync_undo();
        self.load();
    }

    // --- controls ---

    fn render_controls(self: &Rc<Self>) {
        self.building.set(true);
        let vadj = self.ctl_scroll.vadjustment();
        let scroll = vadj.value();
        super::helpers::clear_box(&self.controls_box);
        self.diffs.borrow_mut().clear();
        self.sec_dots.borrow_mut().clear();
        let Some(c) = self.cur_cam() else {
            let l = gtk::Label::new(Some(&tr("No cameras connected")));
            l.add_css_class("camopt-empty");
            l.set_vexpand(true);
            self.controls_box.append(&l);
            self.btn_reset.set_visible(false);
            self.check_box.set_visible(false);
            self.lbl_marks.set_visible(false);
            self.btn_copy.set_visible(false);
            self.sw_keep.set_sensitive(false);
            self.lbl_keep.set_label("");
            self.lbl_cam.set_label("");
            self.building.set(false);
            return;
        };
        let port = c["port"].as_str().unwrap_or("").to_string();
        self.lbl_cam.set_label(&cw::cam_name(&port));
        let kept = c["profile"].is_object();
        let optimized = c["optimized"].as_bool().unwrap_or(false);
        let err = c.get("error").and_then(|e| e.as_str()).map(String::from);
        self.sw_keep.set_sensitive(err.is_none());
        self.sw_keep.set_active(kept);
        self.lbl_keep.set_label(&tr(if optimized {
            "Optimized · re-applied after restarts"
        } else if kept {
            "Re-applied after Autodarts restarts"
        } else {
            "Off · Autodarts resets changes on restart"
        }));
        self.lbl_keep.remove_css_class("camopt-warn");
        if !kept {
            self.lbl_keep.add_css_class("camopt-warn");
        }
        let has_cal = c["calibrated"].is_object();
        self.btn_reset.set_label(&tr(if has_cal { "Reset to calibrated" } else { "Reset to defaults" }));
        self.btn_reset.set_visible(err.is_none());
        self.btn_copy.set_visible(self.cams.borrow().len() > 1 && err.is_none());
        self.lbl_marks.set_visible(true);
        self.check_box.set_visible(optimized);
        if let Some(e) = err {
            self.controls_box.append(&caption(&trf("Camera not reachable: {error}", &[("error", &e)]), &[], true));
            self.lbl_marks.set_label("");
            self.building.set(false);
            return;
        }
        let mut vals: HashMap<String, i64> = c["controls"]
            .as_object()
            .map(|o| o.iter().filter_map(|(k, v)| v["value"].as_i64().map(|x| (k.clone(), x))).collect())
            .unwrap_or_default();
        if let Some(q) = c["q"].as_i64() {
            vals.insert("q".into(), q);
        }
        if let Some(g) = c["sensor_gain"].as_i64() {
            vals.insert("sensor_gain".into(), g);
        }
        for (g, title) in GROUPS {
            let names: Vec<&str> = SPEC.iter().filter(|s| s.group == g && vals.contains_key(s.name)).map(|s| s.name).collect();
            let hint = if g == "stream" { self.hidden_hint(&c) } else { None };
            if names.is_empty() && hint.is_none() {
                continue;
            }
            let sec = self.section(g, &tr(title), &names, hint, &c, &vals);
            self.controls_box.append(&sec);
        }
        self.building.set(false);
        self.update_marks();
        glib::idle_add_local_once(move || vadj.set_value(scroll));
    }

    fn summary(g: &str, c: &Value, vals: &HashMap<String, i64>) -> String {
        match g {
            "exposure" => {
                if vals.get("auto_exposure") != Some(&1) {
                    return tr("Automatic");
                }
                match vals.get("exposure_time_absolute") {
                    Some(t) => format!("{} · {}", tr("Manual"), exposure_text(c, *t)),
                    None => tr("Manual"),
                }
            }
            "color" => {
                if vals.get("white_balance_automatic") == Some(&1) || !vals.contains_key("white_balance_temperature") {
                    return tr("Auto white balance");
                }
                format!("{} · {} K", tr("Manual"), vals["white_balance_temperature"])
            }
            "stream" => {
                let mut p = vec![];
                if let Some(q) = vals.get("q") {
                    p.push(format!("JPEG {}", q));
                }
                if let Some(pl) = vals.get("power_line_frequency").and_then(|v| spec("power_line_frequency").unwrap().options.iter().find(|(k, _)| k == v)) {
                    p.push(tr(pl.1));
                }
                p.join(" · ")
            }
            _ => String::new(),
        }
    }

    fn section(self: &Rc<Self>, g: &str, title: &str, names: &[&str], hint: Option<gtk::Box>, c: &Value, vals: &HashMap<String, i64>) -> gtk::Box {
        let sec = gtk::Box::new(gtk::Orientation::Vertical, 0);
        sec.add_css_class("camctl-sec");
        let head = gtk::Button::new();
        head.add_css_class("flat");
        head.add_css_class("camctl-sechead");
        let hb = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        let t = gtk::Label::new(Some(title));
        t.set_xalign(0.0);
        t.add_css_class("heading");
        hb.append(&t);
        let dot = gtk::Label::new(Some("●"));
        dot.set_visible(false);
        dot.add_css_class("camopt-dot");
        dot.add_css_class("camopt-warn");
        dot.set_tooltip_text(Some(&tr("Differs from the calibration")));
        self.sec_dots.borrow_mut().insert(g.into(), dot.clone());
        hb.append(&dot);
        let sl = gtk::Label::new(Some(&Self::summary(g, c, vals)));
        sl.set_xalign(1.0);
        sl.set_hexpand(true);
        sl.set_ellipsize(pango::EllipsizeMode::End);
        sl.add_css_class("camctl-sec-sum");
        hb.append(&sl);
        let is_open = self.open.borrow().contains(g);
        let chev = gtk::Image::from_icon_name(if is_open { "pan-down-symbolic" } else { "pan-end-symbolic" });
        hb.append(&chev);
        head.set_child(Some(&hb));
        sec.append(&head);
        let rev = gtk::Revealer::new();
        rev.set_reveal_child(is_open);
        rev.set_transition_type(gtk::RevealerTransitionType::SlideDown);
        let body = gtk::Box::new(gtk::Orientation::Vertical, 0);
        body.add_css_class("camctl-grid");
        body.set_margin_bottom(6);
        for n in names {
            self.control_row(&body, c, n, vals);
        }
        if let Some(h) = hint {
            h.set_margin_start(16);
            h.set_margin_end(16);
            body.append(&h);
        }
        rev.set_child(Some(&body));
        sec.append(&rev);
        let (m, g) = (self.clone(), g.to_string());
        head.connect_clicked(move |_| {
            let o = !rev.reveals_child();
            rev.set_reveal_child(o);
            chev.set_icon_name(Some(if o { "pan-down-symbolic" } else { "pan-end-symbolic" }));
            if o {
                m.open.borrow_mut().insert(g.clone());
            } else {
                m.open.borrow_mut().remove(&g);
            }
        });
        sec
    }

    /// HBV camera without its hidden controls: say why, and how to get them.
    fn hidden_hint(&self, c: &Value) -> Option<gtk::Box> {
        if !is_hbv(c) || c["hidden"].as_bool().unwrap_or(false) {
            return None;
        }
        let b = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        if let Some(e) = c["hidden_error"].as_str() {
            b.append(&caption(&trf("JPEG quality and sensor gain not available: {error}", &[("error", &e)]), &[], true));
            return Some(b);
        }
        let l = caption("JPEG quality and sensor gain: confirm HBV cameras first", &[], true);
        l.set_hexpand(true);
        b.append(&l);
        let btn = gtk::Button::with_label(&tr("Optimizer"));
        btn.set_valign(gtk::Align::Center);
        btn.add_css_class("compact-btn");
        let w = self.win.clone();
        btn.connect_clicked(move |_| w.open_camera_optimizer_page());
        b.append(&btn);
        Some(b)
    }

    fn mark(&self, n: &str, d: bool) {
        self.diffs.borrow_mut().insert(n.into(), d);
        self.update_marks();
    }

    /// Section dots and the footer count: how many controls differ from the calibration.
    fn update_marks(&self) {
        let has_cal = self.cur_cam().map(|c| c["calibrated"].is_object()).unwrap_or(false);
        if !has_cal {
            self.lbl_marks.set_label("");
            for d in self.sec_dots.borrow().values() {
                d.set_visible(false);
            }
            return;
        }
        let diffs = self.diffs.borrow();
        for (g, dot) in self.sec_dots.borrow().iter() {
            dot.set_visible(diffs.iter().any(|(n, v)| *v && spec(n).map(|s| s.group == g).unwrap_or(false)));
        }
        let n = diffs.values().filter(|v| **v).count();
        if n > 0 {
            self.lbl_marks.set_label(&format!("↺ {}", if n == 1 { tr("1 differs from the calibration") } else { trf("{n} differ from the calibration", &[("n", &n)]) }));
        } else {
            self.lbl_marks.set_label(&tr("Matches the calibration"));
        }
    }

    /// One touch-sized line: label | control | value, - / + and revert (ranges). Disabled rows say why.
    fn control_row(self: &Rc<Self>, body: &gtk::Box, c: &Value, n: &str, vals: &HashMap<String, i64>) {
        let sp = spec(n).unwrap();
        let v = vals[n];
        let cal = calibrated_value(c, n);
        let sensitive = !disabled(n, vals);
        let row = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        row.add_css_class("camctl-row");
        let label = tr(sp.label);
        let lbl = gtk::Label::new(Some(&label));
        lbl.set_xalign(0.0);
        lbl.add_css_class("camctl-label");
        lbl.set_sensitive(sensitive);
        lbl.set_ellipsize(pango::EllipsizeMode::End);
        lbl.set_size_request(104, -1);
        if n == "backlight_compensation" {
            lbl.set_tooltip_text(Some(&tr("Backlight compensation (steers automatic exposure)")));
        }
        row.append(&lbl);
        body.append(&row);
        self.diffs.borrow_mut().insert(n.into(), cal.is_some() && cal != Some(v));
        let revert = gtk::Button::from_icon_name("edit-undo-symbolic");
        revert.add_css_class("cam-revert");
        revert.set_valign(gtk::Align::Center);
        revert.set_tooltip_text(Some(&tr("Back to the calibrated value")));
        let set_differs = {
            let (m, rv, n) = (Rc::downgrade(self), revert.clone(), n.to_string());
            Rc::new(move |value: i64| {
                let d = cal.is_some() && cal != Some(value);
                rv.set_opacity(if d { 1.0 } else { 0.0 });
                rv.set_sensitive(d);
                if let Some(m) = m.upgrade() {
                    m.mark(&n, d);
                }
            })
        };
        if !sensitive {
            if let Some(r) = off_reason(n) {
                let h = gtk::Label::new(Some(&tr(r)));
                h.set_xalign(0.0);
                h.set_wrap(true);
                h.add_css_class("camctl-hint");
                body.append(&h);
            }
        }
        if matches!(sp.kind, Kind::Select | Kind::Sensor | Kind::Switch) {
            let st = Rc::new(Cell::new(v));
            let (layer, cname) = if sp.kind == Kind::Sensor { ("sensor", "350B".to_string()) } else { ("uvc", n.to_string()) };
            let commit: Rc<dyn Fn(i64)> = {
                let (m, st, sd) = (Rc::downgrade(self), st.clone(), set_differs.clone());
                Rc::new(move |new: i64| {
                    let Some(m) = m.upgrade() else { return };
                    if m.building.get() || new == st.get() {
                        return;
                    }
                    let old = st.replace(new);
                    m.edit(layer, &cname, old, new, true);
                    sd(new);
                })
            };
            let set_ctl: Rc<dyn Fn(i64)>;
            if sp.kind == Kind::Switch {
                let w = gtk::Switch::new();
                w.set_active(v != 0);
                w.set_valign(gtk::Align::Center);
                w.set_sensitive(sensitive);
                let cm = commit.clone();
                w.connect_active_notify(move |s| cm(if s.is_active() { 1 } else { 0 }));
                let spc = gtk::Box::new(gtk::Orientation::Horizontal, 0);
                spc.set_hexpand(true);
                row.append(&spc);
                row.append(&w);
                set_ctl = Rc::new(move |x| w.set_active(x != 0));
            } else {
                let (keys, texts): (Vec<i64>, Vec<String>) = if sp.kind == Kind::Sensor {
                    let mut steps = SENSOR_STEPS.to_vec();
                    if !steps.contains(&v) {
                        steps.push(v);
                        steps.sort_by(|a, b| sensor_gain(*a).partial_cmp(&sensor_gain(*b)).unwrap());
                    }
                    let t = steps.iter().map(|k| gain_label(*k)).collect();
                    (steps, t)
                } else {
                    let mut k: Vec<i64> = sp.options.iter().map(|(k, _)| *k).collect();
                    let mut t: Vec<String> = sp.options.iter().map(|(_, t)| tr(t)).collect();
                    if !k.contains(&v) {
                        k.push(v);
                        t.push(v.to_string());
                    }
                    (k, t)
                };
                if keys.len() <= SEGMENT_MAX {
                    let o: Vec<(String, String)> = keys.iter().zip(&texts).map(|(k, t)| (k.to_string(), t.clone())).collect();
                    let cm = commit.clone();
                    let w = segmented(&o, &v.to_string(), Rc::new(move |s: String| cm(s.parse().unwrap_or(0))));
                    w.set_sensitive(sensitive);
                    row.append(&w);
                    set_ctl = Rc::new(move |x| w.set_active_name(Some(&x.to_string())));
                } else {
                    let tr_: Vec<&str> = texts.iter().map(|s| s.as_str()).collect();
                    let w = gtk::DropDown::from_strings(&tr_);
                    w.set_selected(keys.iter().position(|k| *k == v).unwrap_or(0) as u32);
                    w.set_sensitive(sensitive);
                    w.set_valign(gtk::Align::Center);
                    let (cm, k2) = (commit.clone(), keys.clone());
                    w.connect_selected_notify(move |d| {
                        if let Some(k) = k2.get(d.selected() as usize) {
                            cm(*k);
                        }
                    });
                    row.append(&w);
                    set_ctl = Rc::new(move |x| {
                        if let Some(i) = keys.iter().position(|k| *k == x) {
                            w.set_selected(i as u32);
                        }
                    });
                }
                let spc = gtk::Box::new(gtk::Orientation::Horizontal, 0);
                spc.set_hexpand(true);
                row.append(&spc);
            }
            row.append(&revert);
            revert.connect_clicked(move |_| {
                if let Some(cv) = cal {
                    set_ctl(cv);
                }
            });
            set_differs(v);
            return;
        }
        // range
        let (lo, hi, step): (f64, f64, f64);
        let fmt: Rc<dyn Fn(i64) -> String>;
        if sp.kind == Kind::Q {
            lo = 1.0;
            hi = 30.0;
            step = 1.0;
            fmt = Rc::new(|x| x.to_string());
            lbl.set_tooltip_text(Some(&tr("Lower is sharper and uses more USB bandwidth. Applies when the stream restarts.")));
        } else {
            let ctl = &c["controls"][n];
            lo = ctl["min"].as_f64().unwrap_or(0.0);
            hi = ctl["max"].as_f64().unwrap_or(100.0);
            step = ctl["step"].as_f64().filter(|s| *s > 0.0).unwrap_or(1.0);
            if n == "exposure_time_absolute" {
                let c2 = c.clone();
                fmt = Rc::new(move |x| exposure_text(&c2, x));
            } else if n == "white_balance_temperature" {
                fmt = Rc::new(|x| format!("{} K", x));
            } else {
                fmt = Rc::new(|x| x.to_string());
            }
        }
        let layer = if sp.kind == Kind::Q { "q" } else { "uvc" };
        let scale = gtk::Scale::with_range(gtk::Orientation::Horizontal, lo, hi, step);
        scale.set_value(v as f64);
        scale.set_draw_value(false);
        scale.set_hexpand(true);
        scale.set_size_request(110, -1);
        scale.set_valign(gtk::Align::Center);
        scale.set_sensitive(sensitive);
        let val_btn = gtk::Button::with_label(&fmt(v));
        val_btn.add_css_class("flat");
        val_btn.add_css_class("camctl-value-btn");
        val_btn.add_css_class("numeric");
        val_btn.set_sensitive(sensitive);
        val_btn.set_tooltip_text(Some(&tr("Tap to type a value")));
        val_btn.set_size_request(70, 34);
        let minus = gtk::Button::with_label("−");
        let plus = gtk::Button::with_label("+");
        for b in [&minus, &plus] {
            b.add_css_class("cam-step");
            b.set_sensitive(sensitive);
            b.set_valign(gtk::Align::Center);
        }
        let st = Rc::new(Cell::new(v));
        {
            let (m, vb, f, sd, st, n) = (Rc::downgrade(self), val_btn.clone(), fmt.clone(), set_differs.clone(), st.clone(), n.to_string());
            scale.connect_value_changed(move |s| {
                let value = s.value().round() as i64;
                vb.set_label(&f(value));
                let Some(m) = m.upgrade() else { return };
                if m.building.get() {
                    return;
                }
                sd(value);
                m.origin.borrow_mut().entry(n.clone()).or_insert(st.get());
                if let Some((id, _)) = m.pending.borrow_mut().remove(&n) {
                    id.remove();
                }
                let args: PendingArgs = (layer.into(), n.clone(), value, st.clone());
                let (m2, a2) = (m.clone(), args.clone());
                let id = super::helpers::once(350, move || m2.flush(a2));
                m.pending.borrow_mut().insert(n.clone(), (id, args));
            });
        }
        let s2 = scale.clone();
        minus.connect_clicked(move |_| s2.set_value((s2.value() - step).max(lo)));
        let s2 = scale.clone();
        plus.connect_clicked(move |_| s2.set_value((s2.value() + step).min(hi)));
        let s2 = scale.clone();
        revert.connect_clicked(move |_| {
            if let Some(cv) = cal {
                s2.set_value(cv as f64);
            }
        });
        let (s2, l2) = (scale.clone(), label.clone());
        val_btn.connect_clicked(move |b| type_value(b, &l2, &s2, lo, hi, step));
        row.append(&scale);
        row.append(&val_btn);
        row.append(&minus);
        row.append(&plus);
        row.append(&revert);
        set_differs(v);
        if sp.kind == Kind::Q {
            if let Some(qa) = c["q_active"].as_i64().filter(|qa| *qa != v) {
                let h = gtk::Box::new(gtk::Orientation::Horizontal, 6);
                h.set_margin_start(16);
                h.set_margin_end(16);
                let t = caption(&trf("Running stream still uses {q}", &[("q", &qa)]), &["camopt-warn"], false);
                t.set_hexpand(true);
                h.append(&t);
                let b = gtk::Button::with_label(&tr("Restart Autodarts"));
                b.set_valign(gtk::Align::Center);
                b.add_css_class("compact-btn");
                let m = self.clone();
                b.connect_clicked(move |_| m.ask_restart_autodarts());
                h.append(&b);
                body.append(&h);
            }
        }
    }

    fn flush_pending(self: &Rc<Self>) {
        let all: Vec<(glib::SourceId, PendingArgs)> = self.pending.borrow_mut().drain().map(|(_, v)| v).collect();
        for (id, a) in all {
            id.remove();
            self.flush(a);
        }
    }

    fn flush(self: &Rc<Self>, (layer, n, value, st): PendingArgs) {
        self.pending.borrow_mut().remove(&n);
        let old = self.origin.borrow_mut().remove(&n).unwrap_or(st.get());
        st.set(value);
        // a JPEG level change adds/removes the restart hint; other sliders stay put (no rebuild under the finger)
        self.edit(&layer, &n, old, value, layer == "q");
    }

    /// A change made by the user: remember it for Undo, then write it to the camera.
    fn edit(self: &Rc<Self>, layer: &str, name: &str, old: i64, new: i64, reload: bool) {
        if let Some(dev) = self.cur_dev() {
            if old != new {
                let mut u = self.undo.borrow_mut();
                u.push((dev, layer.into(), name.into(), old));
                let l = u.len();
                if l > UNDO_MAX {
                    u.drain(..l - UNDO_MAX);
                }
            }
            self.sync_undo();
        }
        self.set(layer, name, new, reload);
    }

    fn sync_undo(&self) {
        self.btn_undo.set_sensitive(!self.undo.borrow().is_empty());
    }

    fn undo_last(self: &Rc<Self>) {
        let Some((dev, layer, name, old)) = self.undo.borrow_mut().pop() else { return };
        self.sync_undo();
        if Some(&dev) != self.cur_dev().as_ref() {
            return;
        }
        let shown = match name.as_str() {
            "q" => "JPEG compression",
            "350B" => "Sensor gain",
            n => spec(n).map(|s| s.label).unwrap_or("Setting"),
        };
        self.win.show_toast(&trf("Undone: {setting}", &[("setting", &tr(shown))]));
        self.set(&layer, &name, old, true);
    }

    fn set(self: &Rc<Self>, layer: &str, name: &str, value: i64, reload: bool) {
        if self.building.get() {
            return;
        }
        let Some(dev) = self.cur_dev() else { return };
        let (m, l, n) = (self.clone(), layer.to_string(), name.to_string());
        run_async(move || live::set_control(&dev, &l, &n, value).map_err(|e| e.to_string()), move |r| match r {
            Ok(r) => {
                if let Some(note) = r["note"].as_str() {
                    m.win.show_toast(note);
                }
                if reload {
                    m.load(); // dependent rows (manual exposure, WB temperature) and marks
                }
            }
            Err(e) => {
                m.win.show_toast(&e);
                m.load();
            }
        });
    }

    fn fail(self: &Rc<Self>, e: &str) {
        self.win.show_toast(e);
        self.load();
    }

    fn ask_copy_to_all(self: &Rc<Self>) {
        let Some(c) = self.cur_cam() else { return };
        let others: Vec<Value> = self.cams.borrow().iter().filter(|o| o["port"] != c["port"] && o.get("error").is_none()).cloned().collect();
        if others.is_empty() {
            self.win.show_toast("No other camera to copy to");
            return;
        }
        let who = others.iter().map(|o| cw::cam_name(o["port"].as_str().unwrap_or(""))).collect::<Vec<_>>().join(", ");
        let d = alert(
            &trf("Copy {camera} to {others}?", &[("camera", &cw::cam_name(c["port"].as_str().unwrap_or(""))), ("others", &who)]),
            &tr("Exposure, gain, tone, color, detail and stream settings of this camera are written to the other cameras and kept there. Cameras with their own light or angle may need different values."),
            &[("cancel", &tr("Cancel")), ("copy", &tr("Copy"))],
            Some("copy"),
            None,
        );
        let m = self.clone();
        ask(&self.win.window, d, move |r| {
            if r != "copy" {
                return;
            }
            let (c, others, who) = (c.clone(), others.clone(), who.clone());
            let m2 = m.clone();
            run_async(
                move || {
                    let mut skipped = 0;
                    let ctrls = c["controls"].as_object().cloned().unwrap_or_default();
                    let mut order: Vec<String> = APPLY_FIRST.iter().filter(|n| ctrls.contains_key(**n)).map(|s| s.to_string()).collect();
                    order.extend(ctrls.keys().filter(|k| !APPLY_FIRST.contains(&k.as_str())).cloned());
                    for o in &others {
                        let dev = o["device"].as_str().unwrap_or("");
                        for n in &order {
                            let want = ctrls[n]["value"].as_i64().unwrap_or(0);
                            if o["controls"].get(n).is_none() || o["controls"][n]["value"].as_i64() == Some(want) {
                                continue;
                            }
                            if live::set_control(dev, "uvc", n, want).is_err() {
                                skipped += 1; // e.g. temperature while automatic white balance is on
                            }
                        }
                        if c["hidden"].as_bool().unwrap_or(false) && o["hidden"].as_bool().unwrap_or(false) {
                            for (layer, name, key) in [("q", "q", "q"), ("sensor", "350B", "sensor_gain")] {
                                if let Some(want) = c[key].as_i64() {
                                    if o[key].as_i64() != Some(want) && live::set_control(dev, layer, name, want).is_err() {
                                        skipped += 1;
                                    }
                                }
                            }
                        }
                    }
                    skipped
                },
                move |skipped| {
                    let mut t = trf("Copied to {others}", &[("others", &who)]);
                    if skipped > 0 {
                        t += &format!(" {}", trf("({n} setting(s) refused)", &[("n", &skipped)]));
                    }
                    m2.win.show_toast(&t);
                    m2.load();
                },
            );
        });
    }

    fn on_keep(self: &Rc<Self>, on: bool) {
        if self.building.get() {
            return;
        }
        let Some(c) = self.cur_cam() else { return };
        if on == c["profile"].is_object() {
            return;
        }
        let dev = c["device"].as_str().unwrap_or("").to_string();
        if on {
            let m = self.clone();
            run_async(move || live::keep(&dev), move |_| {
                m.win.show_toast("Settings kept for this camera");
                m.load();
            });
            return;
        }
        if !c["optimized"].as_bool().unwrap_or(false) {
            self.forget(dev);
            return;
        }
        let d = alert(
            &tr("Stop Keeping the Optimized Settings?"),
            &tr("This camera goes back to its stock JPEG quality and the settings from before the optimization. Autodarts writes its own values when it restarts."),
            &[("cancel", &tr("Cancel")), ("stop", &tr("Stop Keeping"))],
            None,
            Some("stop"),
        );
        let m = self.clone();
        ask(&self.win.window, d, move |r| {
            if r == "stop" {
                m.forget(dev.clone());
            } else {
                m.load(); // puts the switch back
            }
        });
    }

    fn forget(self: &Rc<Self>, dev: String) {
        let m = self.clone();
        run_async(move || live::forget(&dev).map_err(|e| e.to_string()), move |r| match r {
            Ok(r) => {
                m.win.show_toast("Settings no longer kept for this camera");
                if r["needs_restart"].as_bool().unwrap_or(false) {
                    m.ask_restart_autodarts();
                }
                m.load();
            }
            Err(e) => m.fail(&e),
        });
    }

    fn reset_cam(self: &Rc<Self>) {
        let Some(c) = self.cur_cam() else { return };
        let dev = c["device"].as_str().unwrap_or("").to_string();
        let m = self.clone();
        if c["calibrated"].is_object() {
            run_async(move || live::reset_profile(&dev).map_err(|e| e.to_string()), move |r| match r {
                Ok(r) => {
                    m.win.show_toast(r["note"].as_str().unwrap_or("Back to the calibrated values"));
                    if r["needs_restart"].as_bool().unwrap_or(false) {
                        m.ask_restart_autodarts();
                    }
                    m.later(2500); // the keeper applies within 2 s
                }
                Err(e) => m.fail(&e),
            });
        } else {
            run_async(move || live::reset_defaults(&dev).map_err(|e| e.to_string()), move |r| match r {
                Ok(r) => {
                    let refused: Vec<String> = r["refused"].as_array().map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect()).unwrap_or_default();
                    let mut t = tr("Defaults restored");
                    if !refused.is_empty() {
                        t += &format!(" {}", trf("(refused: {names})", &[("names", &refused.join(", "))]));
                    }
                    m.win.show_toast(&t);
                    m.load();
                }
                Err(e) => m.fail(&e),
            });
        }
    }

    fn ask_restart_autodarts(self: &Rc<Self>) {
        let d = alert(
            &tr("Restart Autodarts?"),
            &tr("The new JPEG level takes effect when the camera stream restarts. Autodarts is back in about 10 seconds."),
            &[("cancel", &tr("Cancel")), ("restart", &tr("Restart"))],
            Some("restart"),
            None,
        );
        let m = self.clone();
        ask(&self.win.window, d, move |r| {
            if r != "restart" {
                return;
            }
            m.win.show_toast("Restarting Autodarts…");
            let m2 = m.clone();
            run_async(|| oneclick::systemctl("restart", "autodarts"), move |_| {
                m2.later(8000);
                m2.autodarts.refresh();
            });
        });
    }

    fn recheck(self: &Rc<Self>, trim: bool) {
        let Some(dev) = self.cur_dev() else { return };
        self.btn_check.set_sensitive(false);
        self.btn_trim.set_visible(false);
        self.lbl_check.set_label(&tr(if trim { "Fixing…" } else { "Checking…" }));
        let m = self.clone();
        run_async(move || live::recheck(&dev, trim).map_err(|e| e.to_string()), move |r| {
            m.btn_check.set_sensitive(true);
            let r = match r {
                Ok(r) => r,
                Err(e) => {
                    m.lbl_check.set_label(&e);
                    return;
                }
            };
            if r.get("error").is_some() {
                m.lbl_check.set_label(&tr("No picture yet, try again"));
                return;
            }
            let pct = r["deviation_pct"].as_f64().unwrap_or(0.0);
            let ap = format!("{:.0}", pct.abs());
            if let Some(g) = r["gain"].as_array() {
                m.lbl_check.set_label(&format!("{} {} → {}", tr("Gain"), g[0], g[1]));
                m.load();
            } else if r["verdict"].as_str() == Some("ok") {
                m.lbl_check.set_label(&trf("Brightness OK ({pct:+.0f} %)", &[("pct", &format!("{:+.0}", pct))]));
            } else if r["verdict"].as_str() == Some("trim gain") {
                m.lbl_check.set_label(&trf(if pct > 0.0 { "{pct:.0f} % brighter than calibrated" } else { "{pct:.0f} % darker than calibrated" }, &[("pct", &ap)]));
                m.btn_trim.set_visible(true);
            } else {
                m.lbl_check.set_label(&trf(
                    if pct > 0.0 { "{pct:.0f} % brighter: light changed, optimize again" } else { "{pct:.0f} % darker: light changed, optimize again" },
                    &[("pct", &ap)],
                ));
            }
        });
    }

    // --- presets ---

    fn show_presets(self: &Rc<Self>) {
        let dialog = adw::Dialog::new();
        dialog.set_title(&tr("Presets"));
        dialog.set_content_width(480);
        dialog.set_content_height(520);
        let tv = adw::ToolbarView::new();
        tv.add_top_bar(&adw::HeaderBar::new());
        let page = adw::PreferencesPage::new();
        tv.set_content(Some(&page));
        dialog.set_child(Some(&tv));
        let gn = adw::PreferencesGroup::new();
        gn.set_description(Some(&tr("A preset stores the settings of every camera. Loading it applies them and keeps them.")));
        let btn_save = gtk::Button::with_label(&tr("Save Current Settings as Preset…"));
        btn_save.set_halign(gtk::Align::Start);
        btn_save.add_css_class("pill");
        gn.add(&btn_save);
        page.add(&gn);
        let grp = adw::PreferencesGroup::new();
        grp.set_title(&tr("Presets"));
        page.add(&grp);
        let rows: Rc<RefCell<Vec<gtk::Widget>>> = Rc::new(RefCell::new(vec![]));
        let refresh: Rc<RefCell<Option<Rc<dyn Fn()>>>> = Rc::new(RefCell::new(None));
        let do_load: Rc<dyn Fn(String)> = {
            let (m, dialog) = (self.clone(), dialog.clone());
            Rc::new(move |name: String| {
                let (m2, d2, n2) = (m.clone(), dialog.clone(), name.clone());
                run_async(move || live::load_preset(&name).map_err(|e| e.to_string()), move |r| match r {
                    Ok(r) => {
                        d2.close();
                        m2.win.show_toast(&trf("Preset “{name}” loaded on {n} camera(s)", &[("name", &tr(&n2)), ("n", &r["cameras"])]));
                        if r["needs_restart"].as_bool().unwrap_or(false) {
                            m2.ask_restart_autodarts();
                        }
                        m2.load();
                    }
                    Err(e) => m2.win.show_toast(&e),
                });
            })
        };
        let r_fn: Rc<dyn Fn()> = {
            let (m, grp, rows, refresh_cell, do_load) = (self.clone(), grp.clone(), rows.clone(), refresh.clone(), do_load.clone());
            Rc::new(move || {
                super::helpers::clear_rows(&grp, &mut rows.borrow_mut());
                let r = super::helpers::action_row(&tr(live::DEFAULT_PRESET), &tr("Every camera on its own defaults. Nothing is kept or optimized."));
                let load = gtk::Button::with_label(&tr("Load"));
                load.set_valign(gtk::Align::Center);
                let (m2, dl) = (m.clone(), do_load.clone());
                load.connect_clicked(move |_| {
                    let d = alert(
                        &tr("Reset All Cameras?"),
                        &tr("Every camera goes back to its own default settings. Kept and optimized settings are removed; Autodarts writes its own values when it restarts."),
                        &[("cancel", &tr("Cancel")), ("reset", &tr("Reset"))],
                        None,
                        Some("reset"),
                    );
                    let dl = dl.clone();
                    ask(&m2.win.window, d, move |r| {
                        if r == "reset" {
                            dl(live::DEFAULT_PRESET.into());
                        }
                    });
                });
                r.add_suffix(&load);
                grp.add(&r);
                rows.borrow_mut().push(r.upcast());
                let presets = profiles::load_presets();
                let default = profiles::default_preset();
                let mut names: Vec<&String> = presets.keys().collect();
                names.sort_by_key(|n| n.to_lowercase());
                for name in names {
                    let mut ports: Vec<String> = presets[name].as_object().map(|o| o.keys().cloned().collect()).unwrap_or_default();
                    ports.sort();
                    let is_def = default.as_deref() == Some(name.as_str());
                    let mut sub = ports.iter().map(|p| trf("Port {n}", &[("n", &port_no(p))])).collect::<Vec<_>>().join(", ");
                    if is_def {
                        sub += &format!(" · {}", tr("loads at every start"));
                    }
                    let r = super::helpers::action_row(name, &sub);
                    let star = gtk::Button::from_icon_name(if is_def { "starred-symbolic" } else { "non-starred-symbolic" });
                    star.set_valign(gtk::Align::Center);
                    star.set_tooltip_text(Some(&tr(if is_def { "Default: loads at every start" } else { "Make default: load at every start" })));
                    star.add_css_class("flat");
                    let (n, rc) = (name.clone(), refresh_cell.clone());
                    star.connect_clicked(move |_| {
                        live::set_default_preset(&n, !is_def);
                        if let Some(f) = rc.borrow().as_ref() {
                            f();
                        }
                    });
                    r.add_suffix(&star);
                    let ren = gtk::Button::from_icon_name("document-edit-symbolic");
                    ren.set_tooltip_text(Some(&tr("Rename")));
                    ren.set_valign(gtk::Align::Center);
                    ren.add_css_class("flat");
                    let (m2, n, rc) = (m.clone(), name.clone(), refresh_cell.clone());
                    ren.connect_clicked(move |_| {
                        let d = adw::AlertDialog::new(Some(&tr("Rename Preset")), None);
                        let e = gtk::Entry::new();
                        e.set_text(&n);
                        e.set_activates_default(true);
                        d.set_extra_child(Some(&e));
                        d.add_response("cancel", &tr("Cancel"));
                        d.add_response("rename", &tr("Rename"));
                        d.set_response_appearance("rename", adw::ResponseAppearance::Suggested);
                        d.set_default_response(Some("rename"));
                        let (m3, n2, rc2) = (m2.clone(), n.clone(), rc.clone());
                        d.connect_response(None, move |_, resp| {
                            let new = e.text().trim().to_string();
                            if resp != "rename" || new.is_empty() || new == n2 {
                                return;
                            }
                            if let Err(err) = live::rename_preset(&n2, &new) {
                                m3.win.show_toast(&err.to_string());
                            }
                            if let Some(f) = rc2.borrow().as_ref() {
                                f();
                            }
                        });
                        d.present(Some(&m2.win.window));
                    });
                    r.add_suffix(&ren);
                    let del = gtk::Button::from_icon_name("user-trash-symbolic");
                    del.set_tooltip_text(Some(&tr("Delete")));
                    del.set_valign(gtk::Align::Center);
                    del.add_css_class("flat");
                    let (m2, n, rc) = (m.clone(), name.clone(), refresh_cell.clone());
                    del.connect_clicked(move |_| {
                        let d = alert(
                            &tr("Delete Preset?"),
                            &trf("“{name}” is deleted. Camera settings stay as they are.", &[("name", &n)]),
                            &[("cancel", &tr("Cancel")), ("delete", &tr("Delete"))],
                            None,
                            Some("delete"),
                        );
                        let (n2, rc2) = (n.clone(), rc.clone());
                        ask(&m2.win.window, d, move |r| {
                            if r == "delete" {
                                live::delete_preset(&n2);
                                if let Some(f) = rc2.borrow().as_ref() {
                                    f();
                                }
                            }
                        });
                    });
                    r.add_suffix(&del);
                    let load = gtk::Button::with_label(&tr("Load"));
                    load.set_valign(gtk::Align::Center);
                    load.add_css_class("suggested-action");
                    let (dl, n) = (do_load.clone(), name.clone());
                    load.connect_clicked(move |_| dl(n.clone()));
                    r.add_suffix(&load);
                    grp.add(&r);
                    rows.borrow_mut().push(r.upcast());
                }
            })
        };
        *refresh.borrow_mut() = Some(r_fn.clone());
        let (w, rf) = (self.win.clone(), r_fn.clone());
        btn_save.connect_clicked(move |_| ask_save_preset_then(&w, "", Some(rf.clone())));
        r_fn();
        dialog.present(Some(&self.win.window));
    }

    // --- live view ---

    fn set_overlay(self: &Rc<Self>, key: &'static str, on: bool) {
        self.overlays.borrow_mut().insert(key, on);
        if self.frozen.get() {
            let p = self.painted.borrow().clone();
            if let (Some(p), Some(dev)) = (p, self.cur_dev()) {
                if let Ok(img) = self.compose(&dev, &p) {
                    self.live_picture.set_paintable(to_texture(&img, 960).as_ref());
                }
            }
        } else {
            self.shown_seq.set(u64::MAX); // redraw the current picture now
        }
    }

    fn start_live(self: &Rc<Self>) {
        if self.live_timer.borrow().is_some() {
            return;
        }
        self.live_tick.set(0);
        let m = Rc::downgrade(self);
        let id = super::helpers::every(33, move || match m.upgrade() {
            Some(m) => m.tick_live(),
            None => false,
        });
        *self.live_timer.borrow_mut() = Some(id);
    }

    fn stop_live(&self) {
        if let Some(id) = self.live_timer.borrow_mut().take() {
            id.remove();
        }
        live::stop_all();
    }

    /// 255 outside the board area of the scores, at the size of the shown picture (only known at 1280x960).
    fn mask(&self, dev: &str, rows: i32, cols: i32, full: (i32, i32)) -> Option<Mat> {
        let key = (dev.to_string(), rows, cols);
        if let Some(m) = self.masks.borrow().get(&key) {
            return m.clone();
        }
        let m = (|| -> Option<Mat> {
            if full != (960, 1280) {
                return None;
            }
            let h = board::load_homography(dev, 1280, 960, None)?;
            let fm = board::board_mask_image(&h, 960, 1280, board::BOARD_R).ok()?;
            let inside = cvx::resize(&fm, cols, rows, imgproc::INTER_NEAREST).ok()?;
            cvx::not(&inside).ok()
        })();
        self.masks.borrow_mut().insert(key, m.clone());
        m
    }

    /// The picture with the overlays the user switched on (zebra, mask, histogram).
    fn compose(&self, dev: &str, bgr: &Mat) -> cvx::R<Mat> {
        let ov = self.overlays.borrow().clone();
        if !ov.values().any(|v| *v) {
            return bgr.try_clone();
        }
        let s = 960.0 / bgr.cols().max(bgr.rows()) as f64;
        let mut img = if s < 1.0 {
            cvx::resize(bgr, (bgr.cols() as f64 * s) as i32, (bgr.rows() as f64 * s) as i32, imgproc::INTER_AREA)?
        } else {
            bgr.try_clone()?
        };
        let gray = cvx::gray(&img)?;
        if ov["mask"] {
            if let Some(outside) = self.mask(dev, img.rows(), img.cols(), (bgr.rows(), bgr.cols())) {
                let dim = cvx::to_u8(&img, 0.3, 0.0)?;
                cvx::put_masked(&dim, &mut img, &outside)?;
            }
        }
        if ov["zebra"] {
            let mut hot = Mat::default();
            imgproc::threshold(&gray, &mut hot, 249.0, 255.0, imgproc::THRESH_BINARY)?;
            let mut stripes = Mat::zeros(gray.rows(), gray.cols(), opencv::core::CV_8U)?.to_mat()?;
            for y in 0..gray.rows() {
                let row = stripes.at_row_mut::<u8>(y)?;
                for (x, p) in row.iter_mut().enumerate() {
                    if ((x as i32 + y) / 6) % 2 == 0 {
                        *p = 255;
                    }
                }
            }
            let m = cvx::and(&hot, &stripes)?;
            img.set_to(&cvx::bgr(0., 0., 255.), &m)?;
        }
        if ov["hist"] {
            let mut h = [0f64; 64];
            for v in cvx::values_f32(&gray)? {
                h[((v as usize) / 4).min(63)] += 1.0;
            }
            let mx = h.iter().cloned().fold(1.0, f64::max);
            let (x0, y1) = (12, img.rows() - 12);
            cvx::rect(&mut img, x0 - 4, y1 - 84, 64 * 4 + 8, 88, Scalar::all(0.0), -1, false)?;
            for (i, v) in h.iter().enumerate() {
                let hh = ((v / mx).sqrt() * 76.0) as i32;
                let color = if i == 63 { cvx::bgr(80., 80., 255.) } else { cvx::bgr(230., 230., 230.) };
                cvx::rect(&mut img, x0 + i as i32 * 4, y1 - hh, 4, hh + 1, color, -1, false)?;
            }
        }
        Ok(img)
    }

    fn tick_live(self: &Rc<Self>) -> bool {
        let Some(dev) = self.cur_dev() else {
            *self.live_timer.borrow_mut() = None;
            live::stop_all();
            return false;
        };
        if oneclick::JOB.running() || !self.nav.is_mapped() {
            *self.live_timer.borrow_mut() = None;
            live::stop_all();
            return false;
        }
        let lv = live::live(&dev);
        for c in cameras() {
            if c != dev {
                live::live(&c).stop();
            }
        }
        lv.touch();
        let seq = lv.seq.load(std::sync::atomic::Ordering::SeqCst);
        let mut decoded: Option<Mat> = None;
        if seq != self.shown_seq.get() {
            if let Some(jpeg) = lv.frame() {
                if let Some(bgr) = cvx::decode_jpeg(&jpeg) {
                    self.ident.set(self.ident.get() + 1);
                    self.shown_seq.set(seq);
                    if !self.frozen.get() || self.painted.borrow().is_none() {
                        if let Ok(img) = self.compose(&dev, &bgr) {
                            self.live_picture.set_paintable(to_texture(&img, 960).as_ref());
                        }
                        *self.painted.borrow_mut() = Some(bgr.clone());
                    }
                    decoded = Some(bgr);
                }
            }
        }
        let t = self.live_tick.get() + 1;
        self.live_tick.set(t);
        if t % 30 == 0 {
            // about once a second
            let (fc, id0, t0, _) = self.rate.get();
            let src = lv.source();
            let n = if src == "own stream" { lv.frame_count().saturating_sub(fc) } else { self.ident.get().saturating_sub(id0) };
            let el = t0.elapsed().as_secs_f64().max(0.1);
            self.rate.set((lv.frame_count(), self.ident.get(), Instant::now(), n as f64 / el));
            let thumb = decoded.or_else(|| self.painted.borrow().clone());
            if let (Some(b), Some(c)) = (thumb, self.cur_cam()) {
                self.strip.set_thumb(c["port"].as_str().unwrap_or(""), to_texture(&b, THUMB_PX));
            }
            if !self.scores_busy.replace(true) {
                let (m, lv2) = (self.clone(), lv.clone());
                run_async(move || lv2.scores(), move |sc| m.show_scores(&sc));
            }
        }
        true
    }

    fn show_scores(&self, sc: &Value) {
        self.scores_busy.set(false);
        let source = sc["source"].as_str().unwrap_or("");
        let src = tr(match source {
            "own stream" => "Live from the camera",
            "Autodarts" => "Picture from Autodarts",
            "waking Autodarts from standby" => "Waking Autodarts' cameras from standby…",
            s => s,
        });
        let fps = self.rate.get().3;
        let mut tag = format!("● {}", src);
        if fps >= 0.5 && (source == "own stream" || source == "Autodarts") {
            tag += &format!(" · {:.0} fps", fps);
        }
        self.lbl_tag.set_label(&tag);
        self.lbl_tag.set_visible(!src.is_empty());
        let only_note = |t: &str, k: &str| {
            for w in [&self.chip_white, &self.chip_clip, &self.chip_noise, &self.chip_kb] {
                w.set_visible(false);
            }
            set_chip(&self.chip_note, t, k);
            self.chip_note.set_visible(true);
        };
        let Some(white) = sc["white"].as_f64() else {
            only_note("Waiting for a picture…", "neutral");
            return;
        };
        if source == "Autodarts" && white < 2.0 {
            only_note("Autodarts sends a black picture: its cameras are probably in standby", "warn");
            return;
        }
        self.chip_note.set_visible(false);
        let target = sc["white_target"].as_f64().unwrap_or(232.0);
        if self.cur_cam().map(|c| c["optimized"].as_bool().unwrap_or(false)).unwrap_or(false) {
            let d = (100.0 * (white - target) / target).abs();
            set_chip(&self.chip_white, &format!("{} {:.0} / {:.0}", tr("White"), white, target), if d < 6.0 { "ok" } else if d < 15.0 { "warn" } else { "bad" });
        } else {
            set_chip(&self.chip_white, &format!("{} {:.0}", tr("White"), white), "neutral");
        }
        let clip = sc["clip_pct"].as_f64().unwrap_or(0.0);
        set_chip(&self.chip_clip, &format!("{} {:.1} %", tr("Clipped"), clip), if clip < 1.0 { "ok" } else if clip < 3.0 { "warn" } else { "bad" });
        set_chip(&self.chip_kb, &format!("{:.0} KB/frame", sc["kb"].as_f64().unwrap_or(0.0)), "neutral");
        if let Some(n) = sc["tnoise"].as_f64() {
            set_chip(&self.chip_noise, &format!("{} {:.2}", tr("Noise"), n), "neutral");
        }
        self.chip_noise.set_visible(sc.get("tnoise").is_some());
        for w in [&self.chip_white, &self.chip_clip, &self.chip_kb] {
            w.set_visible(true);
        }
        let tip = if sc["board_mask"].as_bool().unwrap_or(false) { String::new() } else { tr("No Autodarts calibration: the scores use the whole picture") };
        self.chips.set_tooltip_text(if tip.is_empty() { None } else { Some(&tip) });
    }
}

/// Tap a value to type it. Exposure shows milliseconds on HBV cameras but is typed in the raw unit.
fn type_value(anchor: &gtk::Button, label: &str, scale: &gtk::Scale, lo: f64, hi: f64, step: f64) {
    let pop = gtk::Popover::new();
    let b = gtk::Box::new(gtk::Orientation::Horizontal, 6);
    for m in [6] {
        b.set_margin_top(m);
        b.set_margin_bottom(m);
        b.set_margin_start(m);
        b.set_margin_end(m);
    }
    let spin = gtk::SpinButton::with_range(lo, hi, step);
    spin.set_value(scale.value());
    spin.set_numeric(true);
    spin.set_width_chars(7);
    let l = gtk::Label::new(Some(&format!("{} ({}–{})", label, lo as i64, hi as i64)));
    l.add_css_class("camopt-small");
    b.append(&l);
    b.append(&spin);
    let ok = gtk::Button::with_label(&tr("Set"));
    ok.add_css_class("suggested-action");
    let apply = {
        let (s, sp, p) = (scale.clone(), spin.clone(), pop.clone());
        Rc::new(move || {
            s.set_value(sp.value());
            p.popdown();
        })
    };
    let a = apply.clone();
    ok.connect_clicked(move |_| a());
    let a = apply.clone();
    spin.connect_activate(move |_| a());
    b.append(&ok);
    pop.set_child(Some(&b));
    pop.set_parent(anchor);
    pop.connect_closed(|p| {
        let p = p.clone();
        glib::idle_add_local_once(move || p.unparent());
    });
    pop.popup();
    spin.grab_focus();
    let _ = esc;
}
