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
    "bg_main": "#1e1e1e",      # Sehr dunkler Hintergrund
    "bg_panel": "#252526",     # Etwas hellerer Container
    "fg_text": "#ffffff",      # Weißer Text
    "fg_sub": "#cccccc",       # Hellgrauer Text
    "accent": "#007acc",       # Blau (VS Code Style)
    "accent_hover": "#0098ff", # Helleres Blau für Hover
    "btn_bg": "#3e3e42",       # Button Dunkelgrau
    "btn_fg": "#ffffff",       # Button Text
    "danger": "#d9534f",       # Rot
    "success": "#28a745",      # Grün
    "warning": "#ff9800",      # Orange
    "select_indicator": "#000000"
}

# --- TEXTE ---
TEXTS = {
    "app_title": {"de": "SUIT - Setup Utilities by IteraThor", "en": "SUIT - Setup Utilities by IteraThor"},
    
    # Menü
    "menu_title": {"de": "Hauptmenü", "en": "Main Menu"},
    "btn_autodarts": {"de": "🎯 Autodarts Manager", "en": "🎯 Autodarts Manager"},
    "btn_autoglow": {"de": "💡 AutoGlow Manager", "en": "💡 AutoGlow Manager"},
    "btn_touch": {"de": "🔄 Touch Screen Input Rotation", "en": "🔄 Touch Screen Input Rotation"},
    "btn_kiosk": {"de": "🖥️ Setup Kiosk Mode", "en": "🖥️ Setup Kiosk Mode"},
    "btn_back": {"de": "❮ Zurück", "en": "❮ Back"},
    
    # Headers
    "ad_header": {"de": "Autodarts Verwaltung", "en": "Autodarts Management"},
    "ag_header": {"de": "AutoGlow Verwaltung", "en": "AutoGlow Management"},
    "kiosk_header": {"de": "Kiosk Modus (Firefox)", "en": "Kiosk Mode (Firefox)"},
    "touch_header": {"de": "Touch Screen Input Rotation", "en": "Touch Screen Input Rotation"},
    
    # Common Actions
    "status_lbl": {"de": "Status:", "en": "Status:"},
    "loading": {"de": "Lade...", "en": "Loading..."},
    "btn_start": {"de": "Starten", "en": "Start"},
    "btn_stop": {"de": "Stoppen", "en": "Stop"},
    "btn_restart": {"de": "Neustarten", "en": "Restart"},
    "btn_install": {"de": "Installieren", "en": "Install"},
    "btn_uninstall": {"de": "Deinstallieren", "en": "Uninstall"},
    "hint": {"de": "Hinweis: Root-Passwort erforderlich.", "en": "Note: Root password required."},
    
    # Kiosk Texts
    "kiosk_active": {"de": "● Aktiv (Autostart an)", "en": "● Active (Autostart on)"},
    "kiosk_inactive": {"de": "● Inaktiv (Autostart aus)", "en": "● Inactive (Autostart off)"},
    "btn_enable_kiosk": {"de": "Einschalten (Autostart)", "en": "Enable (Autostart)"},
    "btn_disable_kiosk": {"de": "Ausschalten", "en": "Disable"},
    "kiosk_hint": {"de": "Startet Firefox (Wayland Mode).\nVerhindert 'Restore Session' Popup & Black Screen.", 
                   "en": "Starts Firefox (Wayland Mode).\nPrevents 'Restore Session' popup & black screen."},

    # Status Messages
    "st_active": {"de": "● Aktiv (Läuft)", "en": "● Active (Running)"},
    "st_inactive": {"de": "● Inaktiv (Gestoppt)", "en": "● Inactive (Stopped)"},
    "st_failed": {"de": "● Fehlgeschlagen", "en": "● Failed"},
    "st_unknown": {"de": "● Unbekannt", "en": "● Unknown"},
    "st_nofile": {"de": "● Nicht installiert", "en": "● Not installed"},

    # Touch Rotation
    "lbl_direction": {"de": "Ausrichtung wählen:", "en": "Select Orientation:"},
    "rot_left": {"de": "Links (90° gegen Uhrzeigersinn)", "en": "Left (90° CCW)"},
    "rot_right": {"de": "Rechts (90° im Uhrzeigersinn)", "en": "Right (90° CW)"},
    "rot_default": {"de": "Standard (Reset / Löschen)", "en": "Default (Reset / Delete)"},
    "btn_apply": {"de": "Anwenden", "en": "Apply"},
    "btn_reboot": {"de": "System Neustart", "en": "System Reboot"},
    "msg_applied": {"de": "Einstellung angewendet.", "en": "Settings applied."},
    "msg_deleted": {"de": "Regel gelöscht. Zurück auf Standard.", "en": "Rule deleted. Back to default."},
    
    # Global
    "err_term": {"de": "Fehler: 'xterm' nicht gefunden.", "en": "Error: 'xterm' not found."},
    "done": {"de": "Fertig.", "en": "Done."}
}

class SuitApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SUIT")
        self.geometry("600x750") 
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

        # General Frames
        style.configure("TFrame", background=COLORS["bg_main"])
        style.configure("Card.TFrame", background=COLORS["bg_panel"], relief="flat")
        
        # Labels
        style.configure("TLabel", background=COLORS["bg_main"], foreground=COLORS["fg_text"], font=("Segoe UI", 11))
        style.configure("Header.TLabel", font=("Segoe UI", 18, "bold"), foreground=COLORS["accent"])
        style.configure("SubHeader.TLabel", font=("Segoe UI", 14, "bold"), foreground=COLORS["fg_text"])
        style.configure("Card.TLabel", background=COLORS["bg_panel"], foreground=COLORS["fg_text"])
        style.configure("Hint.TLabel", foreground=COLORS["fg_sub"], font=("Segoe UI", 9))
        
        # Buttons
        style.configure("TButton", 
                        background=COLORS["btn_bg"], 
                        foreground=COLORS["btn_fg"], 
                        borderwidth=0, 
                        focuscolor=COLORS["bg_main"],
                        font=("Segoe UI", 10, "bold"),
                        padding=8)
        
        style.map("TButton", 
                  background=[("active", COLORS["accent"]), ("pressed", COLORS["accent_hover"])],
                  foreground=[("active", "white")])

        # Custom Button Styles
        style.configure("Accent.TButton", background=COLORS["accent"], foreground="white")
        style.map("Accent.TButton", background=[("active", COLORS["accent_hover"])])

        style.configure("Glow.TButton", background=COLORS["warning"], foreground="white")
        style.map("Glow.TButton", background=[("active", "#e68900")])

        style.configure("Danger.TButton", background=COLORS["danger"], foreground="white")
        style.map("Danger.TButton", background=[("active", "#c9302c")])

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

    def show_menu(self):
        self._switch(MainMenu)

    def show_autodarts(self):
        self._switch(AutodartsView)

    def show_autoglow(self):
        self._switch(AutoGlowView)

    def show_touch(self):
        self._switch(TouchRotationView)

    def show_kiosk(self):
        self._switch(KioskView)

    def _switch(self, frame_class):
        if self.current_frame:
            self.current_frame.destroy()
        self.current_frame = frame_class(self.container, self)
        self.current_frame.pack(fill="both", expand=True)


# --- HELPERS (Mixin) ---
class ServiceViewMixin:
    def _check_status_generic(self, service_name, label_widget):
        l = self.controller.lang
        try:
            res = subprocess.run(["systemctl", "is-active", service_name], capture_output=True, text=True)
            st = res.stdout.strip()
            if st == "active":
                label_widget.config(text=TEXTS["st_active"][l], foreground=COLORS["success"])
            elif st == "inactive":
                label_widget.config(text=TEXTS["st_inactive"][l], foreground=COLORS["danger"])
            elif st == "failed":
                label_widget.config(text=TEXTS["st_failed"][l], foreground=COLORS["danger"])
            else:
                label_widget.config(text=f"{TEXTS['st_unknown'][l]}", foreground="orange")
        except:
            label_widget.config(text=TEXTS["st_nofile"][l], foreground="gray")

    def _run_service_cmd(self, service_name, action, label_widget):
        cmd = self._sudo_cmd(f"systemctl {action} {service_name}")
        subprocess.Popen(cmd, shell=True)
        label_widget.config(text=TEXTS["loading"][self.controller.lang], foreground="orange")
        for t in [1000, 3000, 5000]: 
            self.after(t, lambda: self._check_status_generic(service_name, label_widget))

    def _sudo_cmd(self, shell_cmd):
        if which("pkexec"): return f"pkexec bash -c '{shell_cmd}'"
        return f"xterm -e 'sudo bash -c \"{shell_cmd}\"'"

    def _run_bash_script(self, bash_content):
        """Erstellt eine temporäre Datei und führt sie im Terminal aus (vermeidet Quoting-Fehler)."""
        filename = "/tmp/suit_exec.sh"
        
        # Füge Shebang und 'read' am Ende hinzu, damit das Fenster offen bleibt
        full_content = "#!/bin/bash\n" + bash_content + "\n\necho\nread -p 'Press Enter to close...' "
        
        try:
            with open(filename, "w") as f:
                f.write(full_content)
            
            os.chmod(filename, 0o755)
            
            term = which("xterm") or which("gnome-terminal")
            if not term:
                messagebox.showerror("Error", TEXTS["err_term"][self.controller.lang])
                return

            if "gnome-terminal" in term:
                cmd = f"gnome-terminal -- {filename}"
            else:
                cmd = f"{term} -e {filename}"

            subprocess.Popen(cmd, shell=True)
            
        except Exception as e:
            messagebox.showerror("Error", f"Failed to run script: {e}")


# --- ANSICHT 1: HAUPTMENÜ ---
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


# --- ANSICHT 2: AUTODARTS ---
class AutodartsView(tk.Frame, ServiceViewMixin):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=COLORS["bg_main"])
        self.controller = controller

        self.btn_back = ttk.Button(self, command=controller.show_menu, style="TButton")
        self.btn_back.pack(anchor="w", pady=(0, 10))

        self.header = ttk.Label(self, text="", style="SubHeader.TLabel")
        self.header.pack(pady=(0, 15))

        # Status
        card_status = ttk.LabelFrame(self, text="Status", style="TLabelframe", padding=15)
        card_status.pack(fill="x", pady=5)
        self.status_lbl = ttk.Label(card_status, text="", style="Card.TLabel", font=("Segoe UI", 12, "bold"))
        self.status_lbl.pack(anchor="center")

        # Control
        card_ctrl = ttk.LabelFrame(self, text="Control", style="TLabelframe", padding=15)
        card_ctrl.pack(fill="x", pady=5)
        
        f_btns = tk.Frame(card_ctrl, bg=COLORS["bg_panel"])
        f_btns.pack(fill="x")
        f_btns.columnconfigure(0, weight=1)
        f_btns.columnconfigure(1, weight=1)
        f_btns.columnconfigure(2, weight=1)

        self.btn_start = ttk.Button(f_btns, command=lambda: self._run_service_cmd("autodarts", "start", self.status_lbl), style="Accent.TButton")
        self.btn_start.grid(row=0, column=0, padx=5, sticky="ew")
        self.btn_stop = ttk.Button(f_btns, command=lambda: self._run_service_cmd("autodarts", "stop", self.status_lbl))
        self.btn_stop.grid(row=0, column=1, padx=5, sticky="ew")
        self.btn_restart = ttk.Button(f_btns, command=lambda: self._run_service_cmd("autodarts", "restart", self.status_lbl))
        self.btn_restart.grid(row=0, column=2, padx=5, sticky="ew")

        # Install
        card_inst = ttk.LabelFrame(self, text="System", style="TLabelframe", padding=15)
        card_inst.pack(fill="x", pady=5)
        self.btn_install = ttk.Button(card_inst, command=self.do_install)
        self.btn_install.pack(fill="x", pady=2)
        self.btn_uninstall = ttk.Button(card_inst, command=self.do_uninstall, style="Danger.TButton")
        self.btn_uninstall.pack(fill="x", pady=2)

        # Links
        f_links = tk.Frame(self, bg=COLORS["bg_main"])
        f_links.pack(fill="x", pady=15)
        self.btn_play = tk.Button(f_links, text="🎯 Play (Web)", bg=COLORS["success"], fg="white", 
                                  font=("Segoe UI", 10, "bold"), relief="flat", padx=10, pady=8,
                                  command=lambda: webbrowser.open("https://play.autodarts.io/"))
        self.btn_play.pack(side="left", expand=True, fill="x", padx=(0, 5))

        self.btn_conf = tk.Button(f_links, text="⚙️ Config", bg=COLORS["accent"], fg="white", 
                                  font=("Segoe UI", 10, "bold"), relief="flat", padx=10, pady=8,
                                  command=lambda: webbrowser.open("http://localhost:3180/config"))
        self.btn_conf.pack(side="left", expand=True, fill="x", padx=(5, 0))

        self.hint = ttk.Label(self, style="Hint.TLabel")
        self.hint.pack()

        self.update_texts()
        self._check_status_generic("autodarts", self.status_lbl)

    def update_texts(self):
        l = self.controller.lang
        self.btn_back.config(text=TEXTS["btn_back"][l])
        self.header.config(text=TEXTS["ad_header"][l])
        self.btn_start.config(text=TEXTS["btn_start"][l])
        self.btn_stop.config(text=TEXTS["btn_stop"][l])
        self.btn_restart.config(text=TEXTS["btn_restart"][l])
        self.btn_install.config(text=TEXTS["btn_install"][l])
        self.btn_uninstall.config(text=TEXTS["btn_uninstall"][l])
        self.hint.config(text=TEXTS["hint"][l])
        self._check_status_generic("autodarts", self.status_lbl)

    def do_install(self):
        bash_script = """
echo "Installing Autodarts..."
bash <(curl -sL http://autodarts.io/install)
"""
        self._run_bash_script(bash_script)
        self.after(5000, lambda: self._check_status_generic("autodarts", self.status_lbl))

    def do_uninstall(self):
        if not messagebox.askyesno("SUIT", "Delete Autodarts?"): return
        bash_script = """
echo "Uninstalling Autodarts..."
sudo systemctl stop autodarts
sudo systemctl disable autodarts
sudo rm /etc/systemd/system/autodarts.service
sudo systemctl daemon-reload
sudo rm -rf /usr/bin/autodarts /opt/autodarts
echo "Done."
"""
        self._run_bash_script(bash_script)
        self.after(2000, lambda: self._check_status_generic("autodarts", self.status_lbl))


# --- ANSICHT 3: AUTOGLOW ---
class AutoGlowView(tk.Frame, ServiceViewMixin):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=COLORS["bg_main"])
        self.controller = controller

        self.btn_back = ttk.Button(self, command=controller.show_menu, style="TButton")
        self.btn_back.pack(anchor="w", pady=(0, 10))

        self.header = ttk.Label(self, text="", style="SubHeader.TLabel")
        self.header.pack(pady=(0, 15))

        # Status
        card_status = ttk.LabelFrame(self, text="Status", style="TLabelframe", padding=15)
        card_status.pack(fill="x", pady=5)
        self.status_lbl = ttk.Label(card_status, text="", style="Card.TLabel", font=("Segoe UI", 12, "bold"))
        self.status_lbl.pack(anchor="center")

        # Control
        card_ctrl = ttk.LabelFrame(self, text="Control", style="TLabelframe", padding=15)
        card_ctrl.pack(fill="x", pady=5)
        
        f_btns = tk.Frame(card_ctrl, bg=COLORS["bg_panel"])
        f_btns.pack(fill="x")
        f_btns.columnconfigure(0, weight=1)
        f_btns.columnconfigure(1, weight=1)
        f_btns.columnconfigure(2, weight=1)

        self.btn_start = ttk.Button(f_btns, command=lambda: self._run_service_cmd("autoglow", "start", self.status_lbl), style="Glow.TButton")
        self.btn_start.grid(row=0, column=0, padx=5, sticky="ew")
        self.btn_stop = ttk.Button(f_btns, command=lambda: self._run_service_cmd("autoglow", "stop", self.status_lbl))
        self.btn_stop.grid(row=0, column=1, padx=5, sticky="ew")
        self.btn_restart = ttk.Button(f_btns, command=lambda: self._run_service_cmd("autoglow", "restart", self.status_lbl))
        self.btn_restart.grid(row=0, column=2, padx=5, sticky="ew")

        # Install
        card_inst = ttk.LabelFrame(self, text="System", style="TLabelframe", padding=15)
        card_inst.pack(fill="x", pady=5)
        self.btn_install = ttk.Button(card_inst, command=self.do_install)
        self.btn_install.pack(fill="x", pady=2)
        self.btn_uninstall = ttk.Button(card_inst, command=self.do_uninstall, style="Danger.TButton")
        self.btn_uninstall.pack(fill="x", pady=2)

        # Github
        f_links = tk.Frame(self, bg=COLORS["bg_main"])
        f_links.pack(fill="x", pady=15)
        self.btn_gh = tk.Button(f_links, text="📂 GitHub: IteraThor/AutoGlow", bg=COLORS["bg_panel"], fg="white", 
                                  font=("Segoe UI", 10), relief="flat", padx=10, pady=8,
                                  command=lambda: webbrowser.open("https://github.com/IteraThor/AutoGlow"))
        self.btn_gh.pack(fill="x")

        self.hint = ttk.Label(self, style="Hint.TLabel")
        self.hint.pack()

        self.update_texts()
        self._check_status_generic("autoglow", self.status_lbl)

    def update_texts(self):
        l = self.controller.lang
        self.btn_back.config(text=TEXTS["btn_back"][l])
        self.header.config(text=TEXTS["ag_header"][l])
        self.btn_start.config(text=TEXTS["btn_start"][l])
        self.btn_stop.config(text=TEXTS["btn_stop"][l])
        self.btn_restart.config(text=TEXTS["btn_restart"][l])
        self.btn_install.config(text=TEXTS["btn_install"][l])
        self.btn_uninstall.config(text=TEXTS["btn_uninstall"][l])
        self.hint.config(text=TEXTS["hint"][l])
        self._check_status_generic("autoglow", self.status_lbl)

    def do_install(self):
        bash_script = """
echo "=== Installing AutoGlow ==="
sudo apt-get update
# python3-venv ist wichtig für die virtuelle Umgebung
sudo apt-get install -y git python3-venv

TARGET="/opt/AutoGlow"

# 1. Herunterladen (als Root, da /opt oft geschützt ist)
if [ ! -d "$TARGET" ]; then
    echo "Cloning repository..."
    sudo git clone https://github.com/IteraThor/AutoGlow.git "$TARGET"
else
    echo "Updating repository..."
    cd "$TARGET"
    sudo git pull
fi

# 2. WICHTIG: Besitzrechte auf den User übertragen
# Das verhindert den "Permission denied" Fehler, wenn das Skript intern
# versucht, eine virtuelle Umgebung (venv) zu erstellen.
echo "Fixing permissions..."
sudo chown -R $USER:$USER "$TARGET"

if [ -f "$TARGET/setup.sh" ]; then
    echo "Running installer..."
    sudo chmod +x "$TARGET/setup.sh"
    
    # 3. Starten MIT SUDO
    # Das Skript benötigt Root-Rechte für Dienste/Pakete. Da der Ordner
    # aber nun (Schritt 2) uns gehört, kann es trotzdem darin schreiben.
    sudo "$TARGET/setup.sh"
else
    echo "ERROR: setup.sh not found in $TARGET"
fi
"""
        self._run_bash_script(bash_script)
        self.after(5000, lambda: self._check_status_generic("autoglow", self.status_lbl))

    def do_uninstall(self):
        if not messagebox.askyesno("SUIT", "Delete AutoGlow?"): return
        
        bash_script = """
echo "=== Uninstalling AutoGlow ==="
sudo systemctl stop autoglow
sudo systemctl disable autoglow
sudo rm /etc/systemd/system/autoglow.service
sudo systemctl daemon-reload
sudo rm -rf /opt/AutoGlow
echo "Done."
"""
        self._run_bash_script(bash_script)
        self.after(2000, lambda: self._check_status_generic("autoglow", self.status_lbl))


# --- ANSICHT 4: KIOSK MODE ---
class KioskView(tk.Frame, ServiceViewMixin):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=COLORS["bg_main"])
        self.controller = controller
        self.autostart_dir = os.path.expanduser("~/.config/autostart")
        self.file_path = os.path.join(self.autostart_dir, "autodarts-kiosk.desktop")

        self.btn_back = ttk.Button(self, command=controller.show_menu, style="TButton")
        self.btn_back.pack(anchor="w", pady=(0, 10))

        self.header = ttk.Label(self, text="", style="SubHeader.TLabel")
        self.header.pack(pady=(0, 15))

        # Status
        card_status = ttk.LabelFrame(self, text="Status", style="TLabelframe", padding=15)
        card_status.pack(fill="x", pady=5)
        self.status_lbl = ttk.Label(card_status, text="", style="Card.TLabel", font=("Segoe UI", 12, "bold"))
        self.status_lbl.pack(anchor="center")

        # Config Card
        card_conf = ttk.LabelFrame(self, text="Konfiguration", style="TLabelframe", padding=15)
        card_conf.pack(fill="x", pady=5)
        
        self.btn_enable = ttk.Button(card_conf, command=self.enable_kiosk, style="Accent.TButton")
        self.btn_enable.pack(fill="x", pady=5)
        
        self.btn_disable = ttk.Button(card_conf, command=self.disable_kiosk)
        self.btn_disable.pack(fill="x", pady=5)

        # Hint
        self.lbl_hint = ttk.Label(self, style="Hint.TLabel", justify="center")
        self.lbl_hint.pack(pady=20)

        self.update_texts()
        self.check_status()

    def update_texts(self):
        l = self.controller.lang
        self.btn_back.config(text=TEXTS["btn_back"][l])
        self.header.config(text=TEXTS["kiosk_header"][l])
        self.btn_enable.config(text=TEXTS["btn_enable_kiosk"][l])
        self.btn_disable.config(text=TEXTS["btn_disable_kiosk"][l])
        self.lbl_hint.config(text=TEXTS["kiosk_hint"][l])
        self.check_status()

    def check_status(self):
        l = self.controller.lang
        if os.path.exists(self.file_path):
            self.status_lbl.config(text=TEXTS["kiosk_active"][l], foreground=COLORS["success"])
        else:
            self.status_lbl.config(text=TEXTS["kiosk_inactive"][l], foreground=COLORS["danger"])

    def _disable_crash_restore(self):
        profiles_path = os.path.expanduser("~/.mozilla/firefox/*.default*")
        profiles = glob.glob(profiles_path)
        pref_line = 'user_pref("browser.sessionstore.resume_from_crash", false);'
        
        for p in profiles:
            user_js = os.path.join(p, "user.js")
            try:
                content = ""
                if os.path.exists(user_js):
                    with open(user_js, "r") as f:
                        content = f.read()
                if "browser.sessionstore.resume_from_crash" not in content:
                    with open(user_js, "a") as f:
                        f.write("\n" + pref_line + "\n")
            except Exception as e:
                print(f"Err profile {p}: {e}")

    def enable_kiosk(self):
        # 1. System-Environment Fix (Wayland Black Screen)
        # Check ob Variable schon in /etc/environment steht
        try:
            check = subprocess.run("grep -q 'MOZ_ENABLE_WAYLAND=1' /etc/environment", shell=True)
            if check.returncode != 0:
                # Wenn nicht, hinzufügen (Root Rechte nötig)
                cmd_str = 'echo "MOZ_ENABLE_WAYLAND=1" | tee -a /etc/environment'
                
                # Command bauen (pkexec oder xterm)
                full_cmd = f"pkexec bash -c '{cmd_str}'"
                if not which("pkexec"):
                     full_cmd = f"xterm -e 'sudo bash -c \"{cmd_str}\"'"
                
                # Ausführen
                subprocess.run(full_cmd, shell=True, check=True)
        except Exception as e:
            # Nur warnen, nicht abbrechen, damit der Rest trotzdem läuft
            print(f"Warning setting env: {e}")

        # 2. Autostart Ordner checken
        if not os.path.exists(self.autostart_dir):
            try:
                os.makedirs(self.autostart_dir)
            except OSError as e:
                messagebox.showerror("Error", str(e))
                return
        
        # 3. Crash Restore abschalten
        self._disable_crash_restore()

        # 4. .desktop Datei schreiben
        # FIX: MOZ_ENABLE_WAYLAND=1 für Wayland-Systeme auch im Exec Befehl
        content = """[Desktop Entry]
Type=Application
Name=Autodarts Kiosk
Comment=Start Autodarts in Firefox Kiosk Mode
Exec=bash -c "sleep 5; MOZ_ENABLE_WAYLAND=1 firefox --kiosk https://play.autodarts.io/"
X-GNOME-Autostart-enabled=true
"""
        try:
            with open(self.file_path, "w") as f:
                f.write(content)
            self.check_status()
            messagebox.showinfo("SUIT", TEXTS["msg_applied"][self.controller.lang])
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def disable_kiosk(self):
        if os.path.exists(self.file_path):
            try:
                os.remove(self.file_path)
                self.check_status()
                messagebox.showinfo("SUIT", TEXTS["msg_applied"][self.controller.lang])
            except Exception as e:
                messagebox.showerror("Error", str(e))
        else:
             self.check_status()


# --- ANSICHT 5: TOUCH SCREEN INPUT ROTATION ---
class TouchRotationView(tk.Frame, ServiceViewMixin):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=COLORS["bg_main"])
        self.controller = controller

        self.btn_back = ttk.Button(self, command=controller.show_menu)
        self.btn_back.pack(anchor="w", pady=(0, 10))

        self.header = ttk.Label(self, text="", style="SubHeader.TLabel")
        self.header.pack(pady=(0, 20))

        # Card
        card = ttk.Frame(self, style="Card.TFrame", padding=20)
        card.pack(fill="x", padx=10)

        self.lbl_dir = ttk.Label(card, text="", style="Card.TLabel")
        self.lbl_dir.pack(anchor="w", pady=(0, 10))

        self.var_choice = tk.StringVar(value="left")

        def create_rb(val):
            return tk.Radiobutton(card, variable=self.var_choice, value=val, 
                                  bg=COLORS["bg_panel"], fg=COLORS["fg_text"],
                                  selectcolor="black", 
                                  activebackground=COLORS["bg_panel"],
                                  activeforeground=COLORS["fg_text"],
                                  font=("Segoe UI", 11),
                                  highlightthickness=0)

        self.rb_left = create_rb("left")
        self.rb_left.pack(anchor="w", pady=5)
        
        self.rb_right = create_rb("right")
        self.rb_right.pack(anchor="w", pady=5)

        self.rb_def = create_rb("default")
        self.rb_def.pack(anchor="w", pady=5)

        self.btn_apply = ttk.Button(self, command=self.apply, style="Accent.TButton")
        self.btn_apply.pack(fill="x", pady=(20, 5), padx=10)
        
        self.btn_reboot = ttk.Button(self, command=self.reboot)
        self.btn_reboot.pack(fill="x", pady=5, padx=10)

        self.update_texts()

    def update_texts(self):
        l = self.controller.lang
        self.btn_back.config(text=TEXTS["btn_back"][l])
        self.header.config(text=TEXTS["touch_header"][l])
        self.lbl_dir.config(text=TEXTS["lbl_direction"][l])
        self.rb_left.config(text=TEXTS["rot_left"][l])
        self.rb_right.config(text=TEXTS["rot_right"][l])
        self.rb_def.config(text=TEXTS["rot_default"][l])
        self.btn_apply.config(text=TEXTS["btn_apply"][l])
        self.btn_reboot.config(text=TEXTS["btn_reboot"][l])

    def apply(self):
        choice = self.var_choice.get()
        l = self.controller.lang
        
        tmp_path = "/tmp/suit_99-touch-rotation.rules"
        target_path = "/etc/udev/rules.d/99-touch-rotation.rules"
        
        msg = ""

        try:
            if choice == "default":
                cmd_str = f"rm -f {target_path} && udevadm control --reload-rules && udevadm trigger"
                msg = TEXTS["msg_deleted"][l]
            else:
                matrix = "0 1 0 -1 0 1 0 0 1" if choice == "left" else "0 -1 1 1 0 0 0 0 1"
                rule_content = f'ACTION=="add|change", KERNEL=="event[0-9]*", ATTRS{{idVendor}}=="27c0", ATTRS{{idProduct}}=="0859", ENV{{LIBINPUT_CALIBRATION_MATRIX}}="{matrix}"\n'
                
                with open(tmp_path, "w") as f:
                    f.write(rule_content)
                
                cmd_str = f"mv {tmp_path} {target_path} && udevadm control --reload-rules && udevadm trigger"
                msg = TEXTS["msg_applied"][l]

            full_cmd = f"pkexec bash -c '{cmd_str}'"
            if not which("pkexec"):
                 full_cmd = f"xterm -e 'sudo bash -c \"{cmd_str}\"'"

            subprocess.run(full_cmd, shell=True, check=True)
            messagebox.showinfo("SUIT", msg)

        except subprocess.CalledProcessError:
            pass
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def reboot(self):
        subprocess.run("pkexec reboot", shell=True)


if __name__ == "__main__":
    app = SuitApp()
    app.mainloop()
