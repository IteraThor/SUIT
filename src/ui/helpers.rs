//! Reusable UI helpers for the GTK4 / Libadwaita pages: background work, page shell, buttons, clipboard,
//! QR codes, browser launching and small dialog shortcuts.
use crate::core::i18n::tr;
use adw::prelude::*;
use gtk::{gdk, gio, glib};

/// One content width, margin and rhythm for every page, so pages line up when navigating
pub const PAGE_MAX_WIDTH: i32 = 760;
pub const PAGE_TIGHTEN_AT: i32 = 600;
pub const PAGE_SPACING: i32 = 18;

/// Run `f` on a worker thread and hand its result to `done` on the GTK main loop.
pub fn run_async<T, F, D>(f: F, done: D)
where
    T: Send + 'static,
    F: FnOnce() -> T + Send + 'static,
    D: FnOnce(T) + 'static,
{
    glib::spawn_future_local(async move {
        match gio::spawn_blocking(f).await {
            Ok(v) => done(v),
            Err(_) => log::error!("Async background worker panicked"),
        }
    });
}

/// Fire-and-forget background work.
pub fn run_bg<F: FnOnce() + Send + 'static>(f: F) {
    std::thread::spawn(f);
}

/// Repeat `f` every `ms` on the main loop while it returns true.
pub fn every<F: FnMut() -> bool + 'static>(ms: u64, mut f: F) -> glib::SourceId {
    glib::timeout_add_local(std::time::Duration::from_millis(ms), move || if f() { glib::ControlFlow::Continue } else { glib::ControlFlow::Break })
}

pub fn once<F: FnOnce() + 'static>(ms: u64, f: F) -> glib::SourceId {
    glib::timeout_add_local_once(std::time::Duration::from_millis(ms), f)
}

/// Scrolling, width-clamped page body shared by all pages: (root, content).
pub fn build_page_shell(max_width: i32) -> (gtk::ScrolledWindow, gtk::Box) {
    let scrolled = gtk::ScrolledWindow::builder()
        .hscrollbar_policy(gtk::PolicyType::Never)
        .kinetic_scrolling(true)
        .overlay_scrolling(true)
        .vexpand(true)
        .build();
    let clamp = adw::Clamp::builder()
        .maximum_size(max_width)
        .tightening_threshold(PAGE_TIGHTEN_AT)
        .margin_top(12)
        .margin_bottom(16)
        .margin_start(16)
        .margin_end(16)
        .build();
    scrolled.set_child(Some(&clamp));
    let content = gtk::Box::new(gtk::Orientation::Vertical, PAGE_SPACING);
    clamp.set_child(Some(&content));
    (scrolled, content)
}

pub fn page_shell() -> (gtk::ScrolledWindow, gtk::Box) {
    build_page_shell(PAGE_MAX_WIDTH)
}

pub fn add_classes(w: &impl IsA<gtk::Widget>, classes: &str) {
    for c in classes.split_whitespace() {
        w.add_css_class(c);
    }
}

/// A button with an icon and a (translated) text label.
pub fn icon_button(icon: &str, label: &str, classes: &str, height: i32, touch: bool) -> gtk::Button {
    let btn = gtk::Button::new();
    let bx = gtk::Box::new(gtk::Orientation::Horizontal, if touch { 8 } else { 6 });
    bx.set_halign(gtk::Align::Center);
    bx.set_valign(gtk::Align::Center);
    let img = gtk::Image::from_icon_name(icon);
    img.set_pixel_size(if touch { 18 } else { 16 });
    bx.append(&img);
    bx.append(&gtk::Label::new(Some(&tr(label))));
    btn.set_child(Some(&bx));
    if touch {
        btn.add_css_class("touch-btn");
    }
    add_classes(&btn, classes);
    btn.set_size_request(-1, height);
    btn
}

/// Replace the text of an icon_button.
pub fn set_button_label(btn: &gtk::Button, text: &str) {
    let mut c = btn.child().and_then(|b| b.first_child());
    while let Some(w) = c {
        if let Some(l) = w.downcast_ref::<gtk::Label>() {
            l.set_label(text);
        }
        c = w.next_sibling();
    }
}

pub fn label(text: &str, classes: &str) -> gtk::Label {
    let l = gtk::Label::new(Some(text));
    add_classes(&l, classes);
    l
}

pub fn wrap_label(text: &str, classes: &str) -> gtk::Label {
    let l = label(text, classes);
    l.set_wrap(true);
    l.set_xalign(0.0);
    l
}

pub fn esc(s: &str) -> String {
    glib::markup_escape_text(s).to_string()
}

pub fn action_row(title: &str, subtitle: &str) -> adw::ActionRow {
    let r = adw::ActionRow::new();
    r.set_title(&esc(title));
    if !subtitle.is_empty() {
        r.set_subtitle(&esc(subtitle));
    }
    r
}

pub fn group(title: &str, description: &str) -> adw::PreferencesGroup {
    let g = adw::PreferencesGroup::new();
    if !title.is_empty() {
        g.set_title(&esc(title));
    }
    if !description.is_empty() {
        g.set_description(Some(&esc(description)));
    }
    g
}

pub fn status_pill(text: &str, css: &str) -> gtk::Label {
    let l = gtk::Label::new(Some(text));
    l.add_css_class(css);
    l.set_valign(gtk::Align::Center);
    l
}

pub const PILL_CLASSES: [&str; 6] =
    ["status-pill-running", "status-pill-stopped", "status-pill-checking", "status-pill-pending", "status-pill-new", "status-pill-update"];

pub fn set_pill(l: &gtk::Label, text: &str, css: &str) {
    for c in PILL_CLASSES {
        l.remove_css_class(c);
    }
    l.add_css_class(css);
    l.set_label(text);
}

pub fn copy_to_clipboard(text: &str) -> bool {
    match gdk::Display::default() {
        Some(d) => {
            d.clipboard().set_text(text);
            true
        }
        None => false,
    }
}

/// QR code for `url` as a texture (5 px modules, 2 module border).
pub fn generate_qr_texture(url: &str) -> Option<gdk::Texture> {
    let code = qrcode::QrCode::with_error_correction_level(url.as_bytes(), qrcode::EcLevel::M).ok()?;
    let n = code.width();
    let colors = code.to_colors();
    let (box_size, border) = (5usize, 2usize);
    let size = (n + 2 * border) * box_size;
    let mut px = vec![255u8; size * size * 3];
    for y in 0..n {
        for x in 0..n {
            if colors[y * n + x] == qrcode::Color::Dark {
                for dy in 0..box_size {
                    for dx in 0..box_size {
                        let (yy, xx) = ((y + border) * box_size + dy, (x + border) * box_size + dx);
                        let i = (yy * size + xx) * 3;
                        px[i..i + 3].copy_from_slice(&[0, 0, 0]);
                    }
                }
            }
        }
    }
    let bytes = glib::Bytes::from_owned(px);
    Some(gdk::MemoryTexture::new(size as i32, size as i32, gdk::MemoryFormat::R8g8b8, &bytes, size * 3).upcast())
}

/// RGB bytes -> texture.
pub fn rgb_texture(w: i32, h: i32, data: Vec<u8>) -> gdk::Texture {
    let bytes = glib::Bytes::from_owned(data);
    gdk::MemoryTexture::new(w, h, gdk::MemoryFormat::R8g8b8, &bytes, (w * 3) as usize).upcast()
}

/// BGR OpenCV mat -> texture.
pub fn mat_texture(m: &opencv::core::Mat) -> Option<gdk::Texture> {
    let (w, h, d) = crate::core::cvx::to_rgb_bytes(m).ok()?;
    Some(rgb_texture(w, h, d))
}

/// JPEG/PNG bytes -> texture.
pub fn bytes_texture(data: &[u8]) -> Option<gdk::Texture> {
    gdk::Texture::from_bytes(&glib::Bytes::from(data)).ok()
}

/// Open a URL in the browser, with Wayland flags for Chromium-based browsers.
pub fn open_browser_url(url: &str) -> bool {
    if url.is_empty() {
        return false;
    }
    use crate::core::util::which;
    let browser = ["chromium-browser", "chromium", "google-chrome", "brave-browser", "firefox"].iter().find_map(|b| which(b));
    if let Some(b) = browser {
        let bs = b.to_string_lossy().to_string();
        let mut cmd = std::process::Command::new(&b);
        if bs.contains("chromium") || bs.contains("chrome") || bs.contains("brave") {
            // Essential for Wayland on Fedora: prevents Ozone platform X11 crash
            cmd.args(["--ozone-platform-hint=auto", "--ozone-platform=wayland"]);
        }
        let uid = unsafe { libc::getuid() };
        if std::env::var_os("WAYLAND_DISPLAY").is_none() && std::path::Path::new(&format!("/run/user/{}/wayland-0", uid)).exists() {
            cmd.env("WAYLAND_DISPLAY", "wayland-0");
        }
        if std::env::var_os("XDG_RUNTIME_DIR").is_none() {
            cmd.env("XDG_RUNTIME_DIR", format!("/run/user/{}", uid));
        }
        use std::os::unix::process::CommandExt;
        cmd.arg(url)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .process_group(0);
        if cmd.spawn().is_ok() {
            return true;
        }
    }
    if gio::AppInfo::launch_default_for_uri(url, gio::AppLaunchContext::NONE).is_ok() {
        return true;
    }
    crate::core::util::spawn(&["xdg-open", url])
}

/// Adw.AlertDialog with responses (id, label); `suggested`/`destructive` get their appearance.
pub fn alert(heading: &str, body: &str, responses: &[(&str, &str)], suggested: Option<&str>, destructive: Option<&str>) -> adw::AlertDialog {
    let d = adw::AlertDialog::new(Some(heading), Some(body));
    for (id, lbl) in responses {
        d.add_response(id, lbl);
    }
    if let Some(s) = suggested {
        d.set_response_appearance(s, adw::ResponseAppearance::Suggested);
        d.set_default_response(Some(s));
    }
    if let Some(s) = destructive {
        d.set_response_appearance(s, adw::ResponseAppearance::Destructive);
    }
    if let Some((first, _)) = responses.first() {
        d.set_close_response(first);
    }
    d
}

/// Show an alert and call `f(response_id)`.
pub fn ask<F: Fn(&str) + 'static>(parent: &impl IsA<gtk::Widget>, d: adw::AlertDialog, f: F) {
    d.connect_response(None, move |_, r| f(r));
    d.present(Some(parent));
}

/// Simple OK message.
pub fn message(parent: &impl IsA<gtk::Widget>, heading: &str, body: &str) {
    alert(heading, body, &[("ok", &tr("OK"))], None, None).present(Some(parent));
}

/// Remove all children of a box-like container.
pub fn clear_box(b: &gtk::Box) {
    while let Some(c) = b.first_child() {
        b.remove(&c);
    }
}

/// Remove all rows a PreferencesGroup was given through `add`, tracked by the caller.
pub fn clear_rows(g: &adw::PreferencesGroup, rows: &mut Vec<gtk::Widget>) {
    for r in rows.drain(..) {
        g.remove(&r);
    }
}

pub fn spinner() -> adw::Spinner {
    adw::Spinner::new()
}

/// Warning card with an icon and wrapped text.
pub fn warning_card(text: &str, compact: bool) -> (gtk::Box, gtk::Label) {
    let b = gtk::Box::new(gtk::Orientation::Horizontal, 10);
    b.add_css_class(if compact { "warning-card-compact" } else { "warning-card" });
    let img = gtk::Image::from_icon_name("dialog-warning-symbolic");
    img.set_valign(gtk::Align::Start);
    b.append(&img);
    let l = wrap_label(text, "warning-text");
    l.set_hexpand(true);
    b.append(&l);
    (b, l)
}

/// Row suffix that shows either a status badge or an action button (the System page pattern).
#[derive(Clone)]
pub struct BadgeOrButton {
    pub widget: gtk::Box,
    pub badge: gtk::Label,
    pub button: gtk::Button,
}

impl BadgeOrButton {
    pub fn new(icon: &str, label: &str, classes: &str, height: i32) -> BadgeOrButton {
        let widget = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        widget.set_valign(gtk::Align::Center);
        let badge = gtk::Label::new(Some(&tr("Checking...")));
        badge.add_css_class("status-pill-checking");
        badge.set_valign(gtk::Align::Center);
        let button = icon_button(icon, label, classes, height, false);
        widget.append(&badge);
        BadgeOrButton { widget, badge, button }
    }

    pub fn show_badge(&self, text: &str, css: &str) {
        clear_box(&self.widget);
        set_pill(&self.badge, &tr(text), css);
        self.widget.append(&self.badge);
    }

    /// Show the button with a new icon and label.
    pub fn show_button(&self, icon: &str, text: &str) {
        clear_box(&self.widget);
        let bx = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        bx.set_halign(gtk::Align::Center);
        bx.set_valign(gtk::Align::Center);
        bx.append(&gtk::Image::from_icon_name(icon));
        bx.append(&gtk::Label::new(Some(text)));
        self.button.set_child(Some(&bx));
        self.button.remove_css_class("secondary-btn");
        self.button.add_css_class("suggested-action");
        self.button.set_sensitive(true);
        self.widget.append(&self.button);
    }

    pub fn show_both(&self) {
        clear_box(&self.widget);
        self.widget.append(&self.badge);
        self.widget.append(&self.button);
    }
}

pub fn switch_row(title: &str, subtitle: &str) -> adw::SwitchRow {
    let r = adw::SwitchRow::new();
    r.set_title(&esc(&tr(title)));
    if !subtitle.is_empty() {
        r.set_subtitle(&esc(&tr(subtitle)));
    }
    r
}

/// Clamped header box above a view stack (pinned rows above tabs).
pub fn top_clamp(child: &impl IsA<gtk::Widget>) -> adw::Clamp {
    let c = adw::Clamp::builder().maximum_size(PAGE_MAX_WIDTH).tightening_threshold(PAGE_TIGHTEN_AT).margin_top(12).margin_start(16).margin_end(16).build();
    c.set_child(Some(child));
    c
}

/// Inline view switcher with labels for a stack.
pub fn view_switcher(stack: &adw::ViewStack, margin_top: i32) -> adw::InlineViewSwitcher {
    let s = adw::InlineViewSwitcher::new();
    s.set_stack(Some(stack));
    s.set_display_mode(adw::InlineViewSwitcherDisplayMode::Labels);
    s.set_halign(gtk::Align::Center);
    s.set_margin_top(margin_top);
    s
}
