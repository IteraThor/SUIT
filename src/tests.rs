//! Unit tests for the pure helpers ported from Python.
use crate::core::{autodarts, power, util};
use crate::core::hbvcal::{autocal, profiles, settings::{self, Settings}};

#[test]
fn fmt_named_and_specs() {
    assert_eq!(util::fmt("{a} of {b}", &[("a", &1), ("b", &"x")]), "1 of x");
    assert_eq!(util::fmt("{seconds:.1f} s", &[("seconds", &"3.2")]), "3.2 s");
    assert_eq!(util::fmt("{missing}", &[]), "{missing}");
}

#[test]
fn strv_roundtrip() {
    let v = util::parse_strv("['a.desktop', 'b.desktop']").unwrap();
    assert_eq!(v, vec!["a.desktop", "b.desktop"]);
    assert_eq!(util::parse_strv("@as []").unwrap(), Vec::<String>::new());
    assert_eq!(util::fmt_strv(&v), "['a.desktop', 'b.desktop']");
    assert!(util::parse_strv("uint32 0").is_none());
}

#[test]
fn analog_gain_codes() {
    assert_eq!(autocal::analog_code(1.0), 0x00);
    assert_eq!(autocal::analog_code(2.0), 0x10);
    assert_eq!(autocal::analog_code(4.0), 0x30);
    assert!((autocal::analog_gain(0x10) - 2.0).abs() < 1e-9);
    assert!((autocal::analog_gain(0x38) - 6.0).abs() < 1e-9);
}

#[test]
fn q_levels() {
    assert_eq!(settings::q_above(5, 1), 6);
    assert_eq!(settings::q_above(8, 2), 16);
    assert_eq!(settings::q_above(22, 1), 22);
    assert_eq!(settings::q_class(1280), "mid");
    assert_eq!(settings::q_class(1920), "large");
    assert_eq!(settings::q_class(640), "small");
}

#[test]
fn settings_json_roundtrip() {
    let mut s = Settings::default();
    s.uvc.insert("gain".into(), 53);
    s.mjpeg_q = Some(8);
    s.sensor.insert(0x350B, 0);
    let j = s.to_json();
    assert_eq!(j["sensor"]["0x350B"], 0);
    assert_eq!(Settings::from_json(&j), s);
    assert_eq!(s.label(), "gain=53 Q=8 s350B=0x00");
}

#[test]
fn port_keys() {
    assert_eq!(profiles::port_key("/dev/v4l/by-path/pci-0000:00:14.0-usb-0:1:1.0-video-index0"), "pci-0000:00:14.0-usb-0:1:1.0");
    assert_eq!(autocal::port_no("/dev/v4l/by-path/pci-0000:00:14.0-usb-0:2:1.0-video-index0"), "2");
}

#[test]
fn toml_sections() {
    let c = "[cam]\nwidth = 1\n\n[motion]\nx = 2\n";
    let r = autodarts::replace_section(c, "cam", "[cam]\nwidth = 9", false);
    assert!(r.contains("[cam]\nwidth = 9\n\n[motion]\nx = 2"), "{}", r);
    let r = autodarts::replace_section("a = 1", "auth", "[auth]\nk = 1", true);
    assert!(r.starts_with("[auth]\nk = 1\n\na = 1"));
    let r = autodarts::replace_section("[x]\n", "upstream", "[upstream]\nk = 1", false);
    assert!(r.ends_with("[upstream]\nk = 1\n"));
}

#[test]
fn gdm_config() {
    let dir = std::env::temp_dir().join(format!("suit-test-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let p = dir.join("custom.conf");
    std::fs::write(&p, "[daemon]\nAutomaticLoginEnable=False\nWaylandEnable=true\n").unwrap();
    let out = power::generate_gdm_config(&p, true, "pi");
    assert_eq!(out, "[daemon]\nAutomaticLoginEnable=True\nAutomaticLogin=pi\nWaylandEnable=true\n");
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn camera_labels() {
    assert_eq!(autodarts::format_short_camera_label("USB Camera: USB Camera", "usb-0000:00:14.0-2", ""), "USB Cam (2)");
    assert_eq!(autodarts::parse_cam_device("3"), "/dev/video3");
}

#[test]
fn german_table_loads() {
    // read-only: set_language would write the user's real ~/.config/suit/settings.json
    let de: std::collections::HashMap<String, String> = serde_json::from_str(include_str!("../data/i18n_de.json")).unwrap();
    assert_eq!(de["Later"], "Später");
    assert!(de.len() > 1000);
}
