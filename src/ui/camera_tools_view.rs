//! Camera Tools hub: Focus Calibration, Camera Controls and the Camera Optimizer.
use super::helpers::{esc, page_shell};
use super::{Page, Win};
use crate::core::i18n::tr;
use adw::prelude::*;
use std::rc::Rc;

pub struct CameraToolsView {
    nav: adw::NavigationPage,
}

impl Page for CameraToolsView {
    fn nav(&self) -> &adw::NavigationPage {
        &self.nav
    }
}

impl CameraToolsView {
    pub fn new(win: Win) -> Rc<dyn Page> {
        let (scrolled, main) = page_shell();
        let nav = adw::NavigationPage::builder().title(tr("Camera Tools")).tag("camera_tools").child(&scrolled).build();
        let g = adw::PreferencesGroup::new();
        main.append(&g);
        type Open = fn(&Win);
        let items: [(&str, &str, &str, Open); 3] = [
            ("Focus Calibration", "Tune optical sharpness, target sweet spots, and audio tone guidance.", "camera-photo-symbolic", |w| w.open_focus_page()),
            ("Camera Controls", "Live view and every camera setting, kept across restarts, with presets.", "preferences-system-symbolic", |w| w.open_camera_controls_page()),
            ("Camera Optimizer", "One-click exposure, gain, and JPEG quality calibration for HBV OV2710 cameras.", "applications-science-symbolic", |w| w.open_camera_optimizer_page()),
        ];
        for (t, s, icon, open) in items {
            let r = adw::ActionRow::new();
            r.set_title(&esc(&tr(t)));
            r.set_subtitle(&esc(&tr(s)));
            let i = gtk::Image::from_icon_name(icon);
            i.set_pixel_size(24);
            i.set_margin_end(10);
            i.set_valign(gtk::Align::Center);
            r.add_prefix(&i);
            r.set_activatable(true);
            let w = win.clone();
            r.connect_activated(move |_| open(&w));
            let c = gtk::Image::from_icon_name("go-next-symbolic");
            c.set_opacity(0.5);
            c.set_valign(gtk::Align::Center);
            r.add_suffix(&c);
            g.add(&r);
        }
        Rc::new(CameraToolsView { nav })
    }
}
