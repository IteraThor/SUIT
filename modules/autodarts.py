import tkinter as tk
import customtkinter as ctk
from tkinter import messagebox
import webbrowser
import subprocess
import os
from modules.utils import ServiceUtils

class AutodartsView(ctk.CTkFrame):
    def __init__(self, parent, controller):
        super().__init__(parent, fg_color="transparent")
        self.controller = controller
        self.colors = controller.colors
        
        # --- HEADER ---
        self.header_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.header_frame.pack(fill="x", pady=(10, 25))
        
        # Back Button (Cleaner icon-style)
        self.btn_back = ctk.CTkButton(self.header_frame, text="←", width=50, height=35,
                                     fg_color=self.colors["card"], 
                                     border_color=self.colors["header"],
                                     border_width=1,
                                     text_color="white", command=controller.show_menu)
        self.btn_back.pack(side="left", padx=20)
        
        # Title
        self.lbl_title = ctk.CTkLabel(self.header_frame, text="", font=("Roboto", 28, "bold"), text_color="white")
        self.lbl_title.pack(side="left", padx=10)

        # --- INFO CARD (Minimalist) ---
        self.info_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.info_frame.pack(fill="x", padx=20, pady=(0, 20))
        self.lbl_info = ctk.CTkLabel(self.info_frame, text="", font=("Roboto", 14),
                                    text_color=self.colors["fg_dim"], wraplength=720, justify="left")
        self.lbl_info.pack(fill="x", padx=20)

        # --- STATUS DISPLAY ---
        self.status_container = ctk.CTkFrame(self, fg_color=self.colors["card"], corner_radius=12,
                                            border_color=self.colors["header"], border_width=1)
        self.status_container.pack(fill="x", padx=40, pady=10)
        
        self.status_lbl = ctk.CTkLabel(self.status_container, text="", 
                                      font=("Roboto", 20, "bold"))
        self.status_lbl.pack(expand=True, pady=25)

        # --- CONTROLS (Start/Stop) ---
        self.ctrl_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.ctrl_frame.pack(fill="x", pady=25, padx=30)
        
        self.btn_start = ctk.CTkButton(self.ctrl_frame, text="", height=55, corner_radius=10,
                                      text_color="white", text_color_disabled="white",
                                      font=("Roboto", 15, "bold"),
                                      command=lambda: self._run_cmd("start"))
        self.btn_start.pack(side="left", expand=True, fill="x", padx=10)
        
        self.btn_stop = ctk.CTkButton(self.ctrl_frame, text="", height=55, corner_radius=10,
                                     text_color="white", text_color_disabled="white",
                                     font=("Roboto", 15, "bold"),
                                     command=lambda: self._run_cmd("stop"))
        self.btn_stop.pack(side="left", expand=True, fill="x", padx=10)
        
        self.btn_restart = ctk.CTkButton(self.ctrl_frame, text="", height=55, corner_radius=10,
                                        text_color="white", text_color_disabled="white",
                                        font=("Roboto", 15, "bold"),
                                        command=lambda: self._run_cmd("restart"))
        self.btn_restart.pack(side="left", expand=True, fill="x", padx=10)

        # --- INSTALLATION ---
        self.sys_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.sys_frame.pack(fill="x", pady=10, padx=40)
        
        self.btn_inst = ctk.CTkButton(self.sys_frame, text="", height=48, corner_radius=10,
                                     text_color="white", text_color_disabled="white",
                                     font=("Roboto", 14, "bold"),
                                     command=self.do_install)
        self.btn_inst.pack(fill="x", pady=8)
        
        self.btn_uninst = ctk.CTkButton(self.sys_frame, text="", height=48, corner_radius=10,
                                       text_color="white", text_color_disabled="white",
                                       font=("Roboto", 14, "bold"),
                                       command=self.do_uninstall)
        self.btn_uninst.pack(fill="x", pady=8)

        # --- LINKS ---
        self.link_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.link_frame.pack(fill="x", pady=20, padx=20) # Reduced padding
        
        self.btn_play = ctk.CTkButton(self.link_frame, text="🎯 Play (Web)", height=55, corner_radius=12,
                                     fg_color=self.colors["success"], text_color="white", font=("Roboto", 16, "bold"),
                                     hover_color="#1a8a38",
                                     command=lambda: webbrowser.open("https://play.autodarts.io/"))
        self.btn_play.pack(side="left", expand=True, fill="x", padx=10)
        
        self.btn_conf_link = ctk.CTkButton(self.link_frame, text="⚙️ Config", height=55, corner_radius=12,
                                          text_color="white", text_color_disabled="white",
                                          font=("Roboto", 16, "bold"),
                                          command=lambda: webbrowser.open("http://localhost:3180/config"))
        self.btn_conf_link.pack(side="left", expand=True, fill="x", padx=10)

        self.update_texts()

    def _run_cmd(self, action):
        script = ServiceUtils.sudo_cmd(f"systemctl {action} autodarts")
        ServiceUtils.run_bash_script(self, script, f"Autodarts {action}", on_close=self.update_status)

    def update_status(self):
        """Checks the service status and updates UI colors and states."""
        l = getattr(self.controller, "lang", "en")
        texts = getattr(self.controller, "texts", {})
        def txt(k): return texts.get(k, {}).get(l, k)

        service_file = "/etc/systemd/system/autodarts.service"
        grey = self.colors["header"]
        red = self.colors["danger"]
        blue = self.colors["accent"]
        green = self.colors["success"]
        
        if not os.path.exists(service_file):
            self.status_lbl.configure(text=txt("st_nofile"), text_color=self.colors["fg_dim"])
            # Install is clickable
            self.btn_inst.configure(state="normal", fg_color=green)
            # Others are grey/disabled
            self.btn_uninst.configure(state="disabled", fg_color=grey)
            self.btn_start.configure(state="disabled", fg_color=grey)
            self.btn_stop.configure(state="disabled", fg_color=grey)
            self.btn_restart.configure(state="disabled", fg_color=grey)
            # Config is grey when not installed
            self.btn_conf_link.configure(state="disabled", fg_color=grey)
            return

        status = ServiceUtils.check_status("autodarts")
        self.btn_conf_link.configure(state="normal", fg_color=blue, hover_color="#2563eb")
        
        if status == "running":
            self.status_lbl.configure(text=txt("st_active"), text_color=green)
            self.btn_inst.configure(state="disabled", fg_color=grey)
            self.btn_uninst.configure(state="normal", fg_color=red, hover_color="#b91c1c")
            self.btn_start.configure(state="disabled", fg_color=grey)
            self.btn_stop.configure(state="normal", fg_color=red, hover_color="#b91c1c")
            self.btn_restart.configure(state="normal", fg_color=blue, hover_color="#2563eb")
        else: 
            self.status_lbl.configure(text=txt("st_inactive"), text_color=red)
            self.btn_inst.configure(state="disabled", fg_color=grey)
            self.btn_uninst.configure(state="normal", fg_color=red, hover_color="#b91c1c")
            self.btn_start.configure(state="normal", fg_color=blue, hover_color="#2563eb")
            self.btn_stop.configure(state="disabled", fg_color=grey)
            self.btn_restart.configure(state="normal", fg_color=blue, hover_color="#2563eb")

    def do_install(self):
        distro = ServiceUtils.get_distro()
        import getpass
        user = getpass.getuser()
        
        # The installer handles sudo internally where needed. 
        # We must run it as a normal user so it installs into the correct home directory.
        base_cmd = "bash <(curl -sL get.autodarts.io)"
        
        if distro == "fedora":
            # Fedora fixes:
            # 1. Installer uses 'adduser' which fails on Fedora -> we fix with 'usermod'
            # 2. SELinux: binaries in home dirs often need specific context to be executed by systemd
            # 3. Group changes/SELinux fixes require a service restart
            cmd = (
                f"{base_cmd}; "
                f"sudo usermod -aG video {user}; "
                f"sudo chcon -h -t bin_t /home/{user}/.local/opt/autodarts/autodarts 2>/dev/null || true; "
                f"sudo systemctl restart autodarts"
            )
        else:
            cmd = base_cmd

        ServiceUtils.run_bash_script(self, cmd, "Installation", on_close=self.update_status)

    def do_uninstall(self):
        if not messagebox.askyesno("SUIT", "Really uninstall Autodarts?"): return
        script = ServiceUtils.sudo_cmd("systemctl stop autodarts; systemctl disable autodarts; rm -f /etc/systemd/system/autodarts.service; systemctl daemon-reload; rm -rf /usr/bin/autodarts /opt/autodarts")
        ServiceUtils.run_bash_script(self, script, "Deinstallation", on_close=self.update_status)

    def update_texts(self):
        """Refreshes all UI strings based on the current language."""
        l = getattr(self.controller, "lang", "en")
        texts = getattr(self.controller, "texts", {})
        def txt(k): return texts.get(k, {}).get(l, k)

        # self.btn_back.configure(text=txt("btn_back")) # Forced Icon
        self.lbl_title.configure(text=txt("ad_header"))
        self.lbl_info.configure(text=txt("desc_autodarts"))
        self.btn_start.configure(text=txt("btn_start"))
        self.btn_stop.configure(text=txt("btn_stop"))
        self.btn_restart.configure(text=txt("btn_restart"))
        self.btn_inst.configure(text=txt("btn_install"))
        self.btn_uninst.configure(text=txt("btn_uninstall"))
        self.update_status()
