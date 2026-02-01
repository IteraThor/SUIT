import asyncio
import websockets
import json
import serial
import serial.tools.list_ports
import time
import os

# =============================================================================
# CONFIGURATION MANAGEMENT
# =============================================================================

# Wir nutzen absolute Pfade, damit der Dienst die Datei immer findet
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(PROJECT_DIR, "autoglow_config.json")

DEFAULT_CONFIG = {
    "global_brightness": 255,
    "Throw": {"on": True, "bri": 255, "tt": 0, "seg": {"fx": 0, "col": [[0, 255, 0]]}, "enabled": True},
    "Takeout in progress": {"on": True, "bri": 255, "tt": 0, "seg": {"fx": 0, "col": [[255, 0, 0]]}, "enabled": True},
    "Takeout": {"on": True, "bri": 255, "tt": 0, "seg": {"fx": 0, "col": [[255, 255, 0]]}, "enabled": True},
    "Starting": {"on": True, "bri": 255, "tt": 0, "seg": {"fx": 0, "col": [[0, 0, 255]]}, "enabled": True},
    "Stopped": {"on": True, "bri": 255, "tt": 0, "seg": {"fx": 0, "col": [[255, 0, 255]]}, "enabled": True},
    "Calibrating": {"on": True, "bri": 255, "tt": 0, "seg": {"fx": 0, "col": [[128, 0, 128]]}, "enabled": True},
    "Error": {"on": True, "bri": 255, "tt": 0, "seg": {"fx": 1, "col": [[255, 0, 0]]}, "enabled": True}
}

def load_config():
    if not os.path.exists(CONFIG_FILE) or os.path.getsize(CONFIG_FILE) == 0:
        with open(CONFIG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
        return DEFAULT_CONFIG
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"--> ERROR loading config: {e}")
        return DEFAULT_CONFIG

# =============================================================================
# CORE LOGIC
# =============================================================================

ser = None

def find_esp32_port():
    KNOWN_VID_PIDS = [(0x10C4, 0xEA60), (0x1A86, 0x7523), (0x0403, 0x6001), (0x303A, 0x1001)]
    ports = serial.tools.list_ports.comports()
    for port in ports:
        if (port.vid, port.pid) in KNOWN_VID_PIDS:
            return port.device
    return None

def send_wled_command(command_dict):
    global ser
    if ser and ser.is_open:
        try:
            wled_msg = {k: v for k, v in command_dict.items() if k != "enabled"}
            ser.write((json.dumps(wled_msg) + '\n').encode())
        except Exception as e:
            print(f"--> ERROR sending to WLED: {e}")
            ser.close()

async def handle_status_change(status):
    config = load_config()
    status_config = config.get(status)
    global_bri = config.get("global_brightness", 255)
    
    if status_config and status_config.get("enabled", True):
        print(f"--> [BLOCK] Executing logic for '{status}' (Brightness: {global_bri}).")
        command = status_config.copy()
        command.pop("enabled", None)
        command["bri"] = global_bri
        send_wled_command(command)
    else:
        print(f"--> [BLOCK] Status '{status}' is disabled or not configured.")

async def autodarts_logger():
    uri = "ws://localhost:3180/api/events"
    global ser
    esp_port = find_esp32_port()
    if esp_port:
        try:
            ser = serial.Serial(esp_port, 115200, timeout=1)
            time.sleep(2)
            print(f"--> Serial connection established on {esp_port}.")
        except Exception as e:
            print(f"--> ERROR opening port: {e}")

    while True:
        try:
            async with websockets.connect(uri) as websocket:
                print(f"\nConnected to {uri}. Waiting for events...")
                while True:
                    message = await websocket.recv()
                    data = json.loads(message)
                    if data.get("type") == "state" and "status" in data.get("data", {}):
                        status = data["data"]["status"]
                        print(f"Status received: {status}")
                        await handle_status_change(status)
        except Exception as e:
            print(f"Connection failed: {e}. Retrying in 5 seconds...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(autodarts_logger())
    except KeyboardInterrupt:
        if ser and ser.is_open:
            ser.close()
        print("\nScript terminated.")

