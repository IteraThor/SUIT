//! Camera Focus Tool: the oscilloscope focus method for the Autodarts cameras (furthest double, T20 or a tapped
//! point), live view with a 2.5x loupe, sharpness timeline with peak latch, overshoot detection and audio tone.
use super::{Page, Win};
use crate::core::audio_tone::AudioToneService;
use crate::core::autodarts::{parse_cam_device, read_cam_config};
use crate::core::camera_focus::{CameraFocusService, FocusAnalysis, FocusTrend, TargetMode};
use crate::core::i18n::{tr, trf};
use crate::core::systemd::manager;
use adw::prelude::*;
use gtk::{cairo, glib};
use opencv::prelude::*;
use std::cell::{Cell, RefCell};
use std::f64::consts::PI;
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

const CAMERA_NAMES: [&str; 3] = ["Camera 1", "Camera 2", "Camera 3"];
const PIP: f64 = 240.0;
const PIP_MARGIN: f64 = 16.0;

/// What the capture thread hands to the UI: analysis plus BGRA frame and loupe.
struct Shot {
    analysis: FocusAnalysis,
    frame: (i32, i32, Vec<u8>),
    zoom: Option<(i32, i32, Vec<u8>)>,
}

fn bgra(m: &opencv::core::Mat) -> Option<(i32, i32, Vec<u8>)> {
    let mut o = opencv::core::Mat::default();
    opencv::imgproc::cvt_color_def(m, &mut o, opencv::imgproc::COLOR_BGR2BGRA).ok()?;
    let o = if o.is_continuous() { o } else { o.try_clone().ok()? };
    Some((o.cols(), o.rows(), o.data_bytes().ok()?.to_vec()))
}

fn surface((w, h, d): (i32, i32, Vec<u8>)) -> Option<cairo::ImageSurface> {
    cairo::ImageSurface::create_for_data(d, cairo::Format::ARgb32, w, h, w * 4).ok()
}

enum Pending {
    Target(TargetMode, Option<(i32, i32)>),
    Pick,
}

pub struct CameraFocusView {
    nav: adw::NavigationPage,
    win: Win,
    service: Arc<Mutex<CameraFocusService>>,
    audio: Arc<AudioToneService>,
    step: Cell<usize>,
    results: RefCell<[(f64, f64, String); 3]>,
    was_autodarts_active: Arc<AtomicBool>,
    stop: Arc<AtomicBool>,
    capture: RefCell<Option<std::thread::JoinHandle<()>>>,
    inbox: Arc<Mutex<Option<Shot>>>,
    ui_timer: RefCell<Option<glib::SourceId>>,
    analysis: RefCell<Option<FocusAnalysis>>,
    frame_surf: RefCell<Option<cairo::ImageSurface>>,
    zoom_surf: RefCell<Option<cairo::ImageSurface>>,
    switching: Cell<bool>,
    camera_open: Cell<bool>,
    pending: RefCell<Option<Pending>>,
    was_maximized: Cell<bool>,
    peaking: Arc<AtomicBool>,
    lbl_title: gtk::Label,
    btn_mute: gtk::Button,
    tabs: Vec<gtk::Button>,
    stack: adw::ViewStack,
    banner: gtk::Box,
    banner_title: gtk::Label,
    target_pill: gtk::Label,
    trend: gtk::Label,
    btn_peaking: gtk::Button,
    area: gtk::DrawingArea,
    prompt: gtk::Box,
    loading: gtk::Box,
    loading_lbl: gtk::Label,
    osc_card: gtk::Box,
    osc_peak: gtk::Label,
    osc: gtk::DrawingArea,
    sum_tiles: Vec<gtk::Label>,
}

impl Page for CameraFocusView {
    fn nav(&self) -> &adw::NavigationPage {
        &self.nav
    }

    /// Opening: maximize the window for the calibration workspace and wait for a target pick.
    fn on_opened(self: Rc<Self>) {
        log::info!("CameraFocusView opened");
        self.was_maximized.set(self.win.window.is_maximized());
        self.win.window.maximize();
        self.stack.set_visible_child_name("cam_focus");
        self.loading.set_visible(false);
        self.step.set(0);
        self.update_tabs(0);
        self.show_target_prompt();
    }

    /// Leaving: release the camera and resume Autodarts if SUIT paused it.
    fn on_closed(self: Rc<Self>) {
        log::info!("CameraFocusView closed: releasing hardware and restoring window state");
        self.stop_capture();
        self.service.lock().unwrap().stop_camera();
        self.audio.stop();
        self.camera_open.set(false);
        *self.pending.borrow_mut() = None;
        if !self.was_maximized.get() {
            self.win.window.unmaximize();
        }
        *self.frame_surf.borrow_mut() = None;
        *self.zoom_surf.borrow_mut() = None;
        *self.analysis.borrow_mut() = None;
        if self.was_autodarts_active.swap(false, Ordering::SeqCst) || manager::paused_by_suit() {
            std::thread::spawn(|| {
                log::info!("Restoring autodarts.service");
                manager::resume_autodarts();
            });
        }
    }
}

impl CameraFocusView {
    pub fn new(win: Win) -> Rc<dyn Page> {
        let (scrolled, main) = super::helpers::build_page_shell(1380);
        main.set_spacing(10); // dense live-view layout
        let nav = adw::NavigationPage::builder().title(tr("Camera Focus Tool")).tag("camera_focus").child(&scrolled).build();
        // header
        let hc = gtk::Box::new(gtk::Orientation::Horizontal, 10);
        hc.add_css_class("tile-card-compact");
        main.append(&hc);
        let si = gtk::Image::from_icon_name("camera-photo-symbolic");
        si.set_pixel_size(24);
        si.set_valign(gtk::Align::Center);
        hc.append(&si);
        let tb = gtk::Box::new(gtk::Orientation::Vertical, 1);
        tb.set_hexpand(true);
        hc.append(&tb);
        let lbl_title = gtk::Label::new(Some(&tr("Camera 1")));
        lbl_title.set_xalign(0.0);
        lbl_title.add_css_class("title-4");
        tb.append(&lbl_title);
        let desc = super::helpers::wrap_label(&tr("Optical focus tuning with real-time oscilloscope tracking."), "dim-label");
        tb.append(&desc);
        let btn_mute = gtk::Button::from_icon_name("audio-volume-muted-symbolic");
        btn_mute.add_css_class("flat");
        btn_mute.set_size_request(40, 36);
        btn_mute.set_valign(gtk::Align::Center);
        btn_mute.set_tooltip_text(Some(&tr("Unmute Audio Guidance")));
        hc.append(&btn_mute);
        // camera tabs
        let tab_bar = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        tab_bar.add_css_class("camera-tab-bar");
        tab_bar.set_homogeneous(true);
        main.append(&tab_bar);
        let tabs: Vec<gtk::Button> = CAMERA_NAMES
            .iter()
            .enumerate()
            .map(|(i, n)| {
                let b = gtk::Button::with_label(&tr(n));
                b.add_css_class("flat");
                b.add_css_class("camera-tab-btn");
                if i == 0 {
                    b.add_css_class("active-tab");
                }
                tab_bar.append(&b);
                b
            })
            .collect();
        let stack = adw::ViewStack::new();
        main.append(&stack);
        let fp = gtk::Box::new(gtk::Orientation::Vertical, 10);
        stack.add_named(&fp, Some("cam_focus"));
        let sp = gtk::Box::new(gtk::Orientation::Vertical, 16);
        stack.add_named(&sp, Some("summary"));
        // active banner
        let banner = gtk::Box::new(gtk::Orientation::Horizontal, 10);
        banner.add_css_class("tile-card-compact");
        banner.set_visible(false);
        fp.append(&banner);
        let btb = gtk::Box::new(gtk::Orientation::Vertical, 2);
        btb.set_hexpand(true);
        banner.append(&btb);
        let banner_title = super::helpers::wrap_label(&tr("Rotate focus ring slowly. Look for highest peak."), "title-4");
        btb.append(&banner_title);
        let bir = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        btb.append(&bir);
        let target_pill = gtk::Label::new(Some(&tr("Target: Furthest Double")));
        target_pill.set_xalign(0.0);
        target_pill.add_css_class("usb-path-tag");
        bir.append(&target_pill);
        let trend = gtk::Label::new(Some(&tr("Tuning")));
        trend.add_css_class("focus-trend-badge");
        trend.add_css_class("stable");
        bir.append(&trend);
        let btn_peaking = gtk::Button::with_label(&tr("Peaking: ON"));
        btn_peaking.add_css_class("suggested-action");
        btn_peaking.set_valign(gtk::Align::Center);
        btn_peaking.set_tooltip_text(Some(&tr("Toggle Green Focus Peaking Contour Overlay")));
        banner.append(&btn_peaking);
        let btn_change = gtk::Button::with_label(&tr("Change Target"));
        btn_change.add_css_class("flat");
        btn_change.set_valign(gtk::Align::Center);
        banner.append(&btn_change);
        // video
        let vf = gtk::Frame::new(None);
        vf.set_hexpand(true);
        vf.set_vexpand(true);
        vf.add_css_class("focus-preview-area");
        fp.append(&vf);
        let vo = gtk::Overlay::new();
        vf.set_child(Some(&vo));
        let area = gtk::DrawingArea::new();
        area.set_size_request(320, 240);
        area.set_hexpand(true);
        area.set_vexpand(true);
        vo.set_child(Some(&area));
        // target prompt
        let prompt = gtk::Box::new(gtk::Orientation::Vertical, 10);
        prompt.add_css_class("focus-target-overlay-card");
        prompt.set_halign(gtk::Align::Center);
        prompt.set_valign(gtk::Align::Center);
        for m in [16] {
            prompt.set_margin_start(m);
            prompt.set_margin_end(m);
            prompt.set_margin_top(m);
            prompt.set_margin_bottom(m);
        }
        let pt = gtk::Label::new(Some(&tr("Select Focus Target")));
        pt.add_css_class("title-3");
        prompt.append(&pt);
        let ps = gtk::Label::new(Some(&tr("Choose where to focus. The camera only starts after you pick:")));
        ps.set_justify(gtk::Justification::Center);
        ps.set_wrap(true);
        ps.add_css_class("dim-label");
        prompt.append(&ps);
        let tbr = gtk::Box::new(gtk::Orientation::Horizontal, 12);
        tbr.set_margin_top(6);
        tbr.set_homogeneous(true);
        prompt.append(&tbr);
        let mk = |l: &str, css: &str| {
            let b = gtk::Button::with_label(&tr(l));
            b.add_css_class(css);
            b.add_css_class("touch-btn");
            b.set_size_request(-1, 42);
            b
        };
        let b_far = mk("Furthest Double (Recommended)", "suggested-action");
        let b_t20 = mk("Triple 20 (T20)", "secondary-btn");
        tbr.append(&b_far);
        tbr.append(&b_t20);
        let b_pick = mk("Pick Point on Video", "secondary-btn");
        prompt.append(&b_pick);
        let tip = gtk::Label::new(Some(&tr("Tip: Insert a dart into the target segment for maximum edge contrast.")));
        tip.set_justify(gtk::Justification::Center);
        tip.set_wrap(true);
        tip.add_css_class("dim-label");
        tip.set_margin_top(4);
        prompt.append(&tip);
        vo.add_overlay(&prompt);
        // loading
        let loading = gtk::Box::new(gtk::Orientation::Vertical, 10);
        loading.add_css_class("focus-loading-overlay");
        loading.set_valign(gtk::Align::Center);
        loading.set_halign(gtk::Align::Center);
        let ls = adw::Spinner::new();
        ls.set_size_request(36, 36);
        loading.append(&ls);
        let loading_lbl = gtk::Label::new(Some(&tr("Stopping Autodarts & Opening Camera 1...")));
        loading_lbl.add_css_class("title-4");
        loading.append(&loading_lbl);
        vo.add_overlay(&loading);
        loading.set_visible(false);
        // oscilloscope
        let osc_card = gtk::Box::new(gtk::Orientation::Vertical, 6);
        osc_card.add_css_class("focus-zoom-card");
        osc_card.set_visible(false);
        fp.append(&osc_card);
        let ohr = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        osc_card.append(&ohr);
        let ot = gtk::Label::new(Some(&tr("Relative Sharpness Timeline")));
        ot.set_xalign(0.0);
        ot.add_css_class("title-4");
        ohr.append(&ot);
        let osc_peak = gtk::Label::new(Some(&trf("Peak: {peak:.0f}%", &[("peak", &"0")])));
        osc_peak.set_xalign(1.0);
        osc_peak.add_css_class("dim-label");
        osc_peak.set_hexpand(true);
        osc_peak.set_tooltip_text(Some(&tr("Click to reset peak")));
        ohr.append(&osc_peak);
        let osc = gtk::DrawingArea::new();
        osc.set_size_request(-1, 110);
        osc.set_hexpand(true);
        osc_card.append(&osc);
        // bottom bar
        let bb = gtk::Box::new(gtk::Orientation::Horizontal, 12);
        bb.set_margin_top(4);
        fp.append(&bb);
        let b_reset = gtk::Button::with_label(&tr("Reset Peak"));
        b_reset.add_css_class("secondary-btn");
        bb.append(&b_reset);
        let spacer = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        spacer.set_hexpand(true);
        bb.append(&spacer);
        let b_finish = gtk::Button::with_label(&tr("Finish"));
        b_finish.add_css_class("suggested-action");
        b_finish.add_css_class("touch-btn");
        b_finish.set_size_request(130, 42);
        bb.append(&b_finish);
        // summary
        let sb = gtk::Box::new(gtk::Orientation::Vertical, 16);
        sp.append(&sb);
        let hb = gtk::Box::new(gtk::Orientation::Vertical, 4);
        hb.add_css_class("tile-card");
        sb.append(&hb);
        let sh = gtk::Label::new(Some(&tr("Focus Calibration Summary")));
        sh.set_xalign(0.0);
        sh.add_css_class("title-2");
        hb.append(&sh);
        let sd = gtk::Label::new(Some(&tr("Review recorded sharpness across all 3 cameras before resuming Autodarts.")));
        sd.set_xalign(0.0);
        sd.add_css_class("dim-label");
        hb.append(&sd);
        let scb = gtk::Box::new(gtk::Orientation::Horizontal, 12);
        scb.set_homogeneous(true);
        sb.append(&scb);
        let mut sum_tiles = vec![];
        let mut refocus = vec![];
        for n in CAMERA_NAMES {
            let c = gtk::Box::new(gtk::Orientation::Vertical, 8);
            c.add_css_class("card");
            c.set_margin_top(8);
            c.set_margin_bottom(8);
            c.set_margin_start(8);
            c.set_margin_end(8);
            let t = gtk::Label::new(Some(&tr(n)));
            t.set_xalign(0.0);
            t.add_css_class("title-3");
            c.append(&t);
            let s = gtk::Label::new(Some(&tr("Status: Not tuned")));
            s.set_xalign(0.0);
            s.add_css_class("title-4");
            c.append(&s);
            let r = gtk::Button::with_label(&tr("Refocus"));
            r.add_css_class("secondary-btn");
            c.append(&r);
            scb.append(&c);
            sum_tiles.push(s);
            refocus.push(r);
        }
        let b_sum_finish = gtk::Button::with_label(&tr("Finish"));
        b_sum_finish.add_css_class("suggested-action");
        b_sum_finish.add_css_class("touch-btn");
        b_sum_finish.set_size_request(-1, 44);
        b_sum_finish.set_margin_top(8);
        sb.append(&b_sum_finish);

        let me = Rc::new(CameraFocusView {
            nav,
            win,
            service: Arc::new(Mutex::new(CameraFocusService::new())),
            audio: Arc::new(AudioToneService::new(true)),
            step: Cell::new(0),
            results: RefCell::new([(0.0, 0.0, "Unset".into()), (0.0, 0.0, "Unset".into()), (0.0, 0.0, "Unset".into())]),
            was_autodarts_active: Arc::new(AtomicBool::new(false)),
            stop: Arc::new(AtomicBool::new(false)),
            capture: RefCell::new(None),
            inbox: Arc::new(Mutex::new(None)),
            ui_timer: RefCell::new(None),
            analysis: RefCell::new(None),
            frame_surf: RefCell::new(None),
            zoom_surf: RefCell::new(None),
            switching: Cell::new(false),
            camera_open: Cell::new(false),
            pending: RefCell::new(None),
            was_maximized: Cell::new(false),
            peaking: Arc::new(AtomicBool::new(true)),
            lbl_title,
            btn_mute,
            tabs,
            stack,
            banner,
            banner_title,
            target_pill,
            trend,
            btn_peaking,
            area,
            prompt,
            loading,
            loading_lbl,
            osc_card,
            osc_peak,
            osc,
            sum_tiles,
        });
        let m = Rc::downgrade(&me);
        me.area.set_draw_func(move |_, cr, w, h| {
            if let Some(m) = m.upgrade() {
                m.draw_preview(cr, w as f64, h as f64);
            }
        });
        let m = Rc::downgrade(&me);
        me.osc.set_draw_func(move |_, cr, w, h| {
            if let Some(m) = m.upgrade() {
                m.draw_osc(cr, w as f64, h as f64);
            }
        });
        let click = gtk::GestureClick::new();
        let m = me.clone();
        click.connect_released(move |_, _, x, y| m.on_preview_clicked(x, y));
        me.area.add_controller(click);
        let pc = gtk::GestureClick::new();
        let s = me.service.clone();
        pc.connect_released(move |_, _, _, _| s.lock().unwrap().reset_peak());
        me.osc_peak.add_controller(pc);
        let s = me.service.clone();
        b_reset.connect_clicked(move |_| s.lock().unwrap().reset_peak());
        for (i, b) in me.tabs.iter().enumerate() {
            let m = me.clone();
            b.connect_clicked(move |_| m.on_tab(i));
        }
        let m = me.clone();
        me.btn_peaking.connect_clicked(move |_| m.toggle_peaking());
        let m = me.clone();
        btn_change.connect_clicked(move |_| m.show_target_prompt());
        let m = me.clone();
        b_far.connect_clicked(move |_| m.select_target(TargetMode::FurthestDouble, None));
        let m = me.clone();
        b_t20.connect_clicked(move |_| m.select_target(TargetMode::T20, None));
        let m = me.clone();
        b_pick.connect_clicked(move |_| {
            if !m.camera_open.get() {
                m.open_camera_then(Pending::Pick);
            } else {
                m.enter_pick_ui();
            }
        });
        for b in [&b_finish, &b_sum_finish] {
            let m = me.clone();
            b.connect_clicked(move |_| m.on_finish());
        }
        for (i, r) in refocus.iter().enumerate() {
            let m = me.clone();
            r.connect_clicked(move |_| {
                m.stack.set_visible_child_name("cam_focus");
                m.on_tab(i);
            });
        }
        let m = me.clone();
        me.btn_mute.connect_clicked(move |b| {
            let muted = !m.audio.is_muted();
            m.audio.set_muted(muted);
            b.set_icon_name(if muted { "audio-volume-muted-symbolic" } else { "audio-volume-high-symbolic" });
            b.set_tooltip_text(Some(&tr(if muted { "Unmute Audio Tone" } else { "Mute Audio Tone" })));
        });
        me
    }

    fn toggle_peaking(&self) {
        let on = !self.peaking.load(Ordering::SeqCst);
        self.peaking.store(on, Ordering::SeqCst);
        self.btn_peaking.set_label(&tr(if on { "Peaking: ON" } else { "Peaking: OFF" }));
        if on {
            self.btn_peaking.add_css_class("suggested-action");
            self.btn_peaking.remove_css_class("flat");
        } else {
            self.btn_peaking.remove_css_class("suggested-action");
            self.btn_peaking.add_css_class("flat");
        }
        self.area.queue_draw();
    }

    fn select_target(self: &Rc<Self>, mode: TargetMode, point: Option<(i32, i32)>) {
        if !self.camera_open.get() {
            self.open_camera_then(Pending::Target(mode, point));
            return;
        }
        let shape = self.frame_surf.borrow().as_ref().map(|s| (s.height(), s.width())).unwrap_or((960, 1280));
        let name = {
            let mut s = self.service.lock().unwrap();
            s.set_target_mode(mode, point, shape);
            s.target_name.clone()
        };
        self.prompt.set_visible(false);
        self.banner.set_visible(true);
        self.osc_card.set_visible(true);
        self.banner_title.set_label(&tr("Rotate focus ring slowly. Look for highest peak."));
        self.target_pill.set_label(&trf("Target: {target}", &[("target", &tr(&name))]));
        self.area.queue_draw();
        self.osc.queue_draw();
    }

    fn enter_pick_ui(&self) {
        self.service.lock().unwrap().set_target_mode(TargetMode::None, None, (960, 1280));
        self.prompt.set_visible(false);
        self.banner.set_visible(true);
        self.osc_card.set_visible(false);
        self.banner_title.set_label(&tr("Click on the video where you want to focus."));
        self.target_pill.set_label(&tr("Target: not set"));
        self.area.queue_draw();
    }

    /// Stop Autodarts and open the current camera, then apply the pending selection.
    fn open_camera_then(self: &Rc<Self>, p: Pending) {
        if self.switching.replace(true) {
            return;
        }
        *self.pending.borrow_mut() = Some(p);
        let idx = self.step.get();
        self.prompt.set_visible(false);
        self.loading_lbl.set_label(&trf("Stopping Autodarts & Opening {camera}...", &[("camera", &tr(CAMERA_NAMES[idx]))]));
        self.loading.set_visible(true);
        let (svc, audio, was) = (self.service.clone(), self.audio.clone(), self.was_autodarts_active.clone());
        let m = self.clone();
        super::helpers::run_async(
            move || {
                if !manager::paused_by_suit() && !was.load(Ordering::SeqCst) {
                    let paused = manager::pause_autodarts(true);
                    was.store(paused, Ordering::SeqCst);
                    if paused {
                        std::thread::sleep(std::time::Duration::from_millis(400));
                    }
                    crate::core::util::run(&["pkill", "-TERM", "-f", r"(^|/)(ad|autodarts)(\s|$)"], Some(2));
                    std::thread::sleep(std::time::Duration::from_millis(300));
                }
                let cfg = read_cam_config();
                let mut cams: Vec<String> = cfg.cams.iter().filter(|c| !c.trim().is_empty()).map(|c| parse_cam_device(c)).collect();
                let raw: Vec<String> = cfg.devices.iter().filter(|c| !c.trim().is_empty()).cloned().collect();
                if cams.is_empty() {
                    cams = vec!["/dev/video0".into(), "/dev/video2".into(), "/dev/video4".into()];
                }
                audio.start();
                let dev = cams.get(idx).cloned().unwrap_or_else(|| format!("/dev/video{}", idx * 2));
                let uri = raw.get(idx).cloned();
                svc.lock().unwrap().start_camera(&dev, 1280, 960, 3, uri.as_deref());
            },
            move |_| m.on_camera_opened(),
        );
    }

    fn on_camera_opened(self: &Rc<Self>) {
        self.switching.set(false);
        self.camera_open.set(true);
        self.loading.set_visible(false);
        self.start_capture();
        match self.pending.borrow_mut().take() {
            Some(Pending::Target(m, p)) => self.select_target(m, p),
            Some(Pending::Pick) => self.enter_pick_ui(),
            None => {}
        }
    }

    fn show_target_prompt(&self) {
        self.service.lock().unwrap().set_target_mode(TargetMode::None, None, (960, 1280));
        self.prompt.set_visible(true);
        self.banner.set_visible(false);
        self.osc_card.set_visible(false);
        self.area.queue_draw();
    }

    fn on_preview_clicked(self: &Rc<Self>, x: f64, y: f64) {
        let (fw, fh) = match self.frame_surf.borrow().as_ref() {
            Some(s) => (s.width() as f64, s.height() as f64),
            None => return,
        };
        let (w, h) = (self.area.width() as f64, self.area.height() as f64);
        // click inside the loupe toggles peaking
        let (px, py) = (w - PIP - PIP_MARGIN, h - PIP - PIP_MARGIN);
        if self.zoom_surf.borrow().is_some() && x >= px && x <= px + PIP && y >= py && y <= py + PIP {
            self.toggle_peaking();
            return;
        }
        let scale = (w / fw).min(h / fh);
        let (dw, dh) = (fw * scale, fh * scale);
        let (ox, oy) = ((w - dw) / 2.0, (h - dh) / 2.0);
        if scale > 0.0 && x >= ox && x <= ox + dw && y >= oy && y <= oy + dh {
            self.select_target(TargetMode::Custom, Some((((x - ox) / scale) as i32, ((y - oy) / scale) as i32)));
        }
    }

    fn update_tabs(&self, idx: usize) {
        for (i, b) in self.tabs.iter().enumerate() {
            if i == idx {
                b.add_css_class("active-tab");
            } else {
                b.remove_css_class("active-tab");
            }
        }
    }

    fn on_tab(self: &Rc<Self>, idx: usize) {
        if self.switching.get() || idx == self.step.get() {
            return;
        }
        self.step.set(idx);
        self.update_tabs(idx);
        self.lbl_title.set_label(&tr(CAMERA_NAMES[idx]));
        // Release the current camera; the next one opens once a target is picked
        let was_open = self.camera_open.replace(false);
        *self.pending.borrow_mut() = None;
        *self.frame_surf.borrow_mut() = None;
        *self.zoom_surf.borrow_mut() = None;
        *self.analysis.borrow_mut() = None;
        self.show_target_prompt();
        if was_open {
            self.stop_capture();
            let s = self.service.clone();
            std::thread::spawn(move || s.lock().unwrap().stop_camera());
        }
    }

    fn start_capture(self: &Rc<Self>) {
        self.stop.store(false, Ordering::SeqCst);
        let (svc, stop, inbox, peaking, audio) = (self.service.clone(), self.stop.clone(), self.inbox.clone(), self.peaking.clone(), self.audio.clone());
        let h = std::thread::spawn(move || {
            while !stop.load(Ordering::SeqCst) {
                let shot = {
                    let mut s = svc.lock().unwrap();
                    s.read_frame().map(|f| {
                        let a = s.analyze_frame(&f);
                        let zoom = if a.target_mode != TargetMode::None {
                            s.process_zoom_crop(&f, a.target_roi, 2.5, (240, 240), peaking.load(Ordering::SeqCst)).and_then(|z| bgra(&z))
                        } else {
                            None
                        };
                        (a, bgra(&f), zoom)
                    })
                };
                if let Some((a, Some(frame), zoom)) = shot {
                    if a.target_mode != TargetMode::None {
                        audio.set_score(a.score);
                    }
                    *inbox.lock().unwrap() = Some(Shot { analysis: a, frame, zoom });
                }
                std::thread::sleep(std::time::Duration::from_millis(33));
            }
        });
        *self.capture.borrow_mut() = Some(h);
        let m = Rc::downgrade(self);
        let id = super::helpers::every(33, move || {
            let Some(m) = m.upgrade() else { return false };
            let shot = m.inbox.lock().unwrap().take();
            if let Some(s) = shot {
                *m.frame_surf.borrow_mut() = surface(s.frame);
                *m.zoom_surf.borrow_mut() = s.zoom.and_then(surface);
                m.update_ui(&s.analysis);
                *m.analysis.borrow_mut() = Some(s.analysis);
            }
            true
        });
        *self.ui_timer.borrow_mut() = Some(id);
    }

    fn stop_capture(&self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(id) = self.ui_timer.borrow_mut().take() {
            id.remove();
        }
        if let Some(h) = self.capture.borrow_mut().take() {
            let _ = h.join();
        }
        *self.inbox.lock().unwrap() = None;
    }

    fn update_ui(&self, a: &FocusAnalysis) {
        if a.target_mode == TargetMode::None {
            self.area.queue_draw();
            return;
        }
        self.osc_peak.set_label(&trf("Peak: {peak:.0f}%", &[("peak", &format!("{:.0}", a.peak_score))]));
        let i = self.step.get();
        if let Some(b) = self.tabs.get(i) {
            b.set_label(&format!("{} ({:.0}%)", tr(CAMERA_NAMES[i]), a.score));
        }
        for c in ["sharpening", "stable", "overshot"] {
            self.trend.remove_css_class(c);
        }
        match a.trend {
            FocusTrend::Overshot => {
                self.trend.set_label(&tr("Turn Back — Overshot"));
                self.trend.add_css_class("overshot");
            }
            FocusTrend::Sharpening => {
                self.trend.set_label(&tr("Sharpening"));
                self.trend.add_css_class("sharpening");
            }
            FocusTrend::Stable => {
                self.trend.set_label(&tr(if a.score >= 85.0 { "In Focus" } else { "Tuning" }));
                self.trend.add_css_class("stable");
            }
        }
        self.area.queue_draw();
        self.osc.queue_draw();
    }

    fn rounded(cr: &cairo::Context, x: f64, y: f64, w: f64, h: f64, r: f64) {
        cr.new_sub_path();
        cr.arc(x + w - r, y + r, r, -PI / 2.0, 0.0);
        cr.arc(x + w - r, y + h - r, r, 0.0, PI / 2.0);
        cr.arc(x + r, y + h - r, r, PI / 2.0, PI);
        cr.arc(x + r, y + r, r, PI, 3.0 * PI / 2.0);
        cr.close_path();
    }

    /// Live feed with the target ROI reticle and the 2.5x loupe in the bottom-right corner.
    fn draw_preview(&self, cr: &cairo::Context, w: f64, h: f64) {
        cr.set_source_rgb(0.04, 0.05, 0.06);
        let _ = cr.paint();
        let fs = self.frame_surf.borrow();
        let Some(s) = fs.as_ref() else {
            cr.set_source_rgb(0.6, 0.6, 0.6);
            cr.select_font_face("Sans", cairo::FontSlant::Normal, cairo::FontWeight::Normal);
            cr.set_font_size(15.0);
            cr.move_to((w / 2.0 - 90.0).max(10.0), h / 2.0);
            let _ = cr.show_text(&tr("Waiting for camera feed..."));
            return;
        };
        let (fw, fh) = (s.width() as f64, s.height() as f64);
        let scale = (w / fw).min(h / fh);
        let (ox, oy) = (((w - fw * scale) / 2.0).floor(), ((h - fh * scale) / 2.0).floor());
        let a = self.analysis.borrow();
        let _ = cr.save();
        cr.translate(ox, oy);
        cr.scale(scale, scale);
        let _ = cr.set_source_surface(s, 0.0, 0.0);
        let _ = cr.paint();
        if let Some(a) = a.as_ref().filter(|a| a.target_mode != TargetMode::None) {
            let (tx, ty, tw, th) = (a.target_roi.0 as f64, a.target_roi.1 as f64, a.target_roi.2 as f64, a.target_roi.3 as f64);
            cr.set_source_rgba(0.0, 0.9, 0.95, 0.9);
            cr.set_line_width(2.0 / scale);
            let bl = 16.0 / scale;
            for (p1, p2, p3) in [
                ((tx, ty + bl), (tx, ty), (tx + bl, ty)),
                ((tx + tw - bl, ty), (tx + tw, ty), (tx + tw, ty + bl)),
                ((tx, ty + th - bl), (tx, ty + th), (tx + bl, ty + th)),
                ((tx + tw - bl, ty + th), (tx + tw, ty + th), (tx + tw, ty + th - bl)),
            ] {
                cr.move_to(p1.0, p1.1);
                cr.line_to(p2.0, p2.1);
                cr.line_to(p3.0, p3.1);
            }
            let _ = cr.stroke();
            let (cx, cy) = (tx + (tw / 2.0).floor(), ty + (th / 2.0).floor());
            cr.set_line_width(1.2 / scale);
            cr.move_to(cx - 8.0 / scale, cy);
            cr.line_to(cx + 8.0 / scale, cy);
            cr.move_to(cx, cy - 8.0 / scale);
            cr.line_to(cx, cy + 8.0 / scale);
            let _ = cr.stroke();
        }
        let _ = cr.restore();
        let zs = self.zoom_surf.borrow();
        let (Some(z), Some(a)) = (zs.as_ref(), a.as_ref().filter(|a| a.target_mode != TargetMode::None)) else { return };
        let (px, py) = (w - PIP - PIP_MARGIN, h - PIP - PIP_MARGIN);
        let r = 14.0;
        let _ = cr.save();
        Self::rounded(cr, px, py, PIP, PIP, r);
        cr.clip();
        let zsc = (PIP / z.width() as f64).max(PIP / z.height() as f64);
        cr.translate(px, py);
        cr.scale(zsc, zsc);
        let _ = cr.set_source_surface(z, 0.0, 0.0);
        let _ = cr.paint();
        let _ = cr.restore();
        let _ = cr.save();
        Self::rounded(cr, px, py, PIP, PIP, r);
        if a.score >= 85.0 {
            cr.set_source_rgba(0.18, 0.85, 0.55, 0.95);
            cr.set_line_width(2.5);
        } else if a.trend == FocusTrend::Overshot {
            cr.set_source_rgba(0.95, 0.35, 0.25, 0.95);
            cr.set_line_width(2.0);
        } else {
            cr.set_source_rgba(0.0, 0.85, 0.95, 0.85);
            cr.set_line_width(2.0);
        }
        let _ = cr.stroke();
        cr.select_font_face("Sans", cairo::FontSlant::Normal, cairo::FontWeight::Bold);
        cr.set_font_size(10.0);
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.75);
        cr.rectangle(px + 8.0, py + 8.0, 120.0, 18.0);
        let _ = cr.fill();
        cr.set_source_rgba(0.0, 0.85, 0.95, 0.95);
        cr.move_to(px + 14.0, py + 21.0);
        let pt = tr(if self.peaking.load(Ordering::SeqCst) { "PEAKING ON" } else { "2.5× LOUPE" });
        let _ = cr.show_text(&format!("2.5× • {}", pt));
        let (lx, ly) = (px + PIP / 2.0, py + PIP / 2.0);
        cr.set_line_width(1.0);
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.6);
        for (a1, b1, a2, b2) in [(-12.0, 0.0, -3.0, 0.0), (3.0, 0.0, 12.0, 0.0), (0.0, -12.0, 0.0, -3.0), (0.0, 3.0, 0.0, 12.0)] {
            cr.move_to(lx + a1, ly + b1);
            cr.line_to(lx + a2, ly + b2);
        }
        let _ = cr.stroke();
        let _ = cr.restore();
    }

    /// Sharpness timeline with peak latch.
    fn draw_osc(&self, cr: &cairo::Context, w: f64, h: f64) {
        cr.set_source_rgba(0.04, 0.05, 0.07, 0.95);
        cr.rectangle(0.0, 0.0, w, h);
        let _ = cr.fill();
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.08);
        cr.set_line_width(1.0);
        for f in [0.25, 0.5, 0.75] {
            let gy = h * (1.0 - f);
            cr.move_to(0.0, gy);
            cr.line_to(w, gy);
            let _ = cr.stroke();
        }
        let a = self.analysis.borrow();
        let Some(a) = a.as_ref().filter(|a| a.target_mode != TargetMode::None) else {
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.4);
            cr.select_font_face("Sans", cairo::FontSlant::Normal, cairo::FontWeight::Normal);
            cr.set_font_size(13.0);
            cr.move_to((w / 2.0 - 100.0).max(10.0), h / 2.0 + 4.0);
            let _ = cr.show_text(&tr("Select target to activate oscilloscope"));
            return;
        };
        let hist = &a.history;
        if hist.len() < 2 {
            return;
        }
        let n = hist.len();
        let (pb, ptop) = (10.0, 22.0);
        let gh = (h - ptop - pb).max(10.0);
        let pt = |i: usize, v: f64| (i as f64 / (n - 1).max(1) as f64 * w, h - pb - v / 100.0 * gh);
        if a.peak_score > 0.0 {
            let py = h - pb - a.peak_score / 100.0 * gh;
            cr.set_source_rgba(0.0, 0.85, 0.95, 0.6);
            cr.set_line_width(1.5);
            cr.set_dash(&[4.0, 4.0], 0.0);
            cr.move_to(0.0, py);
            cr.line_to(w, py);
            let _ = cr.stroke();
            cr.set_dash(&[], 0.0);
            cr.select_font_face("Sans", cairo::FontSlant::Normal, cairo::FontWeight::Bold);
            cr.set_font_size(10.0);
            cr.set_source_rgba(0.0, 0.85, 0.95, 0.9);
            cr.move_to((w - 75.0).max(8.0), (py - 4.0).max(12.0));
            let _ = cr.show_text(&format!("PEAK {:.0}%", a.peak_score));
        }
        cr.move_to(0.0, h);
        for (i, v) in hist.iter().enumerate() {
            let (x, y) = pt(i, *v);
            cr.line_to(x, y);
        }
        cr.line_to(w, h);
        cr.close_path();
        let pat = cairo::LinearGradient::new(0.0, ptop, 0.0, h);
        pat.add_color_stop_rgba(0.0, 0.0, 0.9, 0.7, 0.35);
        pat.add_color_stop_rgba(1.0, 0.0, 0.9, 0.7, 0.02);
        let _ = cr.set_source(&pat);
        let _ = cr.fill();
        cr.set_source_rgba(0.1, 0.95, 0.7, 0.95);
        cr.set_line_width(2.5);
        for (i, v) in hist.iter().enumerate() {
            let (x, y) = pt(i, *v);
            if i == 0 {
                cr.move_to(x, y);
            } else {
                cr.line_to(x, y);
            }
        }
        let _ = cr.stroke();
        let (cx, cy) = pt(n - 1, hist[n - 1]);
        cr.set_source_rgba(0.1, 0.95, 0.7, 0.4);
        cr.arc(cx, cy, 6.0, 0.0, 2.0 * PI);
        let _ = cr.fill();
        cr.set_source_rgb(1.0, 1.0, 1.0);
        cr.arc(cx, cy, 3.0, 0.0, 2.0 * PI);
        let _ = cr.fill();
        cr.select_font_face("Monospace", cairo::FontSlant::Normal, cairo::FontWeight::Bold);
        cr.set_font_size(11.0);
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.85);
        cr.move_to(10.0, 16.0);
        let _ = cr.show_text(&format!("{:.0} FPS  |  {}: {:.1}%", a.fps, tr("Score"), a.score));
    }

    /// Record results and leave the page.
    fn on_finish(self: &Rc<Self>) {
        if let Some(a) = self.analysis.borrow().as_ref() {
            let i = self.step.get();
            self.results.borrow_mut()[i] = (a.score, a.peak_score, a.target_name.clone());
            if let Some(l) = self.sum_tiles.get(i) {
                l.set_label(&format!("{:.0}% ({})", a.peak_score, tr(&a.target_name)));
            }
        }
        self.clone().on_closed();
        self.win.nav.pop();
    }
}
