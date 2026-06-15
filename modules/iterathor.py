import customtkinter as ctk
from modules.utils import ServiceUtils
import subprocess
from tkinter import messagebox

class IterathorView(ctk.CTkFrame):
    def __init__(self, parent, controller):
        super().__init__(parent, fg_color="transparent")
        self.controller = controller
        self.colors = controller.colors
        
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
        
        # --- CONTENT (Scrollable for vertical growth) ---
        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.scroll.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        
        # Step 1: System Settings
        self.f1_frame = ctk.CTkFrame(self.scroll, fg_color=self.colors["card"], corner_radius=10)
        self.f1_frame.pack(fill="x", pady=2)
        
        self.f1_left = ctk.CTkFrame(self.f1_frame, fg_color="transparent")
        self.f1_left.pack(side="left", padx=20, pady=8, fill="both", expand=True)
        
        self.lbl_f1 = ctk.CTkLabel(self.f1_left, text="", font=("Roboto", 16, "bold"), anchor="w")
        self.lbl_f1.pack(fill="x", anchor="w")
        
        self.lbl_f1_desc = ctk.CTkLabel(self.f1_left, text="", font=("Roboto", 13), text_color=self.colors["fg_dim"], anchor="w", justify="left")
        self.lbl_f1_desc.pack(fill="x", anchor="w")
        
        self.btn_f1 = ctk.CTkButton(self.f1_frame, text="", width=120, height=35,
                                    font=("Roboto", 14, "bold"),
                                    command=self.apply_sys_settings)
        self.btn_f1.pack(side="right", padx=20, pady=8)

        # Step 2: Automatic Login
        self.f2_frame = ctk.CTkFrame(self.scroll, fg_color=self.colors["card"], corner_radius=10)
        self.f2_frame.pack(fill="x", pady=2)
        
        self.f2_left = ctk.CTkFrame(self.f2_frame, fg_color="transparent")
        self.f2_left.pack(side="left", padx=20, pady=8, fill="both", expand=True)
        
        self.lbl_f2 = ctk.CTkLabel(self.f2_left, text="", font=("Roboto", 16, "bold"), anchor="w")
        self.lbl_f2.pack(fill="x", anchor="w")
        
        self.lbl_f2_desc = ctk.CTkLabel(self.f2_left, text="", font=("Roboto", 13), text_color=self.colors["fg_dim"], anchor="w", justify="left")
        self.lbl_f2_desc.pack(fill="x", anchor="w")
        
        self.btn_f2 = ctk.CTkButton(self.f2_frame, text="", width=120, height=35,
                                    font=("Roboto", 14, "bold"),
                                    command=self.enable_autologin)
        self.btn_f2.pack(side="right", padx=20, pady=8)

        # Step 3: Keyring Fix
        self.f3_frame = ctk.CTkFrame(self.scroll, fg_color=self.colors["card"], corner_radius=10)
        self.f3_frame.pack(fill="x", pady=2)
        
        self.f3_left = ctk.CTkFrame(self.f3_frame, fg_color="transparent")
        self.f3_left.pack(side="left", padx=20, pady=8, fill="both", expand=True)
        
        self.lbl_f3 = ctk.CTkLabel(self.f3_left, text="", font=("Roboto", 16, "bold"), anchor="w")
        self.lbl_f3.pack(fill="x", anchor="w")
        
        self.lbl_f3_desc = ctk.CTkLabel(self.f3_left, text="", font=("Roboto", 13), text_color=self.colors["fg_dim"], anchor="w", justify="left")
        self.lbl_f3_desc.pack(fill="x", anchor="w")
        
        self.btn_f3 = ctk.CTkButton(self.f3_frame, text="", width=120, height=35,
                                    font=("Roboto", 14, "bold"),
                                    command=self.fix_keyring)
        self.btn_f3.pack(side="right", padx=20, pady=8)

        # Step 4: Debloat
        self.f4_frame = ctk.CTkFrame(self.scroll, fg_color=self.colors["card"], corner_radius=10)
        self.f4_frame.pack(fill="x", pady=2)
        
        self.f4_left = ctk.CTkFrame(self.f4_frame, fg_color="transparent")
        self.f4_left.pack(side="left", padx=20, pady=8, fill="both", expand=True)
        
        self.lbl_f4 = ctk.CTkLabel(self.f4_left, text="", font=("Roboto", 16, "bold"), anchor="w")
        self.lbl_f4.pack(fill="x", anchor="w")
        
        self.lbl_f4_desc = ctk.CTkLabel(self.f4_left, text="", font=("Roboto", 13), text_color=self.colors["fg_dim"], anchor="w", justify="left")
        self.lbl_f4_desc.pack(fill="x", anchor="w")
        
        self.btn_f4 = ctk.CTkButton(self.f4_frame, text="", width=120, height=35,
                                    font=("Roboto", 14, "bold"),
                                    command=self.debloat_system)
        self.btn_f4.pack(side="right", padx=20, pady=8)

        # Step 5: Software
        self.f5_frame = ctk.CTkFrame(self.scroll, fg_color=self.colors["card"], corner_radius=10)
        self.f5_frame.pack(fill="x", pady=2)
        
        self.f5_top = ctk.CTkFrame(self.f5_frame, fg_color="transparent")
        self.f5_top.pack(fill="x", padx=20, pady=(8, 2))
        
        self.lbl_f5 = ctk.CTkLabel(self.f5_top, text="", font=("Roboto", 16, "bold"), anchor="w")
        self.lbl_f5.pack(side="left")
        
        self.lbl_f5_desc = ctk.CTkLabel(self.f5_frame, text="", font=("Roboto", 13), text_color=self.colors["fg_dim"], anchor="w", justify="left")
        self.lbl_f5_desc.pack(fill="x", padx=20, pady=(0, 5))
        
        self.f5_btns = ctk.CTkFrame(self.f5_frame, fg_color="transparent")
        self.f5_btns.pack(fill="x", padx=15, pady=(0, 10))
        
        self.btn_chromium = ctk.CTkButton(self.f5_btns, text="Chromium", width=100, height=32, command=lambda: self.install_sw("chromium"))
        self.btn_chromium.pack(side="left", padx=5)
        
        self.btn_tools = ctk.CTkButton(self.f5_btns, text="", height=32, command=self.open_autodarts_tools)
        self.btn_tools.pack(side="left", padx=5)

        self.btn_rustdesk = ctk.CTkButton(self.f5_btns, text="RustDesk", width=100, height=32, 
                                          fg_color="#d35400", hover_color="#e67e22",
                                          command=lambda: self.install_sw("rustdesk"))
        self.btn_rustdesk.pack(side="left", padx=5)

        self.lbl_rustdesk_note = ctk.CTkLabel(self.f5_btns, text="(only required for remote access)", font=("Roboto", 11, "italic"), text_color=self.colors["fg_dim"])
        self.lbl_rustdesk_note.pack(side="left", padx=5)

        # Step 6: Darts Scorer Setup
        self.f6_frame = ctk.CTkFrame(self.scroll, fg_color=self.colors["card"], corner_radius=10)
        self.f6_frame.pack(fill="x", pady=2)
        
        self.f6_top = ctk.CTkFrame(self.f6_frame, fg_color="transparent")
        self.f6_top.pack(fill="x", padx=20, pady=(8, 2))
        
        self.lbl_f6 = ctk.CTkLabel(self.f6_top, text="", font=("Roboto", 16, "bold"), anchor="w")
        self.lbl_f6.pack(side="left")
        
        self.lbl_f6_desc = ctk.CTkLabel(self.f6_frame, text="", font=("Roboto", 13), text_color=self.colors["fg_dim"], anchor="w", justify="left")
        self.lbl_f6_desc.pack(fill="x", padx=20, pady=(0, 5))
        
        self.f6_btns = ctk.CTkFrame(self.f6_frame, fg_color="transparent")
        self.f6_btns.pack(fill="x", padx=15, pady=(0, 10))
        
        self.btn_waydroid = ctk.CTkButton(self.f6_btns, text="Waydroid", width=100, height=32, command=lambda: self.install_sw("waydroid"))
        self.btn_waydroid.pack(side="left", padx=5)

        self.btn_waydroid_debloat = ctk.CTkButton(self.f6_btns, text="Waydroid Debloat", width=130, height=32, command=self.waydroid_debloat)
        self.btn_waydroid_debloat.pack(side="left", padx=5)

        self.btn_aurora = ctk.CTkButton(self.f6_btns, text="Aurora Store Install", width=140, height=32, command=self.install_aurora)
        self.btn_aurora.pack(side="left", padx=5)

        self.btn_darts_scorer = ctk.CTkButton(self.f6_btns, text="Darts Scorer Install", width=140, height=32, command=self.install_darts_scorer)
        self.btn_darts_scorer.pack(side="left", padx=5)

        # Step 7: Visuals
        self.f7_frame = ctk.CTkFrame(self.scroll, fg_color=self.colors["card"], corner_radius=10)
        self.f7_frame.pack(fill="x", pady=2)
        
        self.f7_left = ctk.CTkFrame(self.f7_frame, fg_color="transparent")
        self.f7_left.pack(side="left", padx=20, pady=8, fill="both", expand=True)
        
        self.lbl_f7 = ctk.CTkLabel(self.f7_left, text="", font=("Roboto", 16, "bold"), anchor="w")
        self.lbl_f7.pack(fill="x", anchor="w")
        
        self.lbl_f7_desc = ctk.CTkLabel(self.f7_left, text="", font=("Roboto", 13), text_color=self.colors["fg_dim"], anchor="w", justify="left")
        self.lbl_f7_desc.pack(fill="x", anchor="w")
        
        self.btn_f7 = ctk.CTkButton(self.f7_frame, text="", width=120, height=35,
                                    font=("Roboto", 14, "bold"),
                                    command=self.apply_visuals)
        self.btn_f7.pack(side="right", padx=20, pady=8)
        
        self.update_texts()

    def apply_visuals(self):
        script = '''
        echo "Setting background image..."
        gsettings set org.gnome.desktop.background picture-uri 'file:///usr/share/backgrounds/gnome/map-l.svg'
        gsettings set org.gnome.desktop.background picture-uri-dark 'file:///usr/share/backgrounds/gnome/map-d.svg'
        
        sleep 1
        
        echo "Enabling Dark Mode..."
        gsettings set org.gnome.desktop.interface color-scheme 'prefer-dark'
        
        echo "Visuals updated successfully!"
        '''
        ServiceUtils.run_bash_script(self, script, "IteraThor: Set up Visuals")

    def install_sw(self, app):
        cmds = {
            "chromium": "sudo dnf install -y chromium && python3 -c \"import subprocess; favs=eval(subprocess.check_output(['gsettings', 'get', 'org.gnome.shell', 'favorite-apps']).decode().strip()); favs=list(set(favs + ['chromium-browser.desktop'])); subprocess.run(['gsettings', 'set', 'org.gnome.shell', 'favorite-apps', str(favs)])\"",
            "waydroid": "sudo dnf install -y waydroid && sudo systemctl enable --now waydroid-container && sudo waydroid init -s VANILLA -c https://ota.waydro.id/system -v https://ota.waydro.id/vendor",
            "rustdesk": "sudo dnf install -y https://github.com/rustdesk/rustdesk/releases/download/1.4.7/rustdesk-1.4.7-0.x86_64.rpm"
        }
        ServiceUtils.run_bash_script(self, cmds.get(app), f"Installing {app.capitalize()}")

    def open_autodarts_tools(self):
        try:
            subprocess.run(["chromium-browser", "--version"], capture_output=True, check=True)
            installed = True
        except:
            installed = False
        
        if installed:
            url = "https://chromewebstore.google.com/detail/oolfddhehmbpdnlmoljmllcdggmkgihh?utm_source=item-share-cb"
            subprocess.Popen(["chromium-browser", url])
        else:
            messagebox.showwarning("SUIT", "Chromium is not installed. Please install it first.")

    def install_darts_scorer(self):
        msg = ("1. Search for 'Darts Scorer' in Aurora Store and install it.\n\n"
              "Note: The app language depends on Waydroid system settings. "
              "Change it in Waydroid Settings if needed.")
        messagebox.showinfo("Darts Scorer Installation", msg)
        subprocess.Popen(["waydroid", "app", "launch", "com.aurora.store"])

    def install_aurora(self):
        script = '''
        AURORA_APK="$HOME/SUIT/bin/AuroraStore-4.8.3.apk"
        if [ ! -f "$AURORA_APK" ]; then
            echo "Creating bin directory and downloading Aurora Store..."
            mkdir -p "$HOME/SUIT/bin"
            wget -O "$AURORA_APK" "https://gitlab.com/-/project/6922885/uploads/b9f5d827145461a2195699660545160a/AuroraStore-4.8.3.apk"
        fi
        
        echo "Installing Aurora Store into Waydroid..."
        waydroid app install "$AURORA_APK"
        
        echo "Updating desktop database..."
        update-desktop-database ~/.local/share/applications/
        
        echo "Aurora Store installation complete!"
        '''
        ServiceUtils.run_bash_script(self, script, "IteraThor: Install Aurora Store")

    def waydroid_debloat(self):
        script = '''
        echo "Detecting system locale..."
        LOCALE=$(localectl status | grep "System Locale" | cut -d'=' -f2 | xargs)
        echo "Detected locale: $LOCALE"
        
        echo "Setting Waydroid language and executing debloat..."
        sudo waydroid shell settings put system system_locales $LOCALE
        
        printf "pm disable-user --user 0 org.lineageos.jelly
        pm disable-user --user 0 com.android.calculator2
        pm disable-user --user 0 com.android.providers.calendar
        pm disable-user --user 0 org.lineageos.etar
        pm disable-user --user 0 org.lineageos.aperture
        pm disable-user --user 0 com.android.deskclock
        pm disable-user --user 0 com.android.contacts
        pm disable-user --user 0 com.android.documentsui
        pm disable-user --user 0 com.android.gallery3d
        pm disable-user --user 0 org.lineageos.eleven
        pm disable-user --user 0 org.lineageos.recorder
        am force-stop com.android.launcher3" | sudo waydroid shell
        
        echo "Enabling Multi-Window (Seamless) mode..."
        waydroid prop set persist.waydroid.multi_windows true
        
        echo "Enabling Dark Theme..."
        waydroid prop set persist.waydroid.dark_theme true

        echo "Waydroid debloat and seamless setup complete!"
        echo "Please restart your Waydroid session to apply all changes."
        '''
        ServiceUtils.run_bash_script(self, script, "IteraThor: Waydroid Debloat & Seamless Setup")

    def debloat_system(self):
        pkgs = [
            "gnome-contacts", "gnome-weather", "gnome-clocks", "mediawriter",
            "gnome-maps", "simple-scan", "gnome-boxes", "libreoffice*",
            "showtime", "snapshot", "gnome-characters", "gnome-tour",
            "yelp", "gnome-font-viewer", "papers", "gnome-connections",
            "malcontent-control", "firefox", "gnome-text-editor",
            "gnome-calculator", "gnome-calendar", "loupe", "decibels"
        ]
        script = f'''
        echo "Removing unnecessary packages..."
        sudo dnf remove -y {" ".join(pkgs)}
        
        echo "Cleaning up GNOME Dash (Unpinning Software and Files)..."
        python3 -c "import subprocess; favs=eval(subprocess.check_output(['gsettings', 'get', 'org.gnome.shell', 'favorite-apps']).decode().strip()); favs=[f for f in favs if f not in ['org.gnome.Software.desktop', 'org.gnome.Nautilus.desktop']]; subprocess.run(['gsettings', 'set', 'org.gnome.shell', 'favorite-apps', str(favs)])" || true
        
        echo "Moving Seahorse to Utilities folder..."
        python3 -c "import subprocess; schema='org.gnome.desktop.app-folders.folder:/org/gnome/desktop/app-folders/folders/Utilities/'; apps=eval(subprocess.check_output(['gsettings', 'get', schema, 'apps']).decode().strip()); apps=list(set(apps + ['org.gnome.seahorse.Application.desktop'])); subprocess.run(['gsettings', 'set', schema, 'apps', str(apps)])" || true
        
        echo "Debloat and UI cleanup complete!"
        '''
        ServiceUtils.run_bash_script(self, script, "IteraThor: Debloat")

    def fix_keyring(self):
        script = '''
        echo "Installing Seahorse (Passwords and Keys)..."
        sudo dnf install -y seahorse
        
        echo "Launching Seahorse..."
        seahorse &
        sleep 2
        
        zenity --info --title="GNOME Keyring Manual Action" --width=400 --text="Due to security restrictions, you must disable the keyring password manually:\\n\\n• Open the app\\n• Press the back button on the top left corner\\n• Right-click the 'login' folder and select 'Change Password'\\n• Enter your current password\\n• Press 'Continue' without any input to have a blank keyring" || true
        '''
        ServiceUtils.run_bash_script(self, script, "IteraThor: Keyring Fix")

    def enable_autologin(self):
        script = '''
        USER=$(whoami)
        CONF="/etc/gdm/custom.conf"
        echo "Enabling automatic login for user: $USER..."
        
        # Remove existing entries to avoid duplicates
        sudo sed -i '/^AutomaticLoginEnable=/d' $CONF
        sudo sed -i '/^AutomaticLogin=/d' $CONF
        
        # Add new entries under [daemon]
        sudo sed -i '/^\\[daemon\\]/a AutomaticLoginEnable=True\\nAutomaticLogin='$USER $CONF
        
        echo "Automatic login enabled. Please reboot to verify."
        '''
        ServiceUtils.run_bash_script(self, script, "IteraThor: Auto Login")

    def apply_sys_settings(self):
        script = '''
        echo "Applying energy profile: throughput-performance..."
        sudo tuned-adm profile throughput-performance || true
        
        echo "Disabling screen dimming and auto-suspend..."
        gsettings set org.gnome.desktop.session idle-delay 0 || true
        gsettings set org.gnome.settings-daemon.plugins.power idle-dim false || true
        gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing' || true
        gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-battery-type 'nothing' || true
        
        echo "Setting power button to interactive prompt..."
        gsettings set org.gnome.settings-daemon.plugins.power power-button-action 'interactive' || true
        sudo rm -f /etc/systemd/logind.conf.d/99-suit-power.conf
        
        echo "System settings applied successfully!"
        '''
        ServiceUtils.run_bash_script(self, script, "IteraThor: System Settings")

    def update_texts(self):
        l = getattr(self.controller, "lang", "en")
        texts = getattr(self.controller, "texts", {})
        def txt(k): return texts.get(k, {}).get(l, k)

        self.lbl_title.configure(text=txt("it_header"))
        self.lbl_info.configure(text=txt("desc_iterathor"))
        self.lbl_f1.configure(text=txt("it_step1_lbl"))
        self.lbl_f1_desc.configure(text=txt("it_step1_desc"))
        self.btn_f1.configure(text=txt("it_btn_apply"))
        
        self.lbl_f2.configure(text=txt("it_step2_lbl"))
        self.lbl_f2_desc.configure(text=txt("it_step2_desc"))
        self.btn_f2.configure(text=txt("it_btn_apply"))

        self.lbl_f3.configure(text=txt("it_step3_lbl"))
        self.lbl_f3_desc.configure(text=txt("it_step3_desc"))
        self.btn_f3.configure(text=txt("it_btn_apply"))

        self.lbl_f4.configure(text=txt("it_step4_lbl"))
        self.lbl_f4_desc.configure(text=txt("it_step4_desc"))
        self.btn_f4.configure(text=txt("it_btn_apply"))

        self.lbl_f5.configure(text=txt("it_step5_lbl"))
        self.lbl_f5_desc.configure(text=txt("it_step5_desc"))
        self.btn_tools.configure(text=txt("it_btn_tools"))

        self.lbl_f6.configure(text=txt("it_step6_lbl"))
        self.lbl_f6_desc.configure(text=txt("it_step6_desc"))

        self.lbl_f7.configure(text=txt("it_step7_lbl"))
        self.lbl_f7_desc.configure(text=txt("it_step7_desc"))
        self.btn_f7.configure(text=txt("it_btn_apply"))
