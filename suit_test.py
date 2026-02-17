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
    "ag_header": {"de": "AutoGlow Manager", "en": "AutoGlow Manager"},
    "ag_install": {"de": "AutoGlow Installieren / Updaten", "en": "Install / Update AutoGlow"},
    "ag_config": {"de": "Konfiguration öffnen (GUI)", "en": "Open Configuration (GUI)"},
    "ag_config_hint": {"de": "Hinweis: Dienst muss gestoppt sein!", "en": "Note: Service must be stopped!"},
    "kiosk_header": {"de": "Kiosk Modus", "en": "Kiosk Mode"},
    "kiosk_browser_lbl": {"de": "Browser wählen:", "en": "Select Browser:"},
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
    "kiosk_hint": {"de": "Startet den Browser im Wayland Kiosk Modus.\nVerhindert Session Popups & Black Screen.", 
                   "en": "Starts the browser in Wayland Kiosk Mode.\nPrevents session popups & black screen."},
    "kiosk_url_lbl": {"de": "Kiosk URL:", "en": "Kiosk URL:"},
    "kiosk_emergency_exit": {"de": "Power-Button Not-Aus aktivieren", "en": "Enable Power-Button Emergency Exit"},
    "kiosk_wait_internet": {"de": "Auf Internetverbindung warten (blackscreen fix)", "en": "Wait for internet connection (blackscreen fix)"},
    "lbl_osk": {"de": "Bildschirmtastatur (OSK) aktivieren", "en": "Enable On-Screen Keyboard"},
    "kiosk_emergency_desc": {
        "de": "Drücke den Power-Button 3x innerhalb von 3 Sekunden,\num den Kiosk sofort zu beenden.", 
        "en": "Press the power button 3 times within 3 seconds\nto immediately close the kiosk."
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
    "msg_updated": {"de": "Update wird gestartet! Die Anwendung wird kurz geschlossen.", "en": "Starting update! The application will close briefly."},
    "err_update": {"de": "Fehler beim Update:\n", "en": "Update Error:\n"},
    "err_no_git": {"de": "Kein Git-Repository gefunden.\nBitte 'git clone' nutzen.", "en": "No Git repository found.\nPlease use 'git clone'."},
    "ag_stop_first": {"de": "Bitte stoppe zuerst den AutoGlow Dienst!", "en": "Please stop the AutoGlow service first!"},
    "ag_not_found": {"de": "AutoGlow Ordner nicht gefunden. Bitte erst installieren.", "en": "AutoGlow folder not found. Please install first."}
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
        full_content = "#!/bin/bash\n" + bash_content
        try:
            with open(filename, "w") as f: f.write(full_content)
            os.chmod(filename, 0o755)

            if which("pkexec"):
                cmd = f"pkexec bash {filename}"
                subprocess.Popen(cmd, shell=True)
                messagebox.showinfo("In Progress", "The script is running in the background. You may be prompted for your password.")
            elif which("xterm") or which("gnome-terminal"):
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
        tk.Label(self, bg=COLORS["bg_main"], height=2).pack() 
        self._make_menu_btn(self.update_suit, "btn_update")
        self.update_texts()

    def _make_menu_btn(self, cmd, text_key):
        btn = ttk.Button(self, command=cmd, style="TButton")
        btn.pack(fill="x", ipady=8, padx=40)
        setattr(self, f"_{text_key}", btn) 

    def update_suit(self):
        l = self.controller.lang
        script_dir = os.path.dirname(os.path.abspath(__file__))
        main_script = os.path.join(script_dir, "main.py")

        if not os.path.exists(main_script):
            messagebox.showerror("Update", "main.py nicht gefunden!")
            return

        messagebox.showinfo("Update", TEXTS["msg_updated"][l])
        
        python = sys.executable
        os.execl(python, python, main_script)

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
        self.autoglow_dir = os.path.join(self.project_dir, "AutoGlow")
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
        self.btn_start = ttk.Button(f_btns, command=lambda: self._run_service_cmd("autoglow", "start", self.status_lbl), style="Accent.TButton")
        self.btn_start.grid(row=0, column=0, padx=5, sticky="ew")
        self.btn_stop = ttk.Button(f_btns, command=lambda: self._run_service_cmd("autoglow", "stop", self.status_lbl))
        self.btn_stop.grid(row=0, column=1, padx=5, sticky="ew")
        self.btn_restart = ttk.Button(f_btns, command=lambda: self._run_service_cmd("autoglow", "restart", self.status_lbl))
        self.btn_restart.grid(row=0, column=2, padx=5, sticky="ew")
        card_conf = ttk.LabelFrame(self, text="Settings", style="TLabelframe", padding=15)
        card_conf.pack(fill="x", pady=5)
        self.btn_config = ttk.Button(card_conf, command=self.open_config_gui)
        self.btn_config.pack(fill="x", pady=2)
        self.lbl_conf_hint = ttk.Label(card_conf, style="Hint.TLabel", justify="center")
        self.lbl_conf_hint.pack(pady=(5,0))
        card_inst = ttk.LabelFrame(self, text="Installation", style="TLabelframe", padding=15)
        card_inst.pack(fill="x", pady=5)
        self.btn_install = ttk.Button(card_inst, command=self.run_install)
        self.btn_install.pack(fill="x", pady=2)
        self.update_texts()
        self._check_status_generic("autoglow", self.status_lbl)

    def update_texts(self):
        l = self.controller.lang
        self.btn_back.config(text=TEXTS["btn_back"][l]); self.header.config(text=TEXTS["ag_header"][l])
        self.btn_start.config(text=TEXTS["btn_start"][l]); self.btn_stop.config(text=TEXTS["btn_stop"][l])
        self.btn_restart.config(text=TEXTS["btn_restart"][l]); self.btn_install.config(text=TEXTS["ag_install"][l])
        self.btn_config.config(text=TEXTS["ag_config"][l])
        self.lbl_conf_hint.config(text=TEXTS["ag_config_hint"][l])
        self._check_status_generic("autoglow", self.status_lbl)

    def run_install(self):
        current_user = os.getenv("USER") or os.getlogin()
        script = f"""cd "{self.project_dir}"\nif [ -d "AutoGlow" ]; then\n    echo "Entferne alte AutoGlow Version..."\n    rm -rf AutoGlow\nfi\necho "Clone Repository..."\ngit clone https://github.com/IteraThor/AutoGlow.git\nchown -R "{current_user}":"{current_user}" AutoGlow\ncd AutoGlow\necho "Starte Setup..."\nexport SUDO_USER="{current_user}"\nchmod +x setup.sh\n./setup.sh"""
        self._run_bash_script(script)
        self.after(5000, lambda: self._check_status_generic("autoglow", self.status_lbl))

    def open_config_gui(self):
        l = self.controller.lang
        try:
            res = subprocess.run(["systemctl", "is-active", "autoglow"], capture_output=True, text=True)
            if res.stdout.strip() == "active":
                messagebox.showwarning("SUIT", TEXTS["ag_stop_first"][l])
                return
        except: pass
        gui_script = os.path.join(self.autoglow_dir, "settings_gui.py")
        venv_python = os.path.join(self.autoglow_dir, "venv", "bin", "python3")
        if not os.path.exists(gui_script):
            messagebox.showerror("SUIT", TEXTS["ag_not_found"][l])
            return
        if not os.path.exists(venv_python): venv_python = "python3"
        display = os.environ.get("DISPLAY", ":0")
        xauth = os.environ.get("XAUTHORITY", "")
        try: subprocess.run(["xhost", "+si:localuser:root"], check=False)
        except: pass
        cmd = ["pkexec", "env", f"DISPLAY={display}", f"XAUTHORITY={xauth}", venv_python, gui_script] if which("pkexec") else ["sudo", "env", f"DISPLAY={display}", f"XAUTHORITY={xauth}", venv_python, gui_script]
        try: subprocess.Popen(cmd, cwd=self.autoglow_dir)
        except Exception as e: messagebox.showerror("Error", f"Failed to open GUI: {e}")

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
            if which("pkexec"): subprocess.Popen(f"pkexec {cmd}", shell=True)
            elif which("xterm"): subprocess.Popen(f"xterm -e \"{cmd}\"", shell=True)
            else: messagebox.showerror("Error", "Neither pkexec nor xterm found.")
        except Exception as e: messagebox.showerror("Error", f"Failed to run install script: {e}")
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
        
        # --- BROWSER AUSWAHL ---
        self.lbl_browser = ttk.Label(card_conf, text="", style="Card.TLabel")
        self.lbl_browser.pack(anchor="w", pady=(0, 2))
        self.var_browser = tk.StringVar(value="Firefox")
        self.combo_browser = ttk.Combobox(card_conf, textvariable=self.var_browser, values=["Firefox", "Chromium"], state="readonly", font=("Segoe UI", 10))
        self.combo_browser.pack(fill="x", pady=(0, 10))
        
        # --- URL ---
        self.lbl_url = ttk.Label(card_conf, text="", style="Card.TLabel")
        self.lbl_url.pack(anchor="w", pady=(0, 2))
        self.ent_url = tk.Entry(card_conf, bg="#3e3e42", fg="white", insertbackground="white", borderwidth=0)
        self.ent_url.pack(fill="x", pady=(0, 10), ipady=5)
        self.ent_url.insert(0, "https://play.autodarts.io/")
        
        # --- CHECKBOXES ---
        self.var_emergency = tk.BooleanVar(value=True)
        self.check_emergency = tk.Checkbutton(card_conf, variable=self.var_emergency, bg=COLORS["bg_panel"], fg=COLORS["fg_text"], selectcolor="black", activebackground=COLORS["bg_panel"], activeforeground=COLORS["fg_text"])
        self.check_emergency.pack(anchor="w", pady=2)
        
        self.lbl_emergency_desc = ttk.Label(card_conf, text="", style="Hint.TLabel", justify="left")
        self.lbl_emergency_desc.pack(anchor="w", padx=25, pady=(0, 10))
        
        self.var_wait_internet = tk.BooleanVar(value=True)
        self.check_wait = tk.Checkbutton(card_conf, variable=self.var_wait_internet, bg=COLORS["bg_panel"], fg=COLORS["fg_text"], selectcolor="black", activebackground=COLORS["bg_panel"], activeforeground=COLORS["fg_text"])
        self.check_wait.pack(anchor="w", pady=(0, 10))
        
        # --- BUTTONS ---
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
        self.btn_back.config(text=TEXTS["btn_back"][l])
        self.header.config(text=TEXTS["kiosk_header"][l])
        self.lbl_browser.config(text=TEXTS["kiosk_browser_lbl"][l])
        self.lbl_url.config(text=TEXTS["kiosk_url_lbl"][l])
        self.btn_enable.config(text=TEXTS["btn_enable_kiosk"][l])
        self.btn_disable.config(text=TEXTS["btn_disable_kiosk"][l])
        self.lbl_hint.config(text=TEXTS["kiosk_hint"][l])
        self.check_emergency.config(text=TEXTS["kiosk_emergency_exit"][l])
        self.lbl_emergency_desc.config(text=TEXTS["kiosk_emergency_desc"][l])
        self.check_wait.config(text=TEXTS["kiosk_wait_internet"][l])
        self.check_status()

    def check_status(self):
        l = self.controller.lang
        active = os.path.exists(self.file_path)
        self.status_lbl.config(text=TEXTS["kiosk_active"][l] if active else TEXTS["kiosk_inactive"][l], foreground=COLORS["success"] if active else COLORS["danger"])

    def _save_shutdown_script(self, browser_process_name):
        code = f"""import subprocess
import time
import os
KLICK_LIMIT = 3
ZEITFENSTER = 3
klick_zeiten = []
def beende_kiosk():
    subprocess.run(["pkill", "{browser_process_name}"])
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
        
        browser_choice = self.var_browser.get()
        target_url = self.ent_url.get().strip()
        wait_cmd = 'until curl -s --head --request GET http://www.google.com | grep "200 OK" > /dev/null; do sleep 2; done; ' if self.var_wait_internet.get() else ""
        
        if browser_choice == "Firefox":
            browser_cmd = f"MOZ_ENABLE_WAYLAND=1 firefox --kiosk {target_url}"
            process_name = "firefox"
        else:
            # Chromium mit Wayland Flags und Unterdrückung von Info-Bars
            browser_cmd = f"chromium-browser --kiosk --no-errdialogs --disable-infobars --no-first-run --enable-features=UseOzonePlatform --ozone-platform=wayland {target_url}"
            process_name = "chromium"

        # Not-Aus-Skript mit dem richtigen Browser-Namen generieren, falls aktiviert
        if self.var_emergency.get():
            self._save_shutdown_script(process_name)
            exec_cmd = f'bash -c "python3 {self.script_path} & sleep 5; {wait_cmd}{browser_cmd}"'
        else:
            exec_cmd = f'bash -c "sleep 5; {wait_cmd}{browser_cmd}"'
            
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
        self.button_header = ttk.Label(self, text="", style="SubHeader.TLabel")
        self.button_header.pack(pady=(0, 20))
        card = ttk.Frame(self, style="Card.TFrame", padding=20)
        card.pack(fill="x", padx=10)
        self.lbl_dir = ttk.Label(card, text="", style="Card.TLabel")
        self.lbl_dir.pack(anchor="w", pady=(0, 10))
        self.var_choice = tk.StringVar(value="normal")
        def create_rb(val): return tk.Radiobutton(card, variable=self.var_choice, value=val, bg=COLORS["bg_panel"], fg=COLORS["fg_text"], selectcolor="black", font=("Segoe UI", 11), highlightthickness=0)
        self.rb_norm = create_rb("normal"); self.rb_norm.pack(anchor="w", pady=5)
        self.rb_left = create_rb("left"); self.rb_left.pack(anchor="w", pady=5)
        self.rb_right = create_rb("right"); self.rb_right.pack(anchor="w", pady=5)
        tk.Label(card, bg=COLORS["bg_panel"], height=1).pack()
        self.var_osk = tk.BooleanVar()
        self.check_osk = tk.Checkbutton(card, variable=self.var_osk, command=self.toggle_osk, bg=COLORS["bg_panel"], fg=COLORS["fg_text"], selectcolor="black", activebackground=COLORS["bg_panel"], activeforeground=COLORS["fg_text"], font=("Segoe UI", 11), highlightthickness=0)
        self.check_osk.pack(anchor="w", pady=5)
        self.btn_apply = ttk.Button(self, command=self.apply, style="Accent.TButton")
        self.btn_apply.pack(fill="x", pady=(20, 5), padx=10)
        self.update_texts()
        self.check_osk_status()

    def update_texts(self):
        l = self.controller.lang
        self.btn_back.config(text=TEXTS["btn_back"][l]); self.button_header.config(text=TEXTS["touch_header"][l])
        self.lbl_dir.config(text=TEXTS["lbl_direction"][l])
        self.rb_norm.config(text=TEXTS["rot_normal"][l]); self.rb_left.config(text=TEXTS["rot_left"][l]); self.rb_right.config(text=TEXTS["rot_right"][l])
        self.check_osk.config(text=TEXTS["lbl_osk"][l]); self.btn_apply.config(text=TEXTS["btn_apply"][l])

    def check_osk_status(self):
        try:
            res = subprocess.check_output(["gsettings", "get", "org.gnome.desktop.a11y.applications", "screen-keyboard-enabled"], text=True).strip()
            self.var_osk.set(res == 'true')
        except: self.var_osk.set(False)

    def toggle_osk(self):
        state = "true" if self.var_osk.get() else "false"
        subprocess.run(["gsettings", "set", "org.gnome.desktop.a11y.applications", "screen-keyboard-enabled", state])

    def apply(self):
        rot_mode = self.var_choice.get()
        l = self.controller.lang
        try:
            os.makedirs(AUTOSTART_DIR, exist_ok=True)
            this_script = os.path.abspath(sys.argv[0])
            command = f"python3 '{this_script}' --output {DISPLAY_NAME_DEFAULT} --rotate {rot_mode}"
            desktop_content = f"[Desktop Entry]\nType=Application\nExec={command}\nName=Screen Rotation\nX-GNOME-Autostart-enabled=true\n"
            with open(DESKTOP_FILE_PATH, "w") as f: f.write(desktop_content)
        except Exception as e:
            messagebox.showerror("Error", f"Autostart Error: {e}")
            return
        try:
            touch_name = find_touchscreen_name()
            if touch_name:
                matrix = MATRICES.get(rot_mode, MATRICES["normal"])
                udev_content = f'ACTION=="add|change", ENV{{ID_INPUT_TOUCHSCREEN}}=="1", ATTRS{{name}}=="{touch_name}", ENV{{LIBINPUT_CALIBRATION_MATRIX}}="{matrix}"\n'
                with open(TEMP_RULE_PATH, "w") as f: f.write(udev_content)
                cmd = f"cp {TEMP_RULE_PATH} {UDEV_RULE_FINAL_PATH} && udevadm control --reload-rules && udevadm trigger --subsystem-match=input"
                subprocess.run(self._sudo_cmd(cmd), shell=True)
                messagebox.showinfo("SUIT", TEXTS["msg_applied"][l])
            else: messagebox.showwarning("SUIT", "Touchscreen not found (only screen rotation saved).")
        except Exception as e: messagebox.showerror("Error", f"Touch Error: {e}")

if __name__ == "__main__":
    args = sys.argv[1:]
    if "--rotate" in args:
        try:
            out_idx = args.index("--output") + 1
            rot_idx = args.index("--rotate") + 1
            apply_gnome_rotation(args[out_idx], args[rot_idx])
        except: pass
    else:
        SuitApp().mainloop()
