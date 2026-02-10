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
    "kiosk_wait_internet": {"de": "Auf Internetverbindung warten (Blackscreen Fix)", "en": "Wait for internet connection (Blackscreen Fix)"},
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
    "err_no_git": {"de": "Kein Git-Repository gefunden.\nBitte 'git clone' nutzen.", "en": "No Git repository found.\nPlease use 'git clone'."},
    "ag_stop_first": {"de": "Bitte stoppe zuerst den AutoGlow Dienst!", "en": "Please stop the AutoGlow service first!"},
    "ag_not_found": {"de": "AutoGlow Ordner nicht gefunden. Bitte erst installieren.", "en": "AutoGlow folder not found. Please install first."}
}

# ... (Rest der Logik bleibt gleich wie in der vorherigen Version) ...

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

        # Checkbox für Internet-Warten mit neuem Label
        self.var_wait_internet = tk.BooleanVar(value=True)
        self.check_wait = tk.Checkbutton(card_conf, variable=self.var_wait_internet, bg=COLORS["bg_panel"], fg=COLORS["fg_text"], selectcolor="black", activebackground=COLORS["bg_panel"], activeforeground=COLORS["fg_text"])
        self.check_wait.pack(anchor="w", pady=(0, 10))

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
        self.check_wait.config(text=TEXTS["kiosk_wait_internet"][l])
        self.check_status()

    # ... (Rest der KioskView Logik wie enable_kiosk etc. bleibt identisch) ...
