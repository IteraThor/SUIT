import tkinter as tk
import customtkinter as ctk
from tkinter import messagebox
from pathlib import Path
import subprocess
import threading
import json
import sys
from modules.utils import ServiceUtils

class UsbBandwidthView(ctk.CTkFrame):
    def __init__(self, parent, controller):
        super().__init__(parent, fg_color="transparent")
        self.controller = controller
        self.colors = controller.colors
        self.project_dir = Path(controller.project_dir)
        self.analyzer_script = self.project_dir / "scripts" / "usb_analyzer.py"
        self.is_updating = False
        
        # --- HEADER ---
        self.header_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.header_frame.pack(fill="x", pady=(10, 20))
        
        self.btn_back = ctk.CTkButton(self.header_frame, text="←", width=50, height=35,
                                     fg_color=self.colors["card"], 
                                     border_color=self.colors["header"],
                                     border_width=1,
                                     text_color="white", command=controller.show_menu)
        self.btn_back.pack(side="left", padx=20)
        
        self.lbl_title = ctk.CTkLabel(self.header_frame, text="", font=("Roboto", 28, "bold"), text_color="white")
        self.lbl_title.pack(side="left", padx=10)

        # --- INFO CARD ---
        self.info_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.info_frame.pack(fill="x", padx=20, pady=(0, 15))
        self.lbl_info = ctk.CTkLabel(self.info_frame, text="", font=("Roboto", 14),
                                    text_color=self.colors["fg_dim"], wraplength=720, justify="left")
        self.lbl_info.pack(fill="x", padx=20)

        # --- SCROLLABLE CONTENT ---
        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.scroll.pack(fill="both", expand=True, padx=20, pady=(0, 20))

        # --- RECOMMENDATION HOLDER ---
        self.rec_frame = None

        self.update_texts()

    def update_status(self):
        """Spawns background thread to run USB analyzer."""
        if self.is_updating:
            return
        self.is_updating = True
        self._show_loading()
        threading.Thread(target=self._update_status_worker, daemon=True).start()

    def _update_status_worker(self):
        try:
            # Run with sudo to get exact Alloc isochronous bandwidth metrics
            cmd = f"sudo {sys.executable} {self.analyzer_script}"
            res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if res.returncode == 0:
                data = json.loads(res.stdout)
                self.after(0, lambda: self._update_ui(data))
            else:
                # Fallback to non-sudo if sudo fails
                cmd = f"{sys.executable} {self.analyzer_script}"
                res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                if res.returncode == 0:
                    data = json.loads(res.stdout)
                    self.after(0, lambda: self._update_ui(data))
                else:
                    self.after(0, lambda: self._show_error(res.stderr or "Error running analyzer script."))
        except Exception as e:
            self.after(0, lambda: self._show_error(str(e)))
        finally:
            self.is_updating = False

    def _show_loading(self):
        # Clear previous UI in scrollable frame
        for widget in self.scroll.winfo_children():
            widget.destroy()

        self.loading_lbl = ctk.CTkLabel(self.scroll, text="Analyzing USB Controllers...", font=("Roboto", 16, "bold"), text_color=self.colors["accent"])
        self.loading_lbl.pack(pady=40)
        
        self.progress = ctk.CTkProgressBar(self.scroll, mode="indeterminate", width=300)
        self.progress.pack(pady=10)
        self.progress.start()

    def _show_error(self, err_msg):
        for widget in self.scroll.winfo_children():
            widget.destroy()
        
        err_lbl = ctk.CTkLabel(self.scroll, text=f"Error analyzing USB buses:\n{err_msg}", 
                               font=("Roboto", 14), text_color=self.colors["danger"], wraplength=600)
        err_lbl.pack(pady=40)

    def _update_ui(self, data):
        # Clear loading widgets
        for widget in self.scroll.winfo_children():
            widget.destroy()

        # Render Warnings & Recommendations first if any exist
        warnings = data.get("warnings", [])
        recommendations = data.get("recommendations", [])
        
        if warnings or recommendations:
            self.rec_frame = ctk.CTkFrame(self.scroll, fg_color="#451a1a", border_width=1, border_color=self.colors["danger"], corner_radius=12)
            self.rec_frame.pack(fill="x", padx=10, pady=(10, 20))
            
            lbl_title = ctk.CTkLabel(self.rec_frame, text="⚠️ USB Bandwidth Warning", font=("Roboto", 16, "bold"), text_color=self.colors["danger"])
            lbl_title.pack(anchor="w", padx=20, pady=(15, 5))
            
            for warn in warnings:
                lbl_warn = ctk.CTkLabel(self.rec_frame, text=f"• {warn}", font=("Roboto", 13), text_color="white", justify="left", wraplength=680)
                lbl_warn.pack(anchor="w", padx=30, pady=4)
                
            for rec in recommendations:
                lbl_rec = ctk.CTkLabel(self.rec_frame, text=f"💡 Suggestion: {rec}", font=("Roboto", 13, "bold"), text_color=self.colors["warning"], justify="left", wraplength=680)
                lbl_rec.pack(anchor="w", padx=30, pady=(8, 12))

        # List USB Controllers (Buses)
        for bus in data.get("buses", []):
            bus_card = ctk.CTkFrame(self.scroll, fg_color=self.colors["card"], border_width=1, border_color=self.colors["header"], corner_radius=12)
            bus_card.pack(fill="x", padx=10, pady=10)
            
            # Title Row
            title_row = ctk.CTkFrame(bus_card, fg_color="transparent")
            title_row.pack(fill="x", padx=20, pady=(15, 10))
            
            bus_title = f"🔌 Bus {bus['bus']}: {bus['type']}"
            lbl_bus = ctk.CTkLabel(title_row, text=bus_title, font=("Roboto", 16, "bold"), text_color="white")
            lbl_bus.pack(side="left")
            
            lbl_spd = ctk.CTkLabel(title_row, text=f"Max: {bus['speed_lbl']} Mbps", font=("Roboto", 12, "bold"), 
                                  fg_color=self.colors["header"], text_color="white", corner_radius=6, padx=8, pady=2)
            lbl_spd.pack(side="right")

            # Progress Bar for estimated load (MJPEG load comparison is more realistic for Autodarts setup)
            load = bus["est_load_mjpeg_mbps"]
            capacity = bus["capacity_mbps"]
            load_percentage = min(100.0, (load / capacity) * 100.0) if capacity > 0 else 0.0
            
            progress_val = load_percentage / 100.0
            
            progress_color = self.colors["success"]
            if load_percentage > 80.0:
                progress_color = self.colors["danger"]
            elif load_percentage > 50.0:
                progress_color = self.colors["warning"]

            progress_row = ctk.CTkFrame(bus_card, fg_color="transparent")
            progress_row.pack(fill="x", padx=20, pady=5)
            
            lbl_load = ctk.CTkLabel(progress_row, text=f"Est. load: {load:.1f} Mbps / {capacity:.0f} Mbps ({load_percentage:.1f}%)", 
                                   font=("Roboto", 13), text_color=self.colors["fg_dim"])
            lbl_load.pack(anchor="w")
            
            pbar = ctk.CTkProgressBar(progress_row, width=600, height=10, progress_color=progress_color)
            pbar.pack(fill="x", pady=(5, 10))
            pbar.set(progress_val)

            # Cameras List on this Bus
            cameras = bus.get("cameras", [])
            other_devices = bus.get("other_devices", [])

            if cameras or other_devices:
                dev_box = ctk.CTkFrame(bus_card, fg_color=self.colors["bg"], corner_radius=8)
                dev_box.pack(fill="x", padx=20, pady=(0, 20))
                
                # Header inside Dev Box
                lbl_dev_hdr = ctk.CTkLabel(dev_box, text="Connected USB Devices:", font=("Roboto", 12, "bold"), text_color=self.colors["fg_dim"])
                lbl_dev_hdr.pack(anchor="w", padx=15, pady=(8, 4))
                
                for cam in cameras:
                    cam_frame = ctk.CTkFrame(dev_box, fg_color="transparent")
                    cam_frame.pack(fill="x", padx=20, pady=4)
                    
                    # Highlight cameras
                    cam_name = f"📹 Camera ({cam['node']})"
                    lbl_cam = ctk.CTkLabel(cam_frame, text=cam_name, font=("Roboto", 13, "bold"), text_color=self.colors["accent"])
                    lbl_cam.pack(side="left")
                    
                    # Formats / Bandwidth info
                    bw_text = f"MJPEG: ~{cam['mjpeg_mbps']:.1f}M | YUY2: ~{cam['yuyv_mbps']:.1f}M"
                    lbl_bw = ctk.CTkLabel(cam_frame, text=bw_text, font=("Roboto", 11, "italic"), text_color=self.colors["fg_dim"])
                    lbl_bw.pack(side="right")
                
                for dev in other_devices:
                    dev_frame = ctk.CTkFrame(dev_box, fg_color="transparent")
                    dev_frame.pack(fill="x", padx=20, pady=2)
                    
                    dev_name = f"🔌 {dev['product']}"
                    lbl_dev = ctk.CTkLabel(dev_frame, text=dev_name, font=("Roboto", 13), text_color="white")
                    lbl_dev.pack(side="left")
                    
                    lbl_dev_spd = ctk.CTkLabel(dev_frame, text=f"{dev['speed']}M", font=("Roboto", 11), text_color=self.colors["fg_dim"])
                    lbl_dev_spd.pack(side="right")
            else:
                lbl_empty = ctk.CTkLabel(bus_card, text="No active devices connected.", font=("Roboto", 13, "italic"), text_color=self.colors["fg_dim"])
                lbl_empty.pack(anchor="w", padx=20, pady=(0, 20))

    def update_texts(self):
        l = getattr(self.controller, "lang", "en")
        texts = getattr(self.controller, "texts", {})
        def txt(k): return texts.get(k, {}).get(l, k)

        self.lbl_title.configure(text=txt("usb_header"))
        self.lbl_info.configure(text=txt("desc_usb"))
        self.update_status()
