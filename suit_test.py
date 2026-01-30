import tkinter as tk
from tkinter import ttk, messagebox
import subprocess
import webbrowser
from shutil import which
import os
import glob
import stat

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
    "btn_touch": {"de": "🔄 Touch Screen Input Rotation", "en": "🔄 Touch Screen Input Rotation"},
    "btn_kiosk": {"de": "🖥️ Setup Kiosk Mode", "en": "🖥️ Setup Kiosk Mode"},
    "btn_back": {"de": "❮ Zurück", "en": "❮ Back"},
    "ad_header": {"de": "Autodarts Verwaltung", "en": "Autodarts Management"},
    "ag_header": {"de": "AutoGlow Verwaltung", "en": "AutoGlow Management"},
    "kiosk_header": {"de": "Kiosk Modus (Firefox)", "en": "Kiosk Mode (Firefox)"},
    "touch_header": {"de": "Touch Screen Input Rotation", "en": "Touch Screen Input Rotation"},
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
    "rot_default": {"de": "Standard (Reset / Löschen)", "en": "Default (Reset / Delete)"},
    "btn_apply": {"de": "Anwenden", "en": "Apply"},
    "btn_reboot": {"de": "System Neustart", "en": "System Reboot"},
    "msg_applied": {"de": "Einstellung angewendet.", "en": "Settings applied."},
    "msg_deleted": {"de": "Regel gelöscht. Zurück auf Standard.", "en": "Rule deleted. Back to default."},
    "err_term": {"de": "Fehler: 'xterm' nicht gefunden.", "en": "Error: 'xterm' not found."},
    "done": {"de": "Fertig.", "en": "Done."}
}

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
    def show_autoglow(self): self._switch(AutoGlowView)
    def show_touch(self): self._switch(TouchRotationView)
    def show_kiosk(self): self._switch(KioskView)
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
        full_content = "#!/bin/bash\n" + bash_content + "\n\necho\nread -p 'Press Enter to close...' "
        try:
            with open(filename, "w") as f: f.write(full_content)
            os.chmod(filename, 0o755)
            term = which("xterm") or which("gnome-terminal")
            cmd = f"gnome-terminal -- {filename}" if "gnome-terminal" in term else f"{term} -e {filename}"
            subprocess.Popen(cmd, shell=True)
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
        self.update_texts()

    def _make_menu_btn(self, cmd, text_key):
        btn = ttk.Button(self, command=cmd, style="TButton")
        btn.pack(fill="x", ipady=8, padx=40)
        setattr(self, f"_{text_key}", btn) 

    def update_texts(self):
        l = self.controller.lang
        self.lbl_title.config(text=TEXTS["menu_title"][l])
        self._btn_autodarts.config(text=TEXTS["btn_autodarts"][l])
        self._btn_autoglow.config(text=TEXTS["btn_autoglow"][l])
        self._btn_kiosk.config(text=TEXTS["btn_kiosk"][l])
        self._btn_touch.config(text=TEXTS["btn_touch"][l])

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
        self._run_bash_script("echo 'Installing Autodarts...'\nbash <(curl -sL http://autodarts.io/install)")
        self.after(5000, lambda: self._check_status_generic("autodarts", self.status_lbl))

    def do_uninstall(self):
        if not messagebox.askyesno("SUIT", "Delete Autodarts?"): return
        self._run_bash_script("sudo systemctl stop autodarts\nsudo systemctl disable autodarts\nsudo rm /etc/systemd/system/autodarts.service\nsudo systemctl daemon-reload\nsudo rm -rf /usr/bin/autodarts /opt/autodarts")
        self.after(2000, lambda: self._check_status_generic("autodarts", self.status_lbl))

class AutoGlowView(tk.Frame, ServiceViewMixin):
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
        self.btn_start = ttk.Button(f_btns, command=lambda: self._run_service_cmd("autoglow", "start", self.status_lbl), style="warning")
        self.btn_start.grid(row=0, column=0, padx=5, sticky="ew")
        self.btn_stop = ttk.Button(f_btns, command=lambda: self._run_service_cmd("autoglow", "stop", self.status_lbl))
        self.btn_stop.grid(row=0, column=1, padx=5, sticky="ew")
        self.btn_restart = ttk.Button(f_btns, command=lambda: self._run_service_cmd("autoglow", "restart", self.status_lbl))
        self.btn_restart.grid(row=0, column=2, padx=5, sticky="ew")
        card_inst = ttk.LabelFrame(self, text="System", style="TLabelframe", padding=15)
        card_inst.pack(fill="x", pady=5)
        self.btn_install = ttk.Button(card_inst, command=self.do_install)
        self.btn_install.pack(fill="x", pady=2)
        self.btn_uninstall = ttk.Button(card_inst, command=self.do_uninstall, style="Danger.TButton")
        self.btn_uninstall.pack(fill="x", pady=2)
        self.update_texts()
        self._check_status_generic("autoglow", self.status_lbl)

    def update_texts(self):
        l = self.controller.lang
        self.btn_back.config(text=TEXTS["btn_back"][l]); self.header.config(text=TEXTS["ag_header"][l])
        self.btn_start.config(text=TEXTS["btn_start"][l]); self.btn_stop.config(text=TEXTS["btn_stop"][l])
        self.btn_restart.config(text=TEXTS["btn_restart"][l]); self.btn_install.config(text=TEXTS["btn_install"][l])
        self.btn_uninstall.config(text=TEXTS["btn_uninstall"][l])
        self._check_status_generic("autoglow", self.status_lbl)

    def do_install(self):
        script = "sudo apt-get update\nsudo apt-get install -y git python3-venv\nTARGET='/opt/AutoGlow'\nif [ ! -d '$TARGET' ]; then sudo git clone https://github.com/IteraThor/AutoGlow.git '$TARGET'; else cd '$TARGET' && sudo git pull; fi\ncd '$TARGET'\nsudo chown -R $USER:$USER '$TARGET'\nsudo chmod +x ./setup.sh\nsudo ./setup.sh"
        self._run_bash_script(script)
        self.after(5000, lambda: self._check_status_generic("autoglow", self.status_lbl))

    def do_uninstall(self):
        if not messagebox.askyesno("SUIT", "Delete AutoGlow?"): return
        self._run_bash_script("sudo systemctl stop autoglow\nsudo systemctl disable autoglow\nsudo rm /etc/systemd/system/autoglow.service\nsudo systemctl daemon-reload\nsudo rm -rf /opt/AutoGlow")
        self.after(2000, lambda: self._check_status_generic("autoglow", self.status_lbl))

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
        self.var_choice = tk.StringVar(value="left")
        def create_rb(val): return tk.Radiobutton(card, variable=self.var_choice, value=val, bg=COLORS["bg_panel"], fg=COLORS["fg_text"], selectcolor="black", font=("Segoe UI", 11), highlightthickness=0)
        self.rb_left = create_rb("left"); self.rb_left.pack(anchor="w", pady=5)
        self.rb_right = create_rb("right"); self.rb_right.pack(anchor="w", pady=5)
        self.rb_def = create_rb("default"); self.rb_def.pack(anchor="w", pady=5)
        self.btn_apply = ttk.Button(self, command=self.apply, style="Accent.TButton")
        self.btn_apply.pack(fill="x", pady=(20, 5), padx=10)
        self.btn_reboot = ttk.Button(self, command=lambda: subprocess.run("pkexec reboot", shell=True))
        self.btn_reboot.pack(fill="x", pady=5, padx=10)
        self.update_texts()

    def update_texts(self):
        l = self.controller.lang
        self.btn_back.config(text=TEXTS["btn_back"][l]); self.header.config(text=TEXTS["touch_header"][l])
        self.lbl_dir.config(text=TEXTS["lbl_direction"][l]); self.rb_left.config(text=TEXTS["rot_left"][l])
        self.rb_right.config(text=TEXTS["rot_right"][l]); self.rb_def.config(text=TEXTS["rot_default"][l])
        self.btn_apply.config(text=TEXTS["btn_apply"][l]); self.btn_reboot.config(text=TEXTS["btn_reboot"][l])

    def apply(self):
        choice, l = self.var_choice.get(), self.controller.lang
        target_path = "/etc/udev/rules.d/99-touch-rotation.rules"
        if choice == "default": cmd = f"rm -f {target_path}"
        else:
            m = "0 1 0 -1 0 1 0 0 1" if choice == "left" else "0 -1 1 1 0 0 0 0 1"
            rule = f'ACTION=="add|change", KERNEL=="event[0-9]*", ATTRS{{idVendor}}=="27c0", ATTRS{{idProduct}}=="0859", ENV{{LIBINPUT_CALIBRATION_MATRIX}}="{m}"\n'
            cmd = f"echo '{rule}' > {target_path}"
        full_cmd = self._sudo_cmd(f"{cmd} && udevadm control --reload-rules && udevadm trigger")
        subprocess.run(full_cmd, shell=True)
        messagebox.showinfo("SUIT", TEXTS["msg_applied"][l])

if __name__ == "__main__":
    app = SuitApp()
    app.mainloop()
