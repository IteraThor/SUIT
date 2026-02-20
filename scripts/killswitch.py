import evdev
import subprocess
import time
import json
import os
import sys

# Absolute path for the service
CONFIG_PATH = "/home/autodarts/.suit_killswitch_config"

def kill_kiosk():
    try:
        print("ACTION: Killing kiosk processes...")
        subprocess.run(["/usr/bin/pkill", "-9", "-f", "chromium"], check=False)
        subprocess.run(["/usr/bin/pkill", "-9", "-f", "firefox"], check=False)
    except Exception as e:
        print(f"ERROR: {e}")

def main():
    # Force output to be visible in journal
    print("SUIT Killswitch service starting up...", flush=True)
    
    while True:
        if not os.path.exists(CONFIG_PATH):
            print(f"Waiting for config: {CONFIG_PATH}", flush=True)
            time.sleep(5)
            continue

        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
            
            device_path = config.get('device')
            target_key = config.get('key')
            
            if not os.path.exists(device_path):
                print(f"Device not found: {device_path}", flush=True)
                time.sleep(5)
                continue

            device = evdev.InputDevice(device_path)
            print(f"READY: Monitoring {device.name} for key {target_key}", flush=True)
            
            press_count = 0
            last_press_time = 0
            
            for event in device.read_loop():
                if event.type == evdev.ecodes.EV_KEY and event.code == target_key and event.value == 1:
                    current_time = time.time()
                    if current_time - last_press_time < 1.5:
                        press_count += 1
                    else:
                        press_count = 1
                    
                    last_press_time = current_time
                    print(f"EVENT: Press {press_count}/5", flush=True)
                    
                    if press_count >= 5:
                        kill_kiosk()
                        press_count = 0
                        
        except Exception as e:
            print(f"RUNTIME ERROR: {e}", flush=True)
            time.sleep(5)

if __name__ == "__main__":
    main()
