import dbus
import sys
import os
import subprocess
import time
from collections import defaultdict

# ==========================================
# SCREEN ROTATOR LOGIK (Replicated from oldsuit.py)
# ==========================================
nested_dict = lambda: defaultdict(nested_dict)

def rot_to_trans(r): 
    # Mappings from oldsuit.py
    return {'normal': 0, 'inverted': 6, 'left': 1, 'right': 3}.get(r, 0)

def trans_needs_w_h_swap(old_trans, new_trans): 
    return (old_trans in [0, 6] and new_trans in [1, 3]) or (old_trans in [1, 3] and new_trans in [0, 6])

def mode_id_to_vals(mode_id):
    try:
        w, h_rate = mode_id.split('x')
        h, rate = h_rate.split('@')
        return (int(w), int(h), float(rate))
    except:
        return (0, 0, 0.0)

def get_current_mode(monitor):
    for md in monitor[1]:
        if 'is-current' in md[6]: return md
    return None

class ConfigInfo:
    def __init__(self, serial, monitors, logical_monitors, properties):
        self.serial = serial
        self.monitors = monitors
        self.logical_monitors = logical_monitors
        self.output_config = nested_dict()
        self.primary = None
        self.__init_output_config(logical_monitors)

    def __init_output_config(self, logical_monitors):
        for lm in logical_monitors:
            x, y, scale, trans, is_primary, phys_monitors = lm[:6]
            if is_primary == True and len(phys_monitors) > 0: self.primary = phys_monitors[0][0]
            for m in phys_monitors:
                output_name = m[0]
                conf = self.output_config[output_name]
                monitor = self.get_monitor_by_output(output_name)
                if not monitor: continue
                md = get_current_mode(monitor)
                if not md: continue
                w, h, r = mode_id_to_vals(md[0])
                conf['monitor'] = monitor
                conf['mode-info'] = md
                conf['old-mode-id'] = md[0]
                conf['res'], conf['w'], conf['h'], conf['rate'] = f'{w}x{h}', w, h, r
                conf['scale'], conf['trans'] = scale, trans

    def get_monitor_by_output(self, output):
        for m in self.monitors:
            if m[0][0] == output: return m
        return None

    def update_output_config(self, output_name, rotation_mode):
        if output_name not in self.output_config: return
        conf = self.output_config[output_name]
        new_trans = rot_to_trans(rotation_mode)
        if trans_needs_w_h_swap(conf['trans'], new_trans): conf['w'], conf['h'] = conf['h'], conf['w']
        conf['trans'] = new_trans

    def apply(self):
        new_lm = []
        for lm in self.logical_monitors:
            x, y, scale, trans, is_primary, phys_monitors_raw = lm[:6]
            new_phys_monitors = []
            for pm in phys_monitors_raw:
                connector_name = pm[0]
                if connector_name in self.output_config:
                    saved_conf = self.output_config[connector_name]
                    mode_id = saved_conf['old-mode-id']
                    trans = saved_conf['trans']
                    new_phys_monitors.append([connector_name, mode_id, {}])
            if new_phys_monitors:
                new_lm.append([x, y, scale, trans, is_primary, new_phys_monitors])
        return new_lm

def apply_rotation_gnome(mode, target_output=None):
    try:
        bus = dbus.SessionBus()
        dc = bus.get_object('org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig')
        dc_iface = dbus.Interface(dc, dbus_interface='org.gnome.Mutter.DisplayConfig')
        serial, monitors, logical_monitors, properties = dc_iface.GetCurrentState()
        
        available_outputs = [m[0][0] for m in monitors]
        if target_output not in available_outputs:
            if len(available_outputs) > 0:
                print(f"Monitor {target_output} nicht gefunden. Nutze {available_outputs[0]}")
                target_output = available_outputs[0]
            else:
                return False

        config = ConfigInfo(serial, monitors, logical_monitors, properties)
        config.update_output_config(target_output, mode)
        new_lm = config.apply()
        
        if not new_lm: return False
        dc_iface.ApplyMonitorsConfig(serial, 1, new_lm, {})
        print(f"Successfully rotated {target_output} to {mode} via DBus")
        return True
    except Exception as e:
        sys.stderr.write(f"Screen Rotator Error (DBus): {e}\n")
        return False

def apply_rotation_xrandr(mode, monitor):
    try:
        xr_mode = {'normal': 'normal', 'inverted': 'inverted', 'left': 'left', 'right': 'right'}.get(mode, 'normal')
        cmd = ["xrandr", "--output", monitor, "--rotate", xr_mode]
        subprocess.run(cmd, check=True)
        print(f"Successfully rotated {monitor} to {mode} via xrandr")
        return True
    except Exception as e:
        print(f"Error applying rotation via xrandr: {e}")
        return False

if __name__ == "__main__":
    # Delay to ensure display server is ready during autostart
    time.sleep(3)
    
    mode = "normal"
    monitor = None

    if len(sys.argv) > 2:
        mode = sys.argv[1]
        monitor = sys.argv[2]
    elif len(sys.argv) > 1:
        mode = sys.argv[1]
    else:
        # Fallback to config file
        config_path = os.path.expanduser("~/.suit_rotation_config")
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                for line in f:
                    if line.startswith("ROTATION="):
                        mode = line.split("=")[1].strip()
                    elif line.startswith("MONITOR="):
                        monitor = line.split("=")[1].strip()

    # Try GNOME DBus first, then fallback to xrandr
    if not apply_rotation_gnome(mode, monitor):
        if monitor:
            apply_rotation_xrandr(mode, monitor)
        else:
            # Try to find a connected monitor for xrandr fallback
            try:
                output = subprocess.check_output(["xrandr"], text=True)
                monitors = [line.split()[0] for line in output.splitlines() if " connected" in line]
                if monitors:
                    apply_rotation_xrandr(mode, monitors[0])
            except:
                pass
