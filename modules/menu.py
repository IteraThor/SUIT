import tkinter as tk
import customtkinter as ctk

class MainMenu(ctk.CTkFrame):
    def __init__(self, parent, controller):
        super().__init__(parent, fg_color="transparent") 
        self.controller = controller
        self.texts = controller.texts
        self.colors = controller.colors
        
        # Title
        self.lbl_title = ctk.CTkLabel(self, text="", font=("Segoe UI", 32, "bold"), text_color="white")
        self.lbl_title.pack(pady=(20, 20)) # Reduced padding
        
        # Menu Buttons
        self._make_menu_btn(self.controller.show_autodarts, "btn_autodarts")
        self._make_menu_btn(self.controller.show_autoglow, "btn_autoglow")
        self._make_menu_btn(self.controller.show_kiosk, "btn_kiosk")
        self._make_menu_btn(self.controller.show_touch, "btn_touch")
        
        # --- EXPERIMENTAL INFO CARD ---
        self.info_card = ctk.CTkFrame(self, fg_color="#2a2100", border_color=self.colors["warning"], border_width=1)
        self.info_card.pack(fill="x", padx=80, pady=(20, 10))
        
        self.lbl_exp = ctk.CTkLabel(self.info_card, text="", 
                                   text_color="#ffcc00", font=("Segoe UI", 12, "italic"), 
                                   wraplength=600)
        self.lbl_exp.pack(fill="x", pady=10) # Reduced padding

        # Footer Button (Update)
        self.btn_upd = ctk.CTkButton(self, text="", height=50,
                                    fg_color=self.colors["header"], text_color="white", hover_color=self.colors["accent"],
                                    command=self.controller.update_suit)
        self.btn_upd.pack(fill="x", padx=80, pady=(15, 20))

        self.update_texts()

    def _make_menu_btn(self, cmd, text_key):
        btn = ctk.CTkButton(self, text="", height=55, corner_radius=8,
                           font=("Segoe UI", 15, "bold"), 
                           text_color="white", text_color_disabled="#cccccc",
                           command=cmd)
        btn.pack(fill="x", padx=80, pady=6) 
        setattr(self, f"_{text_key}", btn) 

    def update_texts(self):
        """Safely updates all UI text strings based on current language."""
        l = self.controller.lang
        try:
            self.lbl_title.configure(text=self.texts.get("menu_title", {}).get(l, ""), text_color="white")
            self._btn_autodarts.configure(text=self.texts.get("btn_autodarts", {}).get(l, ""), text_color="white")
            self._btn_autoglow.configure(text=self.texts.get("btn_autoglow", {}).get(l, ""), text_color="white")
            self._btn_kiosk.configure(text=self.texts.get("btn_kiosk", {}).get(l, ""), text_color="white")
            
            if hasattr(self, "_btn_touch"):
                self._btn_touch.configure(text=self.texts.get("btn_touch", {}).get(l, ""), text_color="white")
            
            self.lbl_exp.configure(text=self.texts.get("experimental_info", {}).get(l, ""), text_color="#ffcc00")
            
            # Update button logic
            if self.controller.update_available:
                self.btn_upd.configure(text=self.texts.get("btn_do_update", {}).get(l, "Update Now"), 
                                      fg_color=self.colors["accent"], text_color="white")
            else:
                self.btn_upd.configure(text=self.texts.get("btn_update", {}).get(l, "Check for Updates"), 
                                      fg_color=self.colors["header"], text_color="white")

        except Exception as e:
            print(f"Update texts error in MainMenu: {e}")
