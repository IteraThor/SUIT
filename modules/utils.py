import subprocess
import os
import time
import customtkinter as ctk
from tkinter import messagebox
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
        """Executes a script in the background and shows a modern info window."""
        popup = ctk.CTkToplevel(parent)
        popup.title(title)
        popup.geometry("400x180")
        popup.attributes("-topmost", True)
        
        # Center the popup relative to parent
        parent_x = parent.winfo_rootx()
        parent_y = parent.winfo_rooty()
        parent_w = parent.winfo_width()
        parent_h = parent.winfo_height()
        popup.geometry(f"+{parent_x + (parent_w // 2) - 200}+{parent_y + (parent_h // 2) - 90}")

        l = getattr(parent.controller, "lang", "en")
        texts = getattr(parent.controller, "texts", {})
        def txt(k): return texts.get(k, {}).get(l, k)

        ctk.CTkLabel(popup, text=txt("msg_loading"), font=("Segoe UI", 14, "bold")).pack(pady=20)
        
        progress = ctk.CTkProgressBar(popup, mode="indeterminate", width=300)
        progress.pack(pady=10, padx=40)
        progress.start()

        # Execute script
        cmd = ServiceUtils.sudo_cmd(bash_content)
        proc = subprocess.Popen(cmd, shell=True)

        def check_proc():
            if proc.poll() is not None:
                progress.stop()
                popup.destroy()
                messagebox.showinfo(title, txt("msg_action_complete"))
                if on_close:
                    on_close()
            else:
                popup.after(500, check_proc)
        
        popup.after(500, check_proc)
