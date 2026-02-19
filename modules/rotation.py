import tkinter as tk
import customtkinter as ctk
from tkinter import messagebox
import os
import subprocess
import sys
from modules.utils import ServiceUtils

def get_monitors():
    try:
        output = subprocess.check_output(["xrandr"], text=True)
        return [line.split()[0] for line in output.splitlines() if " connected" in line]
    except:
        return []

def get_touchscreens():
    try:
        cmd = "udevadm info --export-db | grep -E 'E: NAME=|E: ID_INPUT_TOUCHSCREEN=1' | grep -B 1 'ID_INPUT_TOUCHSCREEN=1' | grep 'E: NAME=' | cut -d'=' -f2 | tr -d '\"' | sort -u"
        output = subprocess.check_output(cmd, shell=True, text=True)
        return [line.strip() for line in output.splitlines() if line.strip()]
    except:
        return []

class RotationView(ctk.CTkFrame):
    def __init__(self, parent, controller):
        super().__init__(parent, fg_color="transparent")
        self.controller = controller
        self.colors = controller.colors
        self.project_dir = controller.project_dir
        
        # Paths
        self.user_home = os.path.expanduser("~")
        self.rotation_config = os.path.join(self.user_home, ".suit_rotation_config")
        self.autostart_dir = os.path.join(self.user_home, ".config/autostart")
        self.rotation_desktop = os.path.join(self.autostart_dir, "suit-rotation.desktop")
        self.rotation_script = os.path.join(self.project_dir, "scripts/apply_rotation.py")
        self.udev_rule_final = "/etc/udev/rules.d/99-suit-touch.rules"
        
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

        # --- SELECTION AREA ---
        self.select_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.select_frame.pack(fill="x", pady=5, padx=40)

        # Monitor Selection
        self.lbl_mon = ctk.CTkLabel(self.select_frame, text="", font=("Segoe UI", 13, "bold"), text_color="white")
        self.lbl_mon.pack(anchor="w", padx=5)
        self.monitors = get_monitors()
        self.mon_combo = ctk.CTkOptionMenu(self.select_frame, values=self.monitors if self.monitors else ["None Found"], 
                                         height=50, font=("Segoe UI", 14), fg_color="#1a1a1a", button_color="#333333", text_color="white")
        self.mon_combo.pack(fill="x", pady=(5, 15))

        # Touchscreen Selection
        self.lbl_touch = ctk.CTkLabel(self.select_frame, text="", font=("Segoe UI", 13, "bold"), text_color="white")
        self.lbl_touch.pack(anchor="w", padx=5)
        self.touchscreens = get_touchscreens()
        self.touch_combo = ctk.CTkOptionMenu(self.select_frame, values=self.touchscreens if self.touchscreens else ["None Found"],
                                           height=50, font=("Segoe UI", 14), fg_color="#1a1a1a", button_color="#333333", text_color="white")
        self.touch_combo.pack(fill="x", pady=(5, 25))

        # --- BUTTON GRID ---
        self.btn_grid_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.btn_grid_frame.pack(expand=True, pady=10)

        self.rot_options = [
            ("rot_normal_btn", "normal", "1 0 0 0 1 0 0 0 1"),
            ("rot_right_btn", "right", "0 1 0 -1 0 1 0 0 1"),
            ("rot_left_btn", "left", "0 -1 1 1 0 0 0 0 1")
        ]
        self.rot_buttons = []

        for i, (key, x_val, matrix) in enumerate(self.rot_options):
            btn = ctk.CTkButton(self.btn_grid_frame, text="", height=65, width=220,
                               fg_color=self.colors["accent"], text_color="white", font=("Segoe UI", 14, "bold"),
                               command=lambda x=x_val, m=matrix: self.save_and_apply(x, m))
            btn.pack(side="left", padx=15)
            self.rot_buttons.append((btn, key))

        self.update_texts()

    def save_and_apply(self, x_val, matrix):
        l = getattr(self.controller, "lang", "en")
        def txt(k): return self.controller.texts.get(k, {}).get(l, k)
        target_mon = self.mon_combo.get()
        target_touch = self.touch_combo.get()
        if not target_mon or target_mon == "None Found":
            messagebox.showwarning("SUIT", txt("rot_msg_mon_req"))
            return
        try:
            with open(self.rotation_config, "w") as f:
                f.write(f"ROTATION={x_val}\nMONITOR={target_mon}\nTOUCH='{target_touch}'\nMATRIX='{matrix}'\n")
            if not os.path.exists(self.autostart_dir): os.makedirs(self.autostart_dir)
            with open(self.rotation_desktop, "w") as f:
                f.write(f"[Desktop Entry]\nType=Application\nName=SUIT-Rotation\nExec=python3 {self.rotation_script} {x_val} {target_mon}\nTerminal=false\n")
            if target_touch and target_touch != "None Found":
                udev_rule = f'ACTION=="add|change", ENV{{ID_INPUT_TOUCHSCREEN}}=="1", ATTRS{{name}}=="{target_touch}", ENV{{LIBINPUT_CALIBRATION_MATRIX}}="{matrix}"\n'
                temp_rule = "/tmp/suit_touch.rules"
                with open(temp_rule, "w") as f: f.write(udev_rule)
                cmd = f"cp {temp_rule} {self.udev_rule_final} && udevadm control --reload-rules && udevadm trigger --subsystem-match=input"
                subprocess.run(ServiceUtils.sudo_cmd(cmd), shell=True)
            if messagebox.askyesno("SUIT", txt("rot_msg_reboot")):
                subprocess.run(ServiceUtils.sudo_cmd("reboot"), shell=True)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save rotation: {e}")

    def update_texts(self):
        l = getattr(self.controller, "lang", "en")
        def txt(k): return self.controller.texts.get(k, {}).get(l, k)
        self.btn_back.configure(text=txt("btn_back"))
        self.lbl_title.configure(text=txt("btn_touch"))
        self.lbl_info.configure(text=txt("desc_rotation"))
        self.lbl_mon.configure(text=txt("rot_mon_lbl"))
        self.lbl_touch.configure(text=txt("rot_touch_lbl"))
        for btn, key in self.rot_buttons:
            btn.configure(text=txt(key))
