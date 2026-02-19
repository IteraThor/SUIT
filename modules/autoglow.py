import tkinter as tk
import customtkinter as ctk
from tkinter import messagebox
import os
import subprocess
from modules.utils import ServiceUtils

class AutoGlowView(ctk.CTkFrame):
    def __init__(self, parent, controller):
        super().__init__(parent, fg_color="transparent")
        self.controller = controller
        self.texts = controller.texts
        self.colors = controller.colors
        self.project_dir = controller.project_dir
        self.autoglow_dir = os.path.join(self.project_dir, "AutoGlow")
        
        # Header
        self.header_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.header_frame.pack(fill="x", pady=(5, 15)) # Reduced padding
        
        self.btn_back = ctk.CTkButton(self.header_frame, text="", width=100, height=32,
                                     fg_color=self.colors["header"], text_color="white", command=controller.show_menu)
        self.btn_back.pack(side="left", padx=10)
        
        self.lbl_title = ctk.CTkLabel(self.header_frame, text="", font=("Segoe UI", 24, "bold"), text_color="white")
        self.lbl_title.pack(side="left", padx=20)

        # --- INFO CARD ---
        self.info_frame = ctk.CTkFrame(self, fg_color=self.colors["card"])
        self.info_frame.pack(fill="x", padx=20, pady=(0, 10)) # Reduced padding
        self.lbl_info = ctk.CTkLabel(self.info_frame, text="", font=("Segoe UI", 13, "bold"),
                                    text_color=self.colors["fg_dim"], wraplength=720)
        self.lbl_info.pack(fill="x", padx=20, pady=15) # Reduced padding

        # Status
        self.status_container = ctk.CTkFrame(self, fg_color=self.colors["card"])
        self.status_container.pack(fill="x", padx=20, pady=10)
        self.status_lbl = ctk.CTkLabel(self.status_container, text="", 
                                      font=("Segoe UI", 18, "bold"))
        self.status_lbl.pack(expand=True, pady=15) # Reduced padding

        # Service Controls
        self.ctrl_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.ctrl_frame.pack(fill="x", pady=15, padx=15) # Reduced padding
        
        self.btn_start = ctk.CTkButton(self.ctrl_frame, text="", height=50,
                                      fg_color=self.colors["accent"], text_color="white", 
                                      text_color_disabled="#cccccc", command=lambda: self._run_cmd("start"))
        self.btn_start.pack(side="left", expand=True, fill="x", padx=10)
        
        self.btn_stop = ctk.CTkButton(self.ctrl_frame, text="", height=50,
                                     fg_color=self.colors["header"], text_color="white", 
                                     text_color_disabled="#cccccc", command=lambda: self._run_cmd("stop"))
        self.btn_stop.pack(side="left", expand=True, fill="x", padx=10)
        
        self.btn_restart = ctk.CTkButton(self.ctrl_frame, text="", height=50,
                                        fg_color=self.colors["header"], text_color="white", 
                                        text_color_disabled="#cccccc", command=lambda: self._run_cmd("restart"))
        self.btn_restart.pack(side="left", expand=True, fill="x", padx=10)

        # Config GUI
        self.conf_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.conf_frame.pack(fill="x", pady=5, padx=30) # Reduced padding
        self.btn_config = ctk.CTkButton(self.conf_frame, text="", height=45,
                                       fg_color="#1f538d", text_color="white", 
                                       text_color_disabled="#cccccc", command=self.open_config)
        self.btn_config.pack(fill="x", pady=5) # Reduced padding
        self.lbl_config_hint = ctk.CTkLabel(self.conf_frame, text="", font=("Segoe UI", 11, "bold"), text_color=self.colors["warning"])
        self.lbl_config_hint.pack()

        # System
        self.sys_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.sys_frame.pack(fill="x", pady=5, padx=30) # Reduced padding
        self.btn_inst = ctk.CTkButton(self.sys_frame, text="", height=45,
                                     fg_color=self.colors["success"], text_color="white", 
                                     text_color_disabled="#cccccc", command=self.run_install)
        self.btn_inst.pack(fill="x", pady=5) # Reduced padding
        
        self.btn_uninst = ctk.CTkButton(self.sys_frame, text="", height=45,
                                       fg_color="#3e3e42", text_color="white", 
                                       text_color_disabled="#cccccc", hover_color=self.colors["danger"], command=self.run_uninstall)
        self.btn_uninst.pack(fill="x", pady=5) # Reduced padding

        self.update_texts()

    def _run_cmd(self, action):
        script = f"systemctl {action} autoglow"
        ServiceUtils.run_bash_script(self, script, f"AutoGlow {action}", on_close=self.update_status)

    def update_status(self):
        """Checks service status and updates UI accordingly."""
        status = ServiceUtils.check_status("autoglow")
        l = self.controller.lang
        def txt(k): return self.texts.get(k, {}).get(l, k)
        
        if status == "running":
            self.status_lbl.configure(text=txt("st_active"), text_color=self.colors["success"])
            self.btn_inst.configure(state="disabled")
            self.btn_uninst.configure(state="normal")
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")
            self.btn_config.configure(state="disabled")
        elif status == "stopped":
            self.status_lbl.configure(text=txt("st_inactive"), text_color=self.colors["danger"])
            self.btn_inst.configure(state="disabled")
            self.btn_uninst.configure(state="normal")
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self.btn_config.configure(state="normal" if os.path.exists(self.autoglow_dir) else "disabled")
        else:
            self.status_lbl.configure(text=txt("st_nofile"), text_color="gray")
            self.btn_inst.configure(state="normal")
            self.btn_uninst.configure(state="disabled")
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="disabled")
            self.btn_config.configure(state="disabled")

    def open_config(self):
        gui_script = os.path.join(self.autoglow_dir, "settings_gui.py")
        venv_python = os.path.join(self.autoglow_dir, "venv", "bin", "python3")
        python_exe = venv_python if os.path.exists(venv_python) else "python3"
        
        display = os.environ.get("DISPLAY", ":0")
        xauth = os.environ.get("XAUTHORITY", "")
        try: subprocess.run(["xhost", "+si:localuser:root"], check=False)
        except: pass

        cmd = ServiceUtils.sudo_cmd(f"env DISPLAY={display} XAUTHORITY={xauth} {python_exe} {gui_script}")
        subprocess.Popen(cmd, shell=True, cwd=self.autoglow_dir)

    def run_install(self):
        user = os.getenv("USER") or os.getlogin()
        script = f"rm -rf {self.autoglow_dir}; cd {self.project_dir}; git clone https://github.com/IteraThor/AutoGlow.git; chown -R {user}:{user} AutoGlow; cd AutoGlow; chmod +x setup.sh; ./setup.sh"
        ServiceUtils.run_bash_script(self, script, "Installation", on_close=self.update_status)

    def run_uninstall(self):
        if not messagebox.askyesno("SUIT", "Really uninstall AutoGlow?"): return
        script = f"systemctl stop autoglow; systemctl disable autoglow; rm -f /etc/systemd/system/autoglow.service; systemctl daemon-reload; rm -rf {self.autoglow_dir}"
        ServiceUtils.run_bash_script(self, script, "Deinstallation", on_close=self.update_status)

    def update_texts(self):
        """Refreshes all UI strings based on the current language."""
        l = getattr(self.controller, "lang", "en")
        def txt(k): return self.texts.get(k, {}).get(l, k)

        self.btn_back.configure(text=txt("btn_back"))
        self.lbl_title.configure(text=txt("ag_header"))
        self.lbl_info.configure(text=txt("desc_autoglow"))
        self.btn_start.configure(text=txt("btn_start"))
        self.btn_stop.configure(text=txt("btn_stop"))
        self.btn_restart.configure(text=txt("btn_restart"))
        self.btn_config.configure(text=txt("ag_config"))
        self.lbl_config_hint.configure(text=txt("ag_config_hint"))
        self.btn_inst.configure(text=txt("ag_install"))
        self.btn_uninst.configure(text=txt("btn_uninstall"))
        self.update_status()
