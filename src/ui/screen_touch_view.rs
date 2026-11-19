//! Screen & Touch Manager hub: Wireless Screen (tablet as a screen) and Display Rotation & Touch.
use super::helpers::{esc, page_shell};
use super::{Page, Win};
use crate::core::i18n::tr;
use adw::prelude::*;
use std::rc::Rc;

pub struct ScreenTouchView {
    nav: adw::NavigationPage,
}

impl Page for ScreenTouchView {
    fn nav(&self) -> &adw::NavigationPage {
        &self.nav
    }
}

impl ScreenTouchView {
    pub fn new(win: Win) -> Rc<dyn Page> {
        let (scrolled, main) = page_shell();
        let nav = adw::NavigationPage::builder().title(tr("Screen & Touch Manager")).tag("screen_touch").child(&scrolled).build();
        let g = adw::PreferencesGroup::new();
        main.append(&g);
        type Open = fn(&Win);
        let items: [(&str, &str, &str, Open); 2] = [
            ("Wireless Screen", "Use an Android or iOS tablet as a wireless screen.", "network-wireless-symbolic", |w| w.open_wireless_screen_page()),
            ("Display Rotation & Touch", "Per-display rotation and touch calibration.", "screen-touch-symbolic", |w| w.open_rotation_page()),
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
            if t == "Wireless Screen" {
                let exp = gtk::Label::new(Some(&tr("EXPERIMENTAL")));
                exp.add_css_class("status-pill-experimental");
                exp.set_valign(gtk::Align::Center);
                r.add_suffix(&exp);
            }
            let c = gtk::Image::from_icon_name("go-next-symbolic");
            c.set_opacity(0.5);
            c.set_valign(gtk::Align::Center);
            r.add_suffix(&c);
            g.add(&r);
        }
        Rc::new(ScreenTouchView { nav })
    }
}
