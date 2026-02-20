import tkinter as tk
import customtkinter as ctk
from tkinter import messagebox
import os
import subprocess
import threading
import json
import time
from datetime import datetime
from modules.utils import ServiceUtils

class KioskView(ctk.CTkFrame):
    def __init__(self, parent, controller):
        super().__init__(parent, fg_color="transparent")
        self.controller = controller
        self.colors = controller.colors
        self.project_dir = controller.project_dir
        
        # Paths
        self.user_home = os.path.expanduser("~")
        self.autostart_dir = os.path.join(self.user_home, ".config/autostart")
        self.ff_desktop = os.path.join(self.user_home, ".config/autostart/suit-firefox.desktop")
        self.ch_desktop = os.path.join(self.user_home, ".config/autostart/suit-chromium.desktop")
        
        self.killswitch_file = os.path.join(self.project_dir, "scripts/killswitch.py")
        self.ks_service_file = "/etc/systemd/system/suit-killswitch.service"
        self.config_file = os.path.join(self.user_home, ".suit_killswitch_config")
        
        # --- HEADER ---
        self.header = ctk.CTkFrame(self, fg_color="transparent")
        self.header.pack(fill="x", pady=(5, 15))
        
        self.btn_back = ctk.CTkButton(self.header, text="", width=100, height=32,
                                     fg_color=self.colors["header"], text_color="white", command=controller.show_menu)
        self.btn_back.pack(side="left", padx=10)
        self.lbl_title = ctk.CTkLabel(self.header, text="", font=("Segoe UI", 24, "bold"), text_color="white")
        self.lbl_title.pack(side="left", padx=10)

        # --- INFO CARD ---
        self.info_frame = ctk.CTkFrame(self, fg_color=self.colors["card"])
        self.info_frame.pack(fill="x", padx=20, pady=(0, 10))
        self.lbl_info = ctk.CTkLabel(self.info_frame, text="", font=("Segoe UI", 13, "bold"),
                                    text_color=self.colors["fg_dim"], wraplength=760, justify="left")
        self.lbl_info.pack(fill="both", padx=20, pady=15)

        # --- URL ENTRY ---
        self.url_container = ctk.CTkFrame(self, fg_color="transparent")
        self.url_container.pack(fill="x", pady=(10, 15), padx=40)
        self.lbl_url = ctk.CTkLabel(self.url_container, text="", font=("Segoe UI", 12, "bold"), text_color="white")
        self.lbl_url.pack(anchor="w", padx=5)
        self.url_ent = ctk.CTkEntry(self.url_container, font=("Segoe UI", 16), height=45, fg_color="#1a1a1a", text_color="white")
        self.url_ent.insert(0, "https://play.autodarts.io/")
        self.url_ent.pack(fill="x", pady=5)

        # --- BROWSER SECTION ---
        self.main_browser_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.main_browser_frame.pack(fill="x", padx=40)

        # CHROMIUM BOX
        self.ch_box = ctk.CTkFrame(self.main_browser_frame, fg_color=self.colors["card"], border_width=1, border_color="#444444")
        self.ch_box.pack(fill="x", pady=(0, 10))
        self.lbl_ch_title = ctk.CTkLabel(self.ch_box, text="", font=("Segoe UI", 14, "bold"), text_color="#3b8ed0")
        self.lbl_ch_title.pack(pady=(8, 2))
        
        ch_btn_frame = ctk.CTkFrame(self.ch_box, fg_color="transparent")
        ch_btn_frame.pack(fill="x", padx=15, pady=(5, 12))

        self.btn_auto_ch = ctk.CTkButton(ch_btn_frame, text="", height=45,
                                        fg_color=self.colors["header"], text_color="white", 
                                        text_color_disabled="#cccccc", command=lambda: self.toggle_autostart("Chromium"))
        self.btn_auto_ch.pack(side="left", expand=True, fill="x", padx=5)
        
        self.btn_now_ch = ctk.CTkButton(ch_btn_frame, text="", height=45,
                                       fg_color=self.colors["success"], text_color="white", font=("Segoe UI", 12, "bold"), 
                                       command=lambda: self.launch_now("Chromium"))
        self.btn_now_ch.pack(side="left", expand=True, fill="x", padx=5)

        # FIREFOX BOX
        self.ff_box = ctk.CTkFrame(self.main_browser_frame, fg_color=self.colors["card"], border_width=1, border_color="#444444")
        self.ff_box.pack(fill="x", pady=5)
        self.lbl_ff_title = ctk.CTkLabel(self.ff_box, text="", font=("Segoe UI", 14, "bold"), text_color="white")
        self.lbl_ff_title.pack(pady=(8, 2))

        ff_btn_frame = ctk.CTkFrame(self.ff_box, fg_color="transparent")
        ff_btn_frame.pack(fill="x", padx=15, pady=(5, 12))

        self.btn_auto_ff = ctk.CTkButton(ff_btn_frame, text="", height=45,
                                        fg_color=self.colors["header"], text_color="white", 
                                        text_color_disabled="#cccccc", command=lambda: self.toggle_autostart("Firefox"))
        self.btn_auto_ff.pack(side="left", expand=True, fill="x", padx=5)
        
        self.btn_now_ff = ctk.CTkButton(ff_btn_frame, text="", height=45,
                                       fg_color=self.colors["success"], text_color="white", font=("Segoe UI", 12, "bold"),
                                       command=lambda: self.launch_now("Firefox"))
        self.btn_now_ff.pack(side="left", expand=True, fill="x", padx=5)

        # --- ON-SCREEN KEYBOARD (OSK) ---
        self.osk_container = ctk.CTkFrame(self, fg_color=self.colors["card"], height=50, border_width=1, border_color="#444444")
        self.osk_container.pack(fill="x", padx=40, pady=(10, 5))
        self.osk_container.pack_propagate(False)
        
        self.lbl_osk_title = ctk.CTkLabel(self.osk_container, text="", font=("Segoe UI", 12, "bold"), text_color="white")
        self.lbl_osk_title.pack(side="left", padx=20)

        self.btn_toggle_osk = ctk.CTkButton(self.osk_container, text="", width=120, height=32, font=("Segoe UI", 11, "bold"), 
                                          text_color="white", command=self.toggle_osk)
        self.btn_toggle_osk.pack(side="right", padx=15)

        # --- KILL-SWITCH ---
        self.ks_container = ctk.CTkFrame(self, fg_color=self.colors["card"], border_width=1, border_color="#444444")
        self.ks_container.pack(fill="x", padx=40, pady=(15, 5))
        
        self.lbl_ks_title = ctk.CTkLabel(self.ks_container, text="", font=("Segoe UI", 12, "bold"), text_color="white")
        self.lbl_ks_title.pack(pady=(8, 2))

        ks_inner = ctk.CTkFrame(self.ks_container, fg_color="transparent")
        ks_inner.pack(fill="x", padx=15, pady=5)

        self.btn_learn = ctk.CTkButton(ks_inner, text="", height=40,
                                      fg_color=self.colors["header"], text_color="white", command=self.start_learning_wrapper)
        self.btn_learn.pack(side="left", expand=True, fill="x", padx=(0, 10))

        self.info_box = ctk.CTkFrame(ks_inner, fg_color="#000000", height=40)
        self.info_box.pack(side="left", expand=True, fill="both")

        self.key_label = ctk.CTkLabel(self.info_box, text="", font=("Segoe UI", 11, "bold"), text_color="#cccccc")
        self.key_label.pack(expand=True)

        self.btn_toggle_ks = ctk.CTkButton(self.ks_container, text="", height=50, font=("Segoe UI", 13, "bold"), 
                                          text_color="white", text_color_disabled="#cccccc", command=self.toggle_ks_service)
        self.btn_toggle_ks.pack(fill="x", padx=15, pady=(10, 15))

        self.update_texts()

    def toggle_autostart(self, browser):
        target_file = self.ff_desktop if browser == "Firefox" else self.ch_desktop
        other_file = self.ch_desktop if browser == "Firefox" else self.ff_desktop
        if os.path.exists(target_file):
            os.remove(target_file)
        else:
            if os.path.exists(other_file): os.remove(other_file)
            if not os.path.exists(self.autostart_dir): os.makedirs(self.autostart_dir, exist_ok=True)
            url = self.url_ent.get().strip()
            browser_cmd = f"chromium-browser --kiosk --password-store=basic {url}" if browser == "Chromium" else f"firefox --kiosk {url}"
            cmd = f"bash -c 'sleep 3; {browser_cmd}'"
            with open(target_file, "w") as f:
                f.write(f"[Desktop Entry]\nType=Application\nName=SUIT-{browser}\nExec={cmd}\n")
        self.update_status()

    def stop_ks_quietly(self):
        cmd = "systemctl stop suit-killswitch; systemctl disable suit-killswitch; rm -f /etc/systemd/system/suit-killswitch.service; systemctl daemon-reload"
        subprocess.run(ServiceUtils.sudo_cmd(cmd), shell=True, stderr=subprocess.DEVNULL)

    def start_learning_wrapper(self):
        self.stop_ks_quietly()
        self.update_status()
        self.start_learning()

    def start_learning(self):
        l = getattr(self.controller, "lang", "en")
        def txt(k): return self.controller.texts.get(k, {}).get(l, k)
        learn_script = os.path.join(self.project_dir, "scripts/tmp_learn.py")
        with open(learn_script, "w") as f:
            f.write(f"""
import evdev, time, json, sys, os
from select import select
try:
    devices = [evdev.InputDevice(p) for p in evdev.list_devices()]
    last_k, count, last_t = None, 0, 0
    while True:
        r, w, x = select(devices, [], [], 0.5)
        for dev in r:
            try:
                for ev in dev.read():
                    if ev.type == 1 and ev.value == 1:
                        now = time.time()
                        if ev.code == last_k and now - last_t < 1.5: count += 1
                        else: count = 1
                        last_k, last_t = ev.code, now
                        print(f"FEEDBACK:{{count}}", flush=True)
                        if count >= 5:
                            kn = evdev.ecodes.KEY.get(ev.code, f"CODE:{{ev.code}}")
                            config_data = {{"device": dev.path, "key": ev.code, "key_name": kn, "ts": time.time()}}
                            with open("{self.config_file}", "w") as f_cfg:
                                json.dump(config_data, f_cfg)
                            os.chmod("{self.config_file}", 0o666)
                            print("SUCCESS", flush=True)
                            time.sleep(0.1)
                            sys.exit(0)
            except: continue
except Exception as e:
    print(f"ERROR:{{e}}", flush=True)
    sys.exit(1)
""")
        self.learn_win = ctk.CTkToplevel(self)
        self.learn_win.title(txt("ks_learn_title"))
        self.learn_win.geometry("400x250")
        self.learn_win.after(10, self.learn_win.focus_get)
        self.learn_win.transient(self)
        ctk.CTkLabel(self.learn_win, text=txt("ks_learn_desc"), font=("Segoe UI", 14, "bold"), text_color="white").pack(pady=20)
        self.lbl_count = ctk.CTkLabel(self.learn_win, text="0 / 5", font=("Segoe UI", 40, "bold"), text_color=self.colors["accent"])
        self.lbl_count.pack(pady=10)
        threading.Thread(target=self.run_learn, args=(learn_script,), daemon=True).start()

    def run_learn(self, path):
        proc = subprocess.Popen(ServiceUtils.sudo_cmd(f"python3 {path}"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, shell=True, bufsize=1)
        success = False
        old_ts = 0
        if os.path.exists(self.config_file): old_ts = os.path.getmtime(self.config_file)
        try:
            while True:
                line = proc.stdout.readline()
                if not line: break
                line = line.strip()
                if "FEEDBACK:" in line:
                    try:
                        c = line.split(":")[1].strip()
                        self.after(0, lambda val=c: self.lbl_count.configure(text=f"{val} / 5"))
                    except: pass
                if "SUCCESS" in line:
                    success = True
                    break
        except: pass
        self.after(0, self.learn_win.destroy)
        try:
            proc.terminate()
            proc.wait(timeout=1)
        except: pass
        if proc.returncode == 0 or (os.path.exists(self.config_file) and os.path.getmtime(self.config_file) > old_ts): success = True
        if os.path.exists(path):
            try: os.remove(path)
            except: pass
        if success: self.after(100, self.auto_enable_ks)
        else: self.after(100, self.update_status)

    def auto_enable_ks(self): self.toggle_ks_service(force_enable=True)

    def toggle_ks_service(self, force_enable=False):
        l = getattr(self.controller, "lang", "en")
        def txt(k): return self.controller.texts.get(k, {}).get(l, k)
        status = ServiceUtils.check_status("suit-killswitch")
        is_installed = (status == "running" or status == "stopped")
        if force_enable or not is_installed:
            if not os.path.exists(self.config_file):
                messagebox.showwarning("SUIT", txt("ks_msg_learn_first"))
                return
            python_exe = "/usr/bin/python3"
            svc_content = f"""[Unit]\nDescription=SUIT Kill-Switch\nAfter=multi-user.target\n\n[Service]\nType=simple\nExecStart={python_exe} {self.killswitch_file}\nRestart=always\nStandardOutput=journal\nStandardError=journal\n\n[Install]\nWantedBy=multi-user.target\n"""
            temp_svc = os.path.join(self.user_home, "suit-killswitch.service")
            try:
                with open(temp_svc, "w") as f: f.write(svc_content)
                cmd = f"mv {temp_svc} {self.ks_service_file} && systemctl daemon-reload && systemctl enable suit-killswitch && systemctl start suit-killswitch"
                ServiceUtils.run_bash_script(self, cmd, "Kill-Switch Activation", on_close=self.update_status)
            except Exception as e: messagebox.showerror("Error", f"Service could not be created: {e}")
        else:
            cmd = f"systemctl stop suit-killswitch; systemctl disable suit-killswitch; rm -f {self.ks_service_file}; systemctl daemon-reload"
            ServiceUtils.run_bash_script(self, cmd, "Kill-Switch Deactivation", on_close=self.update_status)
        self.update_status()

    def update_status(self):
        l = getattr(self.controller, "lang", "en")
        def txt(k): return self.controller.texts.get(k, {}).get(l, k)
        
        # OSK Status
        try:
            res = subprocess.check_output(["gsettings", "get", "org.gnome.desktop.a11y.applications", "screen-keyboard-enabled"], text=True).strip()
            osk_active = (res == "true")
            self.btn_toggle_osk.configure(text=txt("btn_osk_on") if osk_active else txt("btn_osk_off"), 
                                         fg_color=self.colors["success"] if osk_active else self.colors["header"])
        except:
            self.btn_toggle_osk.configure(text="OSK: ERROR", state="disabled")

        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r") as f:
                    data = json.load(f)
                    self.key_label.configure(text=f"{txt('ks_current')}{data.get('key_name')}", text_color=self.colors["success"])
            except: pass
        else: self.key_label.configure(text=txt("ks_current_none"), text_color="#aaaaaa")
        status = ServiceUtils.check_status("suit-killswitch")
        if status == "running": self.btn_toggle_ks.configure(text=txt("btn_ks_on"), fg_color=self.colors["success"])
        elif status == "stopped": self.btn_toggle_ks.configure(text=txt("btn_ks_installed"), fg_color=self.colors["danger"])
        else: self.btn_toggle_ks.configure(text=txt("btn_ks_off"), fg_color=self.colors["header"])
        ff_active = os.path.exists(self.ff_desktop)
        ch_active = os.path.exists(self.ch_desktop)
        self.btn_auto_ff.configure(text=txt("btn_autostart_on") if ff_active else txt("btn_autostart_off"), fg_color=self.colors["success"] if ff_active else self.colors["header"])
        self.btn_auto_ch.configure(text=txt("btn_autostart_on") if ch_active else txt("btn_autostart_off"), fg_color=self.colors["success"] if ch_active else self.colors["header"])

    def launch_now(self, browser):
        url = self.url_ent.get().strip()
        cmd = f"chromium-browser --kiosk --password-store=basic {url} &" if browser == "Chromium" else f"firefox --kiosk {url} &"
        subprocess.Popen(cmd, shell=True)

    def toggle_osk(self):
        try:
            res = subprocess.check_output(["gsettings", "get", "org.gnome.desktop.a11y.applications", "screen-keyboard-enabled"], text=True).strip()
            new_state = "false" if res == "true" else "true"
            subprocess.run(["gsettings", "set", "org.gnome.desktop.a11y.applications", "screen-keyboard-enabled", new_state])
        except Exception as e:
            messagebox.showerror("Error", f"Could not toggle OSK: {e}")
        self.update_status()

    def update_texts(self):
        l = getattr(self.controller, "lang", "en")
        def txt(k): return self.controller.texts.get(k, {}).get(l, k)
        self.btn_back.configure(text=txt("btn_back"))
        self.lbl_title.configure(text=txt("kiosk_header"))
        self.lbl_info.configure(text=txt("desc_kiosk"))
        self.lbl_url.configure(text=txt("kiosk_url_lbl"))
        self.lbl_ch_title.configure(text=txt("kiosk_browser_ch"))
        self.lbl_ff_title.configure(text=txt("kiosk_browser_ff"))
        self.btn_now_ch.configure(text=txt("btn_launch_now"))
        self.btn_now_ff.configure(text=txt("btn_launch_now"))
        self.lbl_osk_title.configure(text=txt("osk_header"))
        self.lbl_ks_title.configure(text=txt("ks_header"))
        self.btn_learn.configure(text=txt("btn_ks_learn"))
        self.update_status()
