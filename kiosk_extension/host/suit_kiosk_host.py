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
