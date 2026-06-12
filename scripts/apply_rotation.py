import dbus
import sys
import os
import subprocess
import time
import json
from collections import defaultdict

# ==========================================
# UNIVERSAL SCREEN & TOUCH ROTATOR
# ==========================================

nested_dict = lambda: defaultdict(nested_dict)

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

def apply_rotation_gnome(monitor_name, mode):
    state = get_gnome_display_config()
    if not state: return False
    serial, monitors, logical_monitors, properties = state
    
    target_trans = rot_to_trans(mode)
    new_logical_monitors = []
    
    found = False
    for lm in logical_monitors:
        x, y, scale, trans, is_primary, phys_monitors = lm[:6]
        new_phys = []
        for pm in phys_monitors:
            name, mode_id, props = pm[:3]
            if name == monitor_name:
                trans = target_trans
                found = True
            new_phys.append([name, mode_id, props])
        new_logical_monitors.append([x, y, scale, trans, is_primary, new_phys])

    if not found:
        print(f"Monitor {monitor_name} not found.")
        return False

    try:
        bus = dbus.SessionBus()
        dc = bus.get_object('org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig')
        dc_iface = dbus.Interface(dc, dbus_interface='org.gnome.Mutter.DisplayConfig')
        dc_iface.ApplyMonitorsConfig(serial, 1, new_logical_monitors, {})
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

    # Find total desktop size and target logical monitor
    for lm in logical_monitors:
        x, y, scale, trans, is_primary, phys_monitors = lm[:6]
        # Get logical dimensions
        # Logical monitors don't directly give w/h in the GetCurrentState output sometimes?
        # Actually, they do if we look at the mode of the physical monitor.
        # But wait, LM x/y and trans are what matter.
        for pm in phys_monitors:
            p_name = pm[0]
            # Find the monitor info to get the mode resolution
            for m_info in monitors:
                if m_info[0][0] == p_name:
                    # Current mode
                    for mode in m_info[1]:
                        if 'is-current' in mode[6]:
                            w, h = mode[2], mode[3]
                            # If rotated, swap w/h for the bounding box calculation
                            if trans in [1, 3]: w, h = h, w
                            total_w = max(total_w, x + w)
                            total_h = max(total_h, y + h)
                            if p_name == monitor_name:
                                target_lm = {'x': x, 'y': y, 'w': w, 'h': h, 'trans': trans}

    if not target_lm or total_w == 0 or total_h == 0:
        print("Could not determine geometry.")
        return

    x, y, w, h, trans = target_lm['x'], target_lm['y'], target_lm['w'], target_lm['h'], target_lm['trans']
    
    # Calculate Matrix based on rotation
    # Normal (0): [w/tw, 0, x/tw, 0, h/th, y/th, 0, 0, 1]
    # Left (1):   [0, w/tw, x/tw, -h/th, 0, (y+h)/th, 0, 0, 1]
    # Inverted (2): [-w/tw, 0, (x+w)/tw, 0, -h/th, (y+h)/th, 0, 0, 1]
    # Right (3):  [0, -w/tw, (x+w)/tw, h/th, 0, y/th, 0, 0, 1]

    wf, hf = w/total_w, h/total_h
    xf, yf = x/total_w, y/total_h

    if trans == 0: # Normal
        matrix = [wf, 0, xf, 0, hf, yf, 0, 0, 1]
    elif trans == 1: # Left (90 CCW)
        matrix = [0, wf, xf, -hf, 0, yf + hf, 0, 0, 1]
    elif trans == 2: # Inverted (180)
        matrix = [-wf, 0, xf + wf, 0, -hf, yf + hf, 0, 0, 1]
    elif trans == 3: # Right (90 CW)
        matrix = [0, -wf, xf + wf, hf, 0, yf, 0, 0, 1]
    else:
        matrix = [wf, 0, xf, 0, hf, yf, 0, 0, 1]

    matrix_str = " ".join([f"{v:.6f}" for v in matrix])
    print(f"Applying Matrix to {touch_device_name}: {matrix_str}")

    udev_rule = f'ACTION=="add|change", KERNEL=="event*", ATTRS{{name}}=="{touch_device_name}", ENV{{LIBINPUT_CALIBRATION_MATRIX}}="{matrix_str}"\n'
    rule_path = "/etc/udev/rules.d/99-suit-touch.rules"
    
    try:
        with open("/tmp/suit_touch.rules", "w") as f:
            f.write(udev_rule)
        subprocess.run(f"sudo cp /tmp/suit_touch.rules {rule_path}", shell=True, check=True)
        subprocess.run("sudo udevadm control --reload-rules && sudo udevadm trigger", shell=True, check=True)
    except Exception as e:
        print(f"Error applying udev rule: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        # Load from JSON config
        config_path = os.path.expanduser("~/.suit_rotation_config.json")
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                config = json.load(f)
                for mon_name, settings in config.items():
                    mode = settings.get('rotation', 'normal')
                    touch = settings.get('touch_device')
                    print(f"Restoring {mon_name} to {mode}...")
                    apply_rotation_gnome(mon_name, mode)
                    if touch and touch != "None":
                        calculate_and_apply_matrix(mon_name, touch)
    else:
        mode = sys.argv[1]
        monitor = sys.argv[2]
        touch = sys.argv[3] if len(sys.argv) > 3 else "None"
        
        if apply_rotation_gnome(monitor, mode):
            if touch and touch != "None":
                calculate_and_apply_matrix(monitor, touch)
