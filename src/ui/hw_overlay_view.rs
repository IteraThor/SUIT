//! Hardware Overlay submenu: on/off, its look, and which metrics it shows. Every change is saved at once; the
//! shell extension watches its config file and redraws live, so the wallpaper itself is the preview.
use super::camera_widgets::{opts, segmented};
use super::helpers::{esc, group, icon_button, page_shell, run_async, switch_row, top_clamp, view_switcher};
use super::{Page, Win};
use crate::core::desktop;
use crate::core::i18n::{tr, trf};
use adw::prelude::*;
use gtk::glib;
use serde_json::{json, Value};
use std::cell::{Cell, RefCell};
use std::rc::Rc;

const SYSTEM_METRICS: [(&str, &str); 7] = [
    ("cpu", "CPU Usage and Clock"),
    ("temp", "CPU Temperature"),
    ("ram", "Memory"),
    ("gpu", "Graphics (AMD GPUs only)"),
    ("disk", "Disk Space"),
    ("load", "Load Average"),
    ("uptime", "Uptime"),
];
const NETWORK_METRICS: [(&str, &str); 3] = [("net", "Network Speed"), ("ip_local", "Local Network IP"), ("ip_tailscale", "Tailscale IP")];
const AUTODARTS_METRICS: [(&str, &str); 4] =
    [("ad_state", "Engine State and Version"), ("ad_fps", "Average FPS"), ("ad_cams", "Per-Camera FPS"), ("ad_proc", "Autodarts Process Usage")];
/// Clickable controls the overlay can show, same actions as the buttons on Autodarts' own page
const AUTODARTS_BUTTONS: [(&str, &str); 4] = [
    ("ad_toggle", "Start / Stop Button"),
    ("ad_reset", "Reset Button"),
    ("ad_dump", "Debug Snapshot Button (saves to Downloads)"),
    ("ad_update", "Update Button (when an update is available)"),
];
const POSITIONS: [(&str, &str); 4] = [("top-left", "Top Left"), ("top-right", "Top Right"), ("bottom-left", "Bottom Left"), ("bottom-right", "Bottom Right")];

/// One-line state for the System page row, e.g. 'On · 9 metrics · Top Right'.
pub fn summarize_hw_overlay(enabled: bool, cfg: &Value) -> String {
    if !enabled {
        return tr("Off. Show live system and camera stats on the wallpaper.");
    }
    let n = cfg["metrics"].as_object().map(|m| m.values().filter(|v| v.as_bool().unwrap_or(false)).count()).unwrap_or(0);
    let metrics = if n == 1 { tr("1 metric") } else { trf("{n} metrics", &[("n", &n)]) };
    let pos = cfg["position"].as_str().unwrap_or("top-right");
    let pl = POSITIONS.iter().find(|(k, _)| *k == pos).map(|(_, l)| *l).unwrap_or("Top Right");
    trf("On · {metrics} · {position}", &[("metrics", &metrics), ("position", &tr(pl))])
}

pub struct HwOverlayView {
    nav: adw::NavigationPage,
    win: Win,
    updating: Cell<bool>,
    save_id: RefCell<Option<glib::SourceId>>,
    config: RefCell<Value>,
    row_enabled: adw::SwitchRow,
    segs: RefCell<Vec<(adw::ToggleGroup, &'static str)>>,
    row_opacity: adw::ActionRow,
    scale_opacity: gtk::Scale,
    flags: RefCell<Vec<(adw::SwitchRow, &'static str, Option<&'static str>)>>,
}

impl Page for HwOverlayView {
    fn nav(&self) -> &adw::NavigationPage {
        &self.nav
    }
    fn refresh(self: Rc<Self>) {
        let m = self.clone();
        run_async(
            || (desktop::is_hw_overlay_enabled(), desktop::get_hw_overlay_config(), desktop::hw_overlay_needs_relogin()),
            move |(en, cfg, rl)| {
                *m.config.borrow_mut() = cfg;
                m.load_into_widgets(en, rl);
            },
        );
    }
}

impl HwOverlayView {
    pub fn new(win: Win) -> Rc<dyn Page> {
        let gov = group(&tr("Overlay"), "");
        let btn_reset = icon_button("edit-undo-symbolic", "Reset", "secondary-btn compact-btn", 36, false);
        btn_reset.set_tooltip_text(Some(&tr("Reset every overlay setting to its default")));
        gov.set_header_suffix(Some(&btn_reset));
        let row_enabled = switch_row("Show on Desktop", "Checking...");
        gov.add(&row_enabled);
        let (sl, look) = page_shell();
        let (ss, sys) = page_shell();
        let (sn, net) = page_shell();
        let (sa, ad) = page_shell();
        let stack = adw::ViewStack::new();
        stack.set_vexpand(true);
        stack.add_titled(&sl, Some("look"), &tr("Look"));
        stack.add_titled(&ss, Some("system"), &tr("System"));
        stack.add_titled(&sn, Some("network"), &tr("Network"));
        stack.add_titled(&sa, Some("autodarts"), "Autodarts");
        let pb = gtk::Box::new(gtk::Orientation::Vertical, 0);
        pb.append(&top_clamp(&gov));
        pb.append(&view_switcher(&stack, 4));
        pb.append(&stack);
        let nav = adw::NavigationPage::builder().title(tr("Hardware Overlay")).tag("hw_overlay").child(&pb).build();
        let config = desktop::get_hw_overlay_config();
        let row_opacity = adw::ActionRow::new();
        row_opacity.set_title(&tr("Card Opacity"));
        let scale_opacity = gtk::Scale::with_range(gtk::Orientation::Horizontal, 0.0, 100.0, 5.0);
        scale_opacity.set_size_request(240, -1);
        scale_opacity.set_valign(gtk::Align::Center);
        scale_opacity.set_draw_value(true);
        scale_opacity.set_value_pos(gtk::PositionType::Left);
        scale_opacity.set_format_value_func(|_, v| format!("{:.0} %", v));
        scale_opacity.set_value(config["opacity"].as_f64().unwrap_or(70.0));
        row_opacity.add_suffix(&scale_opacity);
        let me = Rc::new(HwOverlayView {
            nav,
            win,
            updating: Cell::new(false),
            save_id: RefCell::new(None),
            config: RefCell::new(config),
            row_enabled,
            segs: RefCell::new(vec![]),
            row_opacity,
            scale_opacity,
            flags: RefCell::new(vec![]),
        });
        // Look: one group, every row a single line
        let gl = adw::PreferencesGroup::new();
        look.append(&gl);
        me.add_choice(&gl, "Style", "style", opts(&[("card", "Card"), ("minimal", "Text Only")]));
        me.add_choice(&gl, "Position", "position", opts(&POSITIONS));
        me.add_choice(&gl, "Size", "size", opts(&[("small", "Small"), ("medium", "Medium"), ("large", "Large")]));
        me.add_choice(&gl, "Meter Color", "accent", opts(&[("blue", "Blue"), ("teal", "Teal"), ("green", "Green"), ("purple", "Purple"), ("white", "White")]));
        me.add_choice(&gl, "Refresh Every", "interval", opts(&[("1", "1 s"), ("2", "2 s"), ("5", "5 s")]));
        gl.add(&me.row_opacity);
        me.add_flag(&gl, "Usage Meters", "", "meters", None);
        me.add_flag(&gl, "Section Titles", "", "headings", None);
        me.add_flag(&gl, "Color Action Buttons", "Start green, Stop red, Reset yellow, Debug blue", "button_colors", None);
        for (bx, list) in [(&sys, &SYSTEM_METRICS[..]), (&net, &NETWORK_METRICS[..]), (&ad, &AUTODARTS_METRICS[..])] {
            let g = group(&tr("Show"), "");
            bx.append(&g);
            for (k, n) in list {
                me.add_flag(&g, n, "", k, Some("metrics"));
            }
        }
        let gb = group(&tr("Buttons"), "");
        ad.append(&gb);
        for (k, n) in AUTODARTS_BUTTONS {
            me.add_flag(&gb, n, "", k, Some("buttons"));
        }
        me.sync_sensitivity();
        let m = me.clone();
        me.scale_opacity.connect_value_changed(move |s| m.on_opacity_changed(s));
        let m = me.clone();
        btn_reset.connect_clicked(move |_| {
            *m.config.borrow_mut() = desktop::save_hw_overlay_config(&json!({}));
            m.load_into_widgets(m.row_enabled.is_active(), false);
            m.win.show_toast("Overlay settings reset to defaults.");
            m.changed();
        });
        let m = me.clone();
        me.row_enabled.connect_active_notify(move |r| m.on_enabled_toggled(r));
        me
    }

    fn add_choice(self: &Rc<Self>, g: &adw::PreferencesGroup, title: &str, key: &'static str, options: Vec<(String, String)>) {
        let row = adw::ActionRow::new();
        row.set_title(&esc(&tr(title)));
        let cur = val_str(&self.config.borrow()[key]);
        let m = Rc::downgrade(self);
        let seg = segmented(
            &options,
            &cur,
            Rc::new(move |v: String| {
                if let Some(m) = m.upgrade() {
                    let jv = if key == "interval" { json!(v.parse::<i64>().unwrap_or(2)) } else { json!(v) };
                    m.set(key, jv);
                }
            }),
        );
        row.add_suffix(&seg);
        g.add(&row);
        self.segs.borrow_mut().push((seg, key));
    }

    fn add_flag(self: &Rc<Self>, g: &adw::PreferencesGroup, title: &str, sub: &str, key: &'static str, section: Option<&'static str>) {
        let row = switch_row(title, sub);
        row.set_active(self.flag(key, section));
        let m = Rc::downgrade(self);
        row.connect_active_notify(move |r| {
            if let Some(m) = m.upgrade() {
                if m.updating.get() {
                    return;
                }
                match section {
                    Some(s) => {
                        m.config.borrow_mut()[s][key] = json!(r.is_active());
                        m.save();
                    }
                    None => m.set(key, json!(r.is_active())),
                }
            }
        });
        g.add(&row);
        self.flags.borrow_mut().push((row, key, section));
    }

    fn flag(&self, key: &str, section: Option<&str>) -> bool {
        let c = self.config.borrow();
        match section {
            Some(s) => c[s][key].as_bool().unwrap_or(false),
            None => c[key].as_bool().unwrap_or(false),
        }
    }

    fn load_into_widgets(&self, enabled: bool, relogin: bool) {
        self.updating.set(true);
        self.row_enabled.set_active(enabled);
        self.row_enabled.set_subtitle(&tr(if relogin {
            "Overlay updated. Log out and back in once so these settings apply live."
        } else if enabled {
            "Shown on the wallpaper."
        } else {
            "Hidden. The settings below are kept."
        }));
        for (seg, key) in self.segs.borrow().iter() {
            seg.set_active_name(Some(&val_str(&self.config.borrow()[*key])));
        }
        self.scale_opacity.set_value(self.config.borrow()["opacity"].as_f64().unwrap_or(70.0));
        for (row, key, sec) in self.flags.borrow().iter() {
            row.set_active(self.flag(key, *sec));
        }
        self.updating.set(false);
        self.sync_sensitivity();
    }

    fn sync_sensitivity(&self) {
        self.row_opacity.set_sensitive(self.config.borrow()["style"].as_str() == Some("card"));
    }

    fn set(&self, key: &str, v: Value) {
        if self.updating.get() || self.config.borrow()[key] == v {
            return;
        }
        self.config.borrow_mut()[key] = v;
        self.sync_sensitivity();
        self.save();
    }

    fn save(&self) {
        let c = desktop::save_hw_overlay_config(&self.config.borrow());
        *self.config.borrow_mut() = c;
        self.changed();
    }

    fn changed(&self) {
        if let Some(s) = self.win.cached("system") {
            s.refresh();
        }
    }

    fn on_opacity_changed(self: &Rc<Self>, s: &gtk::Scale) {
        if self.updating.get() {
            return;
        }
        // Dragging fires many changes; save once the slider settles
        if let Some(id) = self.save_id.borrow_mut().take() {
            id.remove();
        }
        let (m, s) = (self.clone(), s.clone());
        let id = super::helpers::once(250, move || {
            *m.save_id.borrow_mut() = None;
            m.set("opacity", json!(s.value() as i64));
        });
        *self.save_id.borrow_mut() = Some(id);
    }

    fn on_enabled_toggled(self: &Rc<Self>, row: &adw::SwitchRow) {
        if self.updating.get() {
            return;
        }
        let a = row.is_active();
        row.set_sensitive(false);
        let (m, r) = (self.clone(), row.clone());
        run_async(move || desktop::set_hw_overlay(a), move |(_, msg)| {
            r.set_sensitive(true);
            m.win.show_toast(&msg);
            m.clone().refresh();
            m.changed();
        });
    }
}

fn val_str(v: &Value) -> String {
    v.as_str().map(String::from).unwrap_or_else(|| v.to_string())
}
