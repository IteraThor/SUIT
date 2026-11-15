//! Widgets shared by the Camera Optimizer and Camera Controls pages: camera strip, chips, segmented choices,
//! Autodarts status with start / restart / stop.
use super::helpers::run_async;
use super::Win;
use crate::core::hbvcal::{live, oneclick, profiles};
use crate::core::i18n::{tr, trf};
use adw::prelude::*;
use gtk::{gdk, glib, pango};
use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::rc::Rc;

/// 'pci-0000:00:14.0-usb-0:2:1.0' -> '2'.
pub fn port_no(port: &str) -> String {
    regex::Regex::new(r"usb-0:([\d.]+):").unwrap().captures(port).map(|c| c[1].to_string()).unwrap_or_else(|| "?".into())
}

/// BGR mat -> texture, scaled down so its longest side is at most `max_side` (0 = full size).
pub fn to_texture(bgr: &opencv::core::Mat, max_side: i32) -> Option<gdk::Texture> {
    use opencv::prelude::*;
    if bgr.empty() {
        return None;
    }
    let (w, h) = (bgr.cols(), bgr.rows());
    let m = if max_side > 0 && w.max(h) > max_side {
        let s = max_side as f64 / w.max(h) as f64;
        let interp = if s < 0.5 { opencv::imgproc::INTER_AREA } else { opencv::imgproc::INTER_LINEAR };
        crate::core::cvx::resize(bgr, (w as f64 * s) as i32, (h as f64 * s) as i32, interp).ok()?
    } else {
        bgr.clone()
    };
    super::helpers::mat_texture(&m)
}

pub fn wrap_label(text: &str, css: &[&str]) -> gtk::Label {
    let l = gtk::Label::new(Some(&tr(text)));
    l.set_xalign(0.0);
    l.set_wrap(true);
    for c in css {
        l.add_css_class(c);
    }
    l
}

/// Small, non-interactive text.
pub fn caption(text: &str, css: &[&str], wrap: bool) -> gtk::Label {
    let l = gtk::Label::new(Some(&tr(text)));
    l.set_xalign(0.0);
    l.set_wrap(wrap);
    if wrap {
        l.set_max_width_chars(34); // wrapped text must not widen its column
    } else {
        l.set_ellipsize(pango::EllipsizeMode::End);
    }
    l.add_css_class("camopt-small");
    for c in css {
        l.add_css_class(c);
    }
    l
}

/// ● text, coloured by kind (ok / warn / bad / neutral).
pub fn status_dot(text: &str, kind: &str) -> gtk::Box {
    let b = gtk::Box::new(gtk::Orientation::Horizontal, 6);
    let d = gtk::Label::new(Some("●"));
    d.add_css_class("camopt-dot");
    d.add_css_class(&format!("camopt-{}", kind));
    b.append(&d);
    b.append(&caption(text, &[], false));
    b
}

pub fn set_dot_kind(dot: &gtk::Label, kind: &str) {
    for k in ["ok", "warn", "bad", "neutral"] {
        dot.remove_css_class(&format!("camopt-{}", k));
    }
    dot.add_css_class(&format!("camopt-{}", kind));
}

// --- camera names and the selection both camera pages share ---

fn names_file() -> std::path::PathBuf {
    profiles::store().with_file_name("names.json")
}

thread_local! {
    /// the camera picked on either page
    pub static SELECTED: RefCell<Option<String>> = const { RefCell::new(None) };
}

pub fn selected() -> Option<String> {
    SELECTED.with(|s| s.borrow().clone())
}
pub fn set_selected(p: Option<String>) {
    SELECTED.with(|s| *s.borrow_mut() = p);
}

fn load_names() -> serde_json::Map<String, serde_json::Value> {
    crate::core::util::read_json(names_file()).and_then(|v| v.as_object().cloned()).unwrap_or_default()
}

/// The name the user gave this camera, else 'Port n'.
pub fn cam_name(port: &str) -> String {
    load_names()
        .get(port)
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .map(String::from)
        .unwrap_or_else(|| trf("Port {n}", &[("n", &port_no(port))]))
}

pub fn set_cam_name(port: &str, name: &str) {
    let mut n = load_names();
    let t = name.trim();
    if t.is_empty() {
        n.remove(port);
    } else {
        n.insert(port.into(), serde_json::json!(t.chars().take(24).collect::<String>()));
    }
    let _ = crate::core::util::write_file(names_file(), &serde_json::to_string_pretty(&n).unwrap());
}

pub fn ask_rename(win: &Win, port: &str, on_done: Rc<dyn Fn()>) {
    let d = adw::AlertDialog::new(
        Some(&tr("Name This Camera")),
        Some(&trf("Shown instead of “Port {n}” on the camera pages. Leave empty to go back to the port number.", &[("n", &port_no(port))])),
    );
    let e = gtk::Entry::new();
    e.set_text(load_names().get(port).and_then(|v| v.as_str()).unwrap_or(""));
    e.set_activates_default(true);
    e.set_placeholder_text(Some(&tr("e.g. Left")));
    e.set_max_length(24);
    d.set_extra_child(Some(&e));
    d.add_response("cancel", &tr("Cancel"));
    d.add_response("save", &tr("Save"));
    d.set_response_appearance("save", adw::ResponseAppearance::Suggested);
    d.set_default_response(Some("save"));
    let p = port.to_string();
    d.connect_response(None, move |_, r| {
        if r == "save" {
            set_cam_name(&p, &e.text());
            on_done();
        }
    });
    d.present(Some(&win.window));
}

// --- chips ---

/// Small tinted pill (ok / warn / bad / neutral).
pub fn chip(text: &str, kind: &str) -> gtk::Label {
    let l = gtk::Label::new(Some(&tr(text)));
    l.set_valign(gtk::Align::Center);
    l.add_css_class("status-chip");
    l.add_css_class(&format!("chip-{}", kind));
    l
}

pub fn set_chip(l: &gtk::Label, text: &str, kind: &str) {
    l.set_label(&tr(text));
    for k in ["ok", "warn", "bad", "neutral"] {
        l.remove_css_class(&format!("chip-{}", k));
    }
    l.add_css_class(&format!("chip-{}", kind));
}

/// One-tap alternative to a dropdown for a few options: [(key, label)]. `active` may be a key not in options.
pub fn segmented(options: &[(String, String)], active: &str, on_change: Rc<dyn Fn(String)>) -> adw::ToggleGroup {
    let g = adw::ToggleGroup::new();
    g.set_valign(gtk::Align::Center);
    g.set_halign(gtk::Align::Start);
    g.add_css_class("camctl-seg");
    for (k, l) in options {
        let t = adw::Toggle::new();
        t.set_name(Some(k));
        t.set_label(Some(&tr(l)));
        g.add(t);
    }
    let keys: Vec<String> = options.iter().map(|(k, _)| k.clone()).collect();
    g.set_active_name(if keys.iter().any(|k| k == active) { Some(active) } else { None });
    g.connect_active_name_notify(move |g| {
        if let Some(n) = g.active_name() {
            if keys.iter().any(|k| *k == n.as_str()) {
                on_change(n.to_string());
            }
        }
    });
    g
}

pub fn opts(v: &[(&str, &str)]) -> Vec<(String, String)> {
    v.iter().map(|(a, b)| (a.to_string(), b.to_string())).collect()
}

// --- camera strip: one card per camera with a thumbnail, shared by both pages ---

struct Card {
    btn: gtk::Button,
    pic: gtk::Picture,
    dot: gtk::Label,
    name: gtk::Label,
    texture: Option<gdk::Texture>,
}

/// Row of camera cards that acts as the camera picker. Long-press (or right-click) a card to rename the camera.
pub struct CameraStrip {
    win: Win,
    pub widget: gtk::Box,
    cards: RefCell<HashMap<String, Card>>,
    ports: RefCell<Vec<String>>,
    on_pick: RefCell<Option<Rc<dyn Fn()>>>,
    on_rename: RefCell<Option<Rc<dyn Fn()>>>,
}

pub const THUMB_PX: i32 = 104;

pub struct StripEntry {
    pub port: String,
    pub kind: String,
}

impl CameraStrip {
    pub fn new(win: Win) -> Rc<CameraStrip> {
        let widget = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        widget.set_valign(gtk::Align::Start);
        widget.set_halign(gtk::Align::Start);
        Rc::new(CameraStrip {
            win,
            widget,
            cards: RefCell::new(HashMap::new()),
            ports: RefCell::new(Vec::new()),
            on_pick: RefCell::new(None),
            on_rename: RefCell::new(None),
        })
    }

    pub fn set_callbacks(&self, pick: Rc<dyn Fn()>, rename: Option<Rc<dyn Fn()>>) {
        *self.on_rename.borrow_mut() = Some(rename.unwrap_or_else(|| pick.clone()));
        *self.on_pick.borrow_mut() = Some(pick);
    }

    pub fn active(&self) -> Option<String> {
        let ports = self.ports.borrow();
        match selected() {
            Some(s) if ports.contains(&s) => Some(s),
            _ => ports.first().cloned(),
        }
    }

    pub fn ports(&self) -> Vec<String> {
        self.ports.borrow().clone()
    }

    /// Keeps the cards (and thumbnails) when unchanged.
    pub fn sync(self: &Rc<Self>, entries: &[StripEntry]) {
        let ports: Vec<String> = entries.iter().map(|e| e.port.clone()).collect();
        if ports != *self.ports.borrow() {
            while let Some(c) = self.widget.first_child() {
                self.widget.remove(&c);
            }
            let old: HashMap<String, Card> = std::mem::take(&mut *self.cards.borrow_mut());
            *self.ports.borrow_mut() = ports.clone();
            for p in &ports {
                self.add(p, old.get(p).and_then(|c| c.texture.clone()));
            }
        }
        if !selected().map(|s| ports.contains(&s)).unwrap_or(false) && !ports.is_empty() {
            set_selected(Some(ports[0].clone()));
        }
        for e in entries {
            self.set_kind(&e.port, &e.kind);
        }
        self.mark();
    }

    fn add(self: &Rc<Self>, port: &str, tex: Option<gdk::Texture>) {
        let btn = gtk::Button::new();
        btn.add_css_class("cam-card");
        btn.set_tooltip_text(Some(&tr("Long-press to rename")));
        btn.set_halign(gtk::Align::Start);
        let bx = gtk::Box::new(gtk::Orientation::Vertical, 4);
        let pic = gtk::Picture::new();
        pic.add_css_class("cam-thumb");
        pic.set_size_request(THUMB_PX, THUMB_PX * 3 / 4);
        pic.set_can_shrink(true);
        pic.set_content_fit(gtk::ContentFit::Cover);
        pic.set_halign(gtk::Align::Center);
        pic.set_valign(gtk::Align::Center);
        if let Some(t) = &tex {
            pic.set_paintable(Some(t));
        }
        bx.append(&pic);
        let row = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        let dot = gtk::Label::new(Some("●"));
        dot.add_css_class("camopt-dot");
        dot.add_css_class("camopt-neutral");
        let name = gtk::Label::new(Some(&cam_name(port)));
        name.set_xalign(0.0);
        name.set_ellipsize(pango::EllipsizeMode::End);
        name.set_hexpand(true);
        name.set_max_width_chars(12);
        name.add_css_class("heading");
        row.append(&dot);
        row.append(&name);
        bx.append(&row);
        btn.set_child(Some(&bx));
        let (me, p) = (Rc::downgrade(self), port.to_string());
        btn.connect_clicked(move |_| {
            if let Some(m) = me.upgrade() {
                m.pick(&p);
            }
        });
        for g in [gtk::GestureLongPress::new().upcast::<gtk::GestureSingle>(), {
            let c = gtk::GestureClick::new();
            c.set_button(3);
            c.upcast()
        }] {
            let (me, p) = (Rc::downgrade(self), port.to_string());
            let handler = move || {
                if let Some(m) = me.upgrade() {
                    let m2 = m.clone();
                    ask_rename(&m.win, &p, Rc::new(move || m2.refresh_names()));
                }
            };
            if let Some(lp) = g.downcast_ref::<gtk::GestureLongPress>() {
                lp.connect_pressed(move |_, _, _| handler());
            } else if let Some(c) = g.downcast_ref::<gtk::GestureClick>() {
                c.connect_pressed(move |_, _, _, _| handler());
            }
            btn.add_controller(g);
        }
        self.widget.append(&btn);
        self.cards.borrow_mut().insert(port.into(), Card { btn, pic, dot, name, texture: tex });
    }

    fn pick(&self, port: &str) {
        if Some(port.to_string()) == self.active() {
            return;
        }
        set_selected(Some(port.into()));
        self.mark();
        let cb = self.on_pick.borrow().clone();
        if let Some(cb) = cb {
            cb();
        }
    }

    fn mark(&self) {
        let a = self.active();
        for (p, c) in self.cards.borrow().iter() {
            if Some(p) == a.as_ref() {
                c.btn.add_css_class("selected");
            } else {
                c.btn.remove_css_class("selected");
            }
        }
    }

    pub fn refresh_names(&self) {
        for (p, c) in self.cards.borrow().iter() {
            c.name.set_label(&cam_name(p));
        }
        let cb = self.on_rename.borrow().clone();
        if let Some(cb) = cb {
            cb();
        }
    }

    pub fn set_thumb(&self, port: &str, tex: Option<gdk::Texture>) {
        if let (Some(c), Some(t)) = (self.cards.borrow_mut().get_mut(port), tex) {
            c.pic.set_paintable(Some(&t));
            c.texture = Some(t);
        }
    }

    pub fn set_kind(&self, port: &str, kind: &str) {
        if let Some(c) = self.cards.borrow().get(port) {
            set_dot_kind(&c.dot, kind);
        }
    }
}

/// One picture of this camera from Autodarts' image endpoint (BGR), or None. Never opens the camera itself.
pub fn fetch_autodarts_frame(dev: &str) -> Option<opencv::core::Mat> {
    let idx = crate::core::hbvcal::autodarts::autodarts_index(dev)?;
    let data = crate::core::hbvcal::autodarts::get_bytes(&format!("/api/img/cams/{}", idx), 2.0)?;
    let img = crate::core::cvx::decode_jpeg(&data)?;
    // all black = camera standby
    if crate::core::cvx::max_val(&crate::core::cvx::gray(&img).ok()?).0 > 0.0 { Some(img) } else { None }
}

/// Refreshes the strip thumbnails from Autodarts' pictures every few seconds while the page is open.
pub struct ThumbPoller {
    strip: Rc<CameraStrip>,
    devices: Rc<dyn Fn() -> HashMap<String, String>>,
    skip: Rc<dyn Fn() -> Option<String>>,
    every_ms: u64,
    timer: RefCell<Option<glib::SourceId>>,
    busy: Rc<Cell<bool>>,
}

impl ThumbPoller {
    pub fn new(strip: Rc<CameraStrip>, devices: Rc<dyn Fn() -> HashMap<String, String>>, skip: Rc<dyn Fn() -> Option<String>>) -> Rc<ThumbPoller> {
        Rc::new(ThumbPoller { strip, devices, skip, every_ms: 4000, timer: RefCell::new(None), busy: Rc::new(Cell::new(false)) })
    }

    pub fn start(self: &Rc<Self>) {
        if self.timer.borrow().is_none() {
            let me = Rc::downgrade(self);
            let id = super::helpers::every(self.every_ms, move || match me.upgrade() {
                Some(m) => {
                    m.tick();
                    true
                }
                None => false,
            });
            *self.timer.borrow_mut() = Some(id);
            self.tick();
        }
    }

    /// Fetch now (the camera list has just arrived).
    pub fn kick(&self) {
        if self.timer.borrow().is_some() {
            self.tick();
        }
    }

    pub fn stop(&self) {
        if let Some(id) = self.timer.borrow_mut().take() {
            id.remove();
        }
    }

    fn tick(&self) {
        if self.busy.get() || oneclick::JOB.running() || !self.strip.widget.is_mapped() {
            return;
        }
        self.busy.set(true);
        let skip = (self.skip)();
        let targets: Vec<(String, String)> = (self.devices)().into_iter().filter(|(p, _)| Some(p) != skip.as_ref()).collect();
        let (strip, busy) = (self.strip.clone(), self.busy.clone());
        run_async(
            move || {
                targets
                    .into_iter()
                    .map(|(p, d)| {
                        let img = fetch_autodarts_frame(&d).and_then(|m| {
                            let w = m.cols().max(m.rows());
                            let s = THUMB_PX as f64 / w as f64;
                            crate::core::cvx::resize(&m, (m.cols() as f64 * s) as i32, (m.rows() as f64 * s) as i32, opencv::imgproc::INTER_AREA).ok()
                        });
                        (p, img.and_then(|m| crate::core::cvx::to_rgb_bytes(&m).ok()))
                    })
                    .collect::<Vec<_>>()
            },
            move |res| {
                busy.set(false);
                for (p, rgb) in res {
                    strip.set_thumb(&p, rgb.map(|(w, h, d)| super::helpers::rgb_texture(w, h, d)));
                }
            },
        );
    }
}

use opencv::prelude::MatTraitConst;

// --- Autodarts status with start / restart / stop ---

/// '● Autodarts running ▾': a status pill that opens start / restart / stop.
pub struct AutodartsStatus {
    win: Win,
    pub widget: gtk::MenuButton,
    dot: gtk::Label,
    text: gtk::Label,
    buttons: HashMap<&'static str, gtk::Button>,
    pub state: RefCell<String>,
}

impl AutodartsStatus {
    pub fn new(win: Win) -> Rc<AutodartsStatus> {
        let widget = gtk::MenuButton::new();
        widget.set_valign(gtk::Align::Center);
        widget.add_css_class("flat");
        widget.add_css_class("compact-btn");
        let dot = gtk::Label::new(Some("●"));
        dot.add_css_class("camopt-dot");
        let text = caption("Autodarts…", &[], false);
        let content = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        content.append(&dot);
        content.append(&text);
        content.append(&gtk::Image::from_icon_name("pan-down-symbolic"));
        widget.set_child(Some(&content));
        let pop = gtk::Popover::new();
        let bx = gtk::Box::new(gtk::Orientation::Vertical, 2);
        let mut buttons = HashMap::new();
        for (a, l) in [("start", "Start Autodarts"), ("restart", "Restart Autodarts"), ("stop", "Stop Autodarts")] {
            let b = gtk::Button::with_label(&tr(l));
            b.add_css_class("flat");
            bx.append(&b);
            buttons.insert(a, b);
        }
        pop.set_child(Some(&bx));
        widget.set_popover(Some(&pop));
        let me = Rc::new(AutodartsStatus { win, widget, dot, text, buttons, state: RefCell::new(String::new()) });
        for (a, b) in &me.buttons {
            let (m, a, pop) = (Rc::downgrade(&me), *a, pop.clone());
            b.connect_clicked(move |_| {
                if let Some(m) = m.upgrade() {
                    pop.popdown();
                    m.do_action(a);
                }
            });
        }
        me
    }

    pub fn refresh(self: &Rc<Self>) {
        let m = self.clone();
        run_async(|| oneclick::systemctl("is-active", "autodarts"), move |s| m.show(&s));
    }

    pub fn show(&self, state: &str) {
        *self.state.borrow_mut() = state.to_string();
        let ok = state == "active";
        if ok {
            self.text.set_label(&tr("Autodarts running"));
        } else if !state.is_empty() {
            self.text.set_label(&trf("Autodarts {state}", &[("state", &tr(state))]));
        } else {
            self.text.set_label(&tr("Autodarts not found"));
        }
        set_dot_kind(&self.dot, if ok { "ok" } else { "neutral" });
        self.buttons["start"].set_visible(!ok);
        self.buttons["restart"].set_visible(ok);
        self.buttons["stop"].set_visible(ok);
        self.widget.set_sensitive(!oneclick::JOB.running());
    }

    fn do_action(self: &Rc<Self>, a: &'static str) {
        self.win.show_toast(match a {
            "start" => "Starting Autodarts…",
            "restart" => "Restarting Autodarts…",
            _ => "Stopping Autodarts…",
        });
        let m = self.clone();
        run_async(move || oneclick::systemctl(a, "autodarts"), move |_| {
            let m2 = m.clone();
            super::helpers::once(3000, move || m2.refresh());
        });
    }
}
