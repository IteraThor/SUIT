import subprocess
import os
import time
from tkinter import Toplevel, ttk, messagebox
from shutil import which

class ServiceUtils:
    @staticmethod
    def check_status(service_name):
        """Checks the status based on file existence and systemctl."""
        service_path = f"/etc/systemd/system/{service_name}.service"
        if not os.path.exists(service_path):
            return "nofile"
        
        try:
            res = subprocess.run(["systemctl", "is-active", service_name], capture_output=True, text=True)
            return "running" if res.stdout.strip() == "active" else "stopped"
        except:
            return "stopped"

    @staticmethod
    def sudo_cmd(shell_cmd):
        """Returns the command for root privileges."""
        if which("pkexec"):
            return f"pkexec bash -c '{shell_cmd}'"
        return f"sudo bash -c '{shell_cmd}'"

    @staticmethod
    def run_bash_script(parent, bash_content, title="SUIT", on_close=None):
        """Executes a script in the background and shows an info window."""
        popup = Toplevel(parent)
        popup.title(title)
        popup.geometry("300x120")
        popup.configure(bg="#252526")
        popup.transient(parent)
        popup.grab_set()
        
        ttk.Label(popup, text="Action in progress...", font=("Segoe UI", 10, "bold"), background="#252526").pack(pady=15)
        progress = ttk.Progressbar(popup, mode="indeterminate")
        progress.pack(padx=30, fill="x")
        progress.start()

        # Execute script
        cmd = ServiceUtils.sudo_cmd(bash_content)
        proc = subprocess.Popen(cmd, shell=True)

        def check_proc():
            if proc.poll() is not None:
                popup.destroy()
                messagebox.showinfo(title, "Action completed.")
                if on_close:
                    on_close()
            else:
                popup.after(500, check_proc)
        
        popup.after(500, check_proc)
