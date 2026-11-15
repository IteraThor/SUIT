use super::util::{run, run_shell, username};

pub const SUDOERS_FILE: &str = "/etc/sudoers.d/suit";
pub const POLKIT_RULES_FILE: &str = "/etc/polkit-1/rules.d/50-suit.rules";

pub fn check_sudo_privileges() -> bool {
    run(&["sudo", "-n", "true"], Some(10)).code == 0
}

pub fn get_sudoers_script_command(user: &str) -> String {
    let sudo_rule = format!("{} ALL=(ALL) NOPASSWD: ALL", user);
    let polkit_rule = format!(
        "polkit.addRule(function(action, subject) {{ if ((action.id == \\\"org.freedesktop.systemd1.manage-units\\\" || action.id == \\\"org.freedesktop.systemd1.manage-unit-files\\\") && (subject.user == \\\"{u}\\\" || subject.isInGroup(\\\"wheel\\\"))) {{ return polkit.Result.YES; }}}});",
        u = user
    );
    format!(
        "echo \"{sr}\" > {sf} && chmod 0440 {sf} && mkdir -p /etc/polkit-1/rules.d && echo \"{pr}\" > {pf} && chmod 0644 {pf}",
        sr = sudo_rule,
        sf = SUDOERS_FILE,
        pr = polkit_rule,
        pf = POLKIT_RULES_FILE
    )
}

pub fn setup_sudoers_pkexec(user: Option<&str>) -> (bool, String) {
    let target_user = user.map(|s| s.to_string()).unwrap_or_else(username);
    let script = get_sudoers_script_command(&target_user);
    let full_cmd = format!("pkexec bash -c '{}'", script);
    log::info!("Invoking pkexec for sudoers setup for user {}", target_user);
    let res = run_shell(&full_cmd, None);
    if res.code == 0 && check_sudo_privileges() {
        log::info!("Passwordless sudo setup successfully established.");
        return (true, "Sudo authorization successful".into());
    }
    let err = res.stderr.trim();
    let err = if err.is_empty() { "Polkit authorization cancelled or failed".to_string() } else { err.to_string() };
    log::warn!("Sudoers setup failed: {}", err);
    (false, err)
}
