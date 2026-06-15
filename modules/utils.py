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
    def get_distro():
        """Detects the Linux distribution."""
        if os.path.exists("/etc/fedora-release"):
            return "fedora"
        if os.path.exists("/etc/debian_version") or os.path.exists("/etc/lsb-release"):
            return "debian"
        return "unknown"

    @staticmethod
    def sudo_cmd(shell_cmd):
        """Returns the command for root privileges."""
        if shell_cmd.startswith("sudo "):
            return shell_cmd
        return f"sudo bash -c '{shell_cmd}'"

    @staticmethod
    def check_sudo_nopasswd():
        """Checks if the current user has passwordless sudo for ALL commands."""
        try:
            # -n is non-interactive. If it fails, it means password is required.
            res = subprocess.run(["sudo", "-n", "true"], capture_output=True)
            return res.returncode == 0
        except:
            return False

    @staticmethod
    def setup_sudo_nopasswd(parent):
        """Asks the user and sets up passwordless sudo for the current user."""
        controller = getattr(parent, "controller", parent)
        l = getattr(controller, "lang", "en")
        texts = getattr(controller, "texts", {})
        def txt(k): return texts.get(k, {}).get(l, k)

        if messagebox.askyesno(txt("sudo_req_title"), txt("sudo_req_text")):
            import getpass
            user = getpass.getuser()
            rule = f"{user} ALL=(ALL) NOPASSWD: ALL"
            # We use pkexec to get a GUI password prompt if sudo fails non-interactively
            cmd = f'echo "{rule}" | sudo tee /etc/sudoers.d/{user}'
            
            # Use pkexec for a better GUI experience if available, or just regular sudo in a terminal-like way
            # But here we want to use the existing run_bash_script if possible, 
            # or a direct call that might trigger the system's password prompt.
            
            try:
                # First try pkexec which is the standard way to get a GUI prompt for root actions
                full_cmd = f'pkexec bash -c \'echo "{rule}" > /etc/sudoers.d/{user} && chmod 0440 /etc/sudoers.d/{user}\''
                res = subprocess.run(full_cmd, shell=True)
                
                if res.returncode == 0:
                    messagebox.showinfo(txt("sudo_req_title"), txt("msg_action_complete"))
                    # Restart app
                    import sys
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                else:
                    messagebox.showerror(txt("sudo_req_title"), txt("msg_error"))
            except Exception as e:
                messagebox.showerror(txt("sudo_req_title"), f"Error: {e}")

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

        # Get lang and texts safely from either a Frame (has controller) or the App itself
        controller = getattr(parent, "controller", parent)
        l = getattr(controller, "lang", "en")
        texts = getattr(controller, "texts", {})
        def txt(k): return texts.get(k, {}).get(l, k)

        ctk.CTkLabel(popup, text=txt("msg_loading"), font=("Segoe UI", 14, "bold")).pack(pady=20)
        
        progress = ctk.CTkProgressBar(popup, mode="indeterminate", width=300)
        progress.pack(pady=10, padx=40)
        progress.start()

        # Execute script
        proc = subprocess.Popen(bash_content, shell=True)

        def check_proc():
            if proc.poll() is not None:
                progress.stop()
                popup.destroy()
                if proc.returncode == 0:
                    messagebox.showinfo(title, txt("msg_action_complete"))
                else:
                    messagebox.showerror(title, f"{txt('msg_error')} (Code {proc.returncode})")
                
                if on_close:
                    on_close()
            else:
                popup.after(500, check_proc)
        
        popup.after(500, check_proc)
