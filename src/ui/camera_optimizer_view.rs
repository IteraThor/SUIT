//! Camera Optimizer page: one-click calibration of HBV cameras (core::hbvcal; models in core::hbvcal::models).
//!
//! One centred screen at a time, like the Wireless Screen wizard, picked from the status: Home (the cameras, the
//! resolution, Optimize) -> Working (live picture, progress) -> Review (before & after, Apply or Discard) ->
//! Result. Before & after of the last run stays reachable from Home.
use super::camera_controls_view::ask_save_preset;
use super::camera_widgets::{self as cw, segmented};
use super::helpers::{alert, ask, clear_box, every, run_async};
use super::{Page, Win};
use crate::core::cvx;
use crate::core::hbvcal::autocal::PREVIEW;
use crate::core::hbvcal::daemon::cameras;
use crate::core::hbvcal::oneclick::{self, JobView, Pictures, Support, JOB};
use crate::core::hbvcal::{live, models, profiles};
use crate::core::i18n::{get_language, tr, trf};
use adw::prelude::*;
use gtk::glib;
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
/// Screens in order; the progress dots follow it ("declined" shares the dot of "home").
const SCREENS: [&str; 4] = ["home", "working", "review", "result"];
/// width of the camera list and the resolution row
const LIST_W: i32 = 500;

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
    when: Option<String>,
}

#[derive(Clone)]
struct Status {
    cams: Vec<CamInfo>,
    consent: Option<Value>,
    pending: bool,
    /// resolutions every supported camera offers (largest first) and Autodarts' current one
    sizes: Vec<(u32, u32)>,
    ad_size: (u32, u32),
}

struct BaData {
    imgs: (Mat, Mat),
    kb: (f64, f64),
    stats: [(f64, f64); 2],
    q: (Option<i64>, Option<i64>),
}

/// Centred text line (wrapped).
fn line(text: &str, classes: &[&str]) -> gtk::Label {
    let l = gtk::Label::new(Some(text));
    l.set_wrap(true);
    l.set_justify(gtk::Justification::Center);
    l.set_max_width_chars(64);
    for c in classes {
        l.add_css_class(c);
    }
    l
}

/// Centred screen: big icon (hidden for ""), short title, one dim line; extra widgets go below.
fn screen(icon: &str, title: &str) -> (gtk::Box, gtk::Image, gtk::Label, gtk::Label) {
    let b = gtk::Box::new(gtk::Orientation::Vertical, 14);
    b.set_valign(gtk::Align::Center);
    b.set_halign(gtk::Align::Center);
    b.set_margin_start(24);
    b.set_margin_end(24);
    b.set_margin_top(18);
    b.set_margin_bottom(18);
    let img = gtk::Image::from_icon_name(icon);
    img.set_pixel_size(80);
    img.add_css_class("dim-label");
    img.set_visible(!icon.is_empty());
    b.append(&img);
    let t = line(&tr(title), &["title-1"]);
    t.set_max_width_chars(36);
    b.append(&t);
    let l = line("", &["dim-label"]);
    b.append(&l);
    (b, img, t, l)
}

/// The screen icon and its colour ("success", "warning", "error"; "" = dim).
fn set_icon(img: &gtk::Image, icon: &str, tone: &str) {
    img.set_icon_name(Some(icon));
    for c in ["success", "warning", "error", "dim-label"] {
        img.remove_css_class(c);
    }
    img.add_css_class(if tone.is_empty() { "dim-label" } else { tone });
}

fn big_button(label: &str) -> gtk::Button {
    let b = gtk::Button::with_label(&tr(label));
    b.add_css_class("suggested-action");
    b.add_css_class("pill");
    b.set_size_request(280, 52);
    b.set_halign(gtk::Align::Center);
    b.set_margin_top(6);
    b
}

fn flat(label: &str) -> gtk::Button {
    let b = gtk::Button::with_label(&tr(label));
    b.add_css_class("flat");
    b.set_valign(gtk::Align::Center);
    b
}

fn boxed() -> gtk::ListBox {
    let l = gtk::ListBox::new();
    l.add_css_class("boxed-list");
    l.set_selection_mode(gtk::SelectionMode::None);
    l.set_size_request(LIST_W, -1);
    l
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
    /// the result screen of the last job was closed with Done
    job_seen: Cell<bool>,
    /// Before & after opened from Home (not a calibration waiting for review)
    browsing: Cell<bool>,
    /// the resolution picked for the next optimization (None: the default)
    size: Cell<Option<(u32, u32)>>,
    /// the sizes the resolution row lists; true while the row is filled (its change handler waits)
    sizes: RefCell<Vec<(u32, u32)>>,
    sizes_sync: Cell<bool>,
    lighting: RefCell<HashMap<String, Value>>,
    ba: RefCell<Vec<Pictures>>,
    ba_ports: RefCell<Vec<String>>,
    ba_data: RefCell<Option<Rc<BaData>>>,
    ba_key: RefCell<String>,
    ba_zoom: Cell<i32>,
    ba_mode: RefCell<String>,
    ba_center: Cell<(f64, f64)>,
    ba_comp: Cell<(i32, i32)>,
    ba_drag_center: Cell<(f64, f64)>,
    ba_pointer: Cell<Option<(f64, f64)>>,
    stack: gtk::Stack,
    dots_box: gtk::Box,
    dots: Vec<gtk::Label>,
    home_icon: gtk::Image,
    home_title: gtk::Label,
    home_line: gtk::Label,
    cam_list: gtk::ListBox,
    size_list: gtk::ListBox,
    size_row: adw::ComboRow,
    btn_optimize: gtk::Button,
    btn_ba: gtk::Button,
    btn_light: gtk::Button,
    btn_restore: gtk::Button,
    work_title: gtk::Label,
    work_line: gtk::Label,
    preview_frame: gtk::Overlay,
    preview_pic: gtk::Picture,
    preview_tag: gtk::Label,
    progress: gtk::ProgressBar,
    lbl_time: gtk::Label,
    steps_box: gtk::Box,
    res_icon: gtk::Image,
    res_title: gtk::Label,
    res_line: gtk::Label,
    log_view: gtk::TextView,
    rev_back: gtk::Button,
    rev_title: gtk::Label,
    rev_line: gtk::Label,
    ba_cams: gtk::Box,
    ba_stack: gtk::Stack,
    ba_empty: gtk::Label,
    ba_picture: gtk::Picture,
    tag_before: gtk::Label,
    tag_after: gtk::Label,
    ba_zoom_toggle: adw::ToggleGroup,
    ba_adj: gtk::Adjustment,
    ba_stats: gtk::Label,
    rev_pending: gtk::Box,
    rev_done: gtk::Box,
    btn_ba_preset: gtk::Button,
}

impl Page for CameraOptimizerPage {
    fn nav(&self) -> &adw::NavigationPage {
        &self.nav
    }

    fn on_opened(self: Rc<Self>) {
        self.refresh_status();
        if JOB.running() {
            self.start_job_poll();
        }
    }

    fn on_closed(self: Rc<Self>) {
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
        let stack = gtk::Stack::new();
        stack.set_transition_type(gtk::StackTransitionType::Crossfade);
        stack.set_vexpand(true);
        // each screen as tall as it needs (a homogeneous stack would make short screens scroll like the longest)
        stack.set_vhomogeneous(false);
        stack.set_hhomogeneous(false);

        // Declined
        let (b, _, _, l) = screen("camera-disabled-symbolic", "Optimization is off");
        l.set_label(&tr("This tool changes hidden settings that were only tested on HBV cameras, so it stays off for other cameras."));
        let b_yes = big_button("I do use HBV cameras");
        b_yes.remove_css_class("suggested-action");
        b.append(&b_yes);
        stack.add_named(&b, Some("declined"));

        // Home: the cameras, the resolution, Optimize
        let (b, home_icon, home_title, home_line) = screen("camera-web-symbolic", "Loading…");
        let cam_list = boxed();
        cam_list.set_margin_top(6);
        b.append(&cam_list);
        let size_list = boxed();
        let size_row = adw::ComboRow::new();
        size_row.set_title(&tr("Resolution"));
        size_list.append(&size_row);
        b.append(&size_list);
        let btn_optimize = big_button("Optimize cameras");
        b.append(&btn_optimize);
        b.append(&line(&tr("Remove all darts and turn the lights on the way you play. Keep hands away from the board while it runs."), &["caption", "dim-label"]));
        let more = gtk::Box::new(gtk::Orientation::Horizontal, 4);
        more.set_halign(gtk::Align::Center);
        let btn_ba = flat("Before & After");
        let btn_light = flat("Check lighting");
        let btn_controls = flat("Camera Controls");
        let btn_restore = flat("Restore…");
        for w in [&btn_ba, &btn_light, &btn_controls, &btn_restore] {
            more.append(w);
        }
        b.append(&more);
        stack.add_named(&b, Some("home"));

        // Working: the live picture takes the icon's place
        let (b, _, work_title, work_line) = screen("", "Optimizing…");
        let preview_pic = gtk::Picture::new();
        preview_pic.set_content_fit(gtk::ContentFit::Contain);
        preview_pic.set_can_shrink(true);
        preview_pic.set_size_request(560, 315);
        preview_pic.add_css_class("camopt-frame");
        let preview_frame = gtk::Overlay::new();
        preview_frame.set_child(Some(&preview_pic));
        preview_frame.set_halign(gtk::Align::Center);
        let preview_tag = gtk::Label::new(None);
        preview_tag.set_halign(gtk::Align::Start);
        preview_tag.set_valign(gtk::Align::Start);
        preview_tag.add_css_class("camopt-tag");
        preview_tag.set_visible(false);
        preview_tag.set_margin_top(10);
        preview_tag.set_margin_start(10);
        preview_frame.add_overlay(&preview_tag);
        b.prepend(&preview_frame);
        let progress = gtk::ProgressBar::new();
        progress.set_size_request(360, -1);
        progress.set_halign(gtk::Align::Center);
        b.append(&progress);
        let lbl_time = line("", &["caption", "dim-label"]);
        b.append(&lbl_time);
        b.append(&line(&tr("Please don't touch the board or the cameras."), &["caption"]));
        let steps_box = gtk::Box::new(gtk::Orientation::Vertical, 6);
        steps_box.set_margin_top(6);
        let exp = gtk::Expander::new(Some(&tr("Details")));
        exp.set_child(Some(&steps_box));
        exp.set_halign(gtk::Align::Center);
        b.append(&exp);
        stack.add_named(&b, Some("working"));

        // Result of apply / restore / a failed run
        let (b, res_icon, res_title, res_line) = screen("object-select-symbolic", "");
        let log_view = gtk::TextView::new();
        log_view.set_editable(false);
        log_view.set_monospace(true);
        log_view.set_wrap_mode(gtk::WrapMode::WordChar);
        log_view.add_css_class("camopt-log");
        let ls = gtk::ScrolledWindow::new();
        ls.set_min_content_width(560);
        ls.set_min_content_height(220);
        ls.set_child(Some(&log_view));
        let exp = gtk::Expander::new(Some(&tr("Details")));
        exp.set_child(Some(&ls));
        exp.set_halign(gtk::Align::Center);
        b.append(&exp);
        let btn_done = big_button("Done");
        b.append(&btn_done);
        stack.add_named(&b, Some("result"));

        // Review: before & after, Apply or Discard (or back to Home when opened from there)
        let b = gtk::Box::new(gtk::Orientation::Vertical, 10);
        b.set_margin_start(24);
        b.set_margin_end(24);
        b.set_margin_top(6);
        b.set_margin_bottom(12);
        let rev_back = gtk::Button::new();
        rev_back.set_child(Some(&adw::ButtonContent::builder().icon_name("go-previous-symbolic").label(tr("Back")).build()));
        rev_back.add_css_class("flat");
        rev_back.set_halign(gtk::Align::Start);
        b.append(&rev_back);
        let rev_title = line("", &["title-2"]);
        b.append(&rev_title);
        let rev_line = line("", &["dim-label"]);
        rev_line.set_max_width_chars(90);
        b.append(&rev_line);
        let bar = gtk::Box::new(gtk::Orientation::Horizontal, 12);
        bar.set_halign(gtk::Align::Center);
        let ba_cams = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        bar.append(&ba_cams);
        b.append(&bar);
        let ba_stack = gtk::Stack::new();
        ba_stack.set_vexpand(true);
        let ba_empty = line(&tr("Before and after pictures appear here after an optimization."), &["dim-label"]);
        ba_stack.add_named(&ba_empty, Some("empty"));
        let ba_picture = gtk::Picture::new();
        ba_picture.set_content_fit(gtk::ContentFit::Contain);
        ba_picture.set_can_shrink(true);
        ba_picture.set_size_request(-1, 360);
        ba_picture.set_tooltip_text(Some(&tr("Double-tap or scroll to zoom into a spot; drag to move around when zoomed")));
        ba_picture.add_css_class("camopt-frame");
        let bov = gtk::Overlay::new();
        bov.set_child(Some(&ba_picture));
        // as wide as the picture: the tags sit on its corners, no black bars beside it
        bov.set_halign(gtk::Align::Center);
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
        b.append(&ba_stack);
        let ba_stats = line("", &["caption", "dim-label"]);
        ba_stats.set_max_width_chars(120);
        b.append(&ba_stats);
        let ba_adj = gtk::Adjustment::new(50.0, 0.0, 100.0, 1.0, 10.0, 0.0);
        let rev_pending = gtk::Box::new(gtk::Orientation::Horizontal, 12);
        rev_pending.set_halign(gtk::Align::Center);
        let btn_discard = flat("Discard");
        btn_discard.set_tooltip_text(Some(&tr("Forget this calibration and keep the previous settings")));
        let btn_apply = big_button("Apply now & set as default");
        btn_apply.set_margin_top(0);
        btn_apply.set_tooltip_text(Some(&tr("Keep this calibration, save it as a preset and load that preset at every start")));
        rev_pending.append(&btn_discard);
        rev_pending.append(&btn_apply);
        b.append(&rev_pending);
        let rev_done = gtk::Box::new(gtk::Orientation::Horizontal, 12);
        rev_done.set_halign(gtk::Align::Center);
        let btn_ba_preset = gtk::Button::with_label(&tr("Save as Preset…"));
        btn_ba_preset.add_css_class("pill");
        let btn_fine = flat("Fine-tune in Camera Controls →");
        rev_done.append(&btn_ba_preset);
        rev_done.append(&btn_fine);
        b.append(&rev_done);
        stack.add_named(&b, Some("review"));

        let dots_box = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        dots_box.set_halign(gtk::Align::Center);
        dots_box.set_margin_top(8);
        dots_box.set_margin_bottom(14);
        let dots: Vec<gtk::Label> = SCREENS.iter().map(|_| gtk::Label::new(Some("●"))).collect();
        for d in &dots {
            dots_box.append(d);
        }
        let scrolled = gtk::ScrolledWindow::builder().hscrollbar_policy(gtk::PolicyType::Never).vexpand(true).child(&stack).build();
        let root = gtk::Box::new(gtk::Orientation::Vertical, 0);
        root.append(&scrolled);
        root.append(&dots_box);
        let nav = adw::NavigationPage::builder().title(tr("Camera Optimizer")).tag("camera_optimizer").child(&root).build();

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
            job_seen: Cell::new(false),
            browsing: Cell::new(false),
            size: Cell::new(None),
            sizes: RefCell::new(vec![]),
            sizes_sync: Cell::new(false),
            lighting: RefCell::new(HashMap::new()),
            ba: RefCell::new(vec![]),
            ba_ports: RefCell::new(vec![]),
            ba_data: RefCell::new(None),
            ba_key: RefCell::new(String::new()),
            ba_zoom: Cell::new(1),
            ba_mode: RefCell::new("split".into()),
            ba_center: Cell::new((0.5, 0.5)),
            ba_comp: Cell::new((1, 1)),
            ba_drag_center: Cell::new((0.5, 0.5)),
            ba_pointer: Cell::new(None),
            stack,
            dots_box,
            dots,
            home_icon,
            home_title,
            home_line,
            cam_list,
            size_list,
            size_row,
            btn_optimize,
            btn_ba,
            btn_light,
            btn_restore,
            work_title,
            work_line,
            preview_frame,
            preview_pic,
            preview_tag,
            progress,
            lbl_time,
            steps_box,
            res_icon,
            res_title,
            res_line,
            log_view,
            rev_back,
            rev_title,
            rev_line,
            ba_cams,
            ba_stack,
            ba_empty,
            ba_picture,
            tag_before,
            tag_after,
            ba_zoom_toggle,
            ba_adj,
            ba_stats,
            rev_pending,
            rev_done,
            btn_ba_preset,
        });
        let m = me.clone();
        b_yes.connect_clicked(move |_| m.ask_consent());
        let m = me.clone();
        me.btn_optimize.connect_clicked(move |_| m.ask_prepare());
        let m = me.clone();
        me.btn_ba.connect_clicked(move |_| {
            m.browsing.set(true);
            m.render_screen();
        });
        let m = me.clone();
        me.btn_light.connect_clicked(move |_| m.check_lighting());
        let w = win.clone();
        btn_controls.connect_clicked(move |_| w.open_camera_controls_page());
        let m = me.clone();
        me.btn_restore.connect_clicked(move |_| m.ask_restore());
        let m = me.clone();
        me.size_row.connect_selected_notify(move |r| {
            if !m.sizes_sync.get() {
                let s = m.sizes.borrow().get(r.selected() as usize).copied();
                m.size.set(s);
            }
        });
        let m = me.clone();
        btn_done.connect_clicked(move |_| {
            m.job_seen.set(true);
            m.render_screen();
        });
        let m = me.clone();
        me.rev_back.connect_clicked(move |_| {
            m.browsing.set(false);
            m.render_screen();
        });
        let m = me.clone();
        btn_apply.connect_clicked(move |_| m.start_job("apply"));
        let m = me.clone();
        btn_discard.connect_clicked(move |_| m.ask_discard());
        let w = win.clone();
        me.btn_ba_preset.connect_clicked(move |_| ask_save_preset(&w, &trf("Optimized {when}", &[("when", &chrono::Local::now().format("%Y-%m-%d %H:%M"))])));
        let w = win.clone();
        btn_fine.connect_clicked(move |_| w.open_camera_controls_page());
        let m = Rc::downgrade(&me);
        let seg = segmented(
            &cw::opts(&[("split", "Split"), ("side", "Side by side"), ("diff", "Difference")]),
            "split",
            Rc::new(move |mode: String| {
                if let Some(m) = m.upgrade() {
                    *m.ba_mode.borrow_mut() = mode;
                    m.render_ba();
                }
            }),
        );
        bar.append(&seg);
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
        me.render_screen();
        me
    }

    fn cam(&self, port: &str) -> Option<CamInfo> {
        self.status.borrow().as_ref()?.cams.iter().find(|c| c.s.port == port).cloned()
    }

    fn consented(&self) -> bool {
        self.status.borrow().as_ref().and_then(|s| s.consent.as_ref().map(|c| c["hbv_confirmed"].as_bool().unwrap_or(false))).unwrap_or(false)
    }

    /// Connected camera models with hidden settings the user has not confirmed yet.
    fn unconfirmed_models(st: &Status) -> Vec<&'static str> {
        let done = profiles::consented_models();
        let mut v: Vec<&'static str> = st
            .cams
            .iter()
            .filter_map(|c| c.s.model_key.as_deref().and_then(models::by_key))
            .filter(|m| m.hidden_capable() && !done.iter().any(|k| k == m.key))
            .map(|m| m.key)
            .collect();
        v.dedup();
        v
    }

    /// The resolution the next optimization runs at.
    fn chosen_size(&self) -> Option<(u32, u32)> {
        let st = self.status.borrow();
        let st = st.as_ref()?;
        match self.size.get() {
            Some(s) if st.sizes.contains(&s) => Some(s),
            _ => oneclick::default_size(&st.sizes, st.ad_size),
        }
    }

    /// The camera the before & after view shows: the selected one when it has pictures, else the first.
    fn active_port(&self) -> Option<String> {
        let ba = self.ba.borrow();
        match cw::selected() {
            Some(s) if ba.iter().any(|b| b.port == s) => Some(s),
            _ => ba.first().map(|b| b.port.clone()),
        }
    }

    pub fn refresh_status(self: &Rc<Self>) {
        if self.status_busy.replace(true) {
            return;
        }
        let m = self.clone();
        run_async(
            || {
                let cams: Vec<CamInfo> = cameras()
                    .iter()
                    .map(|d| {
                        let s = oneclick::support(d);
                        let mut c = CamInfo { s, white: None, when: None };
                        if c.s.supported && c.s.optimized {
                            if let Some(p) = profiles::get(d) {
                                let meta = &p["meta"];
                                c.white = meta["reference"]["white"].as_f64();
                                c.when = meta["calibrated"].as_str().map(String::from);
                            }
                        }
                        c
                    })
                    .collect();
                let sup: Vec<String> = cams.iter().filter(|c: &&CamInfo| c.s.supported).map(|c| c.s.device.clone()).collect();
                Status {
                    sizes: oneclick::size_choices(&sup),
                    ad_size: crate::core::hbvcal::autodarts::stream_size(),
                    cams,
                    consent: profiles::consent(),
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
        let declined = st.consent.is_some() && !self.consented();
        if !declined && !Self::unconfirmed_models(&st).is_empty() && !self.consent_asked.replace(true) {
            self.ask_consent();
        }
        self.render_home();
        self.render_job();
        self.render_screen();
        self.load_before_after();
    }

    /// The screen for the current state.
    fn screen_name(&self) -> &'static str {
        let Some(st) = self.status.borrow().clone() else { return "home" };
        if st.consent.is_some() && !self.consented() {
            return "declined";
        }
        if JOB.running() {
            return "working";
        }
        let j = JOB.view();
        // a finished optimization goes on to its review; apply, restore and errors show their result
        if !j.steps.is_empty() && !self.job_seen.get() && !(j.name == "optimize" && j.error.is_empty()) {
            return "result";
        }
        if st.pending || self.browsing.get() {
            return "review";
        }
        "home"
    }

    fn render_screen(&self) {
        let name = self.screen_name();
        if self.stack.visible_child_name().as_deref() != Some(name) {
            self.stack.set_visible_child_name(name);
        }
        let browsing = name == "review" && !self.status.borrow().as_ref().is_some_and(|s| s.pending);
        self.dots_box.set_visible(name != "declined" && !browsing);
        let at = SCREENS.iter().position(|s| *s == name).unwrap_or(0);
        for (i, d) in self.dots.iter().enumerate() {
            d.set_opacity(if i == at { 1.0 } else { 0.3 });
        }
    }

    fn render_home(self: &Rc<Self>) {
        let Some(st) = self.status.borrow().clone() else { return };
        self.cam_list.remove_all();
        for c in &st.cams {
            self.cam_list.append(&self.cam_row(c));
        }
        self.cam_list.set_visible(!st.cams.is_empty());
        let sup: Vec<&CamInfo> = st.cams.iter().filter(|c| c.s.supported).collect();
        let done = sup.iter().filter(|c| c.s.optimized).count();
        let light = self.lighting.borrow();
        let bad: Vec<String> = light.iter().filter(|(_, r)| r["verdict"].as_str().map(|v| v != "ok").unwrap_or(false)).map(|(p, _)| p.clone()).collect();
        let (icon, tone, title, text) = if sup.is_empty() {
            ("camera-disabled-symbolic", "", tr("No supported camera found"), tr("Connect your HBV cameras, then open this page again."))
        } else if let Some(first) = bad.first() {
            let names = bad.iter().map(|p| cw::cam_name(p)).collect::<Vec<_>>().join(", ");
            let pct = light[first]["deviation_pct"].as_f64().unwrap_or(0.0);
            let t = if pct > 0.0 { "{names}: {pct:.0f} % brighter than calibrated" } else { "{names}: {pct:.0f} % darker than calibrated" };
            let what = trf(t, &[("names", &names), ("pct", &format!("{:.0}", pct.abs()))]);
            ("dialog-warning-symbolic", "warning", tr("The light has changed"), format!("{}. {}", what, tr("Optimize again to match the cameras to the new light.")))
        } else if done == sup.len() {
            let t = if sup.len() == 1 { tr("Your camera is optimized") } else { trf("All {n} cameras are optimized", &[("n", &sup.len())]) };
            ("object-select-symbolic", "success", t, tr("Settings stay applied, also after Autodarts restarts. Run it again after changing the lights or moving the board."))
        } else {
            // JPEG quality only where a camera lets us set it (full mode)
            let jpeg = sup.iter().any(|c| c.s.mode == "full");
            let t = match (sup.len() == 1, jpeg) {
                (true, true) => tr("Calibrates exposure, gain and JPEG quality for 1 camera. Takes about 3 minutes."),
                (true, false) => tr("Calibrates exposure and gain for 1 camera. Takes about 3 minutes."),
                (false, true) => trf("Calibrates exposure, gain and JPEG quality for {n} cameras and matches them. Takes about 3 minutes.", &[("n", &sup.len())]),
                (false, false) => trf("Calibrates exposure and gain for {n} cameras and matches them. Takes about 3 minutes.", &[("n", &sup.len())]),
            };
            ("camera-web-symbolic", "", tr("Ready to optimize"), t)
        };
        set_icon(&self.home_icon, icon, tone);
        self.home_title.set_label(&title);
        self.home_line.set_label(&text);
        let need = !sup.is_empty() && (done < sup.len() || !bad.is_empty());
        let base = if sup.len() > 1 { trf("Optimize {n} cameras", &[("n", &sup.len())]) } else { tr("Optimize camera") };
        self.btn_optimize.set_label(&if need { base } else { tr("Optimize again") });
        if need {
            self.btn_optimize.add_css_class("suggested-action");
        } else {
            self.btn_optimize.remove_css_class("suggested-action");
        }
        self.btn_optimize.set_visible(!sup.is_empty());
        self.btn_optimize.set_sensitive(!JOB.running());
        self.btn_restore.set_visible(st.cams.iter().any(|c| c.s.optimized));
        self.btn_light.set_visible(done > 0);
        self.btn_ba.set_visible(!self.ba.borrow().is_empty());
        self.render_sizes(&st);
    }

    fn render_sizes(&self, st: &Status) {
        self.size_list.set_visible(!st.sizes.is_empty());
        if st.sizes.is_empty() {
            return;
        }
        let cur = self.chosen_size().unwrap_or(st.sizes[0]);
        self.sizes_sync.set(true);
        if *self.sizes.borrow() != st.sizes {
            *self.sizes.borrow_mut() = st.sizes.clone();
            let texts: Vec<String> = st.sizes.iter().map(|s| oneclick::size_label(*s)).collect();
            let refs: Vec<&str> = texts.iter().map(|s| s.as_str()).collect();
            self.size_row.set_model(Some(&gtk::StringList::new(&refs)));
        }
        self.size_row.set_selected(st.sizes.iter().position(|s| *s == cur).unwrap_or(0) as u32);
        self.sizes_sync.set(false);
        self.size_row.set_subtitle(&trf("Autodarts now: {size}", &[("size", &oneclick::size_label(st.ad_size))]));
    }

    /// One camera: name, model and state; the lighting check result on the right.
    fn cam_row(&self, c: &CamInfo) -> adw::ActionRow {
        let model = c.s.model.clone().unwrap_or_default();
        let with_model = |s: String| if model.is_empty() { s } else { format!("{} · {}", model, s) };
        let (icon, tone, sub) = if !c.s.supported {
            ("dialog-error-symbolic", "error", if c.s.reason.is_empty() { tr("Not supported") } else { tr(&c.s.reason) })
        } else if let Some(other) = &c.s.mismatch {
            // name the connected model too: the old profile's model alone reads as if the camera were that one
            ("dialog-warning-symbolic", "warning", trf("{model} detected · old profile from {other} not applied", &[("model", &c.s.model.as_deref().unwrap_or("?")), ("other", other)]))
        } else if c.s.optimized {
            ("object-select-symbolic", "success", with_model(trf("Optimized {when}", &[("when", &when_text(c.when.as_deref()))])))
        } else {
            ("camera-web-symbolic", "", with_model(tr("Not optimized")))
        };
        let row = adw::ActionRow::new();
        row.set_use_markup(false);
        row.set_title(&cw::cam_name(&c.s.port));
        row.set_subtitle(&sub);
        let img = gtk::Image::new();
        img.set_pixel_size(22);
        set_icon(&img, icon, tone);
        row.add_prefix(&img);
        if let (Some(r), true) = (self.lighting.borrow().get(&c.s.port), c.s.supported && c.s.optimized) {
            let pct = r["deviation_pct"].as_f64().unwrap_or(0.0);
            let ok = r["verdict"].as_str() == Some("ok");
            let text = if ok {
                trf("✓ OK ({pct:+.0f} %)", &[("pct", &format!("{:+.0}", pct))])
            } else {
                trf(if pct > 0.0 { "{pct:.0f} % brighter" } else { "{pct:.0f} % darker" }, &[("pct", &format!("{:.0}", pct.abs()))])
            };
            let l = gtk::Label::new(Some(&text));
            l.add_css_class(if ok { "success" } else { "warning" });
            row.add_suffix(&l);
        }
        row
    }

    // --- dialogs ---
    fn ask_consent(self: &Rc<Self>) {
        let mut lines = vec![];
        let mut keys: Vec<&'static str> = vec![];
        let mut names: Vec<String> = vec![];
        if let Some(st) = self.status.borrow().as_ref() {
            keys = Self::unconfirmed_models(st);
            for c in &st.cams {
                let model = c.s.model_key.as_deref().and_then(models::by_key);
                let what = match model {
                    Some(m) => trf("{model} detected", &[("model", &m.name)]),
                    None => trf("other camera ({usb_id})", &[("usb_id", &c.s.usb_id.clone().unwrap_or_else(|| tr("unknown")))]),
                };
                lines.push(format!("{}  {}: {}", if model.is_some() { "✓" } else { "✗" }, cw::cam_name(&c.s.port), what));
            }
            names = keys.iter().filter_map(|k| models::by_key(k)).map(|m| m.name.to_string()).collect();
        }
        let list = names.join(", ");
        let mut body = trf("The optimizer uses hidden camera settings (sensor gain, JPEG quality) that were verified on these camera models: {models}.", &[("models", &list)]);
        if !lines.is_empty() {
            body += &format!("\n\n{}", lines.join("\n"));
        }
        let d = alert(&trf("Are these {models} cameras?", &[("models", &list)]), &body, &[("no", &tr("No / Not Sure")), ("yes", &tr("Yes, Continue"))], Some("yes"), None);
        let m = self.clone();
        ask(&self.win.window, d, move |r| {
            let yes = r == "yes";
            let (m2, keys) = (m.clone(), keys.clone());
            run_async(move || profiles::set_consent(yes, &keys), move |_| m2.refresh_status());
        });
    }

    fn ask_prepare(self: &Rc<Self>) {
        let Some(size) = self.chosen_size() else { return };
        let ad = self.status.borrow().as_ref().map(|s| s.ad_size);
        let mut body = tr("Autodarts stops for about 3 minutes and restarts by itself.\n\nDarts removed, lights on, hands away from the board?");
        body += &format!("\n\n{}", trf("Resolution: {size}", &[("size", &oneclick::size_label(size))]));
        if ad.is_some() && ad != Some(size) {
            body += &format!(" · {}", tr("Autodarts is switched to it when you apply; the board must then be calibrated again."));
        }
        let d = alert(
            &tr("Start the Optimization?"),
            &body,
            &[("cancel", &tr("Cancel")), ("start", &tr("Start"))],
            Some("start"),
            None,
        );
        let m = self.clone();
        ask(&self.win.window, d, move |r| {
            if r == "start" {
                m.size.set(Some(size));
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
                m.render_home();
            },
        );
    }

    // --- job ---

    fn start_job(self: &Rc<Self>, name: &str) {
        live::stop_all();
        let started = match name {
            "optimize" => {
                let Some(size) = self.chosen_size() else {
                    self.win.show_toast("No resolution all cameras offer");
                    return;
                };
                JOB.start(name, move |j| oneclick::optimize(j, 30, size))
            }
            "apply" => JOB.start(name, oneclick::apply),
            _ => JOB.start(name, oneclick::restore),
        };
        if !started {
            self.win.show_toast("A camera job is already running");
            return;
        }
        *self.job_t0.borrow_mut() = Some(Instant::now());
        self.lighting.borrow_mut().clear();
        self.job_seen.set(false);
        self.browsing.set(false);
        self.start_job_poll();
    }

    fn start_job_poll(self: &Rc<Self>) {
        if self.job_t0.borrow().is_none() {
            *self.job_t0.borrow_mut() = Some(Instant::now());
        }
        if self.job_timer.borrow().is_none() {
            let m = self.clone();
            let id = every(1000, move || m.poll_job());
            *self.job_timer.borrow_mut() = Some(id);
        }
        if self.preview_timer.borrow().is_none() {
            self.preview_seq.set(-1);
            let m = self.clone();
            let id = every(PREVIEW_MS, move || m.poll_preview());
            *self.preview_timer.borrow_mut() = Some(id);
        }
        self.render_job();
        self.render_screen();
    }

    fn poll_preview(&self) -> bool {
        if !JOB.running() {
            *self.preview_timer.borrow_mut() = None;
            return false;
        }
        let (dev, phase, jpeg, seq) = PREVIEW.get();
        if self.preview_frame.is_visible() {
            self.work_line.set_label(&tr(if phase.is_empty() { "Waiting for the cameras…" } else { &phase }));
        }
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
        self.refresh_status();
        false
    }

    /// The working screen while a job runs, the result screen once it ended.
    fn render_job(&self) {
        let j = JOB.view();
        if j.steps.is_empty() {
            return;
        }
        let restoring = j.name == "restore";
        let applying = j.name == "apply";
        if j.running {
            self.work_title.set_label(&tr(if restoring {
                "Restoring original settings…"
            } else if applying {
                "Applying the calibration…"
            } else {
                "Optimizing…"
            }));
            // the live picture only while optimizing; otherwise the line names the current step
            let watching = j.name == "optimize";
            self.preview_frame.set_visible(watching);
            if !watching {
                self.work_line.set_label(&j.steps.last().map(|s| tr(&s.title)).unwrap_or_default());
            }
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
        }
        clear_box(&self.steps_box);
        for st in &j.steps {
            let row = gtk::Box::new(gtk::Orientation::Horizontal, 8);
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
            row.append(&icon);
            let l = gtk::Label::new(Some(&tr(&st.title)));
            l.set_xalign(0.0);
            l.set_wrap(true);
            l.set_max_width_chars(60);
            l.add_css_class("caption");
            row.append(&l);
            self.steps_box.append(&row);
        }
        self.log_view.buffer().set_text(&j.log.join("\n"));
        if j.running {
            return;
        }
        let (icon, tone, title, text) = if !j.error.is_empty() {
            let t = trf("Something went wrong: {error}. Your previous settings are still in place.", &[("error", &tr(&j.error))]);
            ("dialog-warning-symbolic", "warning", tr("That did not work"), t)
        } else if applying {
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
            ("object-select-symbolic", "success", tr("Calibration applied"), n)
        } else if restoring {
            ("object-select-symbolic", "success", tr("Original settings restored"), tr("Your cameras are back on the settings Autodarts uses."))
        } else {
            return; // a finished optimization goes on to its review
        };
        set_icon(&self.res_icon, icon, tone);
        self.res_title.set_label(&title);
        self.res_line.set_label(&text);
    }

    /// What the last optimization found, for the review screen ("" when it is not the last job).
    fn review_note(j: &JobView) -> String {
        let Some(o) = j.result.as_object().filter(|o| j.name == "optimize" && !o.is_empty()) else { return String::new() };
        let vals: Vec<&Value> = o.values().filter(|v| v.is_object()).collect();
        let Some(first) = vals.first() else { return String::new() };
        let mut qs: Vec<String> = vals.iter().filter(|r| r["mjpeg_q"].is_i64()).map(|r| r["mjpeg_q"].to_string()).collect();
        qs.sort();
        qs.dedup();
        let res = first["resolution"].as_str().unwrap_or("");
        let mut n = if !res.is_empty() { trf("All cameras calibrated and matched at {resolution}", &[("resolution", &res)]) } else { tr("All cameras calibrated and matched") };
        if qs.len() == 1 {
            n += &format!(" · {}", trf("JPEG level {q}", &[("q", &qs[0])]));
        }
        n += ".";
        if first["resolution_change"].as_bool().unwrap_or(false) {
            n += &format!(" {}", trf("Applying sets Autodarts to {resolution} (needs a new board calibration if it has none at it).", &[("resolution", &res)]));
        }
        if let Some(w) = j.result["warning"].as_str() {
            n += &format!(" {}", w);
        }
        if vals.iter().any(|r| r["white_short"].as_bool().unwrap_or(false)) {
            n += &format!(" {}", tr("Some cameras stay darker than the target: more light on the board would help."));
        }
        n
    }

    // --- before / after ---

    fn load_before_after(self: &Rc<Self>) {
        let m = self.clone();
        run_async(oneclick::before_after_info, move |ba| {
            *m.ba.borrow_mut() = ba;
            m.on_ba_pick();
        });
    }

    /// The camera buttons above the picture (when more than one camera has pictures).
    fn render_ba_cams(self: &Rc<Self>, active: &Option<String>) {
        let ports: Vec<String> = self.ba.borrow().iter().map(|b| b.port.clone()).collect();
        if ports == *self.ba_ports.borrow() {
            return;
        }
        *self.ba_ports.borrow_mut() = ports.clone();
        clear_box(&self.ba_cams);
        if ports.len() < 2 {
            return;
        }
        let o: Vec<(String, String)> = ports.iter().map(|p| (p.clone(), cw::cam_name(p))).collect();
        let m = Rc::downgrade(self);
        self.ba_cams.append(&segmented(
            &o,
            active.as_deref().unwrap_or(""),
            Rc::new(move |p: String| {
                if let Some(m) = m.upgrade() {
                    cw::set_selected(Some(p));
                    m.on_ba_pick();
                }
            }),
        ));
    }

    fn on_ba_pick(self: &Rc<Self>) {
        let ba = self.ba.borrow().clone();
        let pending = ba.iter().any(|b| b.pending);
        self.btn_ba.set_visible(!ba.is_empty() && !pending);
        self.rev_pending.set_visible(pending && !JOB.running());
        self.rev_done.set_visible(!pending);
        self.rev_back.set_visible(!pending);
        let name = self.active_port();
        self.render_ba_cams(&name);
        let info = name.as_ref().and_then(|n| ba.iter().find(|b| &b.port == n).cloned());
        if pending {
            self.rev_title.set_label(&tr("Calibration ready to review"));
            let note = Self::review_note(&JOB.view());
            let ask = tr("Nothing has changed yet. Compare before and after, then apply it or discard it.");
            self.rev_line.set_label(&if note.is_empty() { ask } else { format!("{} {}", note, ask) });
        } else {
            self.rev_title.set_label(&tr("Before & After"));
            let when = info.as_ref().and_then(|i| i.meta["after"]["time"].as_str().map(String::from));
            self.rev_line.set_label(&when.map(|t| trf("Optimized {when}", &[("when", &t)])).unwrap_or_default());
        }
        self.render_screen();
        let Some(info) = info else {
            self.ba_stack.set_visible_child_name("empty");
            self.ba_empty.set_label(&tr("Before and after pictures appear here after an optimization."));
            *self.ba_data.borrow_mut() = None;
            self.ba_key.borrow_mut().clear();
            self.render_stats();
            return;
        };
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

    /// One line of numbers under the picture, how close it is to the other cameras, and the gestures.
    fn render_stats(&self) {
        let data = self.ba_data.borrow().clone();
        self.btn_ba_preset.set_sensitive(data.is_some());
        let Some(d) = data else {
            self.ba_stats.set_label("");
            return;
        };
        let (b, a) = (d.stats[0], d.stats[1]);
        let mut parts = vec![
            format!("{} {:.0} → {:.0}", tr("White level"), b.0, a.0),
            format!("{} {:.1} → {:.1} %", tr("Clipped"), b.1, a.1),
        ];
        if d.q.0.is_some() || d.q.1.is_some() {
            let q = |v: Option<i64>| v.map(|v| v.to_string()).unwrap_or_else(|| "–".into());
            parts.push(format!("{} {} → {}", tr("JPEG level"), q(d.q.0), q(d.q.1)));
        }
        parts.push(format!("{} {:.0} → {:.0} KB", tr("Size / frame"), d.kb.0, d.kb.1));
        let mut lines = vec![parts.join("   ·   ")];
        let active = self.active_port();
        let reference = active.as_deref().and_then(|p| self.cam(p)).and_then(|c| c.white);
        if let (Some(st), Some(rf)) = (self.status.borrow().as_ref(), reference) {
            let diffs: Vec<f64> = st.cams.iter().filter(|c| c.s.optimized && Some(&c.s.port) != active.as_ref()).filter_map(|c| c.white).map(|w| (rf - w).abs() / rf).collect();
            if !diffs.is_empty() {
                let worst = diffs.iter().cloned().fold(0.0, f64::max) * 100.0;
                lines.push(if worst < 5.0 {
                    trf("✓ Matches the other cameras within {pct:.0f} %", &[("pct", &format!("{:.0}", worst.max(1.0)))])
                } else {
                    trf("Differs from another camera by {pct:.0f} %", &[("pct", &format!("{:.0}", worst))])
                });
            }
        }
        lines.push(tr("drag the handle · double-tap or scroll to zoom"));
        self.ba_stats.set_label(&lines.join("\n"));
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
