#!/usr/bin/env python3
"""
ISP Sweep Progress Monitor
==========================
Queries the remote camera target and displays current progress, percentage,
ETA, and active parameters for either the empty sweep or the active dart sweep.

Usage:
    python3 tools/isp_sweep_status.py
    watch -n 5 python3 tools/isp_sweep_status.py
"""

import subprocess
import sys

TOTAL_COMBOS = 18634
REMOTE_HOST = "autodarts@192.168.2.103"
SSH_PASS = "autodarts"


def check_progress():
    # Check if dart sweep is active, otherwise fallback to empty sweep
    cmd = [
        "sshpass", "-p", SSH_PASS,
        "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=3",
        REMOTE_HOST,
        """
        if pgrep -f 'isp_sweep_darts.py' >/dev/null 2>&1; then
            echo 'MODE:DARTS'
            wc -l /home/autodarts/.local/share/suit/isp_sweep_darts/isp_sweep_darts_results.csv 2>/dev/null
            tail -n 1 /home/autodarts/.local/share/suit/isp_sweep_darts/isp_sweep_darts_results.csv 2>/dev/null
            pgrep -f 'isp_sweep_darts.py'
        else
            echo 'MODE:EMPTY'
            wc -l /home/autodarts/.local/share/suit/isp_sweep/isp_sweep_results.csv 2>/dev/null
            tail -n 1 /home/autodarts/.local/share/suit/isp_sweep/isp_sweep_results.csv 2>/dev/null
            pgrep -f 'isp_sweep.py'
        fi
        """
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
        lines = [line.strip() for line in res.stdout.strip().splitlines() if line.strip()]
        if len(lines) < 2:
            print("Sweep is not running or output not yet available.")
            return

        mode_str = lines[0]
        count_str = lines[1].split()[0]
        count = max(0, int(count_str) - 1)
        last_row = lines[2] if len(lines) > 2 else ""
        is_running = len(lines) >= 4 and lines[3].isdigit()

        pct = (count / TOTAL_COMBOS) * 100.0
        bar_len = 30
        filled = int(bar_len * (count / TOTAL_COMBOS))
        bar = "█" * filled + "░" * (bar_len - filled)

        mode_title = "DART INSERTION Ground-Truth Sweep" if "DARTS" in mode_str else "Empty Board Sweep"
        status_tag = "[RUNNING]" if is_running else "[COMPLETED / IDLE]"

        params_str = ""
        metrics_str = ""
        if last_row and "," in last_row:
            parts = last_row.split(",")
            if len(parts) >= 4:
                params_str = f"Current: Gain={parts[0]:>3}  Sharp={parts[1]:>3}  Contr={parts[2]:>3}  Gamma={parts[3]:>3}"
            if "DARTS" in mode_str and len(parts) >= 8:
                metrics_str = f"Dart SNR: {float(parts[4]):.1f}x  Peak Diff: {float(parts[5]):.0f} DN  Strong Px: {parts[7]}"

        print(f"\n{status_tag} {mode_title}")
        print(f"[{bar}] {count:>5} / {TOTAL_COMBOS} ({pct:5.1f}%)")
        if params_str:
            print(f"  └─ {params_str}")
        if metrics_str:
            print(f"  └─ {metrics_str}")

        if count > 0:
            rem = TOTAL_COMBOS - count
            est_mins = rem / 360.0
            print(f"  └─ Estimated remaining time: ~{est_mins:.1f} minutes")
        print()

    except Exception as e:
        print(f"Error checking progress: {e}", file=sys.stderr)


if __name__ == "__main__":
    check_progress()
