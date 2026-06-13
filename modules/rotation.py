import tkinter as tk
import customtkinter as ctk
from tkinter import messagebox
import os
import subprocess
import sys
import json
import dbus
import math
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
                res = "Unknown"
                w, h = 0, 0
                for m_info in monitors:
                    if m_info[0][0] == name:
                        for mode in m_info[1]:
                            if 'is-current' in mode[6]:
                                w, h = int(mode[1]), int(mode[2])
                                res = f"{w}x{h}"
                                break
                results.append({
                    'name': name,
                    'res': res,
                    'w': w, 'h': h,
                    'x': x, 'y': y,
                    'is_primary': is_primary,
                    'rotation': trans
                })
        return results
    except Exception as e:
        print(f"DBus Monitor Detection Error: {e}")
        return []

class ScreenPreview(tk.Canvas):
    """Custom Canvas to draw a rotatable screen box with a number (SCALED UP)."""
    def __init__(self, parent, number, **kwargs):
        super().__init__(parent, highlightthickness=0, **kwargs)
        self.number = number
        self.angle = 0
        self.is_active = False
        self.configure(bg="#27272a") # Match card
        self.colors = {"accent": "#3b82f6", "bg": "#18181b", "border": "#555555", "active_bg": "#1e293b"}
        self.bind("<Configure>", lambda e: self.draw())

    def set_state(self, angle, is_active):
        self.angle = angle
        self.is_active = is_active
        self.draw()

    def draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10 or h < 10: return

        cx, cy = w / 2, h / 2
        sw, sh = 140, 88
        
        rad = math.radians(self.angle)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        
        points = [(-sw/2, -sh/2), (sw/2, -sh/2), (sw/2, sh/2), (-sw/2, sh/2)]
        rotated_points = []
        for x, y in points:
            nx = cx + (x * cos_a - y * sin_a)
            ny = cy + (x * sin_a + y * cos_a)
            rotated_points.append(nx)
            rotated_points.append(ny)

        bg = self.colors["active_bg"] if self.is_active else self.colors["bg"]
        border = self.colors["accent"] if self.is_active else self.colors["border"]
        
        self.create_polygon(rotated_points, fill=bg, outline=border, width=4)
        self.create_text(cx, cy, text=str(self.number), fill="white", 
                        font=("Roboto", 32, "bold"), angle=-self.angle)

class RotationView(ctk.CTkFrame):
    def __init__(self, parent, controller):
        super().__init__(parent, fg_color="transparent")
        self.controller = controller
        self.colors = controller.colors
        self.project_dir = controller.project_dir
        
        self.user_home = os.path.expanduser("~")
        self.rotation_config = os.path.join(self.user_home, ".suit_rotation_config.json")
        self.autostart_dir = os.path.join(self.user_home, ".config/autostart")
        self.rotation_desktop = os.path.join(self.autostart_dir, "suit-rotation.desktop")
        self.rotation_script = os.path.join(self.project_dir, "scripts/apply_rotation.py")

        self.monitors = get_monitors_dbus()
        self.touchscreens = get_touchscreens()
        self.current_idx = 0
        self.screen_data = {}
        
        # GNOME trans: 0=normal, 1=left(270), 2=inverted(180), 3=right(90)
        trans_to_deg = {0: 0, 1: 270, 2: 180, 3: 90}
        
        config = self.load_config()
        for mon in self.monitors:
            name = mon['name']
            c = config.get(name, {})
            deg = trans_to_deg.get(mon['rotation'], 0)
            self.screen_data[name] = {
                'rotation': deg,
                'visual_rotation': deg,
                'touch': c.get('touch_device', 'None') != 'None',
                'touchDevice': c.get('touch_device', 'None'),
                'primary': mon['is_primary']
            }

        # --- HEADER ---
        self.header_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.header_frame.pack(fill="x", pady=(10, 10))
        
        self.btn_back = ctk.CTkButton(self.header_frame, text="←", width=60, height=45,
                                     fg_color=self.colors["card"], border_color=self.colors["header"],
                                     border_width=1, text_color="white", 
                                     font=("Roboto", 18, "bold"), command=controller.show_menu)
        self.btn_back.pack(side="left", padx=20)
        
        self.lbl_title = ctk.CTkLabel(self.header_frame, text="Screen & Touch Manager", 
                                     font=("Roboto", 32, "bold"), text_color="white")
        self.lbl_title.pack(side="left", padx=10)

        # --- MAIN CONTENT CONTAINER (Non-Scrollable, Auto-Growing) ---
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.pack(fill="both", expand=True)

        # --- SCREEN SELECTOR AREA ---
        self.screen_area = ctk.CTkFrame(self.content, fg_color=self.colors["card"], corner_radius=15,
                                       border_width=1, border_color=self.colors["header"])
        self.screen_area.pack(fill="x", padx=30, pady=10)
        
        self.selector_inner = ctk.CTkFrame(self.screen_area, fg_color="transparent")
        self.selector_inner.pack(pady=25)

        self.screen_wrappers = []
        self.previews = []
        self.screen_labels = []

        for i, mon in enumerate(self.monitors):
            wrap = ctk.CTkFrame(self.selector_inner, fg_color="transparent", cursor="hand2")
            wrap.grid(row=0, column=i, padx=40)
            
            preview = ScreenPreview(wrap, i+1, width=200, height=140)
            preview.pack()
            
            for widget in [wrap, preview]:
                widget.bind("<Button-1>", lambda e, idx=i: self.switch_screen(idx))

            ctk.CTkFrame(wrap, width=35, height=6, corner_radius=3, fg_color="#666666").pack(pady=(10, 0))
            label = ctk.CTkLabel(wrap, text=f"Screen {i+1}\n({mon['name']})", 
                                font=("Roboto", 13), text_color="#888888")
            label.pack(pady=(15, 0))
            
            self.screen_wrappers.append(wrap)
            self.previews.append(preview)
            self.screen_labels.append(label)

        # --- SETTINGS GROUP ---
        self.settings_group = ctk.CTkFrame(self.content, fg_color="transparent")
        self.settings_group.pack(fill="x", padx=70, pady=10)

        self.var_primary = tk.BooleanVar()
        ctk.CTkCheckBox(self.settings_group, text="Set as primary screen",
                        variable=self.var_primary, font=("Roboto", 18, "bold"),
                        checkbox_width=32, checkbox_height=32,
                        command=self.update_data, text_color="white").pack(anchor="w", pady=10)

        self.var_touch = tk.BooleanVar()
        self.check_touch = ctk.CTkCheckBox(self.settings_group, text="This screen has a touchscreen",
                                          variable=self.var_touch, font=("Roboto", 18, "bold"),
                                          checkbox_width=32, checkbox_height=32,
                                          command=self.toggle_touch_menu, text_color="white")
        self.check_touch.pack(anchor="w", pady=10)

        # Dynamic Touch Menu
        self.touch_menu_frame = ctk.CTkFrame(self.settings_group, fg_color="#1e293b", corner_radius=10,
                                            border_width=2, border_color=self.colors["accent"])
        
        ctk.CTkLabel(self.touch_menu_frame, text="Assign touch device:", 
                    font=("Roboto", 14, "bold"), text_color="#94a3b8").pack(anchor="w", padx=20, pady=(15, 5))
        
        self.var_touch_device = tk.StringVar(value="None")
        self.touch_combo = ctk.CTkOptionMenu(self.touch_menu_frame, values=["None"] + self.touchscreens,
                                           variable=self.var_touch_device, command=lambda x: self.update_data(),
                                           height=55, font=("Roboto", 16, "bold"),
                                           dropdown_font=("Roboto", 16, "bold"),
                                           fg_color=self.colors["bg"], button_color=self.colors["header"])
        self.touch_combo.pack(fill="x", padx=20, pady=(5, 18))

        # Rotation Control
        self.rot_control = ctk.CTkFrame(self.settings_group, fg_color=self.colors["card"], corner_radius=12,
                                       border_width=1, border_color=self.colors["header"])
        self.rot_control.pack(fill="x", pady=20)
        
        self.lbl_rot_preview = ctk.CTkLabel(self.rot_control, text="Visual Angle: 0°", 
                                           font=("Roboto", 18, "bold"), text_color="white")
        self.lbl_rot_preview.pack(side="left", padx=25, pady=15)

        ctk.CTkButton(self.rot_control, text="↺ -90°", width=120, height=50, corner_radius=10,
                      fg_color=self.colors["header"], text_color="white", font=("Roboto", 15, "bold"),
                      command=lambda: self.rotate(-90)).pack(side="right", padx=12)
        
        ctk.CTkButton(self.rot_control, text="↻ +90°", width=120, height=50, corner_radius=10,
                      fg_color=self.colors["header"], text_color="white", font=("Roboto", 15, "bold"),
                      command=lambda: self.rotate(90)).pack(side="right", padx=(25, 0))

        # --- ACTION BUTTONS ---
        self.button_row = ctk.CTkFrame(self.content, fg_color="transparent")
        self.button_row.pack(fill="x", padx=70, pady=(10, 40))

        ctk.CTkButton(self.button_row, text="APPLY SETTINGS", height=65, corner_radius=12,
                      fg_color=self.colors["accent"], text_color="white", 
                      font=("Roboto", 18, "bold"), command=self.apply_confirm).pack(side="right", padx=10)

        ctk.CTkButton(self.button_row, text="Identify", height=65, width=150, corner_radius=12,
                      fg_color="transparent", border_width=1, border_color=self.colors["header"],
                      text_color="white", font=("Roboto", 16, "bold"), command=self.identify).pack(side="right", padx=10)

        self.switch_screen(0)

    def switch_screen(self, idx):
        self.current_idx = idx
        for i, prev in enumerate(self.previews):
            name = self.monitors[i]['name']
            prev.set_state(self.screen_data[name]['visual_rotation'], i == idx)
            self.screen_labels[i].configure(text_color=self.colors["accent"] if i == idx else "#888888",
                                           font=("Roboto", 13, "bold" if i == idx else "normal"))

        name = self.monitors[idx]['name']
        data = self.screen_data[name]
        self.var_primary.set(data['primary'])
        self.var_touch.set(data['touch'])
        self.var_touch_device.set(data['touchDevice'])
        self.toggle_touch_menu()
        self.update_rotation_ui()

    def toggle_touch_menu(self):
        name = self.monitors[self.current_idx]['name']
        if self.var_touch.get():
            # Ensure only ONE screen has touch enabled
            for m_name in self.screen_data:
                if m_name != name:
                    self.screen_data[m_name]['touch'] = False
            
            self.touch_menu_frame.pack(fill="x", after=self.check_touch, padx=30, pady=(5, 15))
        else:
            self.touch_menu_frame.pack_forget()
        
        self.update_data()
        # Force window to resize to fit new content
        self.controller.update_idletasks()

    def rotate(self, deg):
        name = self.monitors[self.current_idx]['name']
        self.screen_data[name]['visual_rotation'] += deg
        self.previews[self.current_idx].set_state(self.screen_data[name]['visual_rotation'], True)
        self.update_rotation_ui()
        self.update_data()

    def update_rotation_ui(self):
        name = self.monitors[self.current_idx]['name']
        display_deg = self.screen_data[name]['visual_rotation'] % 360
        self.lbl_rot_preview.configure(text=f"Visual Angle: {display_deg}°")

    def update_data(self):
        name = self.monitors[self.current_idx]['name']
        if self.var_primary.get():
            for m_name in self.screen_data: self.screen_data[m_name]['primary'] = False
        self.screen_data[name]['primary'] = self.var_primary.get()
        self.screen_data[name]['touch'] = self.var_touch.get()
        self.screen_data[name]['touchDevice'] = self.var_touch_device.get()

    def identify(self):
        hint_win = ctk.CTkToplevel(self)
        hint_win.overrideredirect(True)
        hint_win.attributes("-topmost", True)
        mon = self.monitors[self.current_idx]
        pop_w, pop_h = 500, 300
        cx = mon['x'] + (mon['w'] // 2) - (pop_w // 2)
        cy = mon['y'] + (mon['h'] // 2) - (pop_h // 2)
        hint_win.geometry(f"{pop_w}x{pop_h}+{cx}+{cy}")
        hint_win.configure(fg_color="#333333")
        ctk.CTkLabel(hint_win, text=f"Screen {self.current_idx+1}", font=("Roboto", 60, "bold"), text_color="white").pack(expand=True)
        self.after(2500, hint_win.destroy)

    def _mode_for(self, data):
        return {0: "normal", 90: "right", 180: "inverted", 270: "left"}.get(data['visual_rotation'] % 360, "normal")

    def _write_config(self):
        """Persist every screen's rotation and assigned touch device."""
        config = self.load_config()
        for name, data in self.screen_data.items():
            touch_dev = data['touchDevice'] if data['touch'] else "None"
            config[name] = {'rotation': self._mode_for(data), 'touch_device': touch_dev}
        with open(self.rotation_config, "w") as f:
            json.dump(config, f)
        return config

    def _config_has_touch(self, config):
        return any(isinstance(c, dict) and c.get('touch_device', "None") != "None"
                   for c in config.values())

    def _write_udev_rule_from_config(self):
        """Build and write the touch udev rule to DISK from the saved config, so it
        is present at the NEXT boot's device init. This is the no-arg form of
        apply_rotation.py: it re-applies rotations and writes ROT(target) * H for
        whichever screen has the touch device. Running it after boot does NOT bind
        the matrix to the live device -- only the on-disk rule, read at boot, does."""
        os.system(f"{sys.executable} {self.rotation_script}")

    def _reboot_to_apply(self):
        """A touch udev rule is only consumed when the device initialises at boot,
        so a touch-mapping change cannot take effect until a reboot -- AND the rule
        must already be on disk before that reboot. Save is done by the caller; we
        write the rule, then trigger (or defer) the reboot."""
        if messagebox.askyesno(
            "Reboot required",
            "The touchscreen mapping has been saved.\n\n"
            "Touch calibration rules are only loaded at boot, so the new mapping "
            "takes effect after a reboot. Reboot now?"
        ):
            # Write the rule to disk FIRST so it is loaded during the next boot.
            self._write_udev_rule_from_config()
            os.system("sudo reboot")
        else:
            # Still write it so it is correct whenever the user reboots later.
            self._write_udev_rule_from_config()
            messagebox.showinfo(
                "Saved",
                "Settings saved. The touchscreen mapping will be applied on the "
                "next reboot."
            )
            self.refresh_screens()

    def apply_confirm(self):
        curr_name = self.monitors[self.current_idx]['name']
        curr_data = self.screen_data[curr_name]
        curr_mode = self._mode_for(curr_data)

        has_touch = curr_data['touch'] and curr_data['touchDevice'] != "None"

        if has_touch:
            # A touch mapping only loads at boot, so applying means rebooting. Ask
            # FIRST -- while the screen is still in its current orientation, so the
            # touchscreen still matches and the user can actually tap "Yes". Only
            # after they confirm do we rotate (right before the reboot), so the
            # screen never rotates out from under the still-old touch mapping while
            # a dialog is waiting for a tap.
            if not messagebox.askyesno(
                "Apply & reboot",
                "The touchscreen mapping only takes effect after a reboot.\n\n"
                "The screen will rotate to the new orientation and the system will "
                "reboot now. Continue?"
            ):
                self.refresh_screens()  # reset the preview to the real orientation
                return

            self._write_config()
            touch_dev_name = curr_data['touchDevice']
            # Rotate to the target AND write the udev rule, then reboot -- with NO
            # further dialogs (once rotated, touch won't match until the reboot
            # finishes, so nothing must require a tap in between).
            cmd_rot = f"{sys.executable} {self.rotation_script} {curr_mode} {curr_name} '{touch_dev_name}' 1"

            busy = ctk.CTkToplevel(self)
            busy.overrideredirect(True)
            busy.attributes("-topmost", True)
            bw, bh = 520, 160
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            busy.geometry(f"{bw}x{bh}+{(sw - bw) // 2}+{(sh - bh) // 2}")
            busy.configure(fg_color="#18181b")
            ctk.CTkLabel(busy, text="Applying rotation & rebooting…",
                         font=("Roboto", 22, "bold"), text_color="white").pack(expand=True)
            # Let the message paint, then rotate + write the rule + reboot.
            self.after(500, lambda: os.system(f"{cmd_rot} && sudo reboot"))
            return

        # --- No touchscreen on this screen ---
        # Rotation alone applies live via GNOME (method 2 triggers the confirm
        # dialog) and does NOT need a reboot.
        cmd = f"{sys.executable} {self.rotation_script} {curr_mode} {curr_name} None 2"

        def on_done():
            if messagebox.askyesno("Save Settings", "Do you want to save these settings permanently?"):
                self.do_save()
            else:
                self.refresh_screens()

        ServiceUtils.run_bash_script(self, cmd, f"Applying {curr_name}", on_close=on_done)

    def do_save(self):
        config = self._write_config()

        curr_name = self.monitors[self.current_idx]['name']
        curr_data = self.screen_data[curr_name]
        curr_mode = self._mode_for(curr_data)

        # Apply the rotation persistently (live, via GNOME).
        cmd = f"{sys.executable} {self.rotation_script} {curr_mode} {curr_name} None 1"

        if self._config_has_touch(config):
            # A touch mapping exists; its udev rule only loads at boot, so apply
            # the rotation now for feedback and then reboot to load the mapping.
            ServiceUtils.run_bash_script(self, cmd, f"Saving {curr_name}", on_close=self._reboot_to_apply)
        else:
            ServiceUtils.run_bash_script(self, cmd, f"Saving {curr_name}", on_close=self.refresh_screens)


    def load_config(self):
        if os.path.exists(self.rotation_config):
            try:
                with open(self.rotation_config, "r") as f: return json.load(f)
            except: pass
        return {}

    def refresh_screens(self, idx=None):
        self.monitors = get_monitors_dbus()
        self.touchscreens = get_touchscreens()
        target_idx = idx if idx is not None else self.current_idx
        if target_idx >= len(self.monitors): target_idx = 0
        self.switch_screen(target_idx)

    def update_texts(self): pass
