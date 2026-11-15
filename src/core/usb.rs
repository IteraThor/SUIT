use super::util::read_trim;
use std::path::Path;

#[derive(Debug, Clone, Default)]
pub struct CameraDevice {
    pub dev: String,
    pub dev_path: String,
    pub name: String,
    pub bus_id: String,
    pub devpath: String,
    pub speed: String,
    pub hub_name: String,
    pub hub_id: String,
    pub transfer_mode: String,
}

/// Scans video4linux devices and resolves physical USB hierarchy, transfer mode, and speed.
pub fn get_camera_devices() -> Vec<CameraDevice> {
    let mut cams = Vec::new();
    let v4l = Path::new("/sys/class/video4linux");
    let Ok(rd) = std::fs::read_dir(v4l) else { return cams };
    let mut entries: Vec<_> = rd
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.file_name().and_then(|n| n.to_str()).map(|n| n.starts_with("video")).unwrap_or(false))
        .collect();
    entries.sort();
    for p in entries {
        let idx = read_trim(p.join("index")).unwrap_or_else(|| "0".into());
        let Some(name) = read_trim(p.join("name")) else { continue };
        if idx != "0" {
            continue;
        }
        let devname = p.file_name().unwrap().to_string_lossy().to_string();
        let mut cam = CameraDevice {
            dev_path: format!("/dev/{}", devname),
            dev: devname,
            name,
            bus_id: "unknown".into(),
            speed: "Unknown".into(),
            hub_name: "Root Controller Port".into(),
            hub_id: "root".into(),
            transfer_mode: "Bulk".into(),
            ..Default::default()
        };
        if let Ok(link) = std::fs::canonicalize(p.join("device")) {
            let mut dev_dir = link.clone();
            while !dev_dir.join("idVendor").exists() {
                match dev_dir.parent() {
                    Some(pa) if pa != dev_dir => dev_dir = pa.to_path_buf(),
                    _ => break,
                }
            }
            if dev_dir.join("idVendor").exists() {
                let busnum = read_trim(dev_dir.join("busnum")).unwrap_or_default();
                cam.bus_id = busnum.parse::<u32>().map(|n| format!("Bus {:03}", n)).unwrap_or(busnum);
                cam.devpath = read_trim(dev_dir.join("devpath")).unwrap_or_default();
                cam.speed = read_trim(dev_dir.join("speed")).unwrap_or_else(|| "Unknown".into());
                if let Some(parent) = dev_dir.parent() {
                    let pname = parent.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
                    if parent.join("product").exists() {
                        cam.hub_id = pname;
                        cam.hub_name = read_trim(parent.join("product")).unwrap_or_default();
                    } else if parent.join("idVendor").exists() {
                        cam.hub_id = pname.clone();
                        cam.hub_name = format!("Hub {}", pname);
                    }
                }
                // Inspect video streaming interfaces for transfer mode (Bulk vs Isochronous)
                if let Ok(rd) = std::fs::read_dir(&dev_dir) {
                    'outer: for iface in rd.flatten().map(|e| e.path()) {
                        let n = iface.file_name().unwrap().to_string_lossy().to_string();
                        if !(n.contains(':') && n.contains('.')) {
                            continue;
                        }
                        let c = read_trim(iface.join("bInterfaceClass")).unwrap_or_default().to_lowercase();
                        let sc = read_trim(iface.join("bInterfaceSubClass")).unwrap_or_default().to_lowercase();
                        if (c == "0e" || c == "14") && (sc == "02" || sc == "2") {
                            if let Ok(eps) = std::fs::read_dir(&iface) {
                                for ep in eps.flatten().map(|e| e.path()) {
                                    if !ep.file_name().unwrap().to_string_lossy().starts_with("ep_") {
                                        continue;
                                    }
                                    let t = read_trim(ep.join("type")).unwrap_or_default();
                                    if !t.is_empty() {
                                        cam.transfer_mode = t;
                                        break 'outer;
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        cams.push(cam);
    }
    cams
}
