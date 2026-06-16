import customtkinter as ctk
from modules.utils import ServiceUtils
from tkinter import messagebox
from pathlib import Path
import os

class AIC8800View(ctk.CTkFrame):
    def __init__(self, parent, controller):
        super().__init__(parent, fg_color="transparent")
        self.controller = controller
        self.colors = controller.colors
        self.project_dir = Path(controller.project_dir)
        
        # --- HEADER ---
        self.header_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.header_frame.pack(fill="x", pady=(10, 25))
        
        self.btn_back = ctk.CTkButton(self.header_frame, text="←", width=50, height=35,
                                     fg_color=self.colors["card"], 
                                     border_color=self.colors["header"],
                                     border_width=1,
                                     text_color="white", command=controller.show_menu)
        self.btn_back.pack(side="left", padx=20)
        
        self.lbl_title = ctk.CTkLabel(self.header_frame, text="", font=("Roboto", 28, "bold"), text_color="white")
        self.lbl_title.pack(side="left", padx=10)

        # --- INFO ---
        self.info_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.info_frame.pack(fill="x", padx=20, pady=(0, 20))
        self.lbl_info = ctk.CTkLabel(self.info_frame, text="", font=("Roboto", 14),
                                    text_color=self.colors["fg_dim"], wraplength=720, justify="left")
        self.lbl_info.pack(fill="x", padx=20)
        
        # --- CONTENT ---
        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.scroll.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        
        # Installation Step
        self.f1_frame = ctk.CTkFrame(self.scroll, fg_color=self.colors["card"], corner_radius=10)
        self.f1_frame.pack(fill="x", pady=2)
        
        self.f1_left = ctk.CTkFrame(self.f1_frame, fg_color="transparent")
        self.f1_left.pack(side="left", padx=20, pady=15, fill="both", expand=True)
        
        self.lbl_f1 = ctk.CTkLabel(self.f1_left, text="", font=("Roboto", 16, "bold"), anchor="w")
        self.lbl_f1.pack(fill="x", anchor="w")
        
        self.lbl_f1_desc = ctk.CTkLabel(self.f1_left, text="", font=("Roboto", 13), text_color=self.colors["fg_dim"], anchor="w", justify="left")
        self.lbl_f1_desc.pack(fill="x", anchor="w")
        
        self.btn_f1 = ctk.CTkButton(self.f1_frame, text="", width=120, height=35,
                                    font=("Roboto", 14, "bold"),
                                    command=self.install_local_driver)
        self.btn_f1.pack(side="right", padx=20, pady=15)

        self.update_texts()

    def update_texts(self):
        l = self.controller.lang
        t = self.controller.texts
        def txt(k): return t.get(k, {}).get(l, k)

        self.lbl_title.configure(text=txt("aic_header"))
        self.lbl_info.configure(text=txt("aic_info"))
        
        self.lbl_f1.configure(text=txt("aic_local_lbl"))
        self.lbl_f1_desc.configure(text=txt("aic_local_desc"))
        self.btn_f1.configure(text=txt("it_btn_apply"))

    def install_local_driver(self):
        driver_root = self.project_dir / "drivers" / "aic8800"
        
        if not driver_root.exists():
            l = self.controller.lang
            msg = self.controller.texts.get("aic_msg_no_driver", {}).get(l, "Driver folder not found")
            messagebox.showerror("SUIT", msg)
            return

        bash_script = f"""
DRIVER_ROOT="{driver_root}"
CUR_KERNEL=$(uname -r)
KERNEL_DIR="$DRIVER_ROOT/$CUR_KERNEL"

echo "Current Kernel: $CUR_KERNEL"

if [ ! -d "$KERNEL_DIR" ]; then
    echo "ERROR: No matching driver directory found for kernel $CUR_KERNEL"
    echo "Looking for: $KERNEL_DIR"
    exit 2
fi

# Locate module file
MOD_FILE=$(find "$KERNEL_DIR" -name "aic8800_fdrv.ko*" | head -n 1)
if [ -z "$MOD_FILE" ]; then
    echo "ERROR: aic8800_fdrv.ko* not found in $KERNEL_DIR"
    exit 3
fi

# Locate firmware
FW_SRC="$DRIVER_ROOT/fw"
if [ ! -d "$FW_SRC" ]; then
    # Fallback to check if firmware is inside the kernel dir
    FW_SRC="$KERNEL_DIR/fw"
fi

echo "Installing module from: $MOD_FILE"
sudo mkdir -p "/lib/modules/$CUR_KERNEL/extra/"
sudo cp "$MOD_FILE" "/lib/modules/$CUR_KERNEL/extra/"

if [ -d "$FW_SRC" ]; then
    echo "Installing firmware from: $FW_SRC"
    sudo mkdir -p "/lib/firmware/aic8800/"
    sudo cp -r "$FW_SRC"/* "/lib/firmware/aic8800/"
else
    echo "WARNING: No firmware directory found at $DRIVER_ROOT/fw"
fi

echo "Registering module..."
sudo depmod -a

echo "Loading module..."
sudo modprobe aic8800_fdrv

# Verification
echo "Verifying installation..."
if lsmod | grep -q "aic8800_fdrv"; then
    echo "SUCCESS: AIC8800 Driver is loaded and active."
else
    echo "ERROR: Driver failed to load. Check 'dmesg' for details."
    exit 4
fi
"""
        ServiceUtils.run_bash_script(self, bash_script, title="Install AIC8800 Local")
