//! Screen & Touch Manager: per-display rotation with touch calibration, confirmed within 15 s or reverted.
use super::helpers::{group, page_shell, run_async};
use super::{Page, Win};
use crate::core::display::{self, normalize_rotation};
use crate::core::i18n::{tr, trf};
use adw::prelude::*;
use gtk::glib;
use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::rc::Rc;

pub struct RotationView {
    nav: adw::NavigationPage,
    win: Win,
    container: gtk::Box,
    staged_rot: RefCell<HashMap<String, i64>>,
    staged_touch: RefCell<HashMap<String, String>>,
    live_rot: RefCell<HashMap<String, i64>>,
    live_touch: RefCell<HashMap<String, String>>,
    dialog: RefCell<Option<adw::AlertDialog>>,
    timer: RefCell<Option<glib::SourceId>>,
    remaining: Cell<i32>,
}

impl Page for RotationView {
    fn nav(&self) -> &adw::NavigationPage {
        &self.nav
    }

    fn refresh(self: Rc<Self>) {
        let m = self.clone();
        run_async(|| (display::get_monitors(), display::get_touchscreens(), display::load_config()), move |(mons, ts, cfg)| m.render(mons, ts, cfg));
    }
}

fn rot_name(r: i64) -> String {
    tr(match r {
        0 => "Normal (0°)",
        1 => "Right (90°)",
        2 => "Inverted (180°)",
        3 => "Left (270°)",
        _ => "Unknown",
    })
}

impl RotationView {
    pub fn new(win: Win) -> Rc<dyn Page> {
        let (scrolled, main) = page_shell();
        let nav = adw::NavigationPage::builder().title(tr("Screen & Touch Manager")).tag("rotation").child(&scrolled).build();
        let wc = gtk::Box::new(gtk::Orientation::Horizontal, 14);
        wc.add_css_class("warning-card");
        main.append(&wc);
        let wi = gtk::Image::from_icon_name("dialog-warning-symbolic");
        wi.set_pixel_size(24);
        wi.set_valign(gtk::Align::Center);
        wc.append(&wi);
        let wl = super::helpers::wrap_label(&tr("Only use this tool if your device has issues rotating touch input using default system settings."), "warning-text");
        wl.set_hexpand(true);
        wl.set_valign(gtk::Align::Center);
        wc.append(&wl);
        let ab = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        ab.set_valign(gtk::Align::Center);
        let b_clear = super::helpers::icon_button("edit-clear-symbolic", "Clear Config", "secondary-btn compact-btn", -1, false);
        b_clear.set_tooltip_text(Some(&tr("Clear SUIT rotation overrides and remove boot autostart")));
        b_clear.set_valign(gtk::Align::Center);
        ab.append(&b_clear);
        let b_ds = super::helpers::icon_button("preferences-desktop-display-symbolic", "Display Settings", "secondary-btn compact-btn", -1, false);
        b_ds.set_tooltip_text(Some(&tr("Open GNOME Display Settings")));
        b_ds.set_valign(gtk::Align::Center);
        b_ds.connect_clicked(|_| {
            display::open_display_settings();
        });
        ab.append(&b_ds);
        wc.append(&ab);
        let g = group(&tr("Connected Displays"), "");
        let container = gtk::Box::new(gtk::Orientation::Vertical, 12);
        g.add(&container);
        main.append(&g);
        let me = Rc::new(RotationView {
            nav,
            win,
            container,
            staged_rot: RefCell::new(HashMap::new()),
            staged_touch: RefCell::new(HashMap::new()),
            live_rot: RefCell::new(HashMap::new()),
            live_touch: RefCell::new(HashMap::new()),
            dialog: RefCell::new(None),
            timer: RefCell::new(None),
            remaining: Cell::new(15),
        });
        let m = me.clone();
        b_clear.connect_clicked(move |_| {
            m.win.show_toast("Clearing rotation overrides...");
            let m2 = m.clone();
            run_async(|| display::clear_config(true), move |ok| {
                m2.staged_rot.borrow_mut().clear();
                m2.staged_touch.borrow_mut().clear();
                m2.live_rot.borrow_mut().clear();
                m2.live_touch.borrow_mut().clear();
                m2.win.show_toast(if ok { "Cleared rotation overrides and autostart." } else { "Failed clearing configuration." });
                m2.clone().refresh();
            });
        });
        let m = me.clone();
        me.nav.connect_map(move |_| m.clone().refresh());
        let m = me.clone();
        me.nav.connect_unmap(move |_| m.cleanup_confirmation());
        me
    }

    fn render(self: &Rc<Self>, mons: Vec<display::MonitorView>, ts: Vec<String>, cfg: serde_json::Map<String, serde_json::Value>) {
        super::helpers::clear_box(&self.container);
        if mons.is_empty() {
            let r = super::helpers::action_row(&tr("No monitors detected via GNOME Mutter"), &tr("Check display connections."));
            self.container.append(&r);
            return;
        }
        for mon in mons {
            let name = mon.name.clone();
            let live = mon.rotation as i64;
            let saved = cfg.get(&name).cloned().unwrap_or_default();
            let saved_rot = if saved.get("rotation").is_some() { normalize_rotation(&saved["rotation"]) } else { live };
            let saved_touch = saved.get("touch_device").and_then(|v| v.as_str()).unwrap_or("None").to_string();
            self.live_rot.borrow_mut().insert(name.clone(), live);
            self.live_touch.borrow_mut().insert(name.clone(), saved_touch.clone());
            self.staged_rot.borrow_mut().entry(name.clone()).or_insert(saved_rot);
            // Auto-select the only touchscreen when none is configured yet, so the touch rotates with the screen
            let st = if (saved_touch == "None" || saved_touch.is_empty()) && ts.len() == 1 { ts[0].clone() } else { saved_touch };
            self.staged_touch.borrow_mut().entry(name.clone()).or_insert(st);
            let cur = self.staged_rot.borrow()[&name];

            let card = gtk::Box::new(gtk::Orientation::Vertical, 14);
            card.add_css_class("tile-card");
            card.set_margin_bottom(8);
            let header = gtk::Box::new(gtk::Orientation::Horizontal, 10);
            card.append(&header);
            let icon = gtk::Image::from_icon_name("video-display-symbolic");
            icon.set_pixel_size(24);
            header.append(&icon);
            let tb = gtk::Box::new(gtk::Orientation::Horizontal, 8);
            tb.set_hexpand(true);
            header.append(&tb);
            let lt = gtk::Label::new(Some(&format!("{} ({})", name, mon.res)));
            lt.set_xalign(0.0);
            lt.add_css_class("heading");
            tb.append(&lt);
            if mon.is_primary {
                tb.append(&super::helpers::status_pill(&tr("Primary"), "status-pill-checking"));
            }
            header.append(&super::helpers::status_pill(&rot_name(live), "status-pill-checking"));
            let sec = gtk::Box::new(gtk::Orientation::Vertical, 6);
            card.append(&sec);
            let la = gtk::Label::new(Some(&tr("Target Orientation")));
            la.set_xalign(0.0);
            la.add_css_class("dim-label");
            sec.append(&la);
            let bb = gtk::Box::new(gtk::Orientation::Horizontal, 8);
            bb.set_homogeneous(true);
            sec.append(&bb);
            let buttons: Rc<RefCell<Vec<(i64, gtk::Button)>>> = Rc::new(RefCell::new(vec![]));
            for (r, l) in [(0, "0° Normal"), (1, "90° Right"), (2, "180° Inverted"), (3, "270° Left")] {
                let b = gtk::Button::with_label(&tr(l));
                b.add_css_class("touch-btn");
                b.add_css_class("angle-touch-btn");
                b.set_size_request(-1, 44);
                if r == cur {
                    b.add_css_class("suggested-action");
                }
                let (m, n, bs) = (self.clone(), name.clone(), buttons.clone());
                b.connect_clicked(move |_| {
                    m.staged_rot.borrow_mut().insert(n.clone(), r);
                    for (rr, bo) in bs.borrow().iter() {
                        if *rr == r {
                            bo.add_css_class("suggested-action");
                        } else {
                            bo.remove_css_class("suggested-action");
                        }
                    }
                });
                bb.append(&b);
                buttons.borrow_mut().push((r, b));
            }
            let tbx = gtk::Box::new(gtk::Orientation::Horizontal, 12);
            tbx.set_valign(gtk::Align::Center);
            card.append(&tbx);
            let ti = gtk::Image::from_icon_name("input-tablet-symbolic");
            ti.set_pixel_size(22);
            tbx.append(&ti);
            let tl = gtk::Label::new(Some(&tr("Assigned Touchscreen")));
            tl.set_xalign(0.0);
            tl.set_hexpand(true);
            tbx.append(&tl);
            let options: Vec<String> = std::iter::once("None".to_string()).chain(ts.iter().cloned()).collect();
            let shown: Vec<String> = std::iter::once(tr("None")).chain(ts.iter().cloned()).collect();
            let shown_ref: Vec<&str> = shown.iter().map(|s| s.as_str()).collect();
            let dd = gtk::DropDown::from_strings(&shown_ref);
            dd.set_valign(gtk::Align::Center);
            dd.set_size_request(240, 42);
            let staged = self.staged_touch.borrow().get(&name).cloned().unwrap_or_else(|| "None".into());
            dd.set_selected(options.iter().position(|o| *o == staged).unwrap_or(0) as u32);
            let (m, n, o) = (self.clone(), name.clone(), options.clone());
            dd.connect_selected_notify(move |d| {
                let c = o.get(d.selected() as usize).cloned().unwrap_or_else(|| "None".into());
                m.staged_touch.borrow_mut().insert(n.clone(), c);
            });
            tbx.append(&dd);
            let act = gtk::Box::new(gtk::Orientation::Horizontal, 10);
            act.set_margin_top(4);
            card.append(&act);
            let ba = super::helpers::icon_button("emblem-ok-symbolic", "Apply Changes", "suggested-action", 44, true);
            ba.set_hexpand(true);
            let (m, n) = (self.clone(), name.clone());
            ba.connect_clicked(move |_| m.apply_settings(&n));
            act.append(&ba);
            self.container.append(&card);
        }
    }

    fn cleanup_timer(&self) {
        if let Some(id) = self.timer.borrow_mut().take() {
            id.remove();
        }
    }

    fn cleanup_confirmation(&self) {
        self.cleanup_timer();
        if let Some(d) = self.dialog.borrow_mut().take() {
            d.close();
        }
    }

    fn apply_settings(self: &Rc<Self>, c: &str) {
        let tr_rot = self.staged_rot.borrow().get(c).copied().unwrap_or(0);
        let tt = self.staged_touch.borrow().get(c).cloned().unwrap_or_else(|| "None".into());
        let pr = self.live_rot.borrow().get(c).copied().unwrap_or(0);
        let pt = self.live_touch.borrow().get(c).cloned().unwrap_or_else(|| "None".into());
        let cn = c.to_string();
        if tr_rot == pr && tt == pt {
            self.win.show_toast(&trf("Saving display settings for {connector}...", &[("connector", &c)]));
            let m = self.clone();
            run_async(move || display::apply_display_and_touch(&cn, tr_rot, Some(&tt), None, true, None), move |ok| {
                m.win.show_toast(if ok { "Display settings saved." } else { "Failed saving display settings." });
                m.clone().refresh();
            });
            return;
        }
        self.win.show_toast(&trf("Applying orientation and touch settings to {connector}...", &[("connector", &c)]));
        let m = self.clone();
        let (tt2, pt2) = (tt.clone(), pt.clone());
        // persist=False so an unexpected power loss / reboot restores the previous settings
        run_async(
            move || {
                let prev = if pt2 != "None" && !pt2.is_empty() { Some(pt2.as_str()) } else { None };
                display::apply_display_and_touch(&cn, tr_rot, Some(&tt2), None, false, prev)
            },
            {
                let c = c.to_string();
                move |ok| {
                    if ok {
                        m.show_confirmation(&c, tr_rot, tt, pr, pt);
                    } else {
                        m.win.show_toast("Failed applying display orientation.");
                        m.clone().refresh();
                    }
                }
            },
        );
    }

    fn show_confirmation(self: &Rc<Self>, c: &str, target_rot: i64, target_touch: String, prev_rot: i64, prev_touch: String) {
        self.cleanup_timer();
        self.remaining.set(15);
        let body = |s: i32| trf("Reverting to previous settings in {seconds} seconds.", &[("seconds", &s)]);
        let d = adw::AlertDialog::new(Some(&tr("Keep Display Settings?")), Some(&body(15)));
        d.add_response("revert", &tr("Revert"));
        d.add_response("keep", &tr("Keep Changes"));
        d.set_response_appearance("keep", adw::ResponseAppearance::Suggested);
        d.set_response_appearance("revert", adw::ResponseAppearance::Destructive);
        d.set_default_response(Some("keep"));
        d.set_close_response("revert");
        let done = Rc::new(Cell::new(false));
        let (m, cn, tt, pt, dn) = (self.clone(), c.to_string(), target_touch.clone(), prev_touch.clone(), done.clone());
        d.connect_response(None, move |_, r| {
            if dn.replace(true) {
                return;
            }
            m.cleanup_timer();
            *m.dialog.borrow_mut() = None;
            if r == "keep" {
                let (m2, cn2, tt2) = (m.clone(), cn.clone(), tt.clone());
                run_async(
                    {
                        let (cn, tt) = (cn.clone(), tt.clone());
                        move || display::persist_display_config(&cn, target_rot, Some(&tt))
                    },
                    move |_| {
                        m2.live_rot.borrow_mut().insert(cn2.clone(), target_rot);
                        m2.live_touch.borrow_mut().insert(cn2, tt2);
                        m2.win.show_toast("Display settings confirmed.");
                        m2.clone().refresh();
                    },
                );
            } else {
                m.win.show_toast(&trf("Reverting display orientation for {connector}...", &[("connector", &cn)]));
                let (m2, cn2, pt2) = (m.clone(), cn.clone(), pt.clone());
                run_async(
                    {
                        let (cn, pt, tt) = (cn.clone(), pt.clone(), tt.clone());
                        move || {
                            let prev = if tt != "None" && !tt.is_empty() { Some(tt.as_str()) } else { None };
                            display::apply_display_and_touch(&cn, prev_rot, Some(&pt), None, true, prev)
                        }
                    },
                    move |_| {
                        m2.staged_rot.borrow_mut().insert(cn2.clone(), prev_rot);
                        m2.staged_touch.borrow_mut().insert(cn2.clone(), pt2.clone());
                        m2.live_rot.borrow_mut().insert(cn2.clone(), prev_rot);
                        m2.live_touch.borrow_mut().insert(cn2, pt2);
                        m2.win.show_toast("Reverted to previous display settings.");
                        m2.clone().refresh();
                    },
                );
            }
        });
        let m = self.clone();
        let id = glib::timeout_add_seconds_local(1, move || {
            let r = m.remaining.get() - 1;
            m.remaining.set(r);
            let d = m.dialog.borrow().clone();
            if r <= 0 {
                *m.timer.borrow_mut() = None;
                if let Some(d) = d {
                    d.close(); // close response = revert
                }
                return glib::ControlFlow::Break;
            }
            if let Some(d) = d {
                d.set_body(&body(r));
            }
            glib::ControlFlow::Continue
        });
        *self.timer.borrow_mut() = Some(id);
        d.present(Some(&self.win.window));
        *self.dialog.borrow_mut() = Some(d);
    }
}
