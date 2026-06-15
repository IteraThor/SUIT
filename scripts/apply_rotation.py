import dbus
import sys
from pathlib import Path
import subprocess
import time
import json
from collections import defaultdict

# ==========================================
# UNIVERSAL SCREEN & TOUCH ROTATOR (GNOME 50 FIXED)
# ==========================================

def rot_to_trans(r):
    return {'normal': 0, 'inverted': 2, 'left': 1, 'right': 3}.get(r, 0)

def get_gnome_display_config():
    try:
        bus = dbus.SessionBus()
        dc = bus.get_object('org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig')
        dc_iface = dbus.Interface(dc, dbus_interface='org.gnome.Mutter.DisplayConfig')
        return dc_iface.GetCurrentState()
    except Exception as e:
        print(f"Error getting GNOME display config: {e}")
        return None

def apply_rotation_gnome(monitor_name, mode, method=2):
    state = get_gnome_display_config()
    if not state: return False
    serial, monitors, logical_monitors, properties = state
    
    target_trans = rot_to_trans(mode)
    new_logical_monitors = []
    
    found = False
    for lm in logical_monitors:
        # LM in state: (x, y, scale, transform, is_primary, physical_monitors, properties)
        x, y, scale, trans, is_primary, phys_monitors, lm_props = lm
        
        new_phys = []
        for pm in phys_monitors:
            p_name = pm[0]
            mode_id = ""
            for m_info in monitors:
                if m_info[0][0] == p_name:
                    for m_mode in m_info[1]:
                        if 'is-current' in m_mode[6]:
                            mode_id = m_mode[0]
                            break
            
            if p_name == monitor_name:
                trans = target_trans
                found = True
            
            new_phys.append([dbus.String(p_name), dbus.String(mode_id), {}])
            
        new_logical_monitors.append([
            dbus.Int32(x), dbus.Int32(y), 
            dbus.Double(scale), dbus.UInt32(trans), 
            dbus.Boolean(is_primary), new_phys
        ])

    if not found:
        print(f"Monitor {monitor_name} not found in logical monitors.")
        return False

    try:
        bus = dbus.SessionBus()
        dc = bus.get_object('org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig')
        dc_iface = dbus.Interface(dc, dbus_interface='org.gnome.Mutter.DisplayConfig')
        
        dc_iface.ApplyMonitorsConfig(
            dbus.UInt32(serial), 
            dbus.UInt32(method), 
            new_logical_monitors, 
            {}
        )
        print(f"Successfully rotated {monitor_name} to {mode} (method {method})")
        return True
    except Exception as e:
        print(f"Error applying GNOME config: {e}")
        return False

def calculate_and_apply_matrix(monitor_name, touch_device_name):
    # Wait for GNOME to settle layout
    time.sleep(2)
    state = get_gnome_display_config()
    if not state: return
    serial, monitors, logical_monitors, properties = state

    total_w = 0
    total_h = 0
    target_lm = None

    # Calculate total bounding box and find target monitor's logical geometry
    for lm in logical_monitors:
        x, y, scale, trans, is_primary, phys_monitors, lm_props = lm
        
        for pm in phys_monitors:
            p_name = pm[0]
            for m_info in monitors:
                if m_info[0][0] == p_name:
                    for mode in m_info[1]:
                        if 'is-current' in mode[6]:
                            w, h = mode[1], mode[2]
                            if trans in [1, 3]: w, h = h, w
                            
                            total_w = max(total_w, x + w)
                            total_h = max(total_h, y + h)
                            
                            if p_name == monitor_name:
                                target_lm = {'x': x, 'y': y, 'w': w, 'h': h, 'trans': trans}

    if not target_lm or total_w == 0 or total_h == 0:
        print("Could not determine geometry for matrix calculation.")
        return

    x, y, w, h, trans = target_lm['x'], target_lm['y'], target_lm['w'], target_lm['h'], target_lm['trans']
    
    wf, hf = float(w)/float(total_w), float(h)/float(total_h)
    xf, yf = float(x)/float(total_w), float(y)/float(total_h)

    def mult3x3(A, B):
        C = [0.0]*9
        for i in range(3):
            for j in range(3):
                C[i*3 + j] = A[i*3 + 0]*B[0*3 + j] + A[i*3 + 1]*B[1*3 + j] + A[i*3 + 2]*B[2*3 + j]
        return C

    # The final libinput matrix is composed as  S * ROT(trans) * H , i.e. the
    # raw touch coords are first un-skewed by the hardware calibration H, then
    # rotated to match the screen's CURRENT orientation, then placed into this
    # monitor's rectangle of the desktop. Keeping these three factors separate
    # is what makes flipping rigid: ROT is always recomputed from the live GNOME
    # transform, so the touch follows the screen no matter what H was.

    # ROT(trans): pure rotation in the normalised [0..1] unit square. NO region
    # scaling here (that is S's job) and NO calibration (that is H's job).
    #
    # The touch transform must be the INVERSE of the screen's rotation: to make a
    # touch land under the finger on a screen rotated 90 deg CW, the raw coords
    # have to be rotated 90 deg CCW (and vice-versa). GNOME does NOT auto-rotate
    # this touchscreen, so we do the whole rotation here. 180 deg and normal are
    # their own inverses, so only the two 90 deg cases differ from the screen.
    if trans == 1:   # screen 90 CCW (left)  -> rotate touch 90 CW
        ROT = [0, -1, 1, 1, 0, 0, 0, 0, 1]
    elif trans == 2: # screen 180 (inverted) -> 180 (self-inverse)
        ROT = [-1, 0, 1, 0, -1, 1, 0, 0, 1]
    elif trans == 3: # screen 90 CW (right)  -> rotate touch 90 CCW
        ROT = [0, 1, 0, -1, 0, 1, 0, 0, 1]
    else:            # Normal (0) / unknown
        ROT = [1, 0, 0, 0, 1, 0, 0, 0, 1]

    # S: region scale/translate only -- maps this monitor's [0..1] screen space
    # into its rectangle within the full desktop bounding box libinput spans.
    # Carries NO rotation. For a single monitor this is the identity.
    S = [wf, 0, xf, 0, hf, yf, 0, 0, 1]

    # Final mapping = region * inverse-rotation. No hardware calibration step:
    # the panel maps linearly, so rotation + region placement is all that's needed.
    matrix = mult3x3(S, ROT)

    # Libinput only takes the first 6 elements of the 3x3 affine matrix
    matrix_str = " ".join([f"{v:.6f}" for v in matrix[:6]])
    print(f"Applying Matrix to {touch_device_name}: {matrix_str}")

    rule_path = "/etc/udev/rules.d/99-suit-touch.rules"
    udev_rule = f'ACTION=="add|change", KERNEL=="event*", ATTRS{{name}}=="{touch_device_name}", ENV{{LIBINPUT_CALIBRATION_MATRIX}}="{matrix_str}"\n'
    
    try:
        with open("/tmp/suit_touch.rules", "w") as f:
            f.write(udev_rule)
        subprocess.run(f"sudo cp /tmp/suit_touch.rules {rule_path}", shell=True, check=True)
        subprocess.run("sudo udevadm control --reload-rules && sudo udevadm trigger", shell=True, check=True)
    except Exception as e:
        print(f"Error applying udev rule: {e}")

if __name__ == "__main__":
    config_path = Path.home() / ".suit_rotation_config.json"
    
    if len(sys.argv) < 3:
        if config_path.exists():
            try:
                with open(config_path, "r") as f:
                    config = json.load(f)
                    
                    # 1. Apply all screen rotations first
                    for mon_name, settings in config.items():
                        mode = settings.get('rotation', 'normal')
                        apply_rotation_gnome(mon_name, mode, 1)
                    
                    # 2. Find the one screen with touch and apply its matrix
                    touch_applied = False
                    for mon_name, settings in config.items():
                        touch = settings.get('touch_device')
                        if touch and touch != "None":
                            calculate_and_apply_matrix(mon_name, touch)
                            touch_applied = True
                            break # Only one touchscreen allowed
                    
                    if not touch_applied:
                        try:
                            subprocess.run("sudo rm -f /etc/udev/rules.d/99-suit-touch.rules && sudo udevadm control --reload-rules", shell=True)
                        except Exception: pass

            except Exception as e:
                print(f"Error loading config: {e}")
    else:
        mode = sys.argv[1]
        monitor = sys.argv[2]
        touch = sys.argv[3] if len(sys.argv) > 3 else "None"
        method = int(sys.argv[4]) if len(sys.argv) > 4 else 2
        
        if apply_rotation_gnome(monitor, mode, method):
            if touch and touch != "None":
                calculate_and_apply_matrix(monitor, touch)
            else:
                # If we passed "None" for touch, check the config to see if ANOTHER screen has touch
                # before we blindly delete the udev rule.
                touch_applied = False
                if config_path.exists():
                    try:
                        with open(config_path, "r") as f:
                            config = json.load(f)
                            for mon_name, settings in config.items():
                                if mon_name != monitor: # Don't check the one we just disabled
                                    cfg_touch = settings.get('touch_device')
                                    if cfg_touch and cfg_touch != "None":
                                        calculate_and_apply_matrix(mon_name, cfg_touch)
                                        touch_applied = True
                                        break
                    except Exception: pass
                
                if not touch_applied:
                    try:
                        subprocess.run("sudo rm -f /etc/udev/rules.d/99-suit-touch.rules && sudo udevadm control --reload-rules", shell=True)
                    except Exception: pass
