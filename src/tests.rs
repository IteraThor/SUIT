//! Unit tests for the pure helpers ported from Python.
use crate::core::{autodarts, power, util, wireless_screen as ws};
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

fn block_sum(b: &[u8]) -> u8 {
    b.iter().fold(0u8, |a, x| a.wrapping_add(*x))
}

#[test]
fn edid_matches_spike_timing() {
    let e = ws::build_edid(1920, 1200, 60);
    assert_eq!(&e[0..8], &[0, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0]);
    assert_eq!(block_sum(&e[..128]), 0);
    assert_eq!(block_sum(&e[128..]), 0);
    assert_eq!(e[126], 1);
    // CVT-RB 1920x1200@59.95, byte for byte the timing that worked on the test PC
    assert_eq!(&e[54..66], &[0x28, 0x3c, 0x80, 0xa0, 0x70, 0xb0, 0x23, 0x40, 0x30, 0x20, 0x36, 0x00]);
    assert_eq!(e[71], 0x1a);
    assert_eq!(&e[38..42], &[0xd1, 0xc0, 0x81, 0xc0]);
    // VDB, HDMI VSDB (max TMDS 300 MHz), VCDB; DTDs would start at byte 18
    assert_eq!(&e[128..146], &[0x02, 0x03, 0x12, 0x80, 0x42, 16, 4, 0x67, 0x03, 0x0c, 0x00, 0x10, 0x00, 0x00, 60, 0xe2, 0x00, 0x4a]);
    assert!(e[146..255].iter().all(|b| *b == 0));
    assert_eq!(&e[95..106], b"SUIT Screen");
}

#[test]
fn edid_other_sizes() {
    for (w, h, r) in [(2048u32, 1536u32, 60u32), (2560, 1600, 120), (2360, 1640, 60)] {
        let e = ws::build_edid(w, h, r);
        assert_eq!(block_sum(&e[..128]), 0);
        assert_eq!(block_sum(&e[128..]), 0);
        let d = &e[54..72];
        assert_eq!(d[2] as u32 | ((d[4] as u32 >> 4) << 8), w);
        assert_eq!(d[5] as u32 | ((d[7] as u32 >> 4) << 8), h);
        let clock_mhz = (d[0] as u32 | (d[1] as u32) << 8) as f64 / 100.0;
        let total = ((w + 160) * (h + (d[6] as u32 | ((d[7] as u32 & 0xf) << 8)))) as f64;
        assert!((clock_mhz * 1e6 / total - r as f64).abs() < 0.5, "{}x{}@{} -> {} MHz", w, h, r, clock_mhz);
    }
}

#[test]
fn edid_cmdline_parse() {
    let spike = "BOOT_IMAGE=(hd0,gpt2)/vmlinuz ro rhgb quiet drm.edid_firmware=HDMI-A-3:edid/virtual-hdmi3.bin video=HDMI-A-3:e";
    assert_eq!(ws::parse_edid_arg(spike), Some(("HDMI-A-3".into(), "edid/virtual-hdmi3.bin".into())));
    assert_eq!(ws::parse_edid_arg("ro drm.edid_firmware=DP-2:edid/a.bin,HDMI-A-1:edid/b.bin"), Some(("DP-2".into(), "edid/a.bin".into())));
    assert_eq!(ws::parse_edid_arg("ro rhgb quiet"), None);
}

#[test]
fn connector_suggestion() {
    let c = |n: &str, on: bool| ws::Connector { name: n.into(), connected: on };
    let pc = ["DP-1", "DP-2", "DP-3", "HDMI-A-1", "HDMI-A-2", "HDMI-A-3"].map(|n| c(n, n == "HDMI-A-2"));
    assert_eq!(ws::suggest_connector(&pc, &[]).as_deref(), Some("HDMI-A-3"));
    assert_eq!(ws::suggest_connector(&pc, &["HDMI-A-3".into()]).as_deref(), Some("HDMI-A-1"));
    let dp = [c("eDP-1", true), c("DP-1", false), c("DP-2", false)];
    assert_eq!(ws::suggest_connector(&dp, &[]).as_deref(), Some("DP-2"));
    assert_eq!(ws::suggest_connector(&[c("eDP-1", false), c("HDMI-A-1", true)], &[]), None);
}

#[test]
fn blank_cursor_bytes() {
    let c = ws::blank_xcursor();
    assert_eq!(c.len(), 68);
    assert_eq!(&c[0..4], b"Xcur");
    let w = |i: usize| u32::from_le_bytes(c[i..i + 4].try_into().unwrap());
    assert_eq!((w(4), w(8), w(12)), (16, 0x10000, 1));
    assert_eq!((w(16), w(20), w(24)), (0xfffd0002, 24, 28));
    assert_eq!((w(28), w(32), w(36), w(40)), (36, 0xfffd0002, 24, 1));
    assert_eq!((w(44), w(48)), (1, 1));
    assert_eq!((w(52), w(56), w(60), w(64)), (0, 0, 0, 0)); // hotspot, delay and the one pixel, fully transparent
}

#[test]
fn cursor_prep_in_apps_json() {
    let mut apps = serde_json::json!({"apps": [{"name": "Desktop", "image-path": "desktop.png", "prep-cmd": [{"do": "a", "undo": "b"}]}, {"name": "Steam"}]});
    let cmd = Some(("sh /h/.config/suit/wireless-cursor.sh hide", "sh /h/.config/suit/wireless-cursor.sh show"));
    ws::set_cursor_prep(&mut apps, cmd);
    ws::set_cursor_prep(&mut apps, cmd);
    let prep = apps["apps"][0]["prep-cmd"].as_array().unwrap();
    assert_eq!(prep.len(), 2);
    assert_eq!(prep[0]["do"], "a");
    assert_eq!(prep[1]["undo"], "sh /h/.config/suit/wireless-cursor.sh show");
    ws::set_cursor_prep(&mut apps, None);
    assert_eq!(apps["apps"][0]["prep-cmd"].as_array().unwrap().len(), 1);
    assert_eq!(apps["apps"][1], serde_json::json!({"name": "Steam"}));
    let mut fresh = serde_json::json!({});
    ws::set_cursor_prep(&mut fresh, cmd);
    assert_eq!(fresh["apps"][0]["name"], "Desktop");
    ws::set_cursor_prep(&mut fresh, None);
    assert!(fresh["apps"][0].get("prep-cmd").is_none());
}

#[test]
fn resolution_text() {
    assert_eq!(ws::parse_resolution("1920x1200"), Some((1920, 1200)));
    assert_eq!(ws::parse_resolution(" 2360 X 1640 "), Some((2360, 1640)));
    assert_eq!(ws::parse_resolution("2048×1536"), Some((2048, 1536)));
    assert_eq!(ws::parse_resolution("320x240"), None);
    assert_eq!(ws::parse_resolution("1920"), None);
    assert_eq!(ws::parse_resolution("axb"), None);
}

#[test]
fn friendly_pc_name() {
    use crate::core::wireless_screen::friendly_name;
    assert_eq!(friendly_name("board2"), "board2");
    assert_eq!(friendly_name("dartpc.fritz.box"), "dartpc");
    assert_eq!(friendly_name("p200300Cd7f0EcCA8Af1c699b77Fbc2A6.dip0.t-ipconnect.de"), "SUIT");
}

#[test]
fn forced_connector_wins() {
    let c = |n: &str, on: bool| ws::Connector { name: n.into(), connected: on };
    // test PC: hand-made virtual screen on HDMI-A-3 (connected), real monitors use HDMI-A-2
    let pc = ["DP-1", "HDMI-A-1", "HDMI-A-2", "HDMI-A-3"].map(|n| c(n, n == "HDMI-A-3"));
    let spike = "ro rhgb quiet drm.edid_firmware=HDMI-A-3:edid/virtual-hdmi3.bin video=HDMI-A-3:e";
    assert_eq!(ws::choose_connector(spike, &pc, &[]).as_deref(), Some("HDMI-A-3"));
    assert_eq!(ws::choose_connector("ro video=DP-1:1920x1200@60e", &pc, &[]).as_deref(), Some("DP-1"));
    assert_eq!(ws::choose_connector("ro drm.edid_firmware=DP-1:edid/x.bin", &pc, &[]).as_deref(), Some("DP-1"));
    // not forcing a port: the normal suggestion (connected HDMI-A-3 skipped)
    assert_eq!(ws::choose_connector("ro rhgb quiet video=efifb:off", &pc, &[]).as_deref(), Some("HDMI-A-2"));
    assert_eq!(ws::choose_connector("ro", &pc, &["HDMI-A-2".into()]).as_deref(), Some("HDMI-A-1"));
}

#[test]
fn hdmi_size_fit() {
    let clock_mhz = |w: u32, h: u32, r: u32| {
        let d = &ws::build_edid(w, h, r)[54..72];
        (d[0] as u32 | (d[1] as u32) << 8) as f64 / 100.0
    };
    assert_eq!(ws::fit_size(2160, 1620, 60, true), (2160, 1620)); // 231.75 MHz fits
    assert_eq!(ws::fit_size(2732, 2048, 60, false), (2732, 2048)); // DP: no limit
    for (w, h, r) in [(2732u32, 2048u32, 60u32), (2560, 1600, 60), (1920, 1200, 120), (2360, 1640, 60)] {
        let (fw, fh) = ws::fit_size(w, h, r, true);
        assert!(clock_mhz(fw, fh, r) <= 297.0, "{}x{}@{} -> {}x{}", w, h, r, fw, fh);
        assert!(clock_mhz(fw + 8, (fw + 8) * h / w, r) > 297.0 || fw == w, "{}x{} is not the largest", fw, fh);
        assert!(fw % 8 == 0 && fh % 2 == 0);
        assert!(((fw * h) as f64 / w as f64 - fh as f64).abs() <= 1.0, "aspect {}x{} vs {}x{}", fw, fh, w, h);
    }
    assert_eq!(ws::size_on_port(2732, 2048, 60, "DP-1"), (2732, 2048));
}

#[test]
fn size_change_needs_restart() {
    let old = ws::build_edid(1920, 1200, 60);
    let new = ws::build_edid(2160, 1620, 60);
    assert!(!ws::edid_outdated(&old, &old));
    assert!(ws::edid_outdated(&old, &new));
    assert!(!ws::edid_outdated(&[], &new)); // not running yet: the plain "set up, not active" case
    let s = |active, edid_changed| ws::Status { configured: Some("HDMI-A-2".into()), active, edid_changed, ..Default::default() };
    assert!(s(false, false).reboot_pending());
    assert!(s(true, true).reboot_pending());
    assert!(!s(true, false).reboot_pending());
    assert!(!ws::Status { active: true, edid_changed: true, ..Default::default() }.reboot_pending());
}

#[test]
fn sunshine_conf_merge() {
    let old = "# Written by SUIT Wireless Screen\ncapture = kms\nsunshine_name = old\nencoder = software\nmax_bitrate = 1\n";
    let set = ws::sunshine_settings("autodarts", "192.168.2.110", false);
    let new = ws::merge_conf(old, &set);
    assert!(new.contains("encoder = software"), "user lines stay");
    assert!(new.contains("sunshine_name = autodarts") && !new.contains("sunshine_name = old"));
    assert!(new.contains("max_bitrate = 15000") && !new.contains("max_bitrate = 1\n"));
    for k in ["capture = kms", "sw_tune = zerolatency", "upnp = disabled", "origin_web_ui_allowed = lan", "csrf_allowed_origins = https://192.168.2.110:47990"] {
        assert!(new.contains(k), "{}", k);
    }
    assert_eq!(new.matches("# Written by SUIT").count(), 1);
    // idempotent
    assert_eq!(ws::merge_conf(&new, &set), new);
    assert_eq!(ws::bitrate(true), 35000);
}

#[test]
fn pairing_and_log_parsing() {
    let v: serde_json::Value = serde_json::from_str(r#"{"pairings":[{"id":"a1","name":"Old","address":"x"},{"id":"b2","name":" iPad ","address":"y"}]}"#).unwrap();
    assert_eq!(ws::pending_pairing(&v), Some(("b2".into(), "iPad".into())));
    assert_eq!(ws::pending_pairing(&serde_json::json!({"pairings": []})), None);
    assert_eq!(ws::pending_pairing(&serde_json::Value::Null), None);
    use ws::PairOutcome::*;
    assert_eq!(ws::pair_outcome(Some(true), true), Paired);
    assert_eq!(ws::pair_outcome(Some(true), false), TimedOut);
    assert_eq!(ws::pair_outcome(Some(false), false), WrongPin);
    assert_eq!(ws::pair_outcome(None, false), NoAnswer);

    let log = "[2026-11-18 20:00:00]: Debug: mode -- 2160x1620x60\n[..]: Info: CLIENT CONNECTED\n[..]: Info: CLIENT DISCONNECTED\n";
    assert!(!ws::streaming(log));
    assert!(ws::streaming(&format!("{}[..]: Info: CLIENT CONNECTED\n", log)));
    assert!(!ws::streaming(""));
    assert_eq!(ws::client_mode(log), Some((2160, 1620, 60)));
    assert_eq!(ws::client_mode("GET /launch?uniqueid=0&mode=2388x1668x120&additionalStates=1"), Some((2388, 1668, 120)));
    assert_eq!(ws::client_mode("mode -- 0x0x0"), None);
    assert_eq!(ws::default_dev("default via 192.168.2.1 dev wlp2s0 proto dhcp metric 600\n"), Some("wlp2s0".into()));
    assert_eq!(ws::default_dev(""), None);
}

#[test]
fn rescue_entry_and_watcher_unit() {
    let info = "index=0\nkernel=\"/boot/vmlinuz-6.17.1-300.fc45.x86_64\"\nargs=\"ro rhgb drm.edid_firmware=HDMI-A-3:edid/suit-virtual.bin video=HDMI-A-3:e\"\nindex=1\nkernel=\"/boot/vmlinuz-0-rescue-abc\"\nargs=\"ro rhgb drm.edid_firmware=HDMI-A-3:edid/suit-virtual.bin video=HDMI-A-3:e\"\n";
    assert_eq!(ws::rescue_kernels(info), vec!["/boot/vmlinuz-0-rescue-abc".to_string()]);
    assert!(ws::rescue_kernels("index=0\nkernel=\"/boot/vmlinuz-6\"\n").is_empty());
    let u = ws::watcher_unit("/home/a/suit", "/home/a/SUIT");
    assert!(u.contains("ExecStart=/home/a/suit wireless-screen --watch"));
    assert!(u.contains("Restart=on-failure") && u.contains("WantedBy=graphical-session.target"));
}

#[test]
fn layout_and_offers() {
    use crate::core::display::LogicalMonitor;
    let lm = |m: &[&str]| LogicalMonitor { x: 0, y: 0, scale: 1.0, trans: 0, primary: true, monitors: m.iter().map(|s| s.to_string()).collect() };
    assert_eq!(ws::layout_of(&[lm(&["HDMI-A-1", "HDMI-A-3"])], "HDMI-A-3"), Some(ws::Layout::Mirror));
    assert_eq!(ws::layout_of(&[lm(&["HDMI-A-3"])], "HDMI-A-3"), Some(ws::Layout::TabletOnly));
    assert_eq!(ws::layout_of(&[lm(&["HDMI-A-1"]), lm(&["HDMI-A-3"])], "HDMI-A-3"), Some(ws::Layout::Extend));
    assert_eq!(ws::layout_of(&[lm(&["HDMI-A-1"])], "HDMI-A-3"), None);

    let cfg = ws::Config { connector: "HDMI-A-3".into(), ..ws::Config::default() };
    let mut s = ws::Status { config: cfg, port: Some("HDMI-A-3".into()), online: Some(true), ..ws::Status::default() };
    assert_eq!(s.preflight_error(), None);
    s.online = Some(false);
    assert!(s.preflight_error().is_some());
    s.online = Some(true);
    s.port = None;
    assert!(s.preflight_error().is_some(), "no free port");
    // the tablet asks for its own size: offered as it fits the HDMI port, not when it is the size already
    s.client_mode = Some((2732, 2048, 60));
    assert_eq!(s.tablet_offer(), Some(ws::fit_size(2732, 2048, 60, true)));
    s.client_mode = Some((1920, 1200, 60));
    assert_eq!(s.tablet_offer(), None);
    for (_, w, h) in ws::PRESETS {
        let (fw, fh) = ws::fit_size(w, h, 60, true);
        assert!((fw, fh) == (w, h) || (fw < w && fh < h && fw % 8 == 0 && fh % 2 == 0), "{}x{}", w, h);
    }
}

#[test]
fn mutter_connector_names() {
    use crate::core::wireless_screen::mutter_name;
    assert_eq!(mutter_name("HDMI-A-2"), "HDMI-2");
    assert_eq!(mutter_name("DVI-D-1"), "DVI-1");
    assert_eq!(mutter_name("DP-3"), "DP-3");
    assert_eq!(mutter_name("eDP-1"), "eDP-1");
}

#[test]
fn greeter_layout_puts_real_monitor_first() {
    use crate::core::display::{Mode, PhysMonitor};
    use crate::core::wireless_screen::{greeter_layout, Config};
    let mode = Mode { id: "m".into(), w: 1920, h: 1200, rate: 59.95, preferred: true };
    let mon = PhysMonitor { connector: "HDMI-3".into(), vendor: "RTK".into(), product: "ANMITE".into(), serial: "0x1".into(), current: Some(mode.clone()), modes: vec![mode] };
    let cfg = Config { width: 2160, height: 1620, connector: "HDMI-A-2".into(), ..Config::default() };
    let x = greeter_layout(&cfg, &[&mon]).unwrap();
    assert!(x.find("<connector>HDMI-3").unwrap() < x.find("<connector>HDMI-2").unwrap());
    assert_eq!(x.matches("<primary>yes</primary>").count(), 1);
    assert!(x.contains("<x>1920</x>") && x.contains("<rate>59.959</rate>"), "{}", x);
    assert!(greeter_layout(&cfg, &[]).is_none());
}
