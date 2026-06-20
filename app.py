import tkinter as tk
import customtkinter as ctk
from tkinter import messagebox
from pathlib import Path
import sys
import os
import json
import logging
import subprocess
import fcntl
import time
import importlib.metadata

# Paths
BASE_DIR = Path(__file__).resolve().parent
sys.path.append(str(BASE_DIR))
LOG_FILE = BASE_DIR / "suit.log"
CONFIG_FILE = Path.home() / ".suit_config.json"
lock_file = Path.home() / ".suit.lock"

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Global Exception Handler
def global_exception_handler(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.error("Unhandled exception:", exc_info=(exc_type, exc_value, exc_traceback))

sys.excepthook = global_exception_handler

# Single Instance Lock
lock_fp = open(lock_file, 'w')
try:
    fcntl.lockf(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
except IOError:
    print("SUIT is already running.")
    sys.exit(0)

from modules.utils import ServiceUtils

# Import modules
from modules.menu import MainMenu
from modules.autodarts import AutodartsView
from modules.autoglow import AutoGlowView
from modules.kiosk import KioskView
from modules.iterathor import IterathorView
from modules.usb_bandwidth import UsbBandwidthView

# Optional Rotation
try:
    from modules.rotation import RotationView
except ImportError:
    RotationView = None
    logger.warning("RotationView module could not be imported.")

# Set appearance
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

class SuitApp(ctk.CTk):
    def report_callback_exception(self, exc, val, tb):
        logger.error("Unhandled UI exception:", exc_info=(exc, val, tb))

    def __init__(self):
        super().__init__()
        self.withdraw() # Hide window during initialization
        logger.info("Starting SUIT Application (Modern UI)...")
        self.title("SUIT - Setup Utility")
        
        # Set size (Initial, but allow it to grow)
        self.minsize(800, 650)
        self.project_dir = BASE_DIR
        
        # Load Config & Languages
        self.load_config()
        self.load_translations()

        # Modern Minimalist Palette (Zinc-based)
        self.colors = {
            "bg": "#18181b",        # Zinc 900
            "card": "#27272a",      # Zinc 800
            "header": "#3f3f46",    # Zinc 700
            "accent": "#3b82f6",    # Blue 500
            "success": "#22c55e",   # Green 500
            "danger": "#ef4444",    # Red 500
            "warning": "#eab308",   # Yellow 500
            "fg": "#ffffff",        # Pure White
            "fg_dim": "#d4d4d8"     # Zinc 300 (Brighter than before for better contrast)
        }
        
        # Global Button Style Override
        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")

        # --- FRAME LOADING ---
        self.container = ctk.CTkFrame(self, fg_color="transparent")
        self.container.pack(side="top", fill="both", expand=True, padx=20, pady=20)
        self.container.grid_rowconfigure(0, weight=1)
        self.container.grid_columnconfigure(0, weight=1)

        # --- TOP OVERLAY (Language Switcher) ---
        self.lang_btn = ctk.CTkButton(self, text="DE | EN", width=65, height=32, corner_radius=8,
                                     fg_color=self.colors["card"], 
                                     border_color=self.colors["header"],
                                     border_width=1,
                                     hover_color=self.colors["accent"],
                                     font=("Roboto", 11, "bold"), command=self.toggle_language)
        self.lang_btn.place(relx=1.0, rely=0.0, anchor="ne", x=-20, y=20)

        self.frames = {}
        self.update_available = False
        self.last_lang_switch = 0
        
        # Initialize views (MainMenu last to ensure it stays on top initially)
        views = [AutodartsView, AutoGlowView, KioskView, IterathorView, UsbBandwidthView]
        if RotationView: views.append(RotationView)
        views.append(MainMenu)

        for F in views:
            try:
                frame = F(self.container, self)
                self.frames[F] = frame
                frame.grid(row=0, column=0, sticky="nsew")
            except Exception as e:
                logger.error(f"Error loading {F.__name__}: {e}")

        self.show_menu()
        self.start_polling()
        self.deiconify() # Show window once ready
        self.after(1000, self.check_initial_setup)

    def check_initial_setup(self):
        """Runs initial checks for sudo and requirements."""
        if not ServiceUtils.check_sudo_nopasswd():
            ServiceUtils.setup_sudo_nopasswd(self)
        
        # After sudo check (or if it was skipped/failed), check requirements
        self.check_requirements()

    def start_polling(self):
        self.poll_services()

    def poll_services(self):
        try:
            for frame in self.frames.values():
                if frame.winfo_viewable() and hasattr(frame, "update_status"):
                    frame.update_status()
                    break
        except Exception as e:
            pass
        self.after(5000, self.poll_services)

    def check_requirements(self):
        req_file = BASE_DIR / "requirements.txt"
        if not req_file.exists():
            return

        missing = []
        with open(req_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                
                # Split for name (e.g. dbus-python==1.3.2 -> name=dbus-python)
                name = line.split("==")[0].split(">=")[0].split("<=")[0].strip()
                
                try:
                    importlib.metadata.version(name)
                except importlib.metadata.PackageNotFoundError:
                    missing.append(line)

        if missing:
            l = self.lang
            def txt(k): return self.texts.get(k, {}).get(l, k)
            
            msg = txt("req_missing_text").format(pkg="\n".join(missing))
            if messagebox.askyesno(txt("req_missing_title"), msg):
                pkgs = " ".join(missing)
                # Only use sudo if we are NOT in a virtual environment
                in_venv = sys.prefix != sys.base_prefix
                install_cmd = f"{sys.executable} -m pip install {pkgs}"
                
                if not in_venv:
                    install_cmd = ServiceUtils.sudo_cmd(install_cmd)
                
                logger.info(f"Installing missing requirements: {pkgs}")
                logger.info(f"Running command: {install_cmd}")
                
                ServiceUtils.run_bash_script(self, install_cmd, title=txt("req_missing_title"), on_close=self.check_requirements)

    def load_config(self):
        self.lang = "en"
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r") as f:
                    config = json.load(f)
                    self.lang = config.get("lang", "en")
            except Exception as e:
                logger.error(f"Failed to load config: {e}")

    def save_config(self):
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump({"lang": self.lang}, f)
        except Exception as e:
            logger.error(f"Failed to save config: {e}")

    def load_translations(self):
        lang_path = BASE_DIR / "lang.json"
        try:
            with open(lang_path, "r") as f:
                self.texts = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load lang.json: {e}")
            self.texts = {}

    def toggle_language(self):
        current_time = time.time()
        if current_time - self.last_lang_switch < 0.5:
            return
        self.last_lang_switch = current_time
        
        self.lang = "de" if self.lang == "en" else "en"
        self.save_config()
        self.refresh_ui()

    def refresh_ui(self):
        for frame in self.frames.values():
            if hasattr(frame, "update_texts"):
                frame.update_texts()
        self.lang_btn.lift()

    def show_frame(self, cont):
        if cont in self.frames:
            frame = self.frames[cont]
            frame.tkraise()
            self.lang_btn.lift()
            if hasattr(frame, "update_texts"): frame.update_texts()
            if hasattr(frame, "update_status"): frame.update_status()

    def show_menu(self): self.show_frame(MainMenu)
    def show_autodarts(self): self.show_frame(AutodartsView)
    def show_autoglow(self): self.show_frame(AutoGlowView)
    def show_kiosk(self): self.show_frame(KioskView)
    def show_iterathor(self): self.show_frame(IterathorView)
    def show_usb(self): self.show_frame(UsbBandwidthView)
    def show_touch(self):
        if RotationView: self.show_frame(RotationView)
        else: logger.warning("RotationView missing.")

    def update_suit(self, force=False):
        l = self.lang
        def txt(k): return self.texts.get(k, {}).get(l, k)
        
        if not force and not self.update_available:
            try:
                subprocess.run(["git", "fetch"], check=True, cwd=BASE_DIR, capture_output=True)
                res = subprocess.run(["git", "status", "-uno"], check=True, cwd=BASE_DIR, capture_output=True, text=True)
                if "Your branch is behind" in res.stdout:
                    self.update_available = True
                    messagebox.showinfo("SUIT", txt("update_available_msg"))
                else:
                    messagebox.showinfo("SUIT", txt("msg_uptodate"))
            except Exception as e:
                messagebox.showerror("Error", f"Check failed: {e}")
        else:
            msg = txt("update_confirm_msg") if not force else "Force update will overwrite all local changes. Continue?"
            if messagebox.askyesno("SUIT", msg):
                try:
                    if force:
                        subprocess.run(["git", "fetch", "--all"], check=True, cwd=BASE_DIR)
                        subprocess.run(["git", "reset", "--hard", "origin/main"], check=True, cwd=BASE_DIR)
                    else:
                        subprocess.run(["git", "pull"], check=True, cwd=BASE_DIR)
                    
                    messagebox.showinfo("SUIT", txt("msg_updated"))
                    os.execv(sys.executable, [sys.executable, __file__] + sys.argv[1:])
                except Exception as e:
                    messagebox.showerror("Error", f"Update failed: {e}")
        self.refresh_ui()

if __name__ == "__main__":
    app = SuitApp()
    app.mainloop()
