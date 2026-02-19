import tkinter as tk
import customtkinter as ctk
from tkinter import messagebox
import os
import sys
import json
import logging
import subprocess

# Set up logging
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "suit.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(BASE_DIR)
CONFIG_FILE = os.path.expanduser("~/.suit_config.json")

# Import modules
from modules.menu import MainMenu
from modules.autodarts import AutodartsView
from modules.autoglow import AutoGlowView
from modules.kiosk import KioskView

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
    def __init__(self):
        super().__init__()
        self.withdraw() # Hide window during initialization
        logger.info("Starting SUIT Application (Modern UI)...")
        self.title("SUIT - Setup Utility")
        
        # Set size
        self.geometry("850x720")
        self.minsize(800, 650)
        self.project_dir = BASE_DIR
        
        # Load Config & Languages
        self.load_config()
        self.load_translations()

        # Improved High-Contrast Palette
        self.colors = {
            "bg": "#000000",        # Pure Black
            "card": "#1a1a1a",      # Dark Grey Card
            "header": "#444444",    # Much brighter header for buttons
            "accent": "#1f538d",    # Blue accent
            "success": "#28a745",   # Green
            "danger": "#d9534f",    # Red
            "warning": "#ffcc00",   # Yellow
            "fg": "#ffffff",        # Pure White
            "fg_dim": "#eeeeee"     # Near White for descriptions
        }

        # --- FRAME LOADING ---
        self.container = ctk.CTkFrame(self, fg_color="transparent")
        self.container.pack(side="top", fill="both", expand=True, padx=20, pady=20)
        self.container.grid_rowconfigure(0, weight=1)
        self.container.grid_columnconfigure(0, weight=1)

        # --- TOP OVERLAY (Language Switcher) ---
        self.lang_btn = ctk.CTkButton(self, text="DE | EN", width=60, height=32,
                                     fg_color="#252526", hover_color=self.colors["accent"],
                                     font=("Segoe UI", 11, "bold"), command=self.toggle_language)
        self.lang_btn.place(relx=1.0, rely=0.0, anchor="ne", x=-20, y=20)

        self.frames = {}
        self.update_available = False
        
        # Initialize views (MainMenu last to ensure it stays on top initially)
        views = [AutodartsView, AutoGlowView, KioskView]
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

    def load_config(self):
        self.lang = "en"
        if os.path.exists(CONFIG_FILE):
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
        lang_path = os.path.join(BASE_DIR, "lang.json")
        try:
            with open(lang_path, "r") as f:
                self.texts = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load lang.json: {e}")
            self.texts = {}

    def toggle_language(self):
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
    def show_touch(self):
        if RotationView: self.show_frame(RotationView)
        else: logger.warning("RotationView missing.")

    def update_suit(self):
        l = self.lang
        def txt(k): return self.texts.get(k, {}).get(l, k)
        if not self.update_available:
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
            if messagebox.askyesno("SUIT", txt("update_confirm_msg")):
                try:
                    subprocess.run(["git", "pull"], check=True, cwd=BASE_DIR)
                    messagebox.showinfo("SUIT", txt("msg_updated"))
                    os.execv(sys.executable, [sys.executable, __file__] + sys.argv[1:])
                except Exception as e:
                    messagebox.showerror("Error", f"Update failed: {e}")
        self.refresh_ui()

if __name__ == "__main__":
    app = SuitApp()
    app.mainloop()
