import tkinter as tk
import customtkinter as ctk
from tkinter import messagebox
import os
import subprocess
import serial.tools.list_ports
from modules.utils import ServiceUtils

class AutoGlowView(ctk.CTkFrame):
    # Deployment settings
    AUTOGLOW_REPO = "https://github.com/IteraThor/AutoGlow.git"
    SERVICE_NAME = "autoglow"

    def __init__(self, parent, controller):
        super().__init__(parent, fg_color="transparent")
        self.controller = controller
        self.texts = controller.texts
        self.colors = controller.colors
        self.project_dir = controller.project_dir
        
        # Dynamic paths
        self.user_home = os.path.expanduser("~")
        self.autoglow_dir = os.path.join(self.user_home, "AutoGlow")
        
        # Header
        self.header_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.header_frame.pack(fill="x", pady=(5, 10))
        
        self.btn_back = ctk.CTkButton(self.header_frame, text="", width=100, height=32,
                                     fg_color=self.colors["header"], text_color="white", command=controller.show_menu)
        self.btn_back.pack(side="left", padx=10)
        
        self.lbl_title = ctk.CTkLabel(self.header_frame, text="", font=("Segoe UI", 24, "bold"), text_color="white")
        self.lbl_title.pack(side="left", padx=20)

        # --- HARDWARE & SERVICE STATUS CARD ---
        self.hw_frame = ctk.CTkFrame(self, fg_color=self.colors["card"])
        self.hw_frame.pack(fill="x", padx=20, pady=(0, 10))
        
        # Hardware Row
        hw_row = ctk.CTkFrame(self.hw_frame, fg_color="transparent")
        hw_row.pack(fill="x", padx=20, pady=(10, 5))
        
        self.lbl_hw_title = ctk.CTkLabel(hw_row, text="Hardware:", font=("Segoe UI", 13, "bold"), text_color=self.colors["fg_dim"])
        self.lbl_hw_title.pack(side="left")
        
        self.lbl_hw_status = ctk.CTkLabel(hw_row, text="Scanning...", font=("Segoe UI", 13, "bold"))
        self.lbl_hw_status.pack(side="left", padx=10)
        
        self.btn_hw_test = ctk.CTkButton(hw_row, text="TEST", width=60, height=24, fg_color=self.colors["header"], text_color="white", command=self.test_animation)
        self.btn_hw_test.pack(side="right", padx=(5, 0))

        # Websocket Row
        ws_row = ctk.CTkFrame(self.hw_frame, fg_color="transparent")
        ws_row.pack(fill="x", padx=20, pady=(5, 10))
        
        self.lbl_ws_title = ctk.CTkLabel(ws_row, text="Autodarts Connection:", font=("Segoe UI", 13, "bold"), text_color=self.colors["fg_dim"])
        self.lbl_ws_title.pack(side="left")
        
        self.lbl_ws_status = ctk.CTkLabel(ws_row, text="Checking...", font=("Segoe UI", 13, "bold"))
        self.lbl_ws_status.pack(side="left", padx=10)

        # Status
        self.status_container = ctk.CTkFrame(self, fg_color=self.colors["card"])
        self.status_container.pack(fill="x", padx=20, pady=10)
        self.status_lbl = ctk.CTkLabel(self.status_container, text="", 
                                      font=("Segoe UI", 18, "bold"))
        self.status_lbl.pack(expand=True, pady=15)

        # Service Controls
        self.ctrl_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.ctrl_frame.pack(fill="x", pady=10, padx=15)
        
        self.btn_start = ctk.CTkButton(self.ctrl_frame, text="", height=50,
                                      fg_color=self.colors["accent"], text_color="white", 
                                      command=lambda: self._run_cmd("start"))
        self.btn_start.pack(side="left", expand=True, fill="x", padx=10)
        
        self.btn_stop = ctk.CTkButton(self.ctrl_frame, text="", height=50,
                                     fg_color=self.colors["header"], text_color="white", 
                                     command=lambda: self._run_cmd("stop"))
        self.btn_stop.pack(side="left", expand=True, fill="x", padx=10)
        
        self.btn_restart = ctk.CTkButton(self.ctrl_frame, text="", height=50,
                                        fg_color=self.colors["header"], text_color="white", 
                                        command=lambda: self._run_cmd("restart"))
        self.btn_restart.pack(side="left", expand=True, fill="x", padx=10)

        # Config GUI
        self.conf_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.conf_frame.pack(fill="x", pady=5, padx=30)
        self.btn_config = ctk.CTkButton(self.conf_frame, text="", height=45,
                                       fg_color="#1f538d", text_color="white", 
                                       command=self.open_config)
        self.btn_config.pack(fill="x", pady=5)
        self.lbl_config_hint = ctk.CTkLabel(self.conf_frame, text="", font=("Segoe UI", 11, "bold"), text_color=self.colors["warning"])
        self.lbl_config_hint.pack()

        # System
        self.sys_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.sys_frame.pack(fill="x", pady=5, padx=30)
        self.btn_inst = ctk.CTkButton(self.sys_frame, text="", height=45,
                                     fg_color=self.colors["success"], text_color="white", 
                                     command=self.run_install)
        self.btn_inst.pack(fill="x", pady=5)
        
        self.btn_uninst = ctk.CTkButton(self.sys_frame, text="", height=45,
                                       fg_color="#3e3e42", text_color="white", 
                                       hover_color=self.colors["danger"], command=self.run_uninstall)
        self.btn_uninst.pack(fill="x", pady=5)

        self.update_texts()

    def test_animation(self):
        """Sends a test rainbow command for 3 seconds, then reverts to Throw."""
        port = self.get_esp32_port()
        if not port:
            messagebox.showwarning("AutoGlow", "No ESP32 detected!")
            return
        
        status = ServiceUtils.check_status(self.SERVICE_NAME)
        if status == "running":
            messagebox.showwarning("AutoGlow", "Please stop the service before testing hardware directly.")
            return

        import json
        import serial
        import time
        try:
            with serial.Serial(port, 115200, timeout=1) as ser:
                time.sleep(1.5) # Settle
                # 1. Start Rainbow
                anim = {"on": True, "bri": 255, "seg": {"fx": 9, "sx": 128, "ix": 128}}
                ser.write((json.dumps(anim) + '\n').encode())
                
                # 2. Wait 3 seconds
                time.sleep(3)
                
                # 3. Transition to 'Throw' (Green)
                throw = {"on": True, "bri": 255, "seg": {"fx": 0, "col": [[0, 255, 0]]}}
                ser.write((json.dumps(throw) + '\n').encode())
        except Exception as e:
            messagebox.showerror("Error", f"Could not connect to {port}: {e}")

    def check_ws_connection(self):
        """Checks if port 3180 (Autodarts) is reachable."""
        import socket
        try:
            with socket.create_connection(("127.0.0.1", 3180), timeout=0.5):
                return True
        except:
            return False

    def get_esp32_port(self):
        """Attempts to find the ESP32 serial port."""
        KNOWN_VID_PIDS = [(0x10C4, 0xEA60), (0x1A86, 0x7523), (0x0403, 0x6001), (0x303A, 0x1001)]
        ports = serial.tools.list_ports.comports()
        for port in ports:
            if (port.vid, port.pid) in KNOWN_VID_PIDS:
                return port.device
        return None

    def _run_cmd(self, action):
        script = f"systemctl {action} {self.SERVICE_NAME}"
        ServiceUtils.run_bash_script(self, script, f"AutoGlow {action}", on_close=self.update_status)

    def update_status(self):
        """Checks service and hardware status and updates UI."""
        status = ServiceUtils.check_status(self.SERVICE_NAME)
        l = self.controller.lang
        def txt(k): return self.texts.get(k, {}).get(l, k)
        
        is_installed = status != "nofile"
        is_running = status == "running"

        # Update Service Status
        if is_running:
            self.status_lbl.configure(text=txt("st_active"), text_color=self.colors["success"])
        elif is_installed:
            self.status_lbl.configure(text=txt("st_inactive"), text_color=self.colors["danger"])
        else:
            self.status_lbl.configure(text=txt("st_nofile"), text_color="gray")

        # Update Hardware Status
        esp_port = self.get_esp32_port()
        if esp_port:
            self.lbl_hw_status.configure(text=f"{txt('ag_hw_found')} ({esp_port})", text_color=self.colors["success"])
        else:
            self.lbl_hw_status.configure(text=txt("ag_hw_missing"), text_color=self.colors["warning"])

        # Update Websocket Status
        if self.check_ws_connection():
            self.lbl_ws_status.configure(text=txt("ag_ws_connected"), text_color=self.colors["success"])
        else:
            self.lbl_ws_status.configure(text=txt("ag_ws_failed"), text_color=self.colors["warning"])

        self.btn_inst.configure(state="disabled" if is_installed else "normal")
        self.btn_uninst.configure(state="normal" if is_installed else "disabled")
        self.btn_start.configure(state="normal" if is_installed and not is_running else "disabled")
        self.btn_stop.configure(state="normal" if is_running else "disabled")
        self.btn_restart.configure(state="normal" if is_installed else "disabled")
        
        # Config available whenever installed
        self.btn_config.configure(state="normal" if is_installed else "disabled")

    def open_config(self):
        """Stops the service (if running) and launches the standalone config GUI."""
        gui_script = os.path.join(self.autoglow_dir, "settings_gui.py")
        venv_python = os.path.join(self.autoglow_dir, "venv", "bin", "python3")
        python_exe = venv_python if os.path.exists(venv_python) else "python3"
        
        if not os.path.exists(gui_script):
            messagebox.showerror("Error", f"Config script not found: {gui_script}")
            return

        display = os.environ.get("DISPLAY", ":0")
        xauth = os.environ.get("XAUTHORITY", "")
        
        # Ensure we have access to the X server if running as sudo/root
        try: subprocess.run(["xhost", "+si:localuser:root"], check=False)
        except: pass

        # Combine stopping the service and opening the GUI into one sudo command
        # to minimize password prompts.
        full_cmd = f"systemctl stop {self.SERVICE_NAME} && env DISPLAY={display} XAUTHORITY={xauth} {python_exe} {gui_script}"
        cmd = ServiceUtils.sudo_cmd(full_cmd)
        
        subprocess.Popen(cmd, shell=True, cwd=self.autoglow_dir)
        
        # Give it a second and refresh status
        self.after(1000, self.update_status)

    def run_install(self):
        """Clones the repository and runs the setup script."""
        user = os.getenv("USER") or os.getlogin()
        # Using && for everything to ensure the first failure stops the chain
        # and returns a non-zero exit code.
        script = (
            f"rm -rf {self.autoglow_dir} && "
            f"cd {self.user_home} && "
            f"git clone {self.AUTOGLOW_REPO} && "
            f"chown -R {user}:{user} {self.autoglow_dir} && "
            f"cd {self.autoglow_dir} && "
            f"chmod +x setup.sh && "
            f"sudo bash setup.sh"
        )
        ServiceUtils.run_bash_script(self, script, "Installation", on_close=self.update_status)

    def run_uninstall(self):
        """Stops the service and removes all files."""
        if not messagebox.askyesno("SUIT", "Really uninstall AutoGlow?"):
            return
            
        script = (
            f"systemctl stop {self.SERVICE_NAME} || true && "
            f"systemctl disable {self.SERVICE_NAME} || true && "
            f"rm -f /etc/systemd/system/{self.SERVICE_NAME}.service && "
            f"systemctl daemon-reload && "
            f"rm -rf {self.autoglow_dir}"
        )
        ServiceUtils.run_bash_script(self, script, "Deinstallation", on_close=self.update_status)

    def update_texts(self):
        """Refreshes all UI strings based on the current language."""
        l = getattr(self.controller, "lang", "en")
        def txt(k): return self.texts.get(k, {}).get(l, k)

        self.btn_back.configure(text=txt("btn_back"))
        self.lbl_title.configure(text=txt("ag_header"))
        self.lbl_hw_title.configure(text=txt("ag_hw_title"))
        self.lbl_ws_title.configure(text=txt("ag_ws_title"))
        self.btn_start.configure(text=txt("btn_start"))
        self.btn_stop.configure(text=txt("btn_stop"))
        self.btn_restart.configure(text=txt("btn_restart"))
        self.btn_config.configure(text=txt("ag_config"))
        self.lbl_config_hint.configure(text=txt("ag_config_hint"))
        self.btn_inst.configure(text=txt("ag_install"))
        self.btn_uninst.configure(text=txt("btn_uninstall"))
        self.update_status()
