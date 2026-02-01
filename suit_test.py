import tkinter as tk
from tkinter import ttk, messagebox, colorchooser
import subprocess
import webbrowser
from shutil import which
import os
import sys
import dbus
from collections import defaultdict
import json
import time
import threading
import socket

# --- Optional import for WebSocket feature ---
try:
    import queue
    import asyncio
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False

# --- Optional import for Serial feature ---
try:
    import serial
    import serial.tools.list_ports
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False


# --- KONFIGURATION SCREEN ROTATOR ---
DISPLAY_NAME_DEFAULT = "DP-2"
AUTOSTART_DIR = os.path.expanduser("~/.config/autostart")
DESKTOP_FILE_PATH = os.path.join(AUTOSTART_DIR, "screen-rotation.desktop")
UDEV_RULE_FINAL_PATH = "/etc/udev/rules.d/99-touchscreen-rotation.rules"
TEMP_RULE_PATH = os.path.expanduser("/tmp/touch_rotation.rules")

MATRICES = {
    "normal": "1 0 0 0 1 0 0 0 1",
    "right": "0 1 0 -1 0 1 0 0 1",
    "left": "0 -1 1 1 0 0 0 0 1"
}

# --- FARBPALETTE (DARK MODE) ---
COLORS = {
    "bg_main": "#1e1e1e",
    "bg_panel": "#252526",
    "fg_text": "#ffffff",
    "fg_sub": "#cccccc",
    "accent": "#007acc",
    "accent_hover": "#0098ff",
    "btn_bg": "#3e3e42",
    "btn_fg": "#ffffff",
    "danger": "#d9534f",
    "success": "#28a745",
    "warning": "#ff9800",
    "select_indicator": "#000000"
}

WLED_EFFECTS = {
    "Solid": 0, "Blink": 1, "Breathe": 2, "Wipe": 3, "Scan": 45,
    "Rainbow": 9, "Chase": 28, "Fire": 66, "Strobe": 8,
    "Color Loop": 11, "Heartbeat": 101, "Pacifica": 104
}
EFFECT_ID_TO_NAME = {v: k for k, v in WLED_EFFECTS.items()}


# --- TEXTE ---
TEXTS = {
    "app_title": {"de": "SUIT - Setup Utilities by IteraThor", "en": "SUIT - Setup Utilities by IteraThor"},
    "menu_title": {"de": "Hauptmenü", "en": "Main Menu"},
    "btn_autodarts": {"de": "🎯 Autodarts Manager", "en": "🎯 Autodarts Manager"},
    "btn_autoglow": {"de": "💡 AutoGlow Manager", "en": "💡 AutoGlow Manager"},
    "btn_touch": {"de": "🔄 Screen & Touch Rotation", "en": "🔄 Screen & Touch Rotation"},
    "btn_kiosk": {"de": "🖥️ Setup Kiosk Mode", "en": "🖥️ Setup Kiosk Mode"},
    "btn_update": {"de": "⬇️ Auf Updates prüfen", "en": "⬇️ Check for Updates"},
    "btn_back": {"de": "❮ Zurück", "en": "❮ Back"},
    "ad_header": {"de": "Autodarts Verwaltung", "en": "Autodarts Management"},
    "ag_header": {"de": "AutoGlow Einstellungen", "en": "AutoGlow Settings"},
    "ag_install": {"de": "Installation (Venv & Service)", "en": "Installation (Venv & Service)"},
    "ag_save": {"de": "Speichern & Dienst Neustart", "en": "Save & Restart Service"},
    "kiosk_header": {"de": "Kiosk Modus (Firefox)", "en": "Kiosk Mode (Firefox)"},
    "touch_header": {"de": "Bildschirm & Touch Ausrichtung", "en": "Screen & Touch Orientation"},
    "status_lbl": {"de": "Status:", "en": "Status:"},
    "loading": {"de": "Lade...", "en": "Loading..."},
    "btn_start": {"de": "Starten", "en": "Start"},
    "btn_stop": {"de": "Stoppen", "en": "Stop"},
    "btn_restart": {"de": "Neustarten", "en": "Restart"},
    "btn_install": {"de": "Installieren", "en": "Install"},
    "btn_uninstall": {"de": "Deinstallieren", "en": "Uninstall"},
    "hint": {"de": "Hinweis: Root-Passwort erforderlich.", "en": "Note: Root password required."},
    "kiosk_active": {"de": "● Aktiv (Autostart an)", "en": "● Active (Autostart on)"},
    "kiosk_inactive": {"de": "● Inaktiv (Autostart aus)", "en": "● Inactive (Autostart off)"},
    "btn_enable_kiosk": {"de": "Einschalten (Autostart)", "en": "Enable (Autostart)"},
    "btn_disable_kiosk": {"de": "Ausschalten", "en": "Disable"},
    "kiosk_hint": {"de": "Startet Firefox (Wayland Mode).\nVerhindert 'Restore Session' Popup & Black Screen.", 
                   "en": "Starts Firefox (Wayland Mode).\nPrevents 'Restore Session' popup & black screen."},
    "kiosk_url_lbl": {"de": "Kiosk URL:", "en": "Kiosk URL:"},
    "kiosk_emergency_exit": {"de": "Power-Button Not-Aus aktivieren", "en": "Enable Power-Button Emergency Exit"},
    "lbl_osk": {"de": "Bildschirmtastatur (OSK) aktivieren", "en": "Enable On-Screen Keyboard"},
    "kiosk_emergency_desc": {
        "de": "Drücke den Power-Button 3x innerhalb von 3 Sekunden,\num Firefox sofort zu beenden.", 
        "en": "Press the power button 3 times within 3 seconds\nto immediately close Firefox."
    },
    "st_active": {"de": "● Aktiv (Läuft)", "en": "● Active (Running)"},
    "st_inactive": {"de": "● Inaktiv (Gestoppt)", "en": "● Inactive (Stopped)"},
    "st_failed": {"de": "● Fehlgeschlagen", "en": "● Failed"},
    "st_unknown": {"de": "● Unbekannt", "en": "● Unknown"},
    "st_nofile": {"de": "● Nicht installiert", "en": "● Not installed"},
    "lbl_direction": {"de": "Ausrichtung wählen:", "en": "Select Orientation:"},
    "rot_left": {"de": "Links (90° gegen Uhrzeigersinn)", "en": "Left (90° CCW)"},
    "rot_right": {"de": "Rechts (90° im Uhrzeigersinn)", "en": "Right (90° CW)"},
    "rot_normal": {"de": "Normal (Standard)", "en": "Normal (Default)"},
    "btn_apply": {"de": "Speichern & Fixieren (Reboot nötig)", "en": "Save & Fix (Reboot required)"},
    "msg_applied": {"de": "Einstellung gespeichert.\nBitte neu starten.", "en": "Settings saved.\nPlease reboot."},
    "msg_deleted": {"de": "Regel gelöscht. Zurück auf Standard.", "en": "Rule deleted. Back to default."},
    "err_term": {"de": "Fehler: 'xterm' nicht gefunden.", "en": "Error: 'xterm' not found."},
    "done": {"de": "Fertig.", "en": "Done."},
    "ag_removed": {"de": "Das AutoGlow Modul wurde entfernt.", "en": "The AutoGlow module has been removed."},
    "msg_uptodate": {"de": "SUIT ist bereits auf dem neuesten Stand.", "en": "SUIT is already up to date."},
    "msg_updated": {"de": "Update erfolgreich! Anwendung wird neu gestartet.", "en": "Update successful! Restarting application."},
    "err_update": {"de": "Fehler beim Update:\n", "en": "Update Error:\n"},
    "err_no_git": {"de": "Kein Git-Repository gefunden.\nBitte 'git clone' nutzen.", "en": "No Git repository found.\nPlease use 'git clone'."}
}

# ==========================================
# SCREEN ROTATOR LOGIK (DBUS & MONITOR)
# ==========================================
nested_dict = lambda: defaultdict(nested_dict)

def rot_to_trans(r): return {'normal': 0, 'inverted': 6, 'left': 1, 'right': 3}.get(r, 0)
def trans_needs_w_h_swap(old_trans, new_trans): return (old_trans in [0, 6] and new_trans in [1, 3]) or (old_trans in [1, 3] and new_trans in [0, 6])

def mode_id_to_vals(mode_id):
    w, h_rate = mode_id.split('x')
    h, rate = h_rate.split('@')
    return (int(w), int(h), float(rate))

def get_current_mode(monitor):
    for md in monitor[1]:
        if 'is-current' in md[6]: return md
    return None

class ConfigInfo:
    def __init__(self, serial, monitors, logical_monitors, properties):
        self.serial = serial
        self.monitors = monitors
        self.logical_monitors = logical_monitors
        self.output_config = nested_dict()
        self.primary = None
        self.__init_output_config(logical_monitors)

    def __init_output_config(self, logical_monitors):
        for lm in logical_monitors:
            x, y, scale, trans, is_primary, phys_monitors = lm[:6]
            if is_primary == True and len(phys_monitors) > 0: self.primary = phys_monitors[0][0]
            for m in phys_monitors:
                output_name = m[0]
                conf = self.output_config[output_name]
                monitor = self.get_monitor_by_output(output_name)
                if not monitor: continue
                md = get_current_mode(monitor)
                if not md: continue
                w, h, r = mode_id_to_vals(md[0])
                conf['monitor'] = monitor
                conf['mode-info'] = md
                conf['old-mode-id'] = md[0]
                conf['res'], conf['w'], conf['h'], conf['rate'] = f'{w}x{h}', w, h, r
                conf['scale'], conf['trans'] = scale, trans

    def get_monitor_by_output(self, output):
        for m in self.monitors:
            if m[0][0] == output: return m
        return None

    def update_output_config(self, output_name, rotation_mode):
        if output_name not in self.output_config: return
        conf = self.output_config[output_name]
        new_trans = rot_to_trans(rotation_mode)
        if trans_needs_w_h_swap(conf['trans'], new_trans): conf['w'], conf['h'] = conf['h'], conf['w']
        conf['trans'] = new_trans

    def apply(self):
        new_lm = []
        for lm in self.logical_monitors:
            x, y, scale, trans, is_primary, phys_monitors_raw = lm[:6]
            new_phys_monitors = []
            for pm in phys_monitors_raw:
                connector_name = pm[0]
                if connector_name in self.output_config:
                    saved_conf = self.output_config[connector_name]
                    mode_id = saved_conf['old-mode-id']
                    trans = saved_conf['trans']
                    new_phys_monitors.append([connector_name, mode_id, {}])
            if new_phys_monitors:
                new_lm.append([x, y, scale, trans, is_primary, new_phys_monitors])
        return new_lm

def apply_gnome_rotation(output_name, rotation_mode):
    try:
        bus = dbus.SessionBus()
        dc = bus.get_object('org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig')
        dc_iface = dbus.Interface(dc, dbus_interface='org.gnome.Mutter.DisplayConfig')
        serial, monitors, logical_monitors, properties = dc_iface.GetCurrentState()
        
        target_output = output_name
        available_outputs = [m[0][0] for m in monitors]
        if output_name not in available_outputs and len(available_outputs) > 0:
            target_output = available_outputs[0]
            print(f"Monitor {output_name} nicht gefunden. Nutze {target_output}")

        config = ConfigInfo(serial, monitors, logical_monitors, properties)
        config.update_output_config(target_output, rotation_mode)
        new_lm = config.apply()
        
        if not new_lm: return False
        dc_iface.ApplyMonitorsConfig(serial, 1, new_lm, {})
        return True
    except Exception as e:
        sys.stderr.write(f"Screen Rotator Error: {e}\n")
        return False

def find_touchscreen_name():
    try:
        output = subprocess.check_output(["udevadm", "info", "--export-db"], text=True)
        current_name = None
        for line in output.splitlines():
            if "N: input/event" in line: current_name = None
            if "E: NAME=" in line: current_name = line.split("=")[1].strip('"')
            if "E: ID_INPUT_TOUCHSCREEN=1" in line and current_name: return current_name
    except: pass
    return None

# ==========================================
# GUI APP
# ==========================================

class SuitApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SUIT")
        self.geometry("600x850") 
        self.resizable(False, False)
        self.configure(bg=COLORS["bg_main"])
        self.lang = "de"
        self.current_frame = None
        self._setup_styles()
        self._build_header()
        self.container = tk.Frame(self, bg=COLORS["bg_main"])
        self.container.pack(fill="both", expand=True, padx=20, pady=20)
        self.show_menu()

    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use('clam') 
        style.configure("TFrame", background=COLORS["bg_main"])
        style.configure("Card.TFrame", background=COLORS["bg_panel"], relief="flat")
        style.configure("TLabel", background=COLORS["bg_main"], foreground=COLORS["fg_text"], font=("Segoe UI", 11))
        style.configure("Header.TLabel", font=("Segoe UI", 18, "bold"), foreground=COLORS["accent"])
        style.configure("SubHeader.TLabel", font=("Segoe UI", 14, "bold"), foreground=COLORS["fg_text"])
        style.configure("Card.TLabel", background=COLORS["bg_panel"], foreground=COLORS["fg_text"])
        style.configure("Hint.TLabel", foreground=COLORS["fg_sub"], font=("Segoe UI", 9))
        style.configure("TButton", background=COLORS["btn_bg"], foreground=COLORS["btn_fg"], borderwidth=0, font=("Segoe UI", 10, "bold"), padding=8)
        style.map("TButton", background=[("active", COLORS["accent"]), ("pressed", COLORS["accent_hover"])])
        style.configure("Accent.TButton", background=COLORS["accent"], foreground="white")
        style.configure("Danger.TButton", background=COLORS["danger"], foreground="white")
        style.configure("Lang.TButton", font=("Segoe UI", 9), padding=2, width=4)
        style.configure("LangActive.TButton", background=COLORS["accent"], foreground="white", font=("Segoe UI", 9, "bold"), width=4)
        style.configure("TLabelframe", background=COLORS["bg_panel"], foreground=COLORS["fg_sub"], relief="flat")
        style.configure("TLabelframe.Label", background=COLORS["bg_panel"], foreground=COLORS["accent"])

    def _build_header(self):
        header = tk.Frame(self, bg=COLORS["bg_main"], height=60)
        header.pack(fill="x", padx=20, pady=(15, 0))
        self.title_lbl = ttk.Label(header, text="SUIT", style="Header.TLabel")
        self.title_lbl.pack(side="left")
        lang_frame = tk.Frame(header, bg=COLORS["bg_main"])
        lang_frame.pack(side="right")
        self.btn_de = ttk.Button(lang_frame, text="DE", style="LangActive.TButton", command=lambda: self.set_lang("de"))
        self.btn_de.pack(side="left", padx=2)
        self.btn_en = ttk.Button(lang_frame, text="EN", style="Lang.TButton", command=lambda: self.set_lang("en"))
        self.btn_en.pack(side="left", padx=2)

    def set_lang(self, l):
        self.lang = l
        if l == "de":
            self.btn_de.configure(style="LangActive.TButton")
            self.btn_en.configure(style="Lang.TButton")
        else:
            self.btn_de.configure(style="Lang.TButton")
            self.btn_en.configure(style="LangActive.TButton")
        self.title_lbl.config(text=TEXTS["app_title"][l])
        if self.current_frame and hasattr(self.current_frame, "update_texts"):
            self.current_frame.update_texts()

    def show_menu(self): self._switch(MainMenu)
    def show_autodarts(self): self._switch(AutodartsView)
    def show_touch(self): self._switch(TouchRotationView)
    def show_kiosk(self): self._switch(KioskView)
    
    def show_autoglow(self): self._switch(AutoGlowView)

    def _switch(self, frame_class):
        if self.current_frame: self.current_frame.destroy()
        self.current_frame = frame_class(self.container, self)
        self.current_frame.pack(fill="both", expand=True)

class ServiceViewMixin:
    def _check_status_generic(self, service_name, label_widget):
        l = self.controller.lang
        try:
            res = subprocess.run(["systemctl", "is-active", service_name], capture_output=True, text=True)
            st = res.stdout.strip()
            if st == "active": label_widget.config(text=TEXTS["st_active"][l], foreground=COLORS["success"])
            elif st == "inactive": label_widget.config(text=TEXTS["st_inactive"][l], foreground=COLORS["danger"])
            else: label_widget.config(text=f"{TEXTS['st_unknown'][l]}", foreground="orange")
        except: label_widget.config(text=TEXTS["st_nofile"][l], foreground="gray")

    def _run_service_cmd(self, service_name, action, label_widget):
        cmd = self._sudo_cmd(f"systemctl {action} {service_name}")
        subprocess.Popen(cmd, shell=True)
        label_widget.config(text=TEXTS["loading"][self.controller.lang], foreground="orange")
        for t in [1000, 3000, 5000]: self.after(t, lambda: self._check_status_generic(service_name, label_widget))

    def _sudo_cmd(self, shell_cmd):
        if which("pkexec"): return f"pkexec bash -c '{shell_cmd}'"
        return f"xterm -e 'sudo bash -c \"{shell_cmd}\"'"

    def _run_bash_script(self, bash_content):
        filename = "/tmp/suit_exec.sh"
        # The 'read' part is for interactive terminals, not needed for pkexec
        full_content = "#!/bin/bash\n" + bash_content
        try:
            with open(filename, "w") as f: f.write(full_content)
            os.chmod(filename, 0o755)

            if which("pkexec"):
                cmd = f"pkexec bash {filename}"
                subprocess.Popen(cmd, shell=True)
                messagebox.showinfo("In Progress", "The script is running in the background. You may be prompted for your password.")
            elif which("xterm") or which("gnome-terminal"):
                # Fallback to the old method if pkexec is not available.
                full_content += "\n\necho\nread -p 'Press Enter to close...' "
                with open(filename, "w") as f: f.write(full_content)
                
                term = which("xterm") or which("gnome-terminal")
                cmd = f"gnome-terminal -- {filename}" if "gnome-terminal" in term else f"{term} -e {filename}"
                subprocess.Popen(cmd, shell=True)
            else:
                messagebox.showerror("Error", "Cannot run script. Neither pkexec nor a supported terminal (xterm, gnome-terminal) was found.")

        except Exception as e: messagebox.showerror("Error", f"Failed to run script: {e}")

class MainMenu(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=COLORS["bg_main"])
        self.controller = controller
        self.lbl_title = ttk.Label(self, text="", style="SubHeader.TLabel")
        self.lbl_title.pack(pady=(10, 20))
        self._make_menu_btn(self.controller.show_autodarts, "btn_autodarts")
        tk.Label(self, bg=COLORS["bg_main"], height=1).pack() 
        self._make_menu_btn(self.controller.show_autoglow, "btn_autoglow")
        tk.Label(self, bg=COLORS["bg_main"], height=1).pack() 
        self._make_menu_btn(self.controller.show_kiosk, "btn_kiosk")
        tk.Label(self, bg=COLORS["bg_main"], height=1).pack() 
        self._make_menu_btn(self.controller.show_touch, "btn_touch")
        
        # NEU: Update Button
        tk.Label(self, bg=COLORS["bg_main"], height=2).pack() 
        self._make_menu_btn(self.update_suit, "btn_update")
        
        self.update_texts()

    def _make_menu_btn(self, cmd, text_key):
        btn = ttk.Button(self, command=cmd, style="TButton")
        btn.pack(fill="x", ipady=8, padx=40)
        setattr(self, f"_{text_key}", btn) 

    def update_suit(self):
        l = self.controller.lang
        # Das Verzeichnis des aktuellen Skripts (normalerweise Git Repo Root)
        script_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        
        # Prüfen ob Git Repo
        if not os.path.isdir(os.path.join(script_dir, ".git")):
             messagebox.showerror("Update", TEXTS["err_no_git"][l])
             return

        try:
            # git pull ausführen
            proc = subprocess.run(["git", "pull"], cwd=script_dir, capture_output=True, text=True)
            if proc.returncode == 0:
                output = proc.stdout.strip()
                if "Already up to date" in output or "Bereits aktuell" in output:
                     messagebox.showinfo("Update", TEXTS["msg_uptodate"][l])
                else:
                     messagebox.showinfo("Update", TEXTS["msg_updated"][l])
                     # App Neustarten
                     python = sys.executable
                     os.execl(python, python, *sys.argv)
            else:
                 messagebox.showerror("Update", TEXTS["err_update"][l] + "\n" + proc.stderr)
        except Exception as e:
            messagebox.showerror("Update", f"Error: {e}")

    def update_texts(self):
        l = self.controller.lang
        self.lbl_title.config(text=TEXTS["menu_title"][l])
        self._btn_autodarts.config(text=TEXTS["btn_autodarts"][l])
        self._btn_autoglow.config(text=TEXTS["btn_autoglow"][l])
        self._btn_kiosk.config(text=TEXTS["btn_kiosk"][l])
        self._btn_touch.config(text=TEXTS["btn_touch"][l])
        self._btn_update.config(text=TEXTS["btn_update"][l])

class AutoGlowView(tk.Frame, ServiceViewMixin):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=COLORS["bg_main"])
        self.controller = controller
        self.project_dir = os.path.dirname(os.path.abspath(__file__))
        self.config_file = os.path.join(self.project_dir, "autoglow_config.json")
        self.stop_thread = False
        self.check_vars = {}
        self.fx_dropdowns = {}
        self.color_btns = {}
        
        self.status_keys = ["Throw", "Takeout in progress", "Takeout", "Starting", "Stopping", "Calibrating", "Error", "Stopped"]
        self.config = self._load_config()

        ttk.Button(self, text=TEXTS["btn_back"][controller.lang], command=controller.show_menu).pack(anchor="w")
        ttk.Label(self, text=TEXTS["ag_header"][controller.lang], style="SubHeader.TLabel").pack(pady=10)

        # Status Panel (WebSocket & ESP32)
        status_panel = tk.Frame(self, bg=COLORS["bg_panel"], pady=10)
        status_panel.pack(fill="x", pady=5)
        self.lbl_ws = ttk.Label(status_panel, text="WebSocket: ⏳", background=COLORS["bg_panel"])
        self.lbl_ws.pack(side="left", padx=20)
        self.lbl_esp = ttk.Label(status_panel, text="ESP32: ⏳", background=COLORS["bg_panel"])
        self.lbl_esp.pack(side="left", padx=20)

        if WEBSOCKETS_AVAILABLE:
            self._init_websocket_logger()
        else:
            self._init_websocket_placeholder()
            
        bright_frame = tk.Frame(self, bg=COLORS["bg_panel"], pady=10)
        bright_frame.pack(fill="x", padx=20, pady=5)
        tk.Label(bright_frame, text="Global Brightness:", fg="white", bg=COLORS["bg_panel"]).pack(side="left", padx=10)
        
        current_bright = self.config.get("global_brightness", 255)
        self.bright_slider = tk.Scale(bright_frame, from_=0, to=255, orient="horizontal", bg=COLORS["bg_panel"], fg="white", highlightthickness=0)
        self.bright_slider.set(current_bright)
        self.bright_slider.pack(side="right", fill="x", expand=True, padx=10)

        # Scrollbarer Bereich für die Liste
        container = tk.Frame(self, bg=COLORS["bg_main"])
        container.pack(fill="both", expand=True, pady=10)
        canvas = tk.Canvas(container, bg=COLORS["bg_main"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        self.scroll_frame = tk.Frame(canvas, bg=COLORS["bg_main"])

        self.scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw", width=540)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._build_list()

        # Aktions-Buttons
        btn_f = tk.Frame(self, bg=COLORS["bg_main"])
        btn_f.pack(fill="x", pady=10)
        ttk.Button(btn_f, text=TEXTS["ag_install"][controller.lang], command=self.run_install).pack(side="left", expand=True, fill="x", padx=2)
        ttk.Button(btn_f, text=TEXTS["ag_save"][controller.lang], command=self.save_and_restart).pack(side="left", expand=True, fill="x", padx=2)

        threading.Thread(target=self._status_loop, daemon=True).start()

    def _init_websocket_logger(self):
        self.ws_queue = queue.Queue()
        log_frame = tk.Frame(self, bg=COLORS["bg_main"])
        log_frame.pack(fill="x", pady=(5,10))
        ttk.Label(log_frame, text="WebSocket Log:", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.log_widget = tk.Text(log_frame, height=4, bg=COLORS["bg_panel"], fg=COLORS["fg_sub"], relief="sunken", borderwidth=1, font=("Monospace", 9))
        self.log_widget.pack(fill="x", expand=True)
        self.log_widget.insert(tk.END, "Waiting for WebSocket messages...\n")
        self.log_widget.config(state="disabled")

        threading.Thread(target=self._websocket_listener_thread, daemon=True).start()
        self.after(100, self._process_ws_queue)

    def _init_websocket_placeholder(self):
        log_frame = tk.Frame(self, bg=COLORS["bg_main"])
        log_frame.pack(fill="x", pady=(5,10))
        placeholder = ttk.Label(log_frame, text="Install 'websockets' to enable the live log. Use the Install button below.", font=("Segoe UI", 10, "italic"), foreground=COLORS["warning"])
        placeholder.pack(fill="x", expand=True)
        self.lbl_ws.config(text="WebSocket: ⚠️", foreground=COLORS["warning"])

    def _log_message(self, message):
        self.log_widget.config(state="normal")
        self.log_widget.insert(tk.END, message + "\n")
        self.log_widget.see(tk.END)
        self.log_widget.config(state="disabled")

    def _process_ws_queue(self):
        if not WEBSOCKETS_AVAILABLE: return
        try:
            while True:
                message = self.ws_queue.get_nowait()
                if message.startswith("STATUS:"):
                    status = message.split(":")[1]
                    if status == "CONNECTED":
                        self.lbl_ws.config(text="WebSocket: ✅", foreground=COLORS["success"])
                    else: # DISCONNECTED
                        self.lbl_ws.config(text="WebSocket: ❌", foreground=COLORS["danger"])
                else:
                    self._log_message(message)
        except queue.Empty:
            pass
        finally:
            self.after(100, self._process_ws_queue)

    def _websocket_listener_thread(self):
        if not WEBSOCKETS_AVAILABLE: return
        try:
            asyncio.run(self._websocket_listener())
        except Exception as e:
            self.ws_queue.put(f"WebSocket thread error: {e}")

    async def _websocket_listener(self):
        uri = "ws://localhost:3180"
        while not self.stop_thread:
            try:
                async with websockets.connect(uri) as websocket:
                    self.ws_queue.put("STATUS:CONNECTED")
                    self.ws_queue.put("--- WebSocket connected ---")
                    async for message in websocket:
                        self.ws_queue.put(message)
            except (OSError, websockets.exceptions.ConnectionClosed) as e:
                self.ws_queue.put("STATUS:DISCONNECTED")
                self.ws_queue.put(f"--- WebSocket connection error: {e}. Retrying in 5s... ---")
                await asyncio.sleep(5)
            except Exception as e:
                self.ws_queue.put("STATUS:DISCONNECTED")
                self.ws_queue.put(f"--- WebSocket error: {e}. Retrying in 5s... ---")
                await asyncio.sleep(5)

    def _load_config(self):
        if not os.path.exists(self.config_file) or os.path.getsize(self.config_file) == 0:
            return {}
        with open(self.config_file, "r") as f:
            return json.load(f)

    def _build_list(self):
        for status in self.status_keys:
            settings = self.config.get(status, {})
            row = tk.Frame(self.scroll_frame, bg=COLORS["bg_panel"], pady=5)
            row.pack(fill="x", pady=3)

            tk.Label(row, text=status, fg="white", bg=COLORS["bg_panel"], width=15, anchor="w").pack(side="left", padx=10)

            var = tk.BooleanVar(value=settings.get("enabled", True))
            self.check_vars[status] = var
            tk.Checkbutton(row, variable=var, bg=COLORS["bg_panel"], activebackground=COLORS["bg_panel"], selectcolor="#333333").pack(side="right", padx=5)

            tk.Button(row, text="⚡", bg="#444444", fg="white", width=3, command=lambda s=status: self.test_effect(s)).pack(side="right", padx=5)

            col = settings.get("seg", {}).get("col", [[255, 255, 255]])[0]
            hex_color = f'#{col[0]:02x}{col[1]:02x}{col[2]:02x}'
            btn = tk.Button(row, bg=hex_color, width=3)
            btn.config(command=lambda s=status, b=btn: self.pick_color(s, b))
            btn.pack(side="right", padx=5)
            self.color_btns[status] = btn

            fx_id = settings.get("seg", {}).get("fx", 0)
            fx_var = tk.StringVar(value=EFFECT_ID_TO_NAME.get(fx_id, "Solid"))
            self.fx_dropdowns[status] = fx_var
            ttk.Combobox(row, textvariable=fx_var, values=list(WLED_EFFECTS.keys()), width=10, state="readonly").pack(side="right", padx=5)

    def pick_color(self, status, btn):
        color = colorchooser.askcolor(initialcolor=btn.cget("bg"))[1]
        if color: btn.config(bg=color)

    def test_effect(self, status):
        if not SERIAL_AVAILABLE:
            messagebox.showwarning("Serial Port", "pyserial is not installed. Please use the installation button.")
            return

        port = self.find_esp32_port()
        if not port: 
            messagebox.showwarning("Serial Port", "No ESP32 found.")
            return

        fx_name = self.fx_dropdowns[status].get()
        fx_id = WLED_EFFECTS.get(fx_name, 0)
        hex_col = self.color_btns[status].cget("bg").lstrip('#')
        rgb = [int(hex_col[i:i+2], 16) for i in (0, 2, 4)]
        
        brightness = self.bright_slider.get()
        command = {"on": True, "bri": brightness, "seg": {"fx": fx_id, "col": [rgb]}}
        
        try:
            with serial.Serial(port, 115200, timeout=1) as ser:
                time.sleep(1.5)
                ser.write((json.dumps(command) + '\n').encode())
        except Exception as e:
            messagebox.showerror("Error", f"Failed to send command to ESP32: {e}")

    def _status_loop(self):
        while not self.stop_thread:
            # ESP32 Check
            if SERIAL_AVAILABLE:
                esp_label_text = f"ESP32: ❌"
                esp_label_color = COLORS["danger"]
                try:
                    
                    ports = serial.tools.list_ports.comports()
                    esp32_vids = {0x10C4, 0x1A86, 0x303A}
                    
                    found_esp = False
                    for p in ports:
                        if p.vid in esp32_vids:
                            esp_label_text = f"ESP32: ✅ ({p.product or 'ESP32'})"
                            esp_label_color = COLORS["success"]
                            found_esp = True
                            break
                except ImportError:
                    esp_label_text = "ESP32: ⚠️ (pyserial missing)"
                    esp_label_color = COLORS["warning"]
                except Exception:
                    pass

                try:
                    self.lbl_esp.config(text=esp_label_text, foreground=esp_label_color)
                except: break 
            else:
                self.lbl_esp.config(text="ESP32: ⚠️ (pyserial missing)", foreground=COLORS["warning"])
            time.sleep(3)

    def find_esp32_port(self):
        if not SERIAL_AVAILABLE: return None
        KNOWN_VID_PIDS = [(0x10C4, 0xEA60), (0x1A86, 0x7523), (0x0403, 0x6001), (0x303A, 0x1001)]
        ports = serial.tools.list_ports.comports()
        for port in ports:
            if (port.vid, port.pid) in KNOWN_VID_PIDS:
                return port.device
        return None

    def run_install(self):
        py_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        user = os.getlogin()
        
        service_content = f"""[Unit]
Description=AutoGlow
After=network.target

[Service]
User={user}
WorkingDirectory={self.project_dir}
ExecStart={self.project_dir}/venv/bin/python3 {self.project_dir}/autodarts_wled_mini.py
Restart=always

[Install]
WantedBy=multi-user.target"""

        script = f"""
#!/bin/bash
run_installation() {{
    set -e
    echo "--- Starting Full Installation & Service Setup ---"
    
    # Check if we are root. If not, use sudo. If we are root, SUDO is empty.
    if [ "$(id -u)" -ne 0 ]; then
        SUDO="sudo"
        echo "Not running as root. Using 'sudo' for privileged commands."
    else
        SUDO=""
        echo "Running as root. 'sudo' not required for privileged commands."
    fi

    echo "\\n[STEP 1/5] Updating package list..."
    $SUDO apt update -y
    
    echo "\\n[STEP 2/5] Installing system packages (python-venv, git)..."
    $SUDO apt install -y python{py_version}-venv git
    
    echo "\\n[STEP 3/5] Creating Python virtual environment as user '{user}'..."
    if [ "$SUDO" == "sudo" ]; then
        # If we are not root, we are the user, so no need for sudo/runuser
        rm -rf "{self.project_dir}/venv"
        python3 -m venv "{self.project_dir}/venv"
    else
        # If we are root, de-escalate to the user
        $SUDO runuser -u {user} -- rm -rf "{self.project_dir}/venv"
        $SUDO runuser -u {user} -- python3 -m venv "{self.project_dir}/venv"
    fi
    
    echo "\\n[STEP 4/5] Installing Python dependencies (pyserial, websockets)..."
    if [ "$SUDO" == "sudo" ]; then
        "{self.project_dir}/venv/bin/pip" install pyserial websockets
    else
        $SUDO runuser -u {user} -- "{self.project_dir}/venv/bin/pip" install pyserial websockets
    fi
    
    echo "\\n[STEP 5/5] Setting up systemd service..."
    # Using a heredoc with sudo to write the service file securely
    $SUDO bash -c 'cat > /etc/systemd/system/autoglow.service' << EOF
{service_content}
EOF
    
    echo "Reloading systemd, enabling and restarting the service..."
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable autoglow
    $SUDO systemctl restart autoglow
    
    echo "\\n--- Installation successful! ---"
}}

# Execute the installation function and report outcome
if run_installation; then
    echo -e "\\n---> SUCCESS: The installation completed without errors."
else
    echo -e "\\n---> ERROR: An error occurred during the installation. Please check the messages above."
fi
        """
        self._run_bash_script(script)

    def save_and_restart(self):
        new_config = {"global_brightness": self.bright_slider.get()}
        for status, var in self.check_vars.items():
            fx_name = self.fx_dropdowns[status].get()
            hex_col = self.color_btns[status].cget("bg").lstrip('#')
            rgb = [int(hex_col[i:i+2], 16) for i in (0, 2, 4)]
            new_config[status] = {
                "on": True, "bri": 255, "tt": 0, "enabled": var.get(),
                "seg": {"fx": WLED_EFFECTS.get(fx_name, 0), "col": [rgb]}
            }
        with open(self.config_file, "w") as f:
            json.dump(new_config, f, indent=4)

        cmd = self._sudo_cmd("systemctl restart autoglow")
        subprocess.run(cmd, shell=True)
        messagebox.showinfo("SUIT", "Configuration saved and AutoGlow service restarted.")

class AutodartsView(tk.Frame, ServiceViewMixin):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=COLORS["bg_main"])
        self.controller = controller
        self.btn_back = ttk.Button(self, command=controller.show_menu, style="TButton")
        self.btn_back.pack(anchor="w", pady=(0, 10))
        self.header = ttk.Label(self, text="", style="SubHeader.TLabel")
        self.header.pack(pady=(0, 15))
        card_status = ttk.LabelFrame(self, text="Status", style="TLabelframe", padding=15)
        card_status.pack(fill="x", pady=5)
        self.status_lbl = ttk.Label(card_status, text="", style="Card.TLabel", font=("Segoe UI", 12, "bold"))
        self.status_lbl.pack(anchor="center")
        card_ctrl = ttk.LabelFrame(self, text="Control", style="TLabelframe", padding=15)
        card_ctrl.pack(fill="x", pady=5)
        f_btns = tk.Frame(card_ctrl, bg=COLORS["bg_panel"])
        f_btns.pack(fill="x")
        f_btns.columnconfigure((0,1,2), weight=1)
        self.btn_start = ttk.Button(f_btns, command=lambda: self._run_service_cmd("autodarts", "start", self.status_lbl), style="Accent.TButton")
        self.btn_start.grid(row=0, column=0, padx=5, sticky="ew")
        self.btn_stop = ttk.Button(f_btns, command=lambda: self._run_service_cmd("autodarts", "stop", self.status_lbl))
        self.btn_stop.grid(row=0, column=1, padx=5, sticky="ew")
        self.btn_restart = ttk.Button(f_btns, command=lambda: self._run_service_cmd("autodarts", "restart", self.status_lbl))
        self.btn_restart.grid(row=0, column=2, padx=5, sticky="ew")
        card_inst = ttk.LabelFrame(self, text="System", style="TLabelframe", padding=15)
        card_inst.pack(fill="x", pady=5)
        self.btn_install = ttk.Button(card_inst, command=self.do_install)
        self.btn_install.pack(fill="x", pady=2)
        self.btn_uninstall = ttk.Button(card_inst, command=self.do_uninstall, style="Danger.TButton")
        self.btn_uninstall.pack(fill="x", pady=2)
        f_links = tk.Frame(self, bg=COLORS["bg_main"])
        f_links.pack(fill="x", pady=15)
        self.btn_play = tk.Button(f_links, text="🎯 Play (Web)", bg=COLORS["success"], fg="white", font=("Segoe UI", 10, "bold"), relief="flat", padx=10, pady=8, command=lambda: webbrowser.open("https://play.autodarts.io/"))
        self.btn_play.pack(side="left", expand=True, fill="x", padx=(0, 5))
        self.btn_conf = tk.Button(f_links, text="⚙️ Config", bg=COLORS["accent"], fg="white", font=("Segoe UI", 10, "bold"), relief="flat", padx=10, pady=8, command=lambda: webbrowser.open("http://localhost:3180/config"))
        self.btn_conf.pack(side="left", expand=True, fill="x", padx=(5, 0))
        self.hint = ttk.Label(self, style="Hint.TLabel")
        self.hint.pack()
        self.update_texts()
        self._check_status_generic("autodarts", self.status_lbl)

    def update_texts(self):
        l = self.controller.lang
        self.btn_back.config(text=TEXTS["btn_back"][l]); self.header.config(text=TEXTS["ad_header"][l])
        self.btn_start.config(text=TEXTS["btn_start"][l]); self.btn_stop.config(text=TEXTS["btn_stop"][l])
        self.btn_restart.config(text=TEXTS["btn_restart"][l]); self.btn_install.config(text=TEXTS["btn_install"][l])
        self.btn_uninstall.config(text=TEXTS["btn_uninstall"][l]); self.hint.config(text=TEXTS["hint"][l])
        self._check_status_generic("autodarts", self.status_lbl)

    def do_install(self):
        cmd = "bash -c 'bash <(curl -sL get.autodarts.io); read -p \"Press Enter to continue...\"'"
        try:
            if which("pkexec"):
                subprocess.Popen(f"pkexec {cmd}", shell=True)
            elif which("xterm"):
                subprocess.Popen(f"xterm -e \"{cmd}\"", shell=True)
            else:
                messagebox.showerror("Error", "Neither pkexec nor xterm found.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to run install script: {e}")
        self.after(5000, lambda: self._check_status_generic("autodarts", self.status_lbl))

    def do_uninstall(self):
        if not messagebox.askyesno("SUIT", "Delete Autodarts?"): return
        self._run_bash_script("sudo systemctl stop autodarts\nsudo systemctl disable autodarts\nsudo rm /etc/systemd/system/autodarts.service\nsudo systemctl daemon-reload\nsudo rm -rf /usr/bin/autodarts /opt/autodarts")
        self.after(2000, lambda: self._check_status_generic("autodarts", self.status_lbl))

class KioskView(tk.Frame, ServiceViewMixin):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=COLORS["bg_main"])
        self.controller = controller
        self.autostart_dir = os.path.expanduser("~/.config/autostart")
        self.file_path = os.path.join(self.autostart_dir, "autodarts-kiosk.desktop")
        self.script_path = os.path.expanduser("~/.config/autostart/shutdown_kiosk.py")
        
        self.btn_back = ttk.Button(self, command=controller.show_menu, style="TButton")
        self.btn_back.pack(anchor="w", pady=(0, 10))
        self.header = ttk.Label(self, text="", style="SubHeader.TLabel")
        self.header.pack(pady=(0, 15))
        
        card_status = ttk.LabelFrame(self, text="Status", style="TLabelframe", padding=15)
        card_status.pack(fill="x", pady=5)
        self.status_lbl = ttk.Label(card_status, text="", style="Card.TLabel", font=("Segoe UI", 12, "bold"))
        self.status_lbl.pack(anchor="center")
        
        card_conf = ttk.LabelFrame(self, text="Konfiguration", style="TLabelframe", padding=15)
        card_conf.pack(fill="x", pady=5)
        
        self.lbl_url = ttk.Label(card_conf, text="", style="Card.TLabel")
        self.lbl_url.pack(anchor="w", pady=(0, 2))
        self.ent_url = tk.Entry(card_conf, bg="#3e3e42", fg="white", insertbackground="white", borderwidth=0)
        self.ent_url.pack(fill="x", pady=(0, 10), ipady=5)
        self.ent_url.insert(0, "https://play.autodarts.io/")

        self.var_emergency = tk.BooleanVar(value=True)
        self.check_emergency = tk.Checkbutton(card_conf, variable=self.var_emergency, bg=COLORS["bg_panel"], fg=COLORS["fg_text"], selectcolor="black", activebackground=COLORS["bg_panel"], activeforeground=COLORS["fg_text"])
        self.check_emergency.pack(anchor="w", pady=2)
        
        self.lbl_emergency_desc = ttk.Label(card_conf, text="", style="Hint.TLabel", justify="left")
        self.lbl_emergency_desc.pack(anchor="w", padx=25, pady=(0, 10))

        self.btn_enable = ttk.Button(card_conf, command=self.enable_kiosk, style="Accent.TButton")
        self.btn_enable.pack(fill="x", pady=5)
        self.btn_disable = ttk.Button(card_conf, command=self.disable_kiosk)
        self.btn_disable.pack(fill="x", pady=5)
        
        self.lbl_hint = ttk.Label(self, style="Hint.TLabel", justify="center")
        self.lbl_hint.pack(pady=20)
        
        self.update_texts()
        self.check_status()

    def update_texts(self):
        l = self.controller.lang
        self.btn_back.config(text=TEXTS["btn_back"][l]); self.header.config(text=TEXTS["kiosk_header"][l])
        self.lbl_url.config(text=TEXTS["kiosk_url_lbl"][l])
        self.btn_enable.config(text=TEXTS["btn_enable_kiosk"][l]); self.btn_disable.config(text=TEXTS["btn_disable_kiosk"][l])
        self.lbl_hint.config(text=TEXTS["kiosk_hint"][l])
        self.check_emergency.config(text=TEXTS["kiosk_emergency_exit"][l])
        self.lbl_emergency_desc.config(text=TEXTS["kiosk_emergency_desc"][l])
        self.check_status()

    def check_status(self):
        l = self.controller.lang
        active = os.path.exists(self.file_path)
        self.status_lbl.config(text=TEXTS["kiosk_active"][l] if active else TEXTS["kiosk_inactive"][l], 
                               foreground=COLORS["success"] if active else COLORS["danger"])

    def _save_shutdown_script(self):
        code = """import subprocess
import time
import os

KLICK_LIMIT = 3
ZEITFENSTER = 3
klick_zeiten = []

def beende_kiosk():
    subprocess.run(["pkill", "firefox"])

process = subprocess.Popen(['journalctl', '-u', 'systemd-logind', '-f', '-n', '0'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
for line in process.stdout:
    if "Power key pressed" in line:
        jetzt = time.time()
        klick_zeiten.append(jetzt)
        klick_zeiten = [t for t in klick_zeiten if jetzt - t < ZEITFENSTER]
        if len(klick_zeiten) >= KLICK_LIMIT:
            beende_kiosk()
            klick_zeiten = []"""
        with open(self.script_path, "w") as f: f.write(code)
        os.chmod(self.script_path, 0o755)

    def enable_kiosk(self):
        if not os.path.exists(self.autostart_dir): os.makedirs(self.autostart_dir)
        target_url = self.ent_url.get().strip()
        exec_cmd = f'bash -c "sleep 5; MOZ_ENABLE_WAYLAND=1 firefox --kiosk {target_url}"'
        if self.var_emergency.get():
            self._save_shutdown_script()
            exec_cmd = f'bash -c "python3 {self.script_path} & sleep 5; MOZ_ENABLE_WAYLAND=1 firefox --kiosk {target_url}"'
        content = f"[Desktop Entry]\nType=Application\nName=Autodarts Kiosk\nExec={exec_cmd}\nX-GNOME-Autostart-enabled=true\n"
        with open(self.file_path, "w") as f: f.write(content)
        self.check_status()
        messagebox.showinfo("SUIT", TEXTS["msg_applied"][self.controller.lang])

    def disable_kiosk(self):
        if os.path.exists(self.file_path): os.remove(self.file_path)
        if os.path.exists(self.script_path): os.remove(self.script_path)
        self.check_status()
        messagebox.showinfo("SUIT", TEXTS["msg_applied"][self.controller.lang])

class TouchRotationView(tk.Frame, ServiceViewMixin):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=COLORS["bg_main"])
        self.controller = controller
        self.btn_back = ttk.Button(self, command=controller.show_menu)
        self.btn_back.pack(anchor="w", pady=(0, 10))
        self.header = ttk.Label(self, text="", style="SubHeader.TLabel")
        self.header.pack(pady=(0, 20))
        card = ttk.Frame(self, style="Card.TFrame", padding=20)
        card.pack(fill="x", padx=10)
        self.lbl_dir = ttk.Label(card, text="", style="Card.TLabel")
        self.lbl_dir.pack(anchor="w", pady=(0, 10))
        self.var_choice = tk.StringVar(value="normal")
        def create_rb(val): return tk.Radiobutton(card, variable=self.var_choice, value=val, bg=COLORS["bg_panel"], fg=COLORS["fg_text"], selectcolor="black", font=("Segoe UI", 11), highlightthickness=0)
        
        self.rb_norm = create_rb("normal"); self.rb_norm.pack(anchor="w", pady=5)
        self.rb_left = create_rb("left"); self.rb_left.pack(anchor="w", pady=5)
        self.rb_right = create_rb("right"); self.rb_right.pack(anchor="w", pady=5)
        
        # Abstands-Label
        tk.Label(card, bg=COLORS["bg_panel"], height=1).pack()

        # OSK Schalter (Im Rotation Menü)
        self.var_osk = tk.BooleanVar()
        self.check_osk = tk.Checkbutton(card, variable=self.var_osk, command=self.toggle_osk,
                                        bg=COLORS["bg_panel"], fg=COLORS["fg_text"], selectcolor="black",
                                        activebackground=COLORS["bg_panel"], activeforeground=COLORS["fg_text"],
                                        font=("Segoe UI", 11), highlightthickness=0)
        self.check_osk.pack(anchor="w", pady=5)

        self.btn_apply = ttk.Button(self, command=self.apply, style="Accent.TButton")
        self.btn_apply.pack(fill="x", pady=(20, 5), padx=10)
        self.update_texts()
        self.check_osk_status()

    def update_texts(self):
        l = self.controller.lang
        self.btn_back.config(text=TEXTS["btn_back"][l]); self.header.config(text=TEXTS["touch_header"][l])
        self.lbl_dir.config(text=TEXTS["lbl_direction"][l])
        self.rb_norm.config(text=TEXTS["rot_normal"][l])
        self.rb_left.config(text=TEXTS["rot_left"][l])
        self.rb_right.config(text=TEXTS["rot_right"][l])
        self.check_osk.config(text=TEXTS["lbl_osk"][l])
        self.btn_apply.config(text=TEXTS["btn_apply"][l])

    def check_osk_status(self):
        try:
            res = subprocess.check_output(["gsettings", "get", "org.gnome.desktop.a11y.applications", "screen-keyboard-enabled"], text=True).strip()
            self.var_osk.set(res == 'true')
        except:
            self.var_osk.set(False)

    def toggle_osk(self):
        state = "true" if self.var_osk.get() else "false"
        subprocess.run(["gsettings", "set", "org.gnome.desktop.a11y.applications", "screen-keyboard-enabled", state])

    def apply(self):
        rot_mode = self.var_choice.get()
        l = self.controller.lang
        
        # 1. Autostart Datei schreiben (SUIT selbst wird beim Start aufgerufen)
        try:
            os.makedirs(AUTOSTART_DIR, exist_ok=True)
            this_script = os.path.abspath(sys.argv[0])
            command = f"python3 '{this_script}' --output {DISPLAY_NAME_DEFAULT} --rotate {rot_mode}"
            desktop_content = f"[Desktop Entry]\nType=Application\nExec={command}\nName=Screen Rotation\nX-GNOME-Autostart-enabled=true\n"
            with open(DESKTOP_FILE_PATH, "w") as f: f.write(desktop_content)
        except Exception as e:
            messagebox.showerror("Error", f"Autostart Error: {e}")
            return

        # 2. Touch-Matrix schreiben (Udev Regel)
        try:
            touch_name = find_touchscreen_name()
            if touch_name:
                matrix = MATRICES.get(rot_mode, MATRICES["normal"])
                # FIX: Udev Content sicher per Python schreiben, nicht via Shell echo
                udev_content = f'ACTION=="add|change", ENV{{ID_INPUT_TOUCHSCREEN}}=="1", ATTRS{{name}}=="{touch_name}", ENV{{LIBINPUT_CALIBRATION_MATRIX}}="{matrix}"\n'
                
                # Datei lokal erstellen (Nutzer hat Schreibrechte in /tmp)
                with open(TEMP_RULE_PATH, "w") as f:
                    f.write(udev_content)
                
                # Datei mit root Rechten verschieben und udev neu laden
                cmd = f"cp {TEMP_RULE_PATH} {UDEV_RULE_FINAL_PATH} && udevadm control --reload-rules && udevadm trigger --subsystem-match=input"
                full_cmd = self._sudo_cmd(cmd)
                
                # Wir führen es aus
                subprocess.run(full_cmd, shell=True)
                messagebox.showinfo("SUIT", TEXTS["msg_applied"][l])
            else:
                messagebox.showwarning("SUIT", "Touchscreen not found (only screen rotation saved).")
        except Exception as e:
            messagebox.showerror("Error", f"Touch Error: {e}")

if __name__ == "__main__":
    args = sys.argv[1:]
    
    # CLI Modus (für Autostart Rotation)
    if "--rotate" in args:
        try:
            out_idx = args.index("--output") + 1
            rot_idx = args.index("--rotate") + 1
            arg_output = args[out_idx]
            arg_rotate = args[rot_idx]
            apply_gnome_rotation(arg_output, arg_rotate)
        except Exception:
            pass
    else:
        # GUI Modus
        app = SuitApp()
        app.mainloop()
