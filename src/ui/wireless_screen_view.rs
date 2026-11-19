//! Wireless Screen wizard: an Android or iOS tablet as the screen of this PC (Sunshine + Moonlight).
//! One simple screen at a time, picked from the status (also after the restart): Start -> Working ->
//! Restart -> Connect -> Done. SUIT does everything else by itself.
use super::helpers::{alert, ask, copy_to_clipboard, every, generate_qr_texture, run_async, switch_row};
use super::{Page, Win};
use crate::core::i18n::{tr, trf};
use crate::core::wireless_screen::{self as ws, Check, Layout, Status};
use adw::prelude::*;
use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

const RATES: [u32; 2] = [60, 120];
/// Screens in order; the progress dots follow it ("error" shares the dot of "working", "check" the one of "done").
const SCREENS: [&str; 5] = ["start", "working", "restart", "connect", "done"];
const CHECKS: [(&str, &str); 5] =
    [("screen", "Virtual screen"), ("sunshine", "Sunshine"), ("ports", "Network ports"), ("watcher", "Monitor watcher"), ("paired", "Tablet paired")];

/// Screenshot mode (`suit wireless-screen --screenshots`): fake data only, no status reads, no handler runs.
pub static DEMO: AtomicBool = AtomicBool::new(false);

fn demo() -> bool {
    DEMO.load(Ordering::Relaxed)
}

thread_local! {
    /// The page built in screenshot mode, so the screenshot run can set its fake states.
    pub static DEMO_VIEW: RefCell<Option<Rc<WirelessScreenView>>> = const { RefCell::new(None) };
}

/// Centered screen: big icon (none for ""), short title, one dim line; extra widgets go below.
fn screen(icon: &str, title: &str) -> (gtk::Box, gtk::Label, gtk::Label) {
    let b = gtk::Box::new(gtk::Orientation::Vertical, 14);
    b.set_valign(gtk::Align::Center);
    b.set_halign(gtk::Align::Center);
    b.set_margin_start(24);
    b.set_margin_end(24);
    b.set_margin_top(18);
    b.set_margin_bottom(18);
    if !icon.is_empty() {
        let img = gtk::Image::from_icon_name(icon);
        img.set_pixel_size(80);
        img.add_css_class(if icon == "object-select-symbolic" { "success" } else { "dim-label" });
        b.append(&img);
    }
    let t = gtk::Label::new(Some(&tr(title)));
    t.add_css_class("title-1");
    t.set_wrap(true);
    t.set_justify(gtk::Justification::Center);
    b.append(&t);
    let line = gtk::Label::new(None);
    line.add_css_class("dim-label");
    line.set_wrap(true);
    line.set_justify(gtk::Justification::Center);
    b.append(&line);
    (b, t, line)
}

fn big_button(b: &gtk::Box, label: &str) -> gtk::Button {
    let btn = gtk::Button::with_label(&tr(label));
    btn.add_css_class("suggested-action");
    btn.add_css_class("pill");
    btn.set_size_request(240, 52);
    btn.set_halign(gtk::Align::Center);
    btn.set_margin_top(6);
    b.append(&btn);
    btn
}

fn pill(label: &str) -> gtk::Button {
    let btn = gtk::Button::with_label(&tr(label));
    btn.add_css_class("pill");
    btn.set_size_request(240, 48);
    btn.set_halign(gtk::Align::Center);
    btn
}

fn label(text: &str, class: &str) -> gtk::Label {
    let l = gtk::Label::new(Some(text));
    l.set_wrap(true);
    l.set_justify(gtk::Justification::Center);
    if !class.is_empty() {
        l.add_css_class(class);
    }
    l
}

fn combo(title: &str, items: &[String]) -> adw::ComboRow {
    let r = adw::ComboRow::new();
    r.set_title(&tr(title));
    let refs: Vec<&str> = items.iter().map(|s| s.as_str()).collect();
    r.set_model(Some(&gtk::StringList::new(&refs)));
    r
}

fn button_row(title: &str, button: &str, class: &str) -> (adw::ActionRow, gtk::Button) {
    let row = adw::ActionRow::new();
    row.set_title(&tr(title));
    let b = gtk::Button::with_label(&tr(button));
    if !class.is_empty() {
        b.add_css_class(class);
    }
    b.set_valign(gtk::Align::Center);
    row.add_suffix(&b);
    (row, b)
}

fn link_row(title: &str) -> adw::ActionRow {
    let row = adw::ActionRow::new();
    row.set_title(&tr(title));
    row.set_activatable(true);
    row.add_suffix(&gtk::Image::from_icon_name("go-next-symbolic"));
    row
}

fn boxed(width: i32) -> gtk::ListBox {
    let l = gtk::ListBox::new();
    l.add_css_class("boxed-list");
    l.set_selection_mode(gtk::SelectionMode::None);
    l.set_size_request(width, -1);
    l
}

/// Preset, width, height and refresh rate fields; the Start screen and the Done settings each have one.
struct SizeFields {
    preset: adw::ComboRow,
    w: adw::EntryRow,
    h: adw::EntryRow,
    rate: adw::ComboRow,
}

impl SizeFields {
    fn new() -> SizeFields {
        let entry = |title: &str| {
            let e = adw::EntryRow::new();
            e.set_title(&tr(title));
            e.set_input_purpose(gtk::InputPurpose::Digits);
            e
        };
        let mut names: Vec<String> = ws::PRESETS.iter().map(|(n, _, _)| n.to_string()).collect();
        names.push(tr("Other size"));
        SizeFields { preset: combo("Tablet", &names), w: entry("Width"), h: entry("Height"), rate: combo("Refresh rate", &["60 Hz".to_string(), "120 Hz".to_string()]) }
    }

    fn rows(&self) -> [gtk::Widget; 4] {
        [self.preset.clone().upcast(), self.w.clone().upcast(), self.h.clone().upcast(), self.rate.clone().upcast()]
    }

    fn set(&self, c: &ws::Config) {
        self.set_size(c.width, c.height);
        self.rate.set_selected(RATES.iter().position(|r| *r == c.refresh).unwrap_or(0) as u32);
    }

    fn set_size(&self, w: u32, h: u32) {
        self.w.set_text(&w.to_string());
        self.h.set_text(&h.to_string());
        self.sync_preset();
    }

    /// The preset that matches the typed size, else "Other size".
    fn sync_preset(&self) {
        let t = (self.w.text().trim().parse().unwrap_or(0), self.h.text().trim().parse().unwrap_or(0));
        let i = ws::PRESETS.iter().position(|(_, w, h)| (*w, *h) == t).unwrap_or(ws::PRESETS.len());
        if self.preset.selected() as usize != i {
            self.preset.set_selected(i as u32);
        }
    }

    fn refresh(&self) -> u32 {
        RATES[self.rate.selected() as usize]
    }

    /// The typed size (red fields when invalid).
    fn read(&self) -> Option<(u32, u32)> {
        let v = ws::parse_resolution(&format!("{}x{}", self.w.text().trim(), self.h.text().trim()));
        for e in [&self.w, &self.h] {
            if v.is_some() {
                e.remove_css_class("error");
            } else {
                e.add_css_class("error");
            }
        }
        v
    }

    fn on_change<F: Fn() + Clone + 'static>(self: &Rc<Self>, f: F) {
        let (g, me) = (f.clone(), self.clone());
        self.w.connect_changed(move |_| {
            me.sync_preset();
            g()
        });
        let (g, me) = (f.clone(), self.clone());
        self.h.connect_changed(move |_| {
            me.sync_preset();
            g()
        });
        let me = self.clone();
        self.preset.connect_selected_notify(move |p| {
            if let Some((_, w, h)) = ws::PRESETS.get(p.selected() as usize) {
                if me.read() != Some((*w, *h)) {
                    me.set_size(*w, *h);
                }
            }
        });
        self.rate.connect_selected_notify(move |_| f());
    }
}

/// "Tablet size on this port: W x H" when the port shrinks the size, else the plain size.
fn size_text(w: u32, h: u32, fit: (u32, u32)) -> String {
    if fit != (w, h) {
        trf("Tablet size on this port: {w} x {h}", &[("w", &fit.0), ("h", &fit.1)])
    } else {
        trf("Tablet size: {w} x {h}", &[("w", &w), ("h", &h)])
    }
}

pub struct WirelessScreenView {
    nav: adw::NavigationPage,
    win: Win,
    stack: gtk::Stack,
    scrolled: gtk::ScrolledWindow,
    dots: Vec<gtk::Label>,
    busy: Cell<bool>,
    loaded: Cell<bool>,
    /// Screen the user went to by hand ("Add another tablet", "Check my setup").
    manual: Cell<Option<&'static str>>,
    /// Raw log of a failed run, shown on the error screen until "Try again".
    error: RefCell<Option<String>>,
    status: RefCell<Status>,
    /// The last run: retry repeats it.
    retry: RefCell<Option<Rc<dyn Fn()>>>,
    /// Wrong PINs in a row (the "!" hint shows after two).
    fails: Cell<u32>,
    /// A PIN is being checked (Sunshine needs up to ~10 s for a wrong one).
    pairing: Cell<bool>,
    /// Bumped when pairing starts and ends: a status read from before is dropped.
    gen: Cell<u32>,
    checking: gtk::Box,
    /// Widget that gets keyboard focus on each screen (Enter does the obvious, never a destructive thing).
    primary: Vec<(&'static str, gtk::Widget)>,
    /// Screen the focus was last set for; cleared when the page hides, so reopening sets it again.
    focused: RefCell<String>,
    size_line: gtk::Label,
    preflight: gtk::Label,
    start_size: Rc<SizeFields>,
    new_size: Rc<SizeFields>,
    reveal: gtk::Revealer,
    btn_apply: gtk::Button,
    btn_start: gtk::Button,
    progress: gtk::ProgressBar,
    work_line: gtk::Label,
    details: gtk::Label,
    details_exp: gtk::Expander,
    wait_box: gtk::Box,
    pin_box: gtk::Box,
    connect_ip: gtk::Label,
    pin: gtk::Entry,
    pin_line: gtk::Label,
    pin_hint: gtk::Label,
    btn_connect: gtk::Button,
    live_line: gtk::Label,
    done_line: gtk::Label,
    net_line: gtk::Label,
    btn_switch: gtk::Button,
    btn_match: gtk::Button,
    get_app: gtk::Box,
    policy: adw::ComboRow,
    quality: adw::ComboRow,
    hide_cursor: adw::SwitchRow,
    row_size: adw::ActionRow,
    checks: Vec<(&'static str, adw::ActionRow, gtk::Image, gtk::Button)>,
}

impl Page for WirelessScreenView {
    fn nav(&self) -> &adw::NavigationPage {
        &self.nav
    }
    fn refresh(self: Rc<Self>) {
        if demo() {
            return;
        }
        let (me, g) = (self.clone(), self.gen.get());
        run_async(ws::status, move |s| me.render_if(g, s));
    }
}

impl WirelessScreenView {
    pub fn new(win: Win) -> Rc<dyn Page> {
        let stack = gtk::Stack::new();
        stack.set_transition_type(if demo() { gtk::StackTransitionType::None } else { gtk::StackTransitionType::Crossfade });
        stack.set_vexpand(true);
        // each screen as tall as it needs (a homogeneous stack would make short screens scroll like the longest)
        stack.set_vhomogeneous(false);
        stack.set_hhomogeneous(false);

        // Start
        let (b, _, size_line) = screen("tablet-symbolic", "Use your tablet as a screen");
        let preflight = label("", "error");
        preflight.set_visible(false);
        b.append(&preflight);
        let btn_change = gtk::Button::with_label(&tr("Change"));
        btn_change.add_css_class("flat");
        btn_change.set_halign(gtk::Align::Center);
        b.append(&btn_change);
        let start_size = Rc::new(SizeFields::new());
        start_size.set(&ws::Config::default());
        let fields = boxed(340);
        for r in start_size.rows() {
            fields.append(&r);
        }
        let reveal = gtk::Revealer::new();
        reveal.set_child(Some(&fields));
        b.append(&reveal);
        let r2 = reveal.clone();
        let icon = b.first_child();
        btn_change.connect_clicked(move |_| r2.set_reveal_child(!r2.reveals_child()));
        reveal.connect_reveal_child_notify(move |r| {
            if let Some(i) = &icon {
                i.set_visible(!r.reveals_child());
            }
        });
        let btn_start = big_button(&b, "Start");
        b.append(&label(&tr("Keep your monitor plugged in until it asks you to restart."), "caption"));
        stack.add_named(&b, Some("start"));

        // Working
        let (b, _, work_line) = screen("folder-download-symbolic", "Getting ready");
        let progress = gtk::ProgressBar::new();
        progress.set_size_request(300, -1);
        b.append(&progress);
        stack.add_named(&b, Some("working"));

        // Error
        let (b, _, line) = screen("dialog-warning-symbolic", "That did not work");
        line.set_label(&tr("Check the internet connection and try again."));
        let details = gtk::Label::new(None);
        details.set_wrap(true);
        details.set_selectable(true);
        details.add_css_class("caption");
        details.set_max_width_chars(60);
        let details_exp = gtk::Expander::new(Some(&tr("Details")));
        details_exp.set_child(Some(&details));
        details_exp.set_halign(gtk::Align::Center);
        b.append(&details_exp);
        let btn_retry = big_button(&b, "Try again");
        stack.add_named(&b, Some("error"));

        // Restart
        let (b, _, line) = screen("system-reboot-symbolic", "Restart to finish");
        line.set_label(&tr("SUIT opens again by itself."));
        let btn_restart = big_button(&b, "Restart now");
        stack.add_named(&b, Some("restart"));

        // Connect: get the app, tap this PC, type the PIN
        // the app block only while waiting: once Moonlight asks for a PIN the user has it
        let (b, title, line) = screen("", "");
        title.set_visible(false);
        line.set_visible(false);
        let get_app = gtk::Box::new(gtk::Orientation::Vertical, 14);
        get_app.append(&label(&tr("1. Get Moonlight"), "title-2"));
        let qr = gtk::Box::new(gtk::Orientation::Horizontal, 28);
        qr.set_halign(gtk::Align::Center);
        for (name, url) in [("App Store", ws::MOONLIGHT_IOS), ("Google Play", ws::MOONLIGHT_ANDROID)] {
            let col = gtk::Box::new(gtk::Orientation::Vertical, 6);
            let card = gtk::Box::new(gtk::Orientation::Vertical, 0);
            card.add_css_class("qr-card");
            let pic = gtk::Picture::new();
            pic.set_size_request(96, 96);
            pic.set_can_shrink(false);
            pic.set_paintable(generate_qr_texture(url).as_ref());
            card.append(&pic);
            col.append(&card);
            col.append(&gtk::Label::new(Some(name)));
            qr.append(&col);
        }
        get_app.append(&qr);
        b.append(&get_app);
        let step2 = label(&trf("2. Open Moonlight, tap {name}", &[("name", &ws::pc_name())]), "title-2");
        step2.set_margin_top(8);
        b.append(&step2);
        let wait_box = gtk::Box::new(gtk::Orientation::Horizontal, 10);
        wait_box.set_halign(gtk::Align::Center);
        let spin = gtk::Spinner::new();
        spin.start();
        wait_box.append(&spin);
        wait_box.append(&label(&tr("Waiting for Moonlight…"), "title-4"));
        b.append(&wait_box);
        let connect_ip = label("", "dim-label");
        b.append(&connect_ip);
        let pin_box = gtk::Box::new(gtk::Orientation::Vertical, 10);
        pin_box.append(&label(&tr("Type the 4 numbers Moonlight shows"), "title-4"));
        let pin = gtk::Entry::new();
        pin.set_placeholder_text(Some("0000"));
        pin.set_width_chars(4);
        pin.set_input_purpose(gtk::InputPurpose::Digits);
        gtk::prelude::EditableExt::set_alignment(&pin, 0.5);
        pin.add_css_class("title-1");
        pin.set_size_request(240, 60);
        pin.set_halign(gtk::Align::Center);
        pin_box.append(&pin);
        let pin_line = label("", "error");
        pin_box.append(&pin_line);
        let btn_connect = big_button(&pin_box, "Connect");
        let checking = gtk::Box::new(gtk::Orientation::Horizontal, 10);
        checking.set_halign(gtk::Align::Center);
        let spin = gtk::Spinner::new();
        spin.start();
        checking.append(&spin);
        checking.append(&label(&tr("Checking…"), "title-4"));
        checking.set_visible(false);
        pin_box.append(&checking);
        b.append(&pin_box);
        let pin_hint = label(&trf("Old '{name}' with a ! in Moonlight? Delete it there.", &[("name", &ws::pc_name())]), "dim-label");
        pin_hint.set_visible(false);
        b.append(&pin_hint);
        stack.add_named(&b, Some("connect"));

        // Done
        let (b, _, done_line) = screen("object-select-symbolic", "Your tablet is ready");
        let live_line = label("", "title-4");
        b.append(&live_line);
        let btn_switch = pill("Switch to tablet now");
        b.append(&btn_switch);
        let btn_match = pill("");
        b.append(&btn_match);
        let net_line = label("", "dim-label");
        net_line.add_css_class("caption");
        b.append(&net_line);
        let btn_settings = pill("Settings");
        b.append(&btn_settings);
        stack.add_named(&b, Some("done"));

        // Settings: own screen, back to Done at the top
        let (b, st, sl_line) = screen("", "Settings");
        b.set_spacing(4);
        b.set_margin_top(6);
        b.set_margin_bottom(6);
        st.remove_css_class("title-1");
        st.add_css_class("title-3");
        sl_line.set_visible(false);
        let btn_settings_back = gtk::Button::new();
        btn_settings_back.set_child(Some(&adw::ButtonContent::builder().icon_name("go-previous-symbolic").label(tr("Back")).build()));
        btn_settings_back.add_css_class("flat");
        btn_settings_back.set_halign(gtk::Align::Start);
        b.prepend(&btn_settings_back);
        let policy = combo("Monitor plugged in", &[tr("Same picture"), tr("Extend")]);
        let quality = combo("Picture", &[tr("Sharp"), tr("Smooth (weak Wi-Fi)")]);
        let hide_cursor = switch_row("Hide the cursor", "");
        let (row_size, btn_apply) = button_row("Tablet size", "Apply", "suggested-action");
        let new_size = Rc::new(SizeFields::new());
        let row_check = link_row("Check my setup");
        let row_add = link_row("Add another tablet");
        let (row_copy, btn_copy) = button_row("Copy details", "Copy", "");
        let (row_remove, btn_remove) = button_row("Remove everything", "Remove", "destructive-action");
        let mut rows: Vec<gtk::Widget> = vec![policy.clone().upcast(), quality.clone().upcast(), hide_cursor.clone().upcast(), row_size.clone().upcast()];
        let size_rows = new_size.rows();
        for r in &size_rows {
            r.set_visible(false);
        }
        row_size.set_activatable(true);
        row_size.connect_activated(move |_| {
            for r in &size_rows {
                r.set_visible(!r.is_visible());
            }
        });
        rows.extend(new_size.rows());
        rows.extend([row_check.clone().upcast(), row_add.clone().upcast(), row_remove.upcast()]);
        let sl = boxed(380);
        for r in &rows {
            sl.append(r);
        }
        b.append(&sl);
        stack.add_named(&b, Some("settings"));

        // Check my setup
        let (b, _, _) = screen("", "Check my setup");
        let list = boxed(380);
        let checks: Vec<_> = CHECKS
            .iter()
            .map(|(id, title)| {
                let row = adw::ActionRow::new();
                row.set_title(&tr(title));
                let icon = gtk::Image::new();
                icon.set_pixel_size(24);
                row.add_prefix(&icon);
                let fix = gtk::Button::with_label(&tr("Fix it"));
                fix.add_css_class("suggested-action");
                fix.set_valign(gtk::Align::Center);
                row.add_suffix(&fix);
                list.append(&row);
                (*id, row, icon, fix)
            })
            .collect();
        list.append(&row_copy);
        b.append(&list);
        let btn_back = big_button(&b, "Done");
        stack.add_named(&b, Some("check"));

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
        let nav = adw::NavigationPage::builder().title(tr("Wireless Screen")).tag("wireless_screen").child(&root).build();

        let me = Rc::new(WirelessScreenView {
            nav,
            win,
            stack,
            scrolled,
            dots,
            busy: Cell::new(false),
            loaded: Cell::new(false),
            manual: Cell::new(None),
            error: RefCell::new(None),
            status: RefCell::new(Status::default()),
            retry: RefCell::new(None),
            fails: Cell::new(0),
            pairing: Cell::new(false),
            gen: Cell::new(0),
            checking,
            primary: vec![
                ("start", btn_start.clone().upcast()),
                ("restart", btn_restart.clone().upcast()),
                ("error", btn_retry.clone().upcast()),
                ("done", btn_settings.clone().upcast()),
                ("connect", pin.clone().upcast()),
            ],
            focused: RefCell::new(String::new()),
            size_line,
            preflight,
            start_size,
            new_size,
            reveal,
            btn_apply,
            btn_start,
            progress,
            work_line,
            details,
            details_exp,
            wait_box,
            pin_box,
            connect_ip,
            pin,
            pin_line,
            pin_hint,
            btn_connect,
            live_line,
            done_line,
            net_line,
            btn_switch,
            btn_match,
            get_app,
            policy,
            quality,
            hide_cursor,
            row_size,
            checks,
        });
        let m = me.clone();
        me.start_size.on_change(move || {
            m.check_size();
        });
        let m = me.clone();
        me.new_size.on_change(move || m.check_new_size());
        let m = me.clone();
        me.btn_apply.connect_clicked(move |_| {
            let Some((w, h, _)) = m.fitted(&m.new_size) else { return };
            m.ask_apply(w, h, m.new_size.refresh());
        });
        let m = me.clone();
        me.btn_match.connect_clicked(move |_| {
            let offer = m.status.borrow().tablet_offer();
            if let Some((w, h)) = offer {
                let refresh = m.status.borrow().config.refresh;
                m.ask_apply(w, h, refresh);
            }
        });
        let m = me.clone();
        me.btn_start.connect_clicked(move |_| {
            let m2 = m.clone();
            m.confirm("This changes how this PC starts", "It needs a restart afterwards.", "Continue", move || m2.start());
        });
        let m = me.clone();
        btn_retry.connect_clicked(move |_| {
            let r = m.retry.borrow().clone();
            if let Some(r) = r {
                r()
            }
        });
        let m = me.clone();
        btn_restart.connect_clicked(move |b| {
            let b = b.clone();
            m.confirm("Restart this PC now?", "Save your work first.", "Restart", move || {
                b.set_sensitive(false);
                let b = b.clone();
                run_async(ws::restart, move |(ok, _)| b.set_sensitive(!ok));
            });
        });
        let m = me.clone();
        me.btn_connect.connect_clicked(move |_| m.connect());
        let m = me.clone();
        me.pin.connect_activate(move |_| m.connect());
        // digits only, at most 4 (paste junk is dropped); the 4th digit sends the PIN
        let m = me.clone();
        me.pin.connect_changed(move |e| {
            let t = e.text();
            let clean: String = t.chars().filter(|c| c.is_ascii_digit()).take(4).collect();
            if clean != t.as_str() {
                e.set_text(&clean);
                e.set_position(-1);
                return;
            }
            if clean.len() == 4 && !m.pairing.get() {
                let m2 = m.clone();
                gtk::glib::idle_add_local_once(move || m2.connect());
            }
        });
        let m = me.clone();
        row_add.connect_activated(move |_| m.go("connect"));
        let m = me.clone();
        row_check.connect_activated(move |_| m.open_check());
        let m = me.clone();
        btn_settings.connect_clicked(move |_| m.go("settings"));
        let m = me.clone();
        btn_settings_back.connect_clicked(move |_| {
            m.manual.set(None);
            m.clone().refresh();
        });
        let m = me.clone();
        btn_back.connect_clicked(move |_| m.go("settings"));
        for (id, _, _, fix) in &me.checks {
            let (m, id) = (me.clone(), *id);
            fix.connect_clicked(move |_| m.fix(id));
        }
        let m = me.clone();
        me.btn_switch.connect_clicked(move |b| {
            if !m.live() {
                return;
            }
            let (cfg, now) = { (m.status.borrow().config.clone(), m.status.borrow().layout) };
            b.set_sensitive(false);
            let (m2, b) = (m.clone(), b.clone());
            run_async(move || ws::switch_layout(&cfg, now), move |_| {
                b.set_sensitive(true);
                m2.clone().refresh();
            });
        });
        let m = me.clone();
        me.policy.connect_selected_notify(move |c| {
            if !m.live() {
                return;
            }
            let mut cfg = ws::load_config();
            cfg.mirror = c.selected() == 0;
            ws::save_config(&cfg);
            run_async(move || ws::apply_layout(&cfg), |_| ());
        });
        let m = me.clone();
        me.quality.connect_selected_notify(move |c| {
            let sharp = c.selected() == 0;
            if !m.live() || sharp == m.status.borrow().config.sharp {
                return;
            }
            let m2 = m.clone();
            run_async(move || ws::set_quality(sharp), move |(_, msg)| {
                m2.win.show_toast(&msg);
                m2.clone().refresh();
            });
        });
        let m = me.clone();
        me.hide_cursor.connect_active_notify(move |r| {
            if m.live() && r.is_active() != m.status.borrow().config.hide_cursor {
                let on = r.is_active();
                let m2 = m.clone();
                run_async(move || ws::set_hide_cursor(on), move |(_, msg)| {
                    m2.win.show_toast(&msg);
                    m2.clone().refresh();
                });
            }
        });
        let m = me.clone();
        btn_copy.connect_clicked(move |_| {
            if !m.live() {
                return;
            }
            let m2 = m.clone();
            run_async(ws::details, move |t| {
                copy_to_clipboard(&t);
                m2.win.show_toast(&tr("Details copied."));
            });
        });
        let m = me.clone();
        btn_remove.connect_clicked(move |b| {
            let d = alert(
                &tr("Remove everything?"),
                &tr("The tablet stops working as a screen after the next restart."),
                &[("cancel", &tr("Cancel")), ("remove", &tr("Remove"))],
                None,
                Some("remove"),
            );
            let (m2, b) = (m.clone(), b.clone());
            ask(&m.win.window, d, move |r| {
                if r == "remove" {
                    b.set_sensitive(false);
                    let (m3, b) = (m2.clone(), b.clone());
                    run_async(ws::remove_everything, move |(_, msg)| {
                        b.set_sensitive(true);
                        m3.win.show_toast(&msg);
                        m3.manual.set(None);
                        m3.loaded.set(false);
                        m3.clone().refresh();
                    });
                }
            });
        });
        // quiet repairs each time the page opens (Sunshine and the watcher run), then the status
        let m = me.clone();
        me.nav.connect_unmap(move |_| m.focused.borrow_mut().clear());
        let m = me.clone();
        me.nav.connect_map(move |_| {
            if demo() {
                return;
            }
            // until the first status arrives nothing on the page has focus, so Enter does not hit the
            // header's back button (GTK focuses it when the window appears)
            for ms in [0u64, 150] {
                let w = m.win.window.clone();
                gtk::glib::timeout_add_local_once(std::time::Duration::from_millis(ms), move || {
                    if gtk::prelude::GtkWindowExt::focus(&w).is_some_and(|f| f.ancestor(adw::NavigationPage::static_type()).is_none()) {
                        gtk::prelude::GtkWindowExt::set_focus(&w, None::<&gtk::Widget>);
                    }
                });
            }
            let m2 = m.clone();
            let heal = std::env::var_os("SUIT_NO_HEAL").is_none();
            run_async(
                move || {
                    if heal {
                        ws::self_repair();
                    }
                    ws::status()
                },
                move |s| m2.render_if(0, s),
            );
        });
        // keep the page current while it is open (preflight, Moonlight asking for a PIN, the tablet connecting)
        let weak = Rc::downgrade(&me);
        every(3000, move || {
            let Some(me) = weak.upgrade() else { return false };
            if me.nav.is_mapped() && !me.busy.get() && !me.pairing.get() && me.error.borrow().is_none() {
                me.refresh();
            }
            true
        });
        if demo() {
            DEMO_VIEW.with(|v| *v.borrow_mut() = Some(me.clone()));
        }
        me
    }

    /// Handlers that change the PC run only for real input (not while loading or in screenshot mode).
    fn live(&self) -> bool {
        self.loaded.get() && !demo()
    }

    /// Ask before a step that changes the PC; Cancel is the default answer.
    fn confirm<F: Fn() + 'static>(&self, heading: &str, body: &str, yes: &str, f: F) {
        if demo() {
            return;
        }
        let d = alert(&tr(heading), &tr(body), &[("cancel", &tr("Cancel")), ("yes", &tr(yes))], None, None);
        d.set_default_response(Some("cancel"));
        ask(&self.win.window, d, move |r| {
            if r == "yes" {
                f();
            }
        });
    }

    fn show(&self, name: &str) {
        if self.stack.visible_child_name().as_deref() != Some(name) {
            self.scrolled.vadjustment().set_value(0.0);
        }
        self.stack.set_visible_child_name(name);
        if *self.focused.borrow() != name {
            *self.focused.borrow_mut() = name.to_string();
            self.focus_primary(name);
        }
        let dot = match name {
            "error" => "working",
            "check" | "settings" => "done",
            n => n,
        };
        let at = SCREENS.iter().position(|s| *s == dot).unwrap_or(1);
        for (i, d) in self.dots.iter().enumerate() {
            d.set_opacity(if i == at { 1.0 } else { 0.3 });
        }
    }

    /// Focus the screen's main widget once the window shows it: GTK first focuses the header's back button
    /// when the window appears, so the grab runs again shortly after (idle and 150 ms).
    fn focus_primary(&self, name: &str) {
        let Some((_, w)) = self.primary.iter().find(|(n, _)| *n == name) else { return };
        for ms in [0u64, 150] {
            let (w, stack, name) = (w.clone(), self.stack.clone(), name.to_string());
            gtk::glib::timeout_add_local_once(std::time::Duration::from_millis(ms), move || {
                // the PIN field only in the PIN state (it is hidden while waiting for Moonlight)
                if stack.is_mapped() && stack.visible_child_name().as_deref() == Some(&name) && w.is_visible() && w.is_sensitive() && w.parent().is_some_and(|p| p.is_visible()) {
                    w.grab_focus();
                }
            });
        }
    }

    /// A screen the user picked by hand.
    fn go(&self, name: &'static str) {
        self.manual.set(Some(name));
        self.show(name);
        if name == "connect" {
            self.pin.grab_focus();
        }
    }

    /// Size the port really gets for typed fields (None when invalid).
    fn fitted(&self, f: &SizeFields) -> Option<(u32, u32, (u32, u32))> {
        let (w, h) = f.read()?;
        let port = self.status.borrow().port.clone();
        Some((w, h, port.map(|p| ws::size_on_port(w, h, f.refresh(), &p)).unwrap_or((w, h))))
    }

    /// Size from the Change fields; a bad one, no free port or no internet blocks Start.
    fn check_size(&self) -> Option<(u32, u32)> {
        let v = self.fitted(&self.start_size);
        self.size_line.set_label(&match v {
            Some((w, h, fit)) => size_text(w, h, fit),
            None => tr("Type the width and height in numbers"),
        });
        let pre = self.status.borrow().preflight_error();
        self.preflight.set_label(pre.as_deref().unwrap_or(""));
        self.preflight.set_visible(pre.is_some());
        self.btn_start.set_sensitive(v.is_some() && pre.is_none());
        v.map(|(w, h, _)| (w, h))
    }

    /// Done settings: Apply only for a valid size that differs from the one set up.
    fn check_new_size(&self) {
        let cfg = self.status.borrow().config.clone();
        let v = self.fitted(&self.new_size);
        let refresh = self.new_size.refresh();
        let fit = v.map(|(_, _, f)| f).unwrap_or((cfg.width, cfg.height));
        self.row_size.set_subtitle(&match v {
            Some((w, h, fit)) if (fit, refresh) != ((cfg.width, cfg.height), cfg.refresh) => size_text(w, h, fit),
            Some(_) => format!("{} x {} · {} Hz", cfg.width, cfg.height, cfg.refresh),
            None => tr("Type the width and height in numbers"),
        });
        self.btn_apply.set_sensitive(v.is_some() && !self.busy.get() && (fit, refresh) != ((cfg.width, cfg.height), cfg.refresh));
    }

    /// New tablet size on the configured port (Apply, Match my tablet): confirm, rewrite, Restart screen.
    fn ask_apply(self: &Rc<Self>, w: u32, h: u32, refresh: u32) {
        let m = self.clone();
        self.confirm("This changes how this PC starts", "It needs a restart afterwards.", "Continue", move || {
            let m2 = m.clone();
            m.run(Rc::new(move || m2.work(move |say| ws::apply_size(w, h, refresh, &|t| say(0.5, "Preparing…", t)))));
        });
    }

    /// Run `job` now and keep it for "Try again".
    fn run(&self, job: Rc<dyn Fn()>) {
        *self.retry.borrow_mut() = Some(job.clone());
        job();
    }

    /// Everything SUIT can do in one go: virtual screen, Sunshine with the right settings, cursor hiding.
    fn start(self: &Rc<Self>) {
        let Some((width, height)) = self.check_size() else { return };
        let refresh = self.start_size.refresh();
        let m = self.clone();
        self.run(Rc::new(move || {
            m.work(move |say| {
                let s = ws::status();
                if !s.set_up() {
                    let (ok, msg) = ws::setup(width, height, refresh, &|t| say(0.35, "Preparing…", t));
                    if !ok {
                        return (false, msg);
                    }
                    say(0.35, "Preparing…", &msg);
                }
                if !(s.sunshine_installed && s.sunshine_running) {
                    return ws::install_sunshine(&|t| say(0.95, "Installing…", t));
                }
                (true, String::new())
            })
        }));
    }

    /// Run `job` on the working screen: progress bar, one status word, the error screen with the log.
    fn work<F>(self: &Rc<Self>, job: F)
    where
        F: FnOnce(&(dyn Fn(f64, &str, &str) + Sync)) -> (bool, String) + Send + 'static,
    {
        if demo() {
            return;
        }
        *self.error.borrow_mut() = None;
        self.busy.set(true);
        self.show("working");
        let prog: Arc<Mutex<(f64, String, String)>> = Arc::new(Mutex::new((0.05, tr("Preparing…"), String::new())));
        let (p2, m) = (prog.clone(), self.clone());
        every(200, move || {
            let (f, word, _) = p2.lock().unwrap().clone();
            m.progress.set_fraction(f);
            m.work_line.set_label(&word);
            m.busy.get()
        });
        let job = move || {
            // each step moves the bar a bit (up to `cap`) and adds its line to the log
            let say = |cap: f64, word: &str, log: &str| {
                let mut p = prog.lock().unwrap();
                p.0 = (p.0 + 0.12).min(cap).max(p.0);
                p.1 = tr(word);
                p.2 += &format!("{}\n", tr(log));
            };
            let (ok, msg) = job(&say);
            let log = format!("{}{}", prog.lock().unwrap().2, msg);
            (ok, log)
        };
        let m = self.clone();
        run_async(job, move |(ok, log)| {
            m.busy.set(false);
            if !ok {
                m.details.set_label(&log);
                *m.error.borrow_mut() = Some(log);
            }
            m.clone().refresh();
        });
    }

    fn connect(self: &Rc<Self>) {
        if demo() || self.pairing.get() {
            return;
        }
        let pin = self.pin.text().trim().to_string();
        if pin.len() != 4 || !pin.chars().all(|c| c.is_ascii_digit()) {
            self.pin_line.set_label(&tr("Type the 4 numbers from Moonlight."));
            self.pin.grab_focus();
            return;
        }
        self.set_pairing(true);
        let m = self.clone();
        run_async(move || ws::pair(&pin, ""), move |(ok, msg)| {
            m.set_pairing(false);
            m.pin.set_text("");
            if ok {
                m.fails.set(0);
                m.manual.set(None);
                m.win.show_toast(&msg);
                // straight to Done; a status read from before the pairing is dropped (gen)
                let mut s = m.status.borrow().clone();
                s.paired.push(msg);
                s.pending = None;
                m.render(s);
                m.clone().refresh();
            } else {
                m.fails.set(m.fails.get() + 1);
                m.pin_line.set_label(&msg);
                m.pin_hint.set_visible(m.fails.get() >= 2);
                m.pin.grab_focus();
            }
        });
    }

    /// While a PIN is checked: spinner and "Checking…", field and button off, no status polling.
    fn set_pairing(&self, on: bool) {
        self.pairing.set(on);
        self.gen.set(self.gen.get() + 1);
        self.checking.set_visible(on);
        self.btn_connect.set_visible(!on);
        self.pin.set_sensitive(!on);
        self.btn_connect.set_sensitive(!on);
        if on {
            self.pin_line.set_label("");
        }
    }

    /// Render a status read that started at generation `g` (0: always).
    fn render_if(self: &Rc<Self>, g: u32, s: Status) {
        if (g == 0 || g == self.gen.get()) && !self.pairing.get() {
            self.render(s);
        }
    }

    /// "Check my setup": the checklist screen.
    fn open_check(self: &Rc<Self>) {
        self.go("check");
        if demo() {
            return;
        }
        let m = self.clone();
        run_async(|| ws::checklist(&ws::status()), move |c| m.show_checks(&c));
    }

    fn show_checks(&self, list: &[Check]) {
        for (id, row, icon, fix) in &self.checks {
            let c = list.iter().find(|c| c.id == *id);
            let ok = c.is_some_and(|c| c.ok);
            icon.set_icon_name(Some(if ok { "object-select-symbolic" } else { "dialog-error-symbolic" }));
            for (on, class) in [(ok, "success"), (!ok, "error")] {
                if on {
                    icon.add_css_class(class);
                } else {
                    icon.remove_css_class(class);
                }
            }
            row.set_subtitle(&c.map(|c| glib_escape(&c.detail)).unwrap_or_default());
            fix.set_visible(!ok);
        }
    }

    /// "Fix it": a restart or pairing goes to its screen, the rest is repaired here.
    fn fix(self: &Rc<Self>, id: &'static str) {
        if !self.live() {
            return;
        }
        let s = self.status.borrow().clone();
        match id {
            "paired" => return self.go("connect"),
            "screen" if s.reboot_pending() => {
                self.manual.set(None);
                return self.show("restart");
            }
            _ => {}
        }
        let m = self.clone();
        self.run(Rc::new(move || {
            let m2 = m.clone();
            m.work(move |say| ws::fix(id, &|t| say(0.9, "Preparing…", t)));
            m2.manual.set(Some("check"));
        }));
    }

    fn render(self: &Rc<Self>, s: Status) {
        let cfg = s.config.clone();
        if !self.loaded.get() {
            self.start_size.set(&cfg);
            self.new_size.set(&cfg);
            self.policy.set_selected(if cfg.mirror { 0 } else { 1 });
            self.quality.set_selected(if cfg.sharp { 0 } else { 1 });
            self.hide_cursor.set_active(cfg.hide_cursor);
            self.loaded.set(true);
        }
        let sunshine = s.sunshine_installed && s.sunshine_running;
        let auto = if s.set_up() && sunshine && s.reboot_pending() {
            "restart"
        } else if !s.set_up() || !sunshine {
            "start"
        } else if s.paired.is_empty() {
            "connect"
        } else {
            "done"
        };
        let page = if self.busy.get() {
            "working"
        } else if self.error.borrow().is_some() {
            "error"
        } else {
            self.manual.get().filter(|_| auto == "connect" || auto == "done").unwrap_or(auto)
        };
        // Connect: the PIN field only once Moonlight asks for one
        self.connect_ip.set_label(&trf("Not in the list? Tap + and type {ip}", &[("ip", &s.ip)]));
        self.wait_box.set_visible(s.pending.is_none());
        let pin_now = s.pending.is_some() || self.pairing.get();
        let pin_new = pin_now && !self.pin_box.is_visible();
        self.pin_box.set_visible(pin_now);
        self.get_app.set_visible(!pin_now);
        // Done
        let mode = match s.layout {
            Some(Layout::TabletOnly) => tr("Tablet only"),
            Some(Layout::Mirror) => tr("Mirroring a monitor"),
            Some(Layout::Extend) => tr("Extended desktop"),
            None => String::new(),
        };
        let conn = tr(if s.streaming { "Tablet connected" } else { "Waiting for tablet" });
        self.live_line.set_label(&if mode.is_empty() { conn } else { format!("{} · {}", conn, mode) });
        self.live_line.remove_css_class("success");
        if s.streaming {
            self.live_line.add_css_class("success");
        }
        self.done_line.set_label(&trf("Open Moonlight → {name} → Desktop", &[("name", &s.hostname)]));
        self.btn_switch.set_visible(s.active && s.others > 0);
        self.btn_switch.set_label(&tr(if s.layout == Some(Layout::TabletOnly) { "Use the monitor too" } else { "Switch to tablet now" }));
        let offer = s.tablet_offer();
        self.btn_match.set_visible(offer.is_some());
        if let Some((w, h)) = offer {
            self.btn_match.set_label(&trf("Match my tablet: {w} x {h}", &[("w", &w), ("h", &h)]));
        }
        let mut net = trf("This PC: {ip}", &[("ip", &s.ip)]);
        if s.wifi {
            net = format!("{}\n{}", net, tr("This PC is on Wi-Fi. A network cable is more stable."));
        }
        self.net_line.set_label(&net);
        *self.status.borrow_mut() = s;
        self.hide_cursor.set_active(cfg.hide_cursor);
        self.check_size();
        self.check_new_size();
        let entering = self.stack.visible_child_name().as_deref() != Some(page);
        self.show(page);
        if page == "connect" && (pin_new || entering) && pin_now && self.pin.is_sensitive() && !demo() {
            self.pin.grab_focus();
        }
        if page == "check" && !demo() {
            let m = self.clone();
            run_async(|| ws::checklist(&ws::status()), move |c| m.show_checks(&c));
        }
    }

    // ------------------------------------------------------------ screenshot mode

    /// Fake states for `--screenshots` (names in `DEMO_STATES`); nothing reads or changes the PC.
    pub fn demo_state(self: &Rc<Self>, name: &str) {
        let cfg = ws::Config { connector: "HDMI-A-3".into(), ..ws::Config::default() };
        let ready = Status {
            config: cfg,
            configured: Some("HDMI-A-3".into()),
            active: true,
            port: Some("HDMI-A-3".into()),
            sunshine_installed: true,
            sunshine_running: true,
            paired: vec!["iPad".into()],
            ip: "192.168.2.110".into(),
            hostname: "autodarts".into(),
            watcher_running: true,
            ..Status::default()
        };
        let fresh = Status { configured: None, active: false, sunshine_installed: false, sunshine_running: false, paired: vec![], online: Some(true), ..ready.clone() };
        *self.error.borrow_mut() = None;
        self.busy.set(false);
        self.manual.set(None);
        self.reveal.set_reveal_child(false);
        self.pin_hint.set_visible(false);
        self.pin_line.set_label("");
        match name {
            "start" => self.render(fresh),
            "start-change" => {
                self.render(fresh);
                self.reveal.set_reveal_child(true);
            }
            "start-offline" => self.render(Status { online: Some(false), ..fresh }),
            "error" => {
                let log = "Setting up the virtual screen on HDMI-A-3 (1920 x 1200)...\nInstalling Sunshine failed: Curl error (6): Couldn't resolve host name";
                self.details.set_label(log);
                *self.error.borrow_mut() = Some(log.into());
                self.render(fresh);
                self.details_exp.set_expanded(true);
            }
            "working" => {
                self.busy.set(true);
                self.render(fresh);
                self.progress.set_fraction(0.6);
                self.work_line.set_label(&tr("Installing…"));
            }
            "restart" => self.render(Status { active: false, ..ready }),
            "connect-waiting" => self.render(Status { paired: vec![], ..ready }),
            "connect-pin" => {
                self.render(Status { paired: vec![], pending: Some("iPad".into()), ..ready });
                self.fails.set(2);
                self.pin_line.set_label(&tr("Wrong PIN. Type the new one Moonlight shows."));
                self.pin_hint.set_visible(true);
            }
            "done" | "settings" => {
                self.render(Status { streaming: true, layout: Some(Layout::Mirror), others: 1, client_mode: Some((2160, 1620, 60)), wifi: true, ..ready });
                if name == "settings" {
                    self.show("settings");
                }
            }
            "check" => {
                self.render(ready);
                self.show("check");
                let c = |id, ok, detail: &str| Check { id, ok, detail: detail.into() };
                self.show_checks(&[
                    c("screen", true, "HDMI-A-3 1920 x 1200 @ 60 Hz"),
                    c("sunshine", true, ""),
                    c("ports", false, ""),
                    c("watcher", false, ""),
                    c("paired", true, "iPad"),
                ]);
            }
            _ => {}
        }
    }
}

/// Screens `--screenshots` renders, in order.
pub const DEMO_STATES: [&str; 11] =
    ["start", "start-change", "start-offline", "error", "working", "restart", "connect-waiting", "connect-pin", "done", "settings", "check"];

fn glib_escape(s: &str) -> String {
    gtk::glib::markup_escape_text(s).to_string()
}
