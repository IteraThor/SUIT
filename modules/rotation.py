import tkinter as tk
import customtkinter as ctk
from tkinter import messagebox
import os
import subprocess
import sys
import json
import dbus
from modules.utils import ServiceUtils

def get_touchscreens():
    try:
        cmd = "udevadm info --export-db | grep -E 'E: NAME=|E: ID_INPUT_TOUCHSCREEN=1' | grep -B 1 'ID_INPUT_TOUCHSCREEN=1' | grep 'E: NAME=' | cut -d'=' -f2 | tr -d '\"' | sort -u"
        output = subprocess.check_output(cmd, shell=True, text=True)
        return [line.strip() for line in output.splitlines() if line.strip()]
    except:
        return []

def get_monitors_dbus():
    """Gets connected monitors and their current logical state via GNOME DBus."""
    try:
        bus = dbus.SessionBus()
        dc = bus.get_object('org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig')
        dc_iface = dbus.Interface(dc, dbus_interface='org.gnome.Mutter.DisplayConfig')
        serial, monitors, logical_monitors, properties = dc_iface.GetCurrentState()
        
        results = []
        for lm in logical_monitors:
            x, y, scale, trans, is_primary, phys_monitors = lm[:6]
            for pm in phys_monitors:
                name = pm[0]
                # Find physical info for resolution
                res = "Unknown"
                for m_info in monitors:
                    if m_info[0][0] == name:
                        for mode in m_info[1]:
                            if 'is-current' in mode[6]:
                                res = f"{mode[2]}x{mode[3]}"
                                break
                results.append({
                    'name': name,
                    'res': res,
                    'is_primary': is_primary,
                    'rotation': trans
                })
        return results
    except Exception as e:
        print(f"DBus Monitor Detection Error: {e}")
        return []

class ScreenCard(ctk.CTkFrame):
    def __init__(self, parent, monitor_info, touchscreens, controller, rotation_view):
        super().__init__(parent, fg_color=controller.colors["card"], border_width=1, border_color="#555555")
        self.monitor_info = monitor_info
        self.controller = controller
        self.rotation_view = rotation_view
        self.name = monitor_info['name']

        # --- Header ---
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(15, 10))
        
        title = "PRIMARY" if monitor_info['is_primary'] else "SECONDARY"
        ctk.CTkLabel(header, text=f"{title}: {self.name}", font=("Segoe UI", 16, "bold"), text_color="#3b8ed0").pack(side="left")
        ctk.CTkLabel(header, text=monitor_info['res'], font=("Segoe UI", 14), text_color="gray").pack(side="right")

        # --- Touch Assignment (Touch-Friendly Dropdown) ---
        ctk.CTkLabel(self, text="Physical Touchscreen Device:", font=("Segoe UI", 12, "bold"), text_color="white").pack(anchor="w", padx=25, pady=(5, 0))
        
        self.touch_var = tk.StringVar(value=self.get_saved_touch())
        self.touch_combo = ctk.CTkOptionMenu(self, values=["None"] + touchscreens, variable=self.touch_var,
                                           height=50, # Much taller for fingers
                                           font=("Segoe UI", 14), 
                                           fg_color="#1a1a1a", button_color="#333333", dynamic_resizing=True)
        self.touch_combo.pack(fill="x", padx=20, pady=(5, 20))

        # --- Rotation Grid (2x2 Big Buttons) ---
        grid_frame = ctk.CTkFrame(self, fg_color="transparent")
        grid_frame.pack(fill="x", padx=15, pady=(0, 20))
        
        # Grid config
        grid_frame.columnconfigure((0, 1), weight=1)

        opts = [
            ("rot_normal_btn", "normal", 0, 0),
            ("rot_inverted_btn", "inverted", 0, 1),
            ("rot_left_btn", "left", 1, 0),
            ("rot_right_btn", "right", 1, 1)
        ]

        for key, mode, r, c in opts:
            btn = ctk.CTkButton(grid_frame, text=self.rotation_view.txt(key), 
                               height=75, # Huge touch area
                               fg_color=controller.colors["accent"], 
                               font=("Segoe UI", 14, "bold"),
                               command=lambda m=mode: self.rotation_view.apply_and_save(self.name, m, self.touch_var.get()))
            btn.grid(row=r, column=c, padx=8, pady=8, sticky="nsew")

    def get_saved_touch(self):
        config = self.rotation_view.load_config()
        return config.get(self.name, {}).get('touch_device', 'None')

class RotationView(ctk.CTkFrame):
    def __init__(self, parent, controller):
        super().__init__(parent, fg_color="transparent")
        self.controller = controller
        self.colors = controller.colors
        self.project_dir = controller.project_dir
        
        # Paths
        self.user_home = os.path.expanduser("~")
        self.rotation_config = os.path.join(self.user_home, ".suit_rotation_config.json")
        self.autostart_dir = os.path.join(self.user_home, ".config/autostart")
        self.rotation_desktop = os.path.join(self.autostart_dir, "suit-rotation.desktop")
        self.rotation_script = os.path.join(self.project_dir, "scripts/apply_rotation.py")
        
        # --- HEADER ---
        self.header = ctk.CTkFrame(self, fg_color="transparent")
        self.header.pack(fill="x", pady=(10, 15))
        
        self.btn_back = ctk.CTkButton(self.header, text="", width=120, height=45, # Taller back button
                                     fg_color=self.colors["header"], text_color="white", 
                                     font=("Segoe UI", 14, "bold"), command=controller.show_menu)
        self.btn_back.pack(side="left", padx=15)
        self.lbl_title = ctk.CTkLabel(self.header, text="", font=("Segoe UI", 26, "bold"), text_color="white")
        self.lbl_title.pack(side="left", padx=10)

        # --- INFO CARD (Fingers need clear instructions) ---
        self.info_frame = ctk.CTkFrame(self, fg_color=self.colors["card"])
        self.info_frame.pack(fill="x", padx=25, pady=(0, 15))
        self.lbl_info = ctk.CTkLabel(self.info_frame, text="", font=("Segoe UI", 14),
                                    text_color=self.colors["fg_dim"], wraplength=740, justify="left")
        self.lbl_info.pack(fill="both", padx=20, pady=20)

        # --- SCREENS CONTAINER (Scrollable for Touch) ---
        self.screens_scroll = ctk.CTkScrollableFrame(self, fg_color="transparent", height=450)
        self.screens_scroll.pack(fill="both", expand=True, padx=20, pady=5)

        self.refresh_screens()

    def txt(self, k): 
        l = getattr(self.controller, "lang", "en")
        return self.controller.texts.get(k, {}).get(l, k)

    def load_config(self):
        if os.path.exists(self.rotation_config):
            try:
                with open(self.rotation_config, "r") as f:
                    return json.load(f)
            except: pass
        return {}

    def refresh_screens(self):
        for child in self.screens_scroll.winfo_children():
            child.destroy()

        monitors = get_monitors_dbus()
        touchscreens = get_touchscreens()

        if not monitors:
            ctk.CTkLabel(self.screens_scroll, text="No monitors detected via DBus.", 
                        font=("Segoe UI", 16), text_color="gray").pack(pady=40)
            return

        for mon in monitors:
            card = ScreenCard(self.screens_scroll, mon, touchscreens, self.controller, self)
            card.pack(fill="x", pady=15, padx=5)

    def apply_and_save(self, monitor_name, mode, touch_device):
        # 1. Update JSON config
        config = self.load_config()
        config[monitor_name] = {
            'rotation': mode,
            'touch_device': touch_device
        }
        with open(self.rotation_config, "w") as f:
            json.dump(config, f)

        # 2. Update Autostart Desktop file
        if not os.path.exists(self.autostart_dir): os.makedirs(self.autostart_dir)
        with open(self.rotation_desktop, "w") as f:
            f.write(f"[Desktop Entry]\nType=Application\nName=SUIT-Rotation\nExec={sys.executable} {self.rotation_script}\nTerminal=false\n")

        # 3. Execute immediately
        cmd = f"{sys.executable} {self.rotation_script} {mode} {monitor_name} '{touch_device}'"
        ServiceUtils.run_bash_script(self, cmd, f"Rotating {monitor_name}")

    def update_texts(self):
        self.btn_back.configure(text=self.txt("btn_back"))
        self.lbl_title.configure(text=self.txt("btn_touch"))
        self.lbl_info.configure(text=self.txt("desc_rotation"))
        self.refresh_screens()
