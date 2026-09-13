import subprocess
import getpass
from core.logger import get_logger

logger = get_logger("privileges")

SUDOERS_FILE = "/etc/sudoers.d/suit"
POLKIT_RULES_FILE = "/etc/polkit-1/rules.d/50-suit.rules"

def check_sudo_privileges() -> bool:
    try:
        res = subprocess.run(["sudo", "-n", "true"], capture_output=True)
        return res.returncode == 0
    except Exception:
        logger.exception("Error checking passwordless sudo privileges")
        return False

def get_sudoers_script_command(user: str) -> str:
    sudo_rule = f"{user} ALL=(ALL) NOPASSWD: ALL"
    polkit_rule = (
        'polkit.addRule(function(action, subject) {'
        ' if ((action.id == \\"org.freedesktop.systemd1.manage-units\\" ||'
        ' action.id == \\"org.freedesktop.systemd1.manage-unit-files\\") &&'
        f' (subject.user == \\"{user}\\" || subject.isInGroup(\\"wheel\\"))) {{'
        ' return polkit.Result.YES;'
        ' }'
        '});'
    )
    return (
        f'echo "{sudo_rule}" > {SUDOERS_FILE} && chmod 0440 {SUDOERS_FILE} && '
        f'mkdir -p /etc/polkit-1/rules.d && '
        f'echo "{polkit_rule}" > {POLKIT_RULES_FILE} && chmod 0644 {POLKIT_RULES_FILE}'
    )

def setup_sudoers_pkexec(user: str | None = None) -> tuple[bool, str]:
    target_user = user or getpass.getuser()
    script = get_sudoers_script_command(target_user)
    full_cmd = f"pkexec bash -c '{script}'"
    try:
        logger.info(f"Invoking pkexec for sudoers setup for user {target_user}")
        res = subprocess.run(full_cmd, shell=True, capture_output=True, text=True)
        if res.returncode == 0 and check_sudo_privileges():
            logger.info("Passwordless sudo setup successfully established.")
            return True, "Sudo authorization successful"
        err_msg = res.stderr.strip() or "Polkit authorization cancelled or failed"
        logger.warning(f"Sudoers setup failed: {err_msg}")
        return False, err_msg
    except Exception as e:
        logger.exception("Failed executing pkexec")
        return False, str(e)
