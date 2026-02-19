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
        self.header_frame.pack(fill="x", pady=(5, 15)) # Reduced padding
        
        # Back Button
        self.btn_back = ctk.CTkButton(self.header_frame, text="", width=100, height=32,
                                     fg_color=self.colors["header"], text_color="white", command=controller.show_menu)
        self.btn_back.pack(side="left", padx=10)
        
        # Title
        self.lbl_title = ctk.CTkLabel(self.header_frame, text="", font=("Segoe UI", 24, "bold"), text_color="white")
        self.lbl_title.pack(side="left", padx=20)

        # --- INFO CARD ---
        self.info_frame = ctk.CTkFrame(self, fg_color=self.colors["card"])
        self.info_frame.pack(fill="x", padx=20, pady=(0, 10)) # Reduced padding
        self.lbl_info = ctk.CTkLabel(self.info_frame, text="", font=("Segoe UI", 13, "bold"),
                                    text_color=self.colors["fg_dim"], wraplength=720)
        self.lbl_info.pack(fill="x", padx=20, pady=15) # Reduced padding

        # --- STATUS DISPLAY ---
        self.status_container = ctk.CTkFrame(self, fg_color=self.colors["card"])
        self.status_container.pack(fill="x", padx=20, pady=10)
        
        self.status_lbl = ctk.CTkLabel(self.status_container, text="", 
                                      font=("Segoe UI", 18, "bold"))
        self.status_lbl.pack(expand=True, pady=15) # Reduced padding

        # --- CONTROLS (Start/Stop) ---
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

        # --- INSTALLATION ---
        self.sys_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.sys_frame.pack(fill="x", pady=5, padx=30) # Reduced padding
        
        self.btn_inst = ctk.CTkButton(self.sys_frame, text="", height=45,
                                     fg_color=self.colors["success"], text_color="white", 
                                     text_color_disabled="#cccccc", command=self.do_install)
        self.btn_inst.pack(fill="x", pady=5) # Reduced padding
        
        self.btn_uninst = ctk.CTkButton(self.sys_frame, text="", height=45,
                                       fg_color="#3e3e42", text_color="white", 
                                       text_color_disabled="#cccccc", hover_color=self.colors["danger"], command=self.do_uninstall)
        self.btn_uninst.pack(fill="x", pady=5) # Reduced padding

        # --- LINKS ---
        self.link_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.link_frame.pack(fill="x", pady=20, padx=20) # Reduced padding
        
        self.btn_play = ctk.CTkButton(self.link_frame, text="🎯 Play (Web)", height=55,
                                     fg_color="#28a745", text_color="white", font=("Segoe UI", 14, "bold"),
                                     command=lambda: webbrowser.open("https://play.autodarts.io/"))
        self.btn_play.pack(side="left", expand=True, fill="x", padx=10)
        
        self.btn_conf_link = ctk.CTkButton(self.link_frame, text="⚙️ Config", height=55,
                                          fg_color="#1f538d", text_color="white", font=("Segoe UI", 14, "bold"),
                                          command=lambda: webbrowser.open("http://localhost:3180/config"))
        self.btn_conf_link.pack(side="left", expand=True, fill="x", padx=10)

        self.update_texts()

    def _run_cmd(self, action):
        script = f"systemctl {action} autodarts"
        ServiceUtils.run_bash_script(self, script, f"Autodarts {action}", on_close=self.update_status)

    def update_status(self):
        """Checks the service status and updates the UI accordingly."""
        l = getattr(self.controller, "lang", "en")
        texts = getattr(self.controller, "texts", {})
        def txt(k): return texts.get(k, {}).get(l, k)

        service_file = "/etc/systemd/system/autodarts.service"
        
        if not os.path.exists(service_file):
            self.status_lbl.configure(text=txt("st_nofile"), text_color="gray")
            self.btn_inst.configure(state="normal")
            self.btn_uninst.configure(state="disabled")
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="disabled")
            self.btn_restart.configure(state="disabled")
            return

        status = ServiceUtils.check_status("autodarts")
        
        if status == "running":
            self.status_lbl.configure(text=txt("st_active"), text_color=self.colors["success"])
            self.btn_inst.configure(state="disabled")
            self.btn_uninst.configure(state="normal")
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")
            self.btn_restart.configure(state="normal")
        else: 
            self.status_lbl.configure(text=txt("st_inactive"), text_color=self.colors["danger"])
            self.btn_inst.configure(state="disabled")
            self.btn_uninst.configure(state="normal")
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self.btn_restart.configure(state="normal")

    def do_install(self):
        cmd = "bash <(curl -sL get.autodarts.io)"
        ServiceUtils.run_bash_script(self, cmd, "Installation", on_close=self.update_status)

    def do_uninstall(self):
        if not messagebox.askyesno("SUIT", "Really uninstall Autodarts?"): return
        script = "systemctl stop autodarts; systemctl disable autodarts; rm -f /etc/systemd/system/autodarts.service; systemctl daemon-reload; rm -rf /usr/bin/autodarts /opt/autodarts"
        ServiceUtils.run_bash_script(self, script, "Deinstallation", on_close=self.update_status)

    def update_texts(self):
        """Refreshes all UI strings based on the current language."""
        l = getattr(self.controller, "lang", "en")
        texts = getattr(self.controller, "texts", {})
        def txt(k): return texts.get(k, {}).get(l, k)

        self.btn_back.configure(text=txt("btn_back"))
        self.lbl_title.configure(text=txt("ad_header"))
        self.lbl_info.configure(text=txt("desc_autodarts"))
        self.btn_start.configure(text=txt("btn_start"))
        self.btn_stop.configure(text=txt("btn_stop"))
        self.btn_restart.configure(text=txt("btn_restart"))
        self.btn_inst.configure(text=txt("btn_install"))
        self.btn_uninst.configure(text=txt("btn_uninstall"))
        self.update_status()
