//! Camera Optimizer page: one-click calibration of HBV OV2710 cameras (core::hbvcal).
//!
//! Two tabs under the shared camera strip. Optimize: a table of the cameras with their calibration, a lighting
//! check, the run with a progress bar and step checklist. Before & After: the pictures taken by the last run as
//! split, side-by-side or difference view with zoom, and the measured change.
use super::camera_controls_view::ask_save_preset;
use super::camera_widgets::{self as cw, caption, chip, opts, segmented, set_chip, wrap_label, AutodartsStatus, CameraStrip, StripEntry, ThumbPoller};
use super::helpers::{alert, ask, run_async};
use super::{Page, Win};
use crate::core::cvx;
use crate::core::hbvcal::autocal::{PREVIEW, WHITE_TARGET};
use crate::core::hbvcal::daemon::cameras;
use crate::core::hbvcal::oneclick::{self, Pictures, Support, JOB};
use crate::core::hbvcal::{live, profiles};
use crate::core::i18n::{get_language, tr, trf};
use adw::prelude::*;
use gtk::{glib, pango};
use opencv::core::{Mat, Point, Scalar, Vector};
use opencv::imgproc;
use opencv::prelude::*;
use serde_json::Value;
use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::rc::Rc;
use std::time::Instant;

/// longest side of the rendered before/after view
const BA_VIEW: f64 = 960.0;
const BA_ZOOMS: [i32; 4] = [1, 2, 3, 4];
const PREVIEW_MS: u64 = 250;
const PREVIEW_VIEW: i32 = 720;
/// table: camera, white / target, JPEG, last run (the rest is "Now")
const COLS: [i32; 4] = [130, 96, 52, 118];

fn eta_s(name: &str) -> f64 {
    match name {
        "apply" => 25.0,
        "restore" => 40.0,
        _ => 180.0,
    }
}

/// '2026-10-25 13:38:22' -> 'Today 13:38'.
pub fn when_text(stamp: Option<&str>) -> String {
    let Some(t) = stamp.and_then(|s| chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S").ok()) else { return "–".into() };
    let today = chrono::Local::now().date_naive();
    let day = if t.date() == today {
        tr("Today")
    } else if get_language() == "de" {
        t.format("%d.%m.").to_string()
    } else {
        t.format("%b %d").to_string()
    };
    format!("{} {}", day, t.format("%H:%M"))
}

#[derive(Clone)]
struct CamInfo {
    s: Support,
    white: Option<f64>,
    q: Option<i64>,
    when: Option<String>,
}

#[derive(Clone)]
struct Status {
    cams: Vec<CamInfo>,
    consent: Option<Value>,
    autodarts: String,
    pending: bool,
}

struct BaData {
    imgs: (Mat, Mat),
    kb: (f64, f64),
    stats: [(f64, f64); 2],
    q: (Option<i64>, Option<i64>),
}

/// ● text with the dot coloured by kind (where the text is the camera's name).
fn status_dot_box(text: &str, kind: &str) -> gtk::Box {
    let b = gtk::Box::new(gtk::Orientation::Horizontal, 6);
    let d = gtk::Label::new(Some("●"));
    d.add_css_class("camopt-dot");
    d.add_css_class(&format!("camopt-{}", kind));
    b.append(&d);
    let l = gtk::Label::new(Some(text));
    l.set_xalign(0.0);
    l.set_ellipsize(pango::EllipsizeMode::End);
    l.add_css_class("camopt-cell");
    l.add_css_class("heading");
    b.append(&l);
    b
}

pub struct CameraOptimizerPage {
    nav: adw::NavigationPage,
    win: Win,
    status: RefCell<Option<Status>>,
    status_busy: Cell<bool>,
    job_timer: RefCell<Option<glib::SourceId>>,
    job_t0: RefCell<Option<Instant>>,
    preview_timer: RefCell<Option<glib::SourceId>>,
    preview_seq: Cell<i64>,
    preview_dev: RefCell<String>,
    consent_asked: Cell<bool>,
    lighting: RefCell<HashMap<String, Value>>,
    ba: RefCell<Vec<Pictures>>,
    ba_data: RefCell<Option<Rc<BaData>>>,
    ba_key: RefCell<String>,
    ba_zoom: Cell<i32>,
    ba_mode: RefCell<String>,
    ba_center: Cell<(f64, f64)>,
    ba_comp: Cell<(i32, i32)>,
    ba_drag_center: Cell<(f64, f64)>,
    ba_pointer: Cell<Option<(f64, f64)>>,
    strip: Rc<CameraStrip>,
    autodarts: Rc<AutodartsStatus>,
    thumbs: RefCell<Option<Rc<ThumbPoller>>>,
    stack: gtk::Stack,
    tabs: gtk::Stack,
    tab_toggle: adw::ToggleGroup,
    hero_title: gtk::Label,
    hero_text: gtk::Label,
    banner: gtk::Box,
    banner_title: gtk::Label,
    cam_list: gtk::Box,
    preview_box: gtk::Box,
    preview_pic: gtk::Picture,
    preview_tag: gtk::Label,
    preview_phase: gtk::Label,
    btn_light: gtk::Button,
    guide: gtk::Box,
    btn_optimize: gtk::Button,
    btn_restore: gtk::Button,
    job_empty: gtk::Label,
    job_box: gtk::Box,
    progress: gtk::ProgressBar,
    lbl_time: gtk::Label,
    steps_box: gtk::Box,
    steps_scroll: gtk::ScrolledWindow,
    job_note: gtk::Label,
    btn_after: gtk::Button,
    log_view: gtk::TextView,
    ba_stack: gtk::Stack,
    ba_empty: gtk::Label,
    ba_picture: gtk::Picture,
    tag_before: gtk::Label,
    tag_after: gtk::Label,
    ba_zoom_toggle: adw::ToggleGroup,
    ba_note: gtk::Label,
    ba_adj: gtk::Adjustment,
    ba_title: gtk::Label,
    ba_table: gtk::Grid,
    ba_chip: gtk::Label,
    ba_pending: gtk::Box,
    btn_ba_preset: gtk::Button,
}

impl Page for CameraOptimizerPage {
    fn nav(&self) -> &adw::NavigationPage {
        &self.nav
    }

    fn on_opened(self: Rc<Self>) {
        self.refresh_status();
        if let Some(t) = self.thumbs.borrow().as_ref() {
            t.start();
        }
        if JOB.running() {
            self.start_job_poll();
        }
    }

    fn on_closed(self: Rc<Self>) {
        if let Some(t) = self.thumbs.borrow().as_ref() {
            t.stop();
        }
        if let Some(id) = self.job_timer.borrow_mut().take() {
            id.remove();
        }
        if let Some(id) = self.preview_timer.borrow_mut().take() {
            id.remove();
        }
    }
}

impl CameraOptimizerPage {
    pub fn new(win: Win) -> Rc<dyn Page> {
        let root = gtk::Box::new(gtk::Orientation::Vertical, 8);
        root.set_margin_top(8);
        root.set_margin_bottom(10);
        root.set_margin_start(12);
        root.set_margin_end(12);
        let nav = adw::NavigationPage::builder().title(tr("Camera Optimizer")).tag("camera_optimizer").child(&root).build();
        let top = gtk::CenterBox::new();
        top.set_vexpand(false);
        top.set_valign(gtk::Align::Start);
        let strip = CameraStrip::new(win.clone());
        top.set_start_widget(Some(&strip.widget));
        let side = gtk::Box::new(gtk::Orientation::Vertical, 6);
        side.set_halign(gtk::Align::End);
        let navb = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        navb.set_halign(gtk::Align::End);
        let tab_toggle = adw::ToggleGroup::new();
        tab_toggle.set_valign(gtk::Align::Center);
        for (n, l) in [("optimize", "Optimize"), ("compare", "Before & After")] {
            let t = adw::Toggle::new();
            t.set_name(Some(n));
            t.set_label(Some(&tr(l)));
            tab_toggle.add(t);
        }
        tab_toggle.set_active_name(Some("optimize"));
        navb.append(&tab_toggle);
        let btn_controls = gtk::Button::new();
        let bc = adw::ButtonContent::new();
        bc.set_icon_name("preferences-system-symbolic");
        bc.set_label(&tr("Controls"));
        btn_controls.set_child(Some(&bc));
        btn_controls.set_tooltip_text(Some(&tr("Fine-tune every camera setting, keep settings, presets")));
        let w = win.clone();
        btn_controls.connect_clicked(move |_| w.open_camera_controls_page());
        navb.append(&btn_controls);
        side.append(&navb);
        let autodarts = AutodartsStatus::new(win.clone());
        autodarts.widget.set_halign(gtk::Align::End);
        side.append(&autodarts.widget);
        top.set_end_widget(Some(&side));
        root.append(&top);
        let stack = gtk::Stack::new();
        stack.set_transition_type(gtk::StackTransitionType::Crossfade);
        stack.set_vexpand(true);
        root.append(&stack);

        // declined
        let dp = adw::StatusPage::new();
        dp.set_icon_name(Some("camera-disabled-symbolic"));
        dp.set_title(&tr("Optimization is off"));
        dp.set_description(Some(&tr("This tool changes hidden settings that were only tested on HBV OV2710 cameras, so it stays off for other cameras.")));
        let b_yes = gtk::Button::with_label(&tr("I do use HBV OV2710 cameras"));
        b_yes.set_halign(gtk::Align::Center);
        b_yes.add_css_class("pill");
        dp.set_child(Some(&b_yes));
        stack.add_named(&dp, Some("declined"));

        let tabs = gtk::Stack::new();
        tabs.set_vexpand(true);
        tabs.set_transition_type(gtk::StackTransitionType::Crossfade);
        stack.add_named(&tabs, Some("overview"));
        stack.set_visible_child_name("overview");

        // --- optimize tab ---
        let cols = gtk::Box::new(gtk::Orientation::Horizontal, 12);
        tabs.add_named(&cols, Some("optimize"));
        let left = gtk::Box::new(gtk::Orientation::Vertical, 10);
        left.set_size_request(440, -1);
        left.set_hexpand(true);
        left.add_css_class("card");
        left.add_css_class("camopt-panel");
        let hero_title = wrap_label("Loading…", &["camopt-title"]);
        hero_title.set_max_width_chars(40);
        let hero_text = caption("", &[], true);
        hero_text.set_max_width_chars(60);
        left.append(&hero_title);
        left.append(&hero_text);
        let banner = gtk::Box::new(gtk::Orientation::Horizontal, 10);
        banner.set_visible(false);
        banner.add_css_class("camopt-note");
        banner.add_css_class("camopt-note-warn");
        let bt = gtk::Box::new(gtk::Orientation::Vertical, 0);
        bt.set_hexpand(true);
        let banner_title = gtk::Label::new(None);
        banner_title.set_xalign(0.0);
        banner_title.set_wrap(true);
        banner_title.add_css_class("heading");
        bt.append(&banner_title);
        bt.append(&caption("The light probably changed.", &[], false));
        banner.append(&bt);
        left.append(&banner);
        let cam_list = gtk::Box::new(gtk::Orientation::Vertical, 2);
        left.append(&cam_list);
        let preview_box = gtk::Box::new(gtk::Orientation::Vertical, 6);
        preview_box.set_visible(false);
        preview_box.set_vexpand(true);
        let preview_pic = gtk::Picture::new();
        preview_pic.set_content_fit(gtk::ContentFit::Contain);
        preview_pic.set_can_shrink(true);
        preview_pic.set_vexpand(true);
        preview_pic.add_css_class("camopt-frame");
        preview_pic.set_size_request(-1, 220);
        let pov = gtk::Overlay::new();
        pov.set_child(Some(&preview_pic));
        let preview_tag = gtk::Label::new(None);
        preview_tag.set_halign(gtk::Align::Start);
        preview_tag.set_valign(gtk::Align::Start);
        preview_tag.add_css_class("camopt-tag");
        preview_tag.set_visible(false);
        preview_tag.set_margin_top(10);
        preview_tag.set_margin_start(10);
        pov.add_overlay(&preview_tag);
        preview_box.append(&pov);
        let preview_phase = caption("", &[], true);
        preview_phase.set_max_width_chars(60);
        preview_box.append(&preview_phase);
        left.append(&preview_box);
        let btn_light = gtk::Button::with_label(&tr("Check lighting"));
        btn_light.set_halign(gtk::Align::Start);
        btn_light.add_css_class("compact-btn");
        btn_light.set_tooltip_text(Some(&tr("Compare the brightness of every optimized camera with its calibration")));
        left.append(&btn_light);
        let guide = gtk::Box::new(gtk::Orientation::Vertical, 4);
        guide.append(&caption("BEFORE YOU START", &["camopt-heading"], false));
        for t in ["Remove all darts from the board", "Turn the lights on the way you play", "Keep people and hands away from the board for about 3 minutes"] {
            guide.append(&caption(&format!("☑  {}", tr(t)), &[], false));
        }
        left.append(&guide);
        let sp = gtk::Box::new(gtk::Orientation::Vertical, 0);
        sp.set_vexpand(true);
        left.append(&sp);
        let acts = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        let btn_optimize = gtk::Button::with_label(&tr("Optimize cameras"));
        btn_optimize.set_sensitive(false);
        btn_optimize.set_hexpand(true);
        btn_optimize.add_css_class("pill");
        acts.append(&btn_optimize);
        let btn_restore = gtk::Button::with_label(&tr("Restore…"));
        btn_restore.set_visible(false);
        btn_restore.add_css_class("compact-btn");
        btn_restore.set_tooltip_text(Some(&tr("Back to the settings Autodarts uses")));
        acts.append(&btn_restore);
        left.append(&acts);
        left.append(&caption(
            "Autodarts is stopped and restarted automatically. Nothing is flashed: settings live in camera memory and are re-applied by the keeper.",
            &[],
            true,
        ));
        cols.append(&left);
        let right = gtk::Box::new(gtk::Orientation::Vertical, 10);
        right.set_size_request(380, -1);
        right.set_hexpand(false);
        right.add_css_class("card");
        right.add_css_class("camopt-panel");
        right.append(&caption("PROGRESS", &["camopt-heading"], false));
        let job_empty = gtk::Label::new(Some(&tr("Steps and progress of an optimization appear here.")));
        job_empty.set_wrap(true);
        job_empty.set_vexpand(true);
        job_empty.add_css_class("camopt-empty");
        right.append(&job_empty);
        let job_box = gtk::Box::new(gtk::Orientation::Vertical, 8);
        job_box.set_visible(false);
        job_box.set_vexpand(true);
        let progress = gtk::ProgressBar::new();
        progress.set_hexpand(true);
        progress.set_valign(gtk::Align::Center);
        let lbl_time = caption("", &[], false);
        let pr = gtk::Box::new(gtk::Orientation::Horizontal, 10);
        pr.append(&progress);
        pr.append(&lbl_time);
        job_box.append(&pr);
        let steps_box = gtk::Box::new(gtk::Orientation::Vertical, 6);
        let steps_scroll = gtk::ScrolledWindow::new();
        steps_scroll.set_hscrollbar_policy(gtk::PolicyType::Never);
        steps_scroll.set_vexpand(true);
        steps_scroll.set_child(Some(&steps_box));
        job_box.append(&steps_scroll);
        let job_note = caption("", &["camopt-note"], true);
        job_note.set_visible(false);
        job_box.append(&job_note);
        let ja = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        let btn_after = gtk::Button::with_label(&tr("See before & after"));
        btn_after.set_visible(false);
        btn_after.add_css_class("compact-btn");
        ja.append(&btn_after);
        let sp2 = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        sp2.set_hexpand(true);
        ja.append(&sp2);
        let log_view = gtk::TextView::new();
        log_view.set_editable(false);
        log_view.set_monospace(true);
        log_view.set_wrap_mode(gtk::WrapMode::WordChar);
        log_view.add_css_class("camopt-log");
        let ls = gtk::ScrolledWindow::new();
        ls.set_min_content_width(480);
        ls.set_min_content_height(260);
        ls.set_child(Some(&log_view));
        let lp = gtk::Popover::new();
        lp.set_child(Some(&ls));
        let btn_log = gtk::MenuButton::new();
        btn_log.set_label(&tr("Log"));
        btn_log.set_popover(Some(&lp));
        btn_log.set_tooltip_text(Some(&tr("Details of the last run")));
        btn_log.add_css_class("flat");
        btn_log.add_css_class("compact-btn");
        ja.append(&btn_log);
        job_box.append(&ja);
        right.append(&job_box);
        cols.append(&right);

        // --- compare tab ---
        let ccols = gtk::Box::new(gtk::Orientation::Horizontal, 12);
        tabs.add_named(&ccols, Some("compare"));
        let cl = gtk::Box::new(gtk::Orientation::Vertical, 8);
        cl.set_hexpand(true);
        let ba_stack = gtk::Stack::new();
        ba_stack.set_vexpand(true);
        let ba_empty = gtk::Label::new(Some(&tr("Before and after pictures appear here after an optimization.")));
        ba_empty.set_wrap(true);
        ba_empty.add_css_class("camopt-empty");
        ba_stack.add_named(&ba_empty, Some("empty"));
        let ba_picture = gtk::Picture::new();
        ba_picture.set_content_fit(gtk::ContentFit::Contain);
        ba_picture.set_can_shrink(true);
        ba_picture.set_tooltip_text(Some(&tr("Double-tap or scroll to zoom into a spot; drag to move around when zoomed")));
        ba_picture.add_css_class("camopt-frame");
        let bov = gtk::Overlay::new();
        bov.set_child(Some(&ba_picture));
        let tag_before = gtk::Label::new(Some(&tr("BEFORE")));
        tag_before.set_halign(gtk::Align::Start);
        let tag_after = gtk::Label::new(Some(&tr("AFTER")));
        tag_after.set_halign(gtk::Align::End);
        for t in [&tag_before, &tag_after] {
            t.set_valign(gtk::Align::Start);
            t.add_css_class("camopt-tag");
            t.set_margin_top(10);
            t.set_margin_start(10);
            t.set_margin_end(10);
            bov.add_overlay(t);
        }
        let ba_zoom_toggle = adw::ToggleGroup::new();
        ba_zoom_toggle.set_valign(gtk::Align::End);
        ba_zoom_toggle.set_halign(gtk::Align::End);
        ba_zoom_toggle.set_tooltip_text(Some(&tr("Zoom")));
        ba_zoom_toggle.set_margin_bottom(10);
        ba_zoom_toggle.set_margin_end(10);
        ba_zoom_toggle.add_css_class("osd");
        for z in BA_ZOOMS {
            let t = adw::Toggle::new();
            t.set_name(Some(&z.to_string()));
            t.set_label(Some(&format!("{}×", z)));
            ba_zoom_toggle.add(t);
        }
        ba_zoom_toggle.set_active_name(Some("1"));
        bov.add_overlay(&ba_zoom_toggle);
        ba_stack.add_named(&bov, Some("picture"));
        cl.append(&ba_stack);
        let bar = gtk::Box::new(gtk::Orientation::Horizontal, 10);
        cl.append(&bar);
        let ba_note = caption("", &[], false);
        ba_note.set_hexpand(true);
        ba_note.set_halign(gtk::Align::End);
        ba_note.set_xalign(1.0);
        let ba_adj = gtk::Adjustment::new(50.0, 0.0, 100.0, 1.0, 10.0, 0.0);
        ccols.append(&cl);
        let cr = gtk::Box::new(gtk::Orientation::Vertical, 10);
        cr.set_size_request(340, -1);
        cr.set_hexpand(false);
        cr.add_css_class("card");
        cr.add_css_class("camopt-panel");
        let ba_title = caption("RESULT", &["camopt-heading"], false);
        cr.append(&ba_title);
        let ba_table = gtk::Grid::new();
        ba_table.set_column_spacing(14);
        ba_table.set_row_spacing(8);
        cr.append(&ba_table);
        let ba_chip = chip("", "neutral");
        ba_chip.set_halign(gtk::Align::Start);
        ba_chip.set_visible(false);
        cr.append(&ba_chip);
        let sp3 = gtk::Box::new(gtk::Orientation::Vertical, 0);
        sp3.set_vexpand(true);
        cr.append(&sp3);
        let ba_pending = gtk::Box::new(gtk::Orientation::Vertical, 6);
        ba_pending.set_visible(false);
        ba_pending.append(&caption("Not applied yet: the cameras still run their previous settings.", &[], true));
        let btn_apply = gtk::Button::with_label(&tr("Apply now & set as default"));
        btn_apply.add_css_class("pill");
        btn_apply.add_css_class("suggested-action");
        btn_apply.set_tooltip_text(Some(&tr("Keep this calibration, save it as a preset and load that preset at every start")));
        ba_pending.append(&btn_apply);
        let btn_discard = gtk::Button::with_label(&tr("Discard"));
        btn_discard.add_css_class("flat");
        btn_discard.set_tooltip_text(Some(&tr("Forget this calibration and keep the previous settings")));
        ba_pending.append(&btn_discard);
        cr.append(&ba_pending);
        let btn_ba_preset = gtk::Button::with_label(&tr("Save as Preset…"));
        cr.append(&btn_ba_preset);
        let btn_fine = gtk::Button::with_label(&tr("Fine-tune in Camera Controls →"));
        btn_fine.add_css_class("flat");
        let w = win.clone();
        btn_fine.connect_clicked(move |_| w.open_camera_controls_page());
        cr.append(&btn_fine);
        ccols.append(&cr);

        let me = Rc::new(CameraOptimizerPage {
            nav,
            win: win.clone(),
            status: RefCell::new(None),
            status_busy: Cell::new(false),
            job_timer: RefCell::new(None),
            job_t0: RefCell::new(None),
            preview_timer: RefCell::new(None),
            preview_seq: Cell::new(-1),
            preview_dev: RefCell::new(String::new()),
            consent_asked: Cell::new(false),
            lighting: RefCell::new(HashMap::new()),
            ba: RefCell::new(vec![]),
            ba_data: RefCell::new(None),
            ba_key: RefCell::new(String::new()),
            ba_zoom: Cell::new(1),
            ba_mode: RefCell::new("split".into()),
            ba_center: Cell::new((0.5, 0.5)),
            ba_comp: Cell::new((1, 1)),
            ba_drag_center: Cell::new((0.5, 0.5)),
            ba_pointer: Cell::new(None),
            strip,
            autodarts,
            thumbs: RefCell::new(None),
            stack,
            tabs,
            tab_toggle,
            hero_title,
            hero_text,
            banner,
            banner_title,
            cam_list,
            preview_box,
            preview_pic,
            preview_tag,
            preview_phase,
            btn_light,
            guide,
            btn_optimize,
            btn_restore,
            job_empty,
            job_box,
            progress,
            lbl_time,
            steps_box,
            steps_scroll,
            job_note,
            btn_after,
            log_view,
            ba_stack,
            ba_empty,
            ba_picture,
            tag_before,
            tag_after,
            ba_zoom_toggle,
            ba_note,
            ba_adj,
            ba_title,
            ba_table,
            ba_chip,
            ba_pending,
            btn_ba_preset,
        });
        let m = Rc::downgrade(&me);
        me.strip.set_callbacks(
            Rc::new(move || {
                if let Some(m) = m.upgrade() {
                    m.render_cams();
                    m.on_ba_pick();
                }
            }),
            None,
        );
        let m = Rc::downgrade(&me);
        let dev = Rc::new(move || {
            m.upgrade()
                .and_then(|m| m.status.borrow().as_ref().map(|s| s.cams.iter().map(|c| (c.s.port.clone(), c.s.device.clone())).collect()))
                .unwrap_or_default()
        });
        *me.thumbs.borrow_mut() = Some(ThumbPoller::new(me.strip.clone(), dev, Rc::new(|| None)));
        let m = me.clone();
        me.tab_toggle.connect_active_name_notify(move |t| {
            m.tabs.set_visible_child_name(&t.active_name().map(|s| s.to_string()).unwrap_or_else(|| "optimize".into()));
        });
        let m = me.clone();
        b_yes.connect_clicked(move |_| m.ask_consent());
        let m = me.clone();
        me.btn_light.connect_clicked(move |_| m.check_lighting());
        let m = me.clone();
        me.btn_optimize.connect_clicked(move |_| m.ask_prepare());
        let m = me.clone();
        me.btn_restore.connect_clicked(move |_| m.ask_restore());
        let m = me.clone();
        me.btn_after.connect_clicked(move |_| m.tab_toggle.set_active_name(Some("compare")));
        let m = me.clone();
        btn_apply.connect_clicked(move |_| m.start_job("apply"));
        let m = me.clone();
        btn_discard.connect_clicked(move |_| m.ask_discard());
        let w = win.clone();
        me.btn_ba_preset.connect_clicked(move |_| ask_save_preset(&w, &trf("Optimized {when}", &[("when", &chrono::Local::now().format("%Y-%m-%d %H:%M"))])));
        let m = Rc::downgrade(&me);
        let seg = segmented(
            &opts(&[("split", "Split"), ("side", "Side by side"), ("diff", "Difference")]),
            "split",
            Rc::new(move |mode: String| {
                if let Some(m) = m.upgrade() {
                    *m.ba_mode.borrow_mut() = mode;
                    m.render_ba();
                }
            }),
        );
        bar.append(&seg);
        bar.append(&me.ba_note);
        let m = me.clone();
        me.ba_adj.connect_value_changed(move |_| m.render_ba());
        let m = me.clone();
        me.ba_zoom_toggle.connect_active_name_notify(move |t| {
            let z = t.active_name().and_then(|s| s.parse().ok()).unwrap_or(1);
            if z != m.ba_zoom.get() {
                m.set_zoom(z);
            }
        });
        // gestures on the before/after picture
        let drag = gtk::GestureDrag::new();
        let m = me.clone();
        drag.connect_drag_begin(move |_, x, _| m.drag_begin(x));
        let m = me.clone();
        drag.connect_drag_update(move |g, dx, dy| {
            let x0 = g.start_point().map(|p| p.0).unwrap_or(0.0);
            m.drag_update(x0, dx, dy);
        });
        let m = me.clone();
        drag.connect_drag_end(move |_, _, _| m.update_cursor());
        me.ba_picture.add_controller(drag);
        let click = gtk::GestureClick::new();
        let m = me.clone();
        click.connect_pressed(move |_, n, x, y| {
            if n == 2 {
                m.zoom_at(x, y, m.ba_zoom.get().max(2));
            }
        });
        me.ba_picture.add_controller(click);
        let wheel = gtk::EventControllerScroll::new(gtk::EventControllerScrollFlags::VERTICAL);
        let m = me.clone();
        wheel.connect_scroll(move |_, _, dy| {
            if m.ba_data.borrow().is_none() || dy == 0.0 {
                return glib::Propagation::Proceed;
            }
            let i = BA_ZOOMS.iter().position(|z| *z == m.ba_zoom.get()).unwrap_or(0) as i32 + if dy < 0.0 { 1 } else { -1 };
            let (x, y) = m.ba_pointer.get().unwrap_or((m.ba_picture.width() as f64 / 2.0, m.ba_picture.height() as f64 / 2.0));
            m.zoom_at(x, y, BA_ZOOMS[i.clamp(0, BA_ZOOMS.len() as i32 - 1) as usize]);
            glib::Propagation::Stop
        });
        me.ba_picture.add_controller(wheel);
        let motion = gtk::EventControllerMotion::new();
        let m = me.clone();
        motion.connect_motion(move |_, x, y| m.ba_pointer.set(Some((x, y))));
        me.ba_picture.add_controller(motion);
        me
    }

    fn cam(&self, port: Option<&str>) -> Option<CamInfo> {
        let p = port.map(String::from).or_else(|| self.strip.active())?;
        self.status.borrow().as_ref()?.cams.iter().find(|c| c.s.port == p).cloned()
    }

    fn consented(&self) -> bool {
        self.status.borrow().as_ref().and_then(|s| s.consent.as_ref().map(|c| c["hbv_confirmed"].as_bool().unwrap_or(false))).unwrap_or(false)
    }

    pub fn refresh_status(self: &Rc<Self>) {
        if self.status_busy.replace(true) {
            return;
        }
        let m = self.clone();
        run_async(
            || {
                let cams = cameras()
                    .iter()
                    .map(|d| {
                        let s = oneclick::support(d);
                        let mut c = CamInfo { s, white: None, q: None, when: None };
                        if c.s.supported && c.s.optimized {
                            if let Some(p) = profiles::get(d) {
                                let meta = &p["meta"];
                                c.white = meta["reference"]["white"].as_f64();
                                c.q = meta["mjpeg_q"].as_i64();
                                c.when = meta["calibrated"].as_str().map(String::from);
                            }
                        }
                        c
                    })
                    .collect();
                Status {
                    cams,
                    consent: profiles::consent(),
                    autodarts: oneclick::systemctl("is-active", "autodarts"),
                    pending: !oneclick::load_pending().is_empty(),
                }
            },
            move |st| {
                m.status_busy.set(false);
                *m.status.borrow_mut() = Some(st);
                m.render_status();
            },
        );
    }

    fn render_status(self: &Rc<Self>) {
        let st = self.status.borrow().clone().unwrap();
        if st.consent.is_none() && !self.consent_asked.replace(true) {
            self.ask_consent();
        }
        let on = self.consented();
        self.stack.set_visible_child_name(if st.consent.is_some() && !on { "declined" } else { "overview" });
        self.autodarts.show(&st.autodarts);
        let entries: Vec<StripEntry> = st
            .cams
            .iter()
            .map(|c| StripEntry { port: c.s.port.clone(), kind: (if !c.s.supported { "bad" } else if c.s.optimized { "ok" } else { "warn" }).into() })
            .collect();
        self.strip.sync(&entries);
        self.render_cams();
        self.render_job();
        self.load_before_after();
        if let Some(t) = self.thumbs.borrow().as_ref() {
            t.kick();
        }
    }

    fn render_cams(self: &Rc<Self>) {
        let Some(st) = self.status.borrow().clone() else { return };
        super::helpers::clear_box(&self.cam_list);
        let sup: Vec<&CamInfo> = st.cams.iter().filter(|c| c.s.supported).collect();
        if !st.cams.is_empty() {
            let head = gtk::Box::new(gtk::Orientation::Horizontal, 8);
            head.set_margin_start(14); // the rows are buttons with this padding
            for (t, w) in ["CAMERA", "WHITE / TARGET", "JPEG", "LAST RUN", "NOW"].iter().zip(COLS.iter().chain([-1].iter())) {
                let h = gtk::Label::new(Some(&tr(t)));
                h.set_xalign(0.0);
                h.add_css_class("camopt-table-head");
                h.set_hexpand(*w < 0);
                h.set_size_request(*w, -1);
                head.append(&h);
            }
            self.cam_list.append(&head);
        }
        for c in &st.cams {
            self.cam_list.append(&self.cam_row(c));
        }
        if st.cams.is_empty() {
            self.cam_list.append(&caption("No cameras connected", &[], false));
        }
        let done = sup.iter().filter(|c| c.s.optimized).count();
        let light = self.lighting.borrow();
        let bad: Vec<String> = light.iter().filter(|(_, r)| r["verdict"].as_str().map(|v| v != "ok").unwrap_or(false)).map(|(p, _)| p.clone()).collect();
        let running = JOB.running();
        if running {
            // render_job owns the headline
        } else if st.pending {
            self.hero_title.set_label(&tr("Calibration ready to review"));
            self.hero_text.set_label(&tr("Nothing has changed yet. Compare before and after, then apply it or discard it."));
        } else if sup.is_empty() {
            self.hero_title.set_label(&tr("No supported camera found"));
            self.hero_text.set_label(&tr("Connect your HBV OV2710 cameras, then open this page again."));
        } else if !bad.is_empty() {
            self.hero_title.set_label(&tr("The light has changed"));
            self.hero_text.set_label(&tr("Optimize again to match the cameras to the new light."));
        } else if done == sup.len() {
            self.hero_title.set_label(&if sup.len() == 1 { tr("Your camera is optimized") } else { trf("All {n} cameras are optimized", &[("n", &sup.len())]) });
            self.hero_text.set_label(&tr("Settings stay applied, also after Autodarts restarts. Run it again after changing the lights or moving the board."));
        } else {
            self.hero_title.set_label(&tr("Ready to optimize"));
            self.hero_text.set_label(&if sup.len() == 1 {
                tr("Calibrates exposure, gain and JPEG quality for 1 camera. Takes about 3 minutes.")
            } else {
                trf("Calibrates exposure, gain and JPEG quality for {n} cameras and matches them. Takes about 3 minutes.", &[("n", &sup.len())])
            });
        }
        let need = !sup.is_empty() && (done < sup.len() || !bad.is_empty());
        let base = if sup.len() > 1 { trf("Optimize {n} cameras", &[("n", &sup.len())]) } else { tr("Optimize camera") };
        self.btn_optimize.set_label(&if need { format!("{} · {}", base, tr("about 3 min")) } else { tr("Optimize again") });
        if need {
            self.btn_optimize.add_css_class("suggested-action");
        } else {
            self.btn_optimize.remove_css_class("suggested-action");
        }
        self.btn_optimize.set_sensitive(!sup.is_empty() && !running);
        self.btn_restore.set_visible(st.cams.iter().any(|c| c.s.optimized) && !running);
        self.btn_light.set_visible(done > 0 && !running);
        self.guide.set_visible(!running);
        self.banner.set_visible(!bad.is_empty() && !running);
        if let Some(first) = bad.first() {
            let names = bad.iter().map(|p| cw::cam_name(p)).collect::<Vec<_>>().join(", ");
            let pct = light[first]["deviation_pct"].as_f64().unwrap_or(0.0);
            let t = if pct > 0.0 { "{names}: {pct:.0f} % brighter than calibrated" } else { "{names}: {pct:.0f} % darker than calibrated" };
            self.banner_title.set_label(&trf(t, &[("names", &names), ("pct", &format!("{:.0}", pct.abs()))]));
        }
    }

    /// One table line; tapping it picks the camera (for Before & After and Controls).
    fn cam_row(self: &Rc<Self>, c: &CamInfo) -> gtk::Widget {
        let (kind, cells, tip): (&str, Vec<String>, String) = if !c.s.supported {
            ("bad", vec![tr("Not supported"), String::new(), String::new(), String::new()], c.s.reason.clone())
        } else if c.s.optimized {
            let white = c.white.map(|w| format!("{:.0} / {:.0}", w, WHITE_TARGET)).unwrap_or_else(|| "–".into());
            ("ok", vec![white, c.q.map(|q| q.to_string()).unwrap_or_else(|| "–".into()), when_text(c.when.as_deref())], trf("Firmware {fw}", &[("fw", &c.s.firmware)]))
        } else {
            ("warn", vec![tr("Not optimized"), String::new(), String::new()], trf("Firmware {fw}", &[("fw", &c.s.firmware)]))
        };
        let row = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        let name = status_dot_box(&cw::cam_name(&c.s.port), kind);
        name.set_size_request(COLS[0], -1);
        row.append(&name);
        for (t, w) in cells.iter().zip(COLS[1..].iter()) {
            let l = gtk::Label::new(Some(t));
            l.set_xalign(0.0);
            l.add_css_class("camopt-cell");
            l.set_ellipsize(pango::EllipsizeMode::End);
            l.set_size_request(*w, -1);
            row.append(&l);
        }
        let now = gtk::Label::new(None);
        now.set_xalign(0.0);
        now.set_hexpand(true);
        now.add_css_class("camopt-cell");
        if let (Some(r), true) = (self.lighting.borrow().get(&c.s.port), c.s.supported && c.s.optimized) {
            let pct = r["deviation_pct"].as_f64().unwrap_or(0.0);
            if r["verdict"].as_str() == Some("ok") {
                now.set_label(&trf("✓ OK ({pct:+.0f} %)", &[("pct", &format!("{:+.0}", pct))]));
                now.add_css_class("camopt-ok");
            } else {
                now.set_label(&trf(if pct > 0.0 { "{pct:.0f} % brighter" } else { "{pct:.0f} % darker" }, &[("pct", &format!("{:.0}", pct.abs()))]));
                now.add_css_class("camopt-warn");
            }
        }
        row.append(&now);
        let btn = gtk::Button::new();
        btn.add_css_class("flat");
        btn.set_child(Some(&row));
        if !tip.is_empty() {
            btn.set_tooltip_text(Some(&tr(&tip)));
        }
        let (m, p) = (self.clone(), c.s.port.clone());
        btn.connect_clicked(move |_| {
            if Some(&p) != m.strip.active().as_ref() {
                cw::set_selected(Some(p.clone()));
                m.strip.sync(&m.status.borrow().as_ref().unwrap().cams.iter().map(|c| StripEntry { port: c.s.port.clone(), kind: (if !c.s.supported { "bad" } else if c.s.optimized { "ok" } else { "warn" }).into() }).collect::<Vec<_>>());
                m.render_cams();
                m.on_ba_pick();
            }
        });
        btn.upcast()
    }

    // --- dialogs ---

    fn ask_consent(self: &Rc<Self>) {
        let mut lines = vec![];
        if let Some(st) = self.status.borrow().as_ref() {
            for c in &st.cams {
                let hbv = c.s.usb_id.as_deref() == Some(profiles::HBV_USB_ID);
                let what = if hbv {
                    tr("HBV camera detected")
                } else {
                    trf("other camera ({usb_id})", &[("usb_id", &c.s.usb_id.clone().unwrap_or_else(|| tr("unknown")))])
                };
                lines.push(format!("{}  {}: {}", if hbv { "✓" } else { "✗" }, cw::cam_name(&c.s.port), what));
            }
        }
        let mut body = tr("The optimizer uses hidden camera settings (JPEG quality, sensor gain) that were verified on HBV OV2710 cameras only.");
        if !lines.is_empty() {
            body += &format!("\n\n{}", lines.join("\n"));
        }
        let d = alert(&tr("Are these HBV OV2710 cameras?"), &body, &[("no", &tr("No / Not Sure")), ("yes", &tr("Yes, Continue"))], Some("yes"), None);
        let m = self.clone();
        ask(&self.win.window, d, move |r| {
            let yes = r == "yes";
            let m2 = m.clone();
            run_async(move || profiles::set_consent(yes), move |_| m2.refresh_status());
        });
    }

    fn ask_prepare(self: &Rc<Self>) {
        let d = alert(
            &tr("Start the Optimization?"),
            &tr("Autodarts stops for about 3 minutes and restarts by itself.\n\nDarts removed, lights on, hands away from the board?"),
            &[("cancel", &tr("Cancel")), ("start", &tr("Start"))],
            Some("start"),
            None,
        );
        let m = self.clone();
        ask(&self.win.window, d, move |r| {
            if r == "start" {
                m.start_job("optimize");
            }
        });
    }

    fn ask_discard(self: &Rc<Self>) {
        let d = alert(
            &tr("Discard This Calibration?"),
            &tr("The cameras keep the settings they run now. You can optimize again at any time."),
            &[("cancel", &tr("Cancel")), ("discard", &tr("Discard"))],
            None,
            Some("discard"),
        );
        let m = self.clone();
        ask(&self.win.window, d, move |r| {
            if r == "discard" {
                oneclick::discard();
                m.win.show_toast("Calibration discarded");
                m.refresh_status();
            }
        });
    }

    fn ask_restore(self: &Rc<Self>) {
        let d = alert(
            &tr("Restore Original Settings?"),
            &tr("Your cameras go back to the settings Autodarts uses. Autodarts will restart."),
            &[("cancel", &tr("Cancel")), ("restore", &tr("Restore"))],
            None,
            Some("restore"),
        );
        let m = self.clone();
        ask(&self.win.window, d, move |r| {
            if r == "restore" {
                m.start_job("restore");
            }
        });
    }

    fn check_lighting(self: &Rc<Self>) {
        let Some(st) = self.status.borrow().clone() else { return };
        let targets: Vec<(String, String)> = st.cams.iter().filter(|c| c.s.supported && c.s.optimized).map(|c| (c.s.device.clone(), c.s.port.clone())).collect();
        self.btn_light.set_sensitive(false);
        self.btn_light.set_label(&tr("Checking…"));
        let m = self.clone();
        run_async(
            move || {
                let mut out = HashMap::new();
                for (d, p) in targets {
                    if let Ok(r) = live::recheck(&d, false) {
                        if r.get("deviation_pct").is_some() {
                            out.insert(p, r);
                        }
                    }
                }
                live::stop_all();
                out
            },
            move |res| {
                m.btn_light.set_sensitive(true);
                m.btn_light.set_label(&tr("Check lighting"));
                if res.is_empty() {
                    m.win.show_toast("No picture to compare yet, try again");
                }
                *m.lighting.borrow_mut() = res;
                m.render_cams();
            },
        );
    }

    // --- job ---

    fn start_job(self: &Rc<Self>, name: &str) {
        live::stop_all();
        let started = match name {
            "optimize" => JOB.start(name, |j| oneclick::optimize(j, 30)),
            "apply" => JOB.start(name, oneclick::apply),
            _ => JOB.start(name, oneclick::restore),
        };
        if !started {
            self.win.show_toast("A camera job is already running");
            return;
        }
        *self.job_t0.borrow_mut() = Some(Instant::now());
        self.lighting.borrow_mut().clear();
        self.tab_toggle.set_active_name(Some("optimize"));
        self.start_job_poll();
    }

    fn start_job_poll(self: &Rc<Self>) {
        self.btn_optimize.set_sensitive(false);
        self.btn_restore.set_visible(false);
        self.btn_light.set_visible(false);
        self.banner.set_visible(false);
        if self.job_t0.borrow().is_none() {
            *self.job_t0.borrow_mut() = Some(Instant::now());
        }
        if self.job_timer.borrow().is_none() {
            let m = self.clone();
            let id = super::helpers::every(1000, move || m.poll_job());
            *self.job_timer.borrow_mut() = Some(id);
        }
        if self.preview_timer.borrow().is_none() {
            self.preview_seq.set(-1);
            let m = self.clone();
            let id = super::helpers::every(PREVIEW_MS, move || m.poll_preview());
            *self.preview_timer.borrow_mut() = Some(id);
        }
        self.render_job();
    }

    fn poll_preview(&self) -> bool {
        if !JOB.running() {
            *self.preview_timer.borrow_mut() = None;
            return false;
        }
        let (dev, phase, jpeg, seq) = PREVIEW.get();
        self.preview_phase.set_label(&tr(if phase.is_empty() { "Waiting for the cameras…" } else { &phase }));
        match jpeg {
            None => {
                self.preview_pic.set_paintable(None::<&gtk::gdk::Paintable>);
                self.preview_tag.set_visible(false);
            }
            Some(j) if seq as i64 != self.preview_seq.get() => {
                self.preview_seq.set(seq as i64);
                if let Some(img) = cvx::decode_jpeg(&j) {
                    self.preview_pic.set_paintable(cw::to_texture(&img, PREVIEW_VIEW).as_ref());
                }
                if dev != *self.preview_dev.borrow() {
                    self.preview_tag.set_label(&cw::cam_name(&profiles::port_key(&dev)));
                    *self.preview_dev.borrow_mut() = dev;
                }
                self.preview_tag.set_visible(true);
            }
            _ => {}
        }
        true
    }

    fn poll_job(self: &Rc<Self>) -> bool {
        self.render_job();
        if JOB.running() {
            return true;
        }
        *self.job_timer.borrow_mut() = None;
        *self.job_t0.borrow_mut() = None;
        let j = JOB.view();
        if j.name == "optimize" && j.result.as_object().map(|o| !o.is_empty()).unwrap_or(false) && j.error.is_empty() {
            self.tab_toggle.set_active_name(Some("compare")); // fills once refresh_status reloads the pictures
        }
        self.refresh_status();
        false
    }

    fn render_job(&self) {
        let j = JOB.view();
        if j.steps.is_empty() {
            return;
        }
        self.job_empty.set_visible(false);
        self.job_box.set_visible(true);
        let restoring = j.name == "restore";
        let applying = j.name == "apply";
        let watching = j.running && j.name == "optimize";
        self.cam_list.set_visible(!watching);
        self.preview_box.set_visible(watching);
        if j.running {
            self.hero_title.set_label(&tr(if restoring {
                "Restoring original settings…"
            } else if applying {
                "Applying the calibration…"
            } else {
                "Optimizing…"
            }));
            self.hero_text.set_label(&tr("Please don't touch the board or the cameras."));
            self.guide.set_visible(false);
            let el = self.job_t0.borrow().map(|t| t.elapsed().as_secs_f64()).unwrap_or(0.0);
            let eta = eta_s(&j.name);
            self.progress.set_fraction((el / eta).min(0.97));
            let left = (eta - el).max(0.0) as i64;
            let e = el as i64;
            self.lbl_time.set_label(&format!(
                "{}:{:02} · {}",
                e / 60,
                e % 60,
                if left > 0 { trf("about {time} left", &[("time", &format!("{}:{:02}", left / 60, left % 60))]) } else { tr("almost done") }
            ));
        } else {
            self.progress.set_fraction(if j.error.is_empty() { 1.0 } else { 0.0 });
            self.lbl_time.set_label("");
        }
        super::helpers::clear_box(&self.steps_box);
        for st in &j.steps {
            let line = gtk::Box::new(gtk::Orientation::Horizontal, 8);
            let icon: gtk::Widget = if st.state == "running" {
                adw::Spinner::new().upcast()
            } else {
                let (n, css) = match st.state.as_str() {
                    "done" => ("object-select-symbolic", "success"),
                    "skipped" => ("dialog-warning-symbolic", "warning"),
                    _ => ("window-close-symbolic", "error"),
                };
                let i = gtk::Image::from_icon_name(n);
                i.add_css_class(css);
                i.upcast()
            };
            icon.set_size_request(16, 16);
            icon.set_valign(gtk::Align::Start);
            line.append(&icon);
            let l = caption(&st.title, &[], true);
            l.set_max_width_chars(60);
            line.append(&l);
            self.steps_box.append(&line);
        }
        let adj = self.steps_scroll.vadjustment();
        glib::idle_add_local_once(move || adj.set_value(adj.upper()));
        self.log_view.buffer().set_text(&j.log.join("\n"));
        let (note, kind) = if !j.error.is_empty() {
            (trf("Something went wrong: {error}. Your previous settings are still in place.", &[("error", &tr(&j.error))]), "bad")
        } else if !j.running && applying && j.result.as_object().map(|o| !o.is_empty()).unwrap_or(false) {
            let r = &j.result;
            let mut n = trf("Applied. Saved as “{preset}”, loaded at every start.", &[("preset", &r["preset"].as_str().unwrap_or(""))]);
            if let Some(res) = r["resolution"].as_str() {
                n += &format!(" {}", trf("Autodarts now runs at {resolution}.", &[("resolution", &res)]));
            }
            match r["board"].as_str() {
                Some("started") => n += &format!(" {}", tr("Autodarts is calibrating the board at it.")),
                Some("needed") => n += &format!(" {}", tr("Calibrate the board in Autodarts at it.")),
                _ => {}
            }
            (n, "ok")
        } else if !j.running && !restoring && j.result.as_object().map(|o| !o.is_empty()).unwrap_or(false) {
            let vals: Vec<&Value> = j.result.as_object().unwrap().values().collect();
            let mut qs: Vec<String> = vals.iter().map(|r| r["mjpeg_q"].to_string()).collect();
            qs.sort();
            qs.dedup();
            let first = vals[0];
            let res = first["resolution"].as_str().unwrap_or("");
            let change = if first["resolution_change"].as_bool().unwrap_or(false) {
                format!(" {}", trf("Applying sets Autodarts to {resolution} (needs a new board calibration if it has none at it).", &[("resolution", &res)]))
            } else {
                String::new()
            };
            let mut n = if !res.is_empty() { trf("All cameras calibrated and matched at {resolution}", &[("resolution", &res)]) } else { tr("All cameras calibrated and matched") };
            if qs.len() == 1 {
                n += &format!(" · {}", trf("JPEG level {q}", &[("q", &qs[0])]));
            }
            n += &format!(".{} {}", change, tr("Review before & after, then apply."));
            (n, "review")
        } else if !j.running && restoring {
            (tr("Original settings restored."), "")
        } else {
            (String::new(), "")
        };
        self.job_note.set_label(&note);
        self.job_note.set_visible(!note.is_empty());
        self.btn_after.set_visible(kind == "ok" || kind == "review");
        self.btn_after.set_label(&tr(if kind == "review" { "Review & apply" } else { "See before & after" }));
        self.job_note.remove_css_class("camopt-note-ok");
        self.job_note.remove_css_class("camopt-note-bad");
        if !kind.is_empty() {
            self.job_note.add_css_class(&format!("camopt-note-{}", if kind == "review" { "ok" } else { kind }));
        }
    }

    // --- before / after ---

    fn load_before_after(self: &Rc<Self>) {
        let m = self.clone();
        run_async(oneclick::before_after_info, move |ba| {
            *m.ba.borrow_mut() = ba;
            m.on_ba_pick();
        });
    }

    fn on_ba_pick(self: &Rc<Self>) {
        let ba = self.ba.borrow().clone();
        let pending = ba.iter().any(|b| b.pending);
        self.ba_pending.set_visible(pending && !JOB.running());
        self.btn_ba_preset.set_visible(!pending);
        let name = self.strip.active();
        let info = name.as_ref().and_then(|n| ba.iter().find(|b| &b.port == n).cloned());
        let Some(info) = info else {
            self.ba_stack.set_visible_child_name("empty");
            self.ba_empty.set_label(&if ba.is_empty() {
                tr("Before and after pictures appear here after an optimization.")
            } else if let Some(n) = &name {
                trf("No before and after pictures for {camera} yet.", &[("camera", &cw::cam_name(n))])
            } else {
                String::new()
            });
            *self.ba_data.borrow_mut() = None;
            self.ba_key.borrow_mut().clear();
            self.ba_note.set_label("");
            self.render_stats();
            return;
        };
        let mut stamp = String::new();
        if let Some(t) = info.meta["after"]["time"].as_str() {
            stamp = format!("{} · ", trf(if info.pending { "Calibrated {when}" } else { "Optimized {when}" }, &[("when", &t)]));
        }
        self.ba_note.set_label(&format!("{}{}", stamp, tr("drag the handle · double-tap or scroll to zoom")));
        // the meta time is per minute: the file times tell a rerun within the same minute apart
        let stamps: Vec<u128> = ["before", "after"]
            .iter()
            .map(|w| std::fs::metadata(info.dir.join(format!("{}.jpg", w))).and_then(|m| m.modified()).map(|t| t.duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_nanos()).unwrap_or(0))
            .collect();
        let key = format!("{}{}{:?}", name.clone().unwrap_or_default(), info.meta, stamps);
        if key == *self.ba_key.borrow() {
            return;
        }
        *self.ba_key.borrow_mut() = key.clone();
        let m = self.clone();
        let i2 = info.clone();
        run_async(
            move || -> Option<(Mat, Mat, (f64, f64), [(f64, f64); 2], (Option<i64>, Option<i64>))> {
                let b = live::board_view(&i2.device, &i2.dir.join("before.jpg"))?;
                let a = live::board_view(&i2.device, &i2.dir.join("after.jpg"))?;
                if b.rows() != a.rows() || b.cols() != a.cols() {
                    return None;
                }
                let kb = |w: &str| std::fs::metadata(i2.dir.join(format!("{}.jpg", w))).map(|m| m.len() as f64 / 1024.0).unwrap_or(0.0);
                let (b, a) = crop_black(&b, &a, 6).ok()?;
                let stats = [img_stats(&b), img_stats(&a)];
                Some((b, a, (kb("before"), kb("after")), stats, i2.q))
            },
            move |d| {
                if key != *m.ba_key.borrow() {
                    return; // the camera (or its pictures) changed while loading
                }
                let data = d.map(|(b, a, kb, stats, q)| Rc::new(BaData { imgs: (b, a), kb, stats, q }));
                m.ba_stack.set_visible_child_name(if data.is_some() { "picture" } else { "empty" });
                if data.is_none() {
                    m.ba_empty.set_label(&tr("The before and after pictures cannot be compared."));
                }
                *m.ba_data.borrow_mut() = data;
                m.render_stats();
                m.render_ba();
            },
        );
    }

    fn render_stats(&self) {
        let t = &self.ba_table;
        while let Some(c) = t.first_child() {
            t.remove(&c);
        }
        let data = self.ba_data.borrow().clone();
        self.ba_chip.set_visible(false);
        self.btn_ba_preset.set_sensitive(data.is_some());
        let name = self.strip.active().map(|p| cw::cam_name(&p)).unwrap_or_default();
        self.ba_title.set_label(&if data.is_some() { format!("{} · {}", tr("RESULT"), name.to_uppercase()) } else { tr("RESULT") });
        let Some(d) = data else { return };
        let (b, a) = (d.stats[0], d.stats[1]);
        let q = |v: Option<i64>| v.map(|v| v.to_string()).unwrap_or_else(|| "–".into());
        let rows: [(&str, String, String, Option<bool>); 4] = [
            ("White level", format!("{:.0}", b.0), format!("{:.0}", a.0), Some(a.0 >= b.0)),
            ("Clipped", format!("{:.1} %", b.1), format!("{:.1} %", a.1), Some(a.1 <= b.1.max(1.0))),
            ("JPEG level", q(d.q.0), q(d.q.1), Some(true)),
            ("Size / frame", format!("{:.0} KB", d.kb.0), format!("{:.0} KB", d.kb.1), None),
        ];
        for (c, h) in ["", "BEFORE", "AFTER"].iter().enumerate() {
            let l = gtk::Label::new(Some(&tr(h)));
            l.set_xalign(if c > 0 { 1.0 } else { 0.0 });
            l.add_css_class("camopt-table-head");
            t.attach(&l, c as i32, 0, 1, 1);
        }
        for (r, (lab, bf, af, good)) in rows.iter().enumerate() {
            let r = r as i32 + 1;
            let l = gtk::Label::new(Some(&tr(lab)));
            l.set_xalign(0.0);
            l.add_css_class("camopt-cell");
            t.attach(&l, 0, r, 1, 1);
            let lb = gtk::Label::new(Some(bf));
            lb.set_xalign(1.0);
            lb.set_hexpand(true);
            lb.add_css_class("camopt-cell");
            t.attach(&lb, 1, r, 1, 1);
            let la = gtk::Label::new(Some(af));
            la.set_xalign(1.0);
            la.add_css_class("camopt-cell");
            if good == &Some(true) {
                la.add_css_class("camopt-ok");
            }
            t.attach(&la, 2, r, 1, 1);
        }
        let active = self.strip.active();
        let st = self.status.borrow();
        let reference = self.cam(None).and_then(|c| c.white);
        if let (Some(st), Some(rf)) = (st.as_ref(), reference) {
            let diffs: Vec<f64> = st.cams.iter().filter(|c| c.s.optimized && Some(&c.s.port) != active.as_ref()).filter_map(|c| c.white).map(|w| (rf - w).abs() / rf).collect();
            if !diffs.is_empty() {
                let worst = diffs.iter().cloned().fold(0.0, f64::max) * 100.0;
                if worst < 5.0 {
                    set_chip(&self.ba_chip, &trf("✓ Matches the other cameras within {pct:.0f} %", &[("pct", &format!("{:.0}", worst.max(1.0)))]), "ok");
                } else {
                    set_chip(&self.ba_chip, &trf("Differs from another camera by {pct:.0f} %", &[("pct", &format!("{:.0}", worst))]), "warn");
                }
                self.ba_chip.set_visible(true);
            }
        }
    }

    /// (x0, y0, w, h) of the zoomed view in full-picture pixels.
    fn crop(&self, d: &BaData) -> (f64, f64, f64, f64) {
        let (iw, ih) = (d.imgs.0.cols() as f64, d.imgs.0.rows() as f64);
        let z = self.ba_zoom.get() as f64;
        let (cw, ch) = (iw / z, ih / z);
        let c = self.ba_center.get();
        ((c.0 * iw - cw / 2.0).max(0.0).min(iw - cw), (c.1 * ih - ch / 2.0).max(0.0).min(ih - ch), cw, ch)
    }

    /// (x0, y0, w, h) of the picture inside the widget (ContentFit::Contain letterboxes it).
    fn shown(&self) -> (f64, f64, f64, f64) {
        let (w, h) = (self.ba_picture.width() as f64, self.ba_picture.height() as f64);
        let (ih, iw) = (self.ba_comp.get().0 as f64, self.ba_comp.get().1 as f64);
        let s = if w > 0.0 && h > 0.0 { (w / iw).min(h / ih) } else { 0.0 };
        ((w - iw * s) / 2.0, (h - ih * s) / 2.0, iw * s, ih * s)
    }

    fn pane(&self, x: f64, sx: f64, sw: f64) -> f64 {
        let f = if sw > 0.0 { ((x - sx) / sw).clamp(0.0, 1.0) } else { 0.0 };
        if *self.ba_mode.borrow() == "side" && f < 1.0 { (f * 2.0) % 1.0 } else { f }
    }

    /// Zoomed in: grab and move the picture. At 1x in split view the drag sets the divider.
    fn drag_begin(&self, x: f64) {
        if self.ba_data.borrow().is_none() {
            return;
        }
        if self.ba_zoom.get() > 1 {
            self.ba_drag_center.set(self.ba_center.get());
            self.ba_picture.set_cursor_from_name(Some("grabbing"));
        } else if *self.ba_mode.borrow() == "split" {
            self.divider_to(x);
        }
    }

    fn drag_update(&self, x0: f64, dx: f64, dy: f64) {
        let Some(d) = self.ba_data.borrow().clone() else { return };
        if self.ba_zoom.get() == 1 {
            if *self.ba_mode.borrow() == "split" {
                self.divider_to(x0 + dx);
            }
            return;
        }
        let (_, _, mut sw, sh) = self.shown();
        if sw <= 0.0 || sh <= 0.0 {
            return;
        }
        if *self.ba_mode.borrow() == "side" {
            sw /= 2.0;
        }
        let (cx, cy) = self.ba_drag_center.get();
        let z = self.ba_zoom.get() as f64;
        // the picture follows the finger: the view centre moves the other way
        self.ba_center.set((cx - dx / sw / z, cy - dy / sh / z));
        let (x, y, cw, ch) = self.crop(&d);
        let (iw, ih) = (d.imgs.0.cols() as f64, d.imgs.0.rows() as f64);
        self.ba_center.set(((x + cw / 2.0) / iw, (y + ch / 2.0) / ih)); // clamped to the picture edges
        self.render_ba();
    }

    fn divider_to(&self, x: f64) {
        let (sx, _, sw, _) = self.shown();
        if sw > 0.0 {
            self.ba_adj.set_value((100.0 * (x - sx) / sw).clamp(0.0, 100.0));
        }
    }

    fn update_cursor(&self) {
        self.ba_picture.set_cursor_from_name(if self.ba_zoom.get() > 1 { Some("grab") } else { None });
    }

    /// Zoom with the picture point under (x, y) as the new centre.
    fn zoom_at(&self, x: f64, y: f64, zoom: i32) {
        let Some(d) = self.ba_data.borrow().clone() else { return };
        let (sx, sy, sw, sh) = self.shown();
        if sw <= 0.0 || sh <= 0.0 {
            return;
        }
        let (x0, y0, cw, ch) = self.crop(&d);
        let (iw, ih) = (d.imgs.0.cols() as f64, d.imgs.0.rows() as f64);
        let fx = self.pane(x, sx, sw);
        let fy = ((y - sy) / sh).clamp(0.0, 1.0);
        self.ba_center.set(((x0 + fx * cw) / iw, (y0 + fy * ch) / ih));
        self.set_zoom(zoom);
    }

    fn set_zoom(&self, z: i32) {
        self.ba_zoom.set(z);
        if z == 1 {
            self.ba_center.set((0.5, 0.5));
        }
        if self.ba_zoom_toggle.active_name().map(|n| n.to_string()) != Some(z.to_string()) {
            self.ba_zoom_toggle.set_active_name(Some(&z.to_string()));
        }
        self.update_cursor();
        self.render_ba();
    }

    fn render_ba(&self) {
        let Some(d) = self.ba_data.borrow().clone() else {
            self.ba_picture.set_paintable(None::<&gtk::gdk::Paintable>);
            return;
        };
        let mode = self.ba_mode.borrow().clone();
        self.tag_before.set_visible(mode != "diff");
        self.tag_after.set_visible(mode != "diff");
        let r = (|| -> cvx::R<Mat> {
            let (x0, y0, cw, ch) = self.crop(&d);
            let (x0, y0, cw, ch) = (x0.round() as i32, y0.round() as i32, cw.round() as i32, ch.round() as i32);
            let cw = cw.min(d.imgs.0.cols() - x0);
            let ch = ch.min(d.imgs.0.rows() - y0);
            let b = cvx::crop(&d.imgs.0, x0, y0, cw, ch)?;
            let a = cvx::crop(&d.imgs.1, x0, y0, cw, ch)?;
            let wide = cw * if mode == "side" { 2 } else { 1 };
            let s = BA_VIEW / wide.max(ch) as f64;
            let interp = if s < 1.0 { imgproc::INTER_AREA } else { imgproc::INTER_CUBIC };
            let (nw, nh) = ((cw as f64 * s).round() as i32, (ch as f64 * s).round() as i32);
            let b = cvx::resize(&b, nw, nh, interp)?;
            let a = cvx::resize(&a, nw, nh, interp)?;
            Ok(match mode.as_str() {
                "side" => {
                    let gap = Mat::new_rows_cols_with_default(b.rows(), 4, b.typ(), Scalar::all(255.0))?;
                    let mut out = Mat::default();
                    let parts: Vector<Mat> = Vector::from_iter([b, gap, a]);
                    opencv::core::hconcat(&parts, &mut out)?;
                    out
                }
                "diff" => {
                    let mut df = Mat::default();
                    opencv::core::absdiff(&b, &a, &mut df)?;
                    let mut o = Mat::default();
                    opencv::core::convert_scale_abs(&df, &mut o, 3.0, 0.0)?;
                    o
                }
                _ => {
                    let x = (a.cols() as f64 * self.ba_adj.value() / 100.0) as i32;
                    let mut comp = a.try_clone()?;
                    if x > 0 {
                        let left = cvx::crop(&b, 0, 0, x.min(b.cols()), b.rows())?;
                        let mut roi = Mat::roi_mut(&mut comp, opencv::core::Rect::new(0, 0, x.min(b.cols()), b.rows()))?;
                        left.copy_to(&mut roi)?;
                    }
                    let white = cvx::bgr(255., 255., 255.);
                    let bottom = comp.rows() - 1;
                    cvx::line(&mut comp, (x, 0), (x, bottom), white, 2)?;
                    let (cy, r) = (comp.rows() / 2, (comp.rows() / 28).max(14));
                    imgproc::circle(&mut comp, Point::new(x, cy), r, white, -1, imgproc::LINE_AA, 0)?;
                    let k = r / 2;
                    for sgn in [-1, 1] {
                        // two small arrows pointing apart
                        let tip = Point::new(x + sgn * (k + k / 2), cy);
                        let pts: Vector<Point> = Vector::from_iter([tip, Point::new(x + sgn * k / 4, cy - k / 2), Point::new(x + sgn * k / 4, cy + k / 2)]);
                        let poly: Vector<Vector<Point>> = Vector::from_iter([pts]);
                        imgproc::fill_poly(&mut comp, &poly, cvx::bgr(40., 40., 40.), imgproc::LINE_AA, 0, Point::new(0, 0))?;
                    }
                    comp
                }
            })
        })();
        match r {
            Ok(comp) => {
                self.ba_comp.set((comp.rows(), comp.cols()));
                self.ba_picture.set_paintable(super::helpers::mat_texture(&comp).as_ref());
            }
            Err(e) => log::warn!("before/after render failed: {}", e),
        }
    }
}

/// Cut the black margin (outside the warped board view) so the board fills the view.
fn crop_black(b: &Mat, a: &Mat, pad: i32) -> cvx::R<(Mat, Mat)> {
    let (gb, ga) = (cvx::gray(b)?, cvx::gray(a)?);
    let mut lum = Mat::default();
    opencv::core::max(&gb, &ga, &mut lum)?;
    let mut mask = Mat::default();
    imgproc::threshold(&lum, &mut mask, 14.0, 255.0, imgproc::THRESH_BINARY)?;
    if cvx::count(&mask) < 100 {
        return Ok((b.clone(), a.clone()));
    }
    let mut pts = Vector::<Point>::new();
    opencv::core::find_non_zero(&mask, &mut pts)?;
    let r = imgproc::bounding_rect(&pts)?;
    let (y0, y1) = ((r.y - pad).max(0), (r.y + r.height + pad).min(lum.rows()));
    let (x0, x1) = ((r.x - pad).max(0), (r.x + r.width + pad).min(lum.cols()));
    Ok((cvx::crop(b, x0, y0, x1 - x0, y1 - y0)?, cvx::crop(a, x0, y0, x1 - x0, y1 - y0)?))
}

/// (white level, clipped %) of the picture itself, not the black margin.
fn img_stats(img: &Mat) -> (f64, f64) {
    let g = match cvx::gray(img) {
        Ok(g) => g,
        Err(_) => return (0.0, 0.0),
    };
    let v = cvx::values_f32(&g).unwrap_or_default();
    let mut px: Vec<f32> = v.iter().cloned().filter(|&p| p > 14.0).collect();
    if px.is_empty() {
        px = v;
    }
    let clip = 100.0 * px.iter().filter(|&&p| p >= 250.0).count() as f64 / px.len().max(1) as f64;
    (cvx::percentile(&px, 97.0), clip)
}
