import tkinter as tk
import customtkinter as ctk

class MainMenu(ctk.CTkFrame):
    def __init__(self, parent, controller):
        super().__init__(parent, fg_color="transparent") 
        self.controller = controller
        self.texts = controller.texts
        self.colors = controller.colors
        
        # Configure Grid
        self.grid_columnconfigure((0, 1), weight=1, uniform="tiles")
        self.grid_rowconfigure((1, 2, 3), weight=1, uniform="tiles")

        # Title
        self.lbl_title = ctk.CTkLabel(self, text="", 
                                     font=("Roboto", 36, "bold"), 
                                     text_color="white")
        self.lbl_title.grid(row=0, column=0, columnspan=2, pady=(20, 40))
        
        # Menu Tiles
        self._make_tile(self.controller.show_autodarts, "btn_autodarts", "🎯", 1, 0)
        self._make_tile(self.controller.show_autoglow, "btn_autoglow", "💡", 1, 1)
        self._make_tile(self.controller.show_kiosk, "btn_kiosk", "🖥️", 2, 0)
        self._make_tile(self.controller.show_touch, "btn_touch", "📱", 2, 1)
        self._make_tile(self.controller.show_iterathor, "btn_iterathor", "🐧", 3, 0)
        
        # --- EXPERIMENTAL INFO ---
        self.lbl_exp = ctk.CTkLabel(self, text="", 
                                   text_color=self.colors["warning"], 
                                   font=("Roboto", 13, "italic"), 
                                   wraplength=600)
        self.lbl_exp.grid(row=4, column=0, columnspan=2, pady=(30, 10))

        # Footer Buttons (Update)
        self.update_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.update_frame.grid(row=5, column=0, columnspan=2, sticky="ew", padx=80, pady=(10, 20))
        self.update_frame.grid_columnconfigure(0, weight=3)
        self.update_frame.grid_columnconfigure(1, weight=1)

        self.btn_upd = ctk.CTkButton(self.update_frame, text="", height=50,
                                    fg_color=self.colors["card"], 
                                    border_color=self.colors["header"],
                                    border_width=1,
                                    text_color="white", 
                                    text_color_disabled="white",
                                    hover_color=self.colors["accent"],
                                    font=("Roboto", 14, "bold"),
                                    command=self.controller.update_suit)
        self.btn_upd.grid(row=0, column=0, sticky="ew", padx=(0, 5))

        self.btn_upd_force = ctk.CTkButton(self.update_frame, text="", height=50,
                                          fg_color=self.colors["card"],
                                          border_color=self.colors["header"],
                                          border_width=1,
                                          text_color="white",
                                          hover_color=self.colors["danger"],
                                          font=("Roboto", 14, "bold"),
                                          command=lambda: self.controller.update_suit(force=True))
        self.btn_upd_force.grid(row=0, column=1, sticky="ew", padx=(5, 0))

        self.update_texts()

    def _make_tile(self, cmd, text_key, emoji, row, col):
        # Container for the tile to manage padding
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.grid(row=row, column=col, sticky="nsew", padx=15, pady=15)
        
        btn = ctk.CTkButton(frame, text="", height=180, corner_radius=15,
                           fg_color=self.colors["card"],
                           border_color=self.colors["header"],
                           border_width=1,
                           font=("Roboto", 18, "bold"), 
                           text_color="white", 
                           text_color_disabled="white",
                           hover_color=self.colors["accent"],
                           command=cmd)
        btn.pack(expand=True, fill="both")
        
        # Store emoji for update_texts
        setattr(self, f"_{text_key}_emoji", emoji)
        setattr(self, f"_{text_key}", btn) 

    def update_texts(self):
        """Safely updates all UI text strings based on current language."""
        l = self.controller.lang
        try:
            self.lbl_title.configure(text=self.texts.get("menu_title", {}).get(l, ""))
            
            # Update Tiles with Emojis
            keys = ["btn_autodarts", "btn_autoglow", "btn_kiosk", "btn_touch", "btn_iterathor"]
            for key in keys:
                btn = getattr(self, f"_{key}")
                emoji = getattr(self, f"_{key}_emoji")
                raw_text = self.texts.get(key, {}).get(l, "")
                btn.configure(text=f"{emoji}\n\n{raw_text}")
            
            self.lbl_exp.configure(text=self.texts.get("experimental_info", {}).get(l, ""))
            
            # Update buttons logic
            self.btn_upd_force.configure(text=self.texts.get("btn_force_update", {}).get(l, "Force Update"))

            if self.controller.update_available:
                self.btn_upd.configure(text=self.texts.get("btn_do_update", {}).get(l, "Update Now"), 
                                      fg_color=self.colors["accent"], border_width=0)
            else:
                self.btn_upd.configure(text=self.texts.get("btn_update", {}).get(l, "Check for Updates"), 
                                      fg_color=self.colors["card"], border_width=1)

        except Exception as e:
            print(f"Update texts error in MainMenu: {e}")
