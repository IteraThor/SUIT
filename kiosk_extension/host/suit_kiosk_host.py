#!/usr/bin/env python3
import sys
import json
import struct
import subprocess
import os
import logging

LOG_FILE = "/tmp/suit_kiosk_host.log"
try:
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )
except Exception:
    pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
if os.path.isdir(os.path.join(PROJECT_ROOT, "core")):
    if PROJECT_ROOT not in sys.path:
        sys.path.insert(0, PROJECT_ROOT)
else:
    for cand in [os.path.expanduser("~/SUIT-Fedora"), os.path.expanduser("~/SUIT")]:
        if os.path.isdir(os.path.join(cand, "core")):
            if cand not in sys.path:
                sys.path.insert(0, cand)
            break

try:
    from core.light_service import LightService
except Exception as e:
    logging.warning("Could not import LightService: %s", e)
    LightService = None

def read_message():
    raw_len = sys.stdin.buffer.read(4)
    if not raw_len or len(raw_len) < 4:
        return None
    msg_len = struct.unpack("@I", raw_len)[0]
    raw_data = sys.stdin.buffer.read(msg_len)
    if len(raw_data) < msg_len:
        return None
    return json.loads(raw_data.decode("utf-8"))

def send_message(payload):
    encoded = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("@I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()

def _run_cmd(cmd_list, timeout=5) -> tuple[int, str, str]:
    try:
        res = subprocess.run(
            cmd_list,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        logging.debug("Ran %s -> exit %d, out=%r, err=%r", cmd_list, res.returncode, res.stdout, res.stderr)
        return res.returncode, res.stdout, res.stderr
    except Exception as e:
        logging.exception("Exception running %s: %s", cmd_list, e)
        return -1, "", str(e)

def handle_action(action: str) -> dict:
    logging.info("Handling action: %s", action)

    if action == "poweroff":
        commands = [
            ["systemctl", "poweroff", "-i", "--no-ask-password", "--no-block"],
            ["sudo", "-n", "/usr/bin/systemctl", "poweroff", "-i", "--no-ask-password", "--no-block"],
            ["loginctl", "poweroff", "-i", "--no-ask-password"],
        ]
        last_err = ""
        success = False
        for cmd in commands:
            ret, out, err = _run_cmd(cmd)
            if ret == 0:
                success = True
                break
            last_err = err or f"exit code {ret}"

        if success:
            # Terminate kiosk browser after signaling shutdown
            _run_cmd(["pkill", "-TERM", "-f", "(chromium|chrome|firefox).*(--kiosk|play.autodarts.com)"])
            return {"status": "ok", "action": "poweroff"}

        logging.error("All poweroff attempts failed. Last err: %s", last_err)
        return {"status": "error", "error": f"Power off failed: {last_err or 'permission denied'}"}

    elif action == "reboot":
        commands = [
            ["systemctl", "reboot", "-i", "--no-ask-password", "--no-block"],
            ["sudo", "-n", "/usr/bin/systemctl", "reboot", "-i", "--no-ask-password", "--no-block"],
            ["loginctl", "reboot", "-i", "--no-ask-password"],
        ]
        last_err = ""
        success = False
        for cmd in commands:
            ret, out, err = _run_cmd(cmd)
            if ret == 0:
                success = True
                break
            last_err = err or f"exit code {ret}"

        if success:
            # Terminate kiosk browser after signaling reboot
            _run_cmd(["pkill", "-TERM", "-f", "(chromium|chrome|firefox).*(--kiosk|play.autodarts.com)"])
            return {"status": "ok", "action": "reboot"}

        logging.error("All reboot attempts failed. Last err: %s", last_err)
        return {"status": "error", "error": f"Reboot failed: {last_err or 'permission denied'}"}

    elif action == "exit_kiosk":
        ret, out, err = _run_cmd(["pkill", "-TERM", "-f", "(chromium|chrome|firefox).*(--kiosk|play.autodarts.com)"])
        return {"status": "ok", "action": "exit_kiosk"}

    elif action == "ping":
        return {"status": "ok", "action": "ping", "message": "SUIT Kiosk Host ready"}

    elif action == "get_light_status":
        if not LightService:
            return {"status": "error", "error": "Light service unavailable"}
        cfg = LightService.read_config()
        if not cfg.get("light_enabled"):
            return {
                "status": "ok",
                "action": "get_light_status",
                "enabled": False,
                "is_on": False,
                "ip": cfg.get("light_ip", ""),
            }
        reachable, is_on, msg_text = LightService.query_status(cfg)
        return {
            "status": "ok",
            "action": "get_light_status",
            "enabled": True,
            "device_type": cfg.get("light_device_type", "wled"),
            "ip": cfg.get("light_ip", ""),
            "reachable": reachable,
            "is_on": is_on,
            "message": msg_text,
        }

    elif action == "toggle_light":
        if not LightService:
            return {"status": "error", "error": "Light service unavailable"}
        ok, is_now_on, msg_text = LightService.toggle_light()
        if ok:
            return {
                "status": "ok",
                "action": "toggle_light",
                "is_on": is_now_on,
                "message": msg_text,
            }
        return {"status": "error", "action": "toggle_light", "error": msg_text}

    return {"status": "error", "error": f"Unknown action: {action}"}

def main():
    logging.info("SUIT Kiosk Host started (PID %d)", os.getpid())
    while True:
        try:
            msg = read_message()
            if msg is None:
                break
            action = msg.get("action", "")
            response = handle_action(action)
            send_message(response)
        except Exception as e:
            logging.exception("Error in main loop: %s", e)
            try:
                send_message({"status": "error", "error": str(e)})
            except Exception:
                pass
            break
    logging.info("SUIT Kiosk Host exiting (PID %d)", os.getpid())

if __name__ == "__main__":
    main()
