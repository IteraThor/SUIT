use super::i18n::trf;
use super::util::{has_cmd, run, run_shell};

#[derive(Debug, Clone, Default)]
pub struct TailscaleStatus {
    pub installed: bool,
    pub running: bool,
    pub state: String,
    pub auth_url: Option<String>,
    pub ips: Vec<String>,
    pub hostname: String,
    pub dns_name: String,
}

pub fn install_tailscale() -> (bool, String) {
    let r = run_shell("curl -fsSL https://tailscale.com/install.sh | sudo -n sh", None);
    if r.code != 0 {
        log::error!("Failed installing Tailscale: {}", r.stderr);
        return (false, trf("Failed installing Tailscale: {error}", &[("error", &r.stderr.trim())]));
    }
    run(&["sudo", "-n", "systemctl", "enable", "--now", "tailscaled"], None);
    (true, "Tailscale installed and service started.".into())
}

fn status_json(timeout: u64) -> Option<serde_json::Value> {
    let r = run(&["tailscale", "status", "--json"], Some(timeout));
    if r.code != 0 {
        return None;
    }
    serde_json::from_str(&r.stdout).ok()
}

pub fn get_tailscale_status() -> TailscaleStatus {
    if !has_cmd("tailscale") {
        return TailscaleStatus { state: "NotInstalled".into(), ..Default::default() };
    }
    let mut st = TailscaleStatus { installed: true, state: "Stopped".into(), ..Default::default() };
    if let Some(data) = status_json(3) {
        let backend = data["BackendState"].as_str().unwrap_or("");
        st.auth_url = data["AuthURL"].as_str().filter(|s| !s.is_empty()).map(String::from);
        let me = &data["Self"];
        st.ips = me["TailscaleIPs"]
            .as_array()
            .map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect())
            .unwrap_or_default();
        st.hostname = me["HostName"].as_str().unwrap_or("").to_string();
        st.dns_name = me["DNSName"].as_str().unwrap_or("").trim_end_matches('.').to_string();
        st.state = if backend.is_empty() { "Unknown".into() } else { backend.to_string() };
    }
    st.running = st.state == "Running";
    st
}

pub fn get_tailscale_auth_url() -> Option<String> {
    let from_status = || status_json(2).and_then(|d| d["AuthURL"].as_str().filter(|s| !s.is_empty()).map(String::from));
    if let Some(u) = from_status() {
        return Some(u);
    }
    let r = run(&["sudo", "-n", "tailscale", "up", "--reset", "--timeout=15s"], Some(18));
    let re = regex::Regex::new(r"https://login\.tailscale\.com/a/\S+").unwrap();
    if let Some(m) = re.find(&r.combined()) {
        return Some(m.as_str().to_string());
    }
    from_status()
}

pub fn disconnect_tailscale() -> (bool, String) {
    let r = run(&["sudo", "-n", "tailscale", "down"], None);
    if r.code == 0 {
        (true, "Tailscale disconnected.".into())
    } else {
        log::error!("Failed disconnecting Tailscale: {}", r.stderr);
        (false, trf("Failed disconnecting Tailscale: {error}", &[("error", &r.stderr.trim())]))
    }
}

pub fn connect_tailscale() -> (bool, String) {
    let r = run(&["sudo", "-n", "tailscale", "up"], Some(5));
    if r.ok() {
        (true, "Tailscale connected.".into())
    } else {
        log::error!("Failed connecting Tailscale: {}", r.stderr);
        (false, trf("Failed connecting Tailscale: {error}", &[("error", &r.stderr.trim())]))
    }
}

pub fn launch_tailscale_up() -> bool {
    get_tailscale_auth_url().is_some()
}
