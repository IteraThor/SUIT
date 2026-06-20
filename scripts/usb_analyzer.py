#!/usr/bin/env python3
import os
import sys
import json
import urllib.request
import subprocess
from pathlib import Path

def get_autodarts_config():
    """Fetches the camera configuration from local Autodarts API."""
    try:
        req = urllib.request.Request("http://localhost:3180/api/config", method="GET")
        with urllib.request.urlopen(req, timeout=1.0) as response:
            data = json.loads(response.read().decode())
            return data.get("cam", {})
    except Exception as e:
        # Return empty config if Autodarts is not running or unreachable
        return {}

def get_camera_info():
    """Maps video4linux video nodes to their USB bus and device numbers."""
    cameras = {}
    v4l_dir = Path("/sys/class/video4linux")
    if not v4l_dir.exists():
        return cameras

    for video_node in v4l_dir.glob("video*"):
        device_symlink = video_node / "device"
        if not device_symlink.exists():
            continue
        try:
            resolved_path = device_symlink.resolve()
            # USB devices in sysfs have busnum and devnum files
            # Travel up to find a directory containing busnum and devnum
            curr = resolved_path
            busnum, devnum = None, None
            while curr != curr.parent and len(curr.parts) > 2:
                if (curr / "busnum").exists() and (curr / "devnum").exists():
                    with open(curr / "busnum", "r") as f:
                        busnum = int(f.read().strip())
                    with open(curr / "devnum", "r") as f:
                        devnum = int(f.read().strip())
                    break
                curr = curr.parent

            if busnum is not None and devnum is not None:
                video_dev = f"/dev/{video_node.name}"
                
                # Check what formats are supported via v4l2-ctl (optional fallback)
                formats = []
                try:
                    res = subprocess.run(
                        ["v4l2-ctl", f"--device={video_dev}", "--list-formats"],
                        capture_output=True, text=True, timeout=1.0
                    )
                    for line in res.stdout.splitlines():
                        if "[" in line and "]" in line:
                            fmt = line.split("'")[1] if "'" in line else line.split("]")[1].strip()
                            formats.append(fmt)
                except Exception:
                    pass

                cameras[video_dev] = {
                    "busnum": busnum,
                    "devnum": devnum,
                    "sys_path": str(resolved_path),
                    "formats": formats
                }
        except Exception:
            continue
    return cameras

def parse_usb_devices_file():
    """Parses /sys/kernel/debug/usb/devices for bandwidth allocations and controller speeds."""
    buses = {}
    devices = []
    usb_devices_path = Path("/sys/kernel/debug/usb/devices")
    if not usb_devices_path.exists():
        return buses, devices

    try:
        with open(usb_devices_path, "r") as f:
            content = f.read()
    except Exception as e:
        # Return empty if permission denied or file missing
        return buses, devices

    current_dev = {}
    for block in content.split("\n\n"):
        if not block.strip():
            continue
        
        dev = {}
        for line in block.splitlines():
            line = line.strip()
            parts = line.split()
            if not parts:
                continue
            prefix = parts[0]
            
            if prefix == "T:":
                # T:  Bus=01 Lev=00 Prnt=00 Port=00 Cnt=00 Dev#=  1 Spd=480  MxCh= 9
                line_norm = line.replace("Dev#=  ", "Dev#=").replace("Dev#= ", "Dev#=").replace("MxCh=  ", "MxCh=").replace("MxCh= ", "MxCh=")
                parts_norm = line_norm.split()
                for p in parts_norm[1:]:
                    if "=" in p:
                        k, v = p.split("=")
                        if k == "Bus": dev["bus"] = int(v)
                        elif k == "Dev#": dev["devnum"] = int(v)
                        elif k == "Spd": dev["speed"] = v
                        elif k == "Lev": dev["level"] = int(v)
            elif prefix == "B:":
                # B:  Alloc=  0/800 us ( 0%), #Int=  0, #Iso=  0
                if "Alloc=" in line:
                    alloc_part = line.split("Alloc=")[1].split()[0]
                    percent_part = line.split("(")[1].split("%")[0].strip()
                    dev["alloc_us"] = alloc_part
                    dev["alloc_percent"] = int(percent_part)
            elif prefix == "P:":
                # P:  Vendor=1d6b ProdID=0002 Rev= 7.00
                for p in parts[1:]:
                    if "=" in p:
                        k, v = p.split("=")
                        if k == "Vendor": dev["vendor_id"] = v
                        elif k == "ProdID": dev["product_id"] = v
            elif prefix == "S:":
                # S:  Manufacturer=Linux 7.0.12-201.fc44.x86_64 xhci-hcd
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.split("Manufacturer")[-1].split("Product")[-1].split("SerialNumber")[-1].strip()
                    dev[k.lower()] = v.strip()
            elif prefix == "I:":
                # I:* If#= 0 Alt= 0 #EPs= 1 Cls=09(hub  ) Sub=00 Prot=00 Driver=hub
                if "Driver=" in line:
                    driver = line.split("Driver=")[1].split()[0]
                    if "drivers" not in dev:
                        dev["drivers"] = []
                    if driver not in dev["drivers"]:
                        dev["drivers"].append(driver)

        if "bus" in dev and "devnum" in dev:
            # If level is 0, it is a host controller/root hub
            if dev.get("level", -1) == 0:
                buses[dev["bus"]] = {
                    "bus": dev["bus"],
                    "speed": dev.get("speed", "480"),
                    "alloc_percent": dev.get("alloc_percent", 0),
                    "alloc_us": dev.get("alloc_us", "0/800"),
                    "product": dev.get("product", "Host Controller"),
                    "manufacturer": dev.get("manufacturer", "")
                }
            devices.append(dev)

    return buses, devices

def calculate_bandwidth_estimates(cameras_config, sys_cameras):
    """Estimates bandwidth usage for configured cameras (YUYV and MJPEG options)."""
    # Defaults from Autodarts config or general fallback
    width = cameras_config.get("width", 1280)
    height = cameras_config.get("height", 720)
    fps = cameras_config.get("fps", 30)
    configured_nodes = cameras_config.get("cams", [])

    estimates = {}
    for node, info in sys_cameras.items():
        is_configured = node in configured_nodes
        # Uncompressed (YUYV) uses 16 bits per pixel
        yuyv_bps = width * height * 16 * fps
        yuyv_mbps = round(yuyv_bps / 1_000_000.0, 1)

        # MJPEG is compressed. Frame size varies, but a typical factor is ~1.5 bits per pixel
        mjpeg_bps = width * height * 1.5 * fps
        mjpeg_mbps = round(mjpeg_bps / 1_000_000.0, 1)

        estimates[node] = {
            "width": width,
            "height": height,
            "fps": fps,
            "yuyv_mbps": yuyv_mbps,
            "mjpeg_mbps": mjpeg_mbps,
            "is_configured": is_configured,
            "supported_formats": info["formats"]
        }
    return estimates

def main():
    cameras_config = get_autodarts_config()
    sys_cameras = get_camera_info()
    buses, devices = parse_usb_devices_file()
    estimates = calculate_bandwidth_estimates(cameras_config, sys_cameras)

    # Attach cameras to devices list
    devices_by_bus = {}
    for dev in devices:
        bus = dev["bus"]
        if bus not in devices_by_bus:
            devices_by_bus[bus] = []

        # Find if this device is a camera in our system camera map
        matched_cam = None
        for cam_node, cam_info in sys_cameras.items():
            if cam_info["busnum"] == bus and cam_info["devnum"] == dev["devnum"]:
                matched_cam = cam_node
                break

        if matched_cam:
            dev["camera_node"] = matched_cam
            dev["bandwidth_est"] = estimates[matched_cam]
            dev["is_camera"] = True
        else:
            dev["is_camera"] = False

        devices_by_bus[bus].append(dev)

    # Aggregate loads per bus
    bus_reports = []
    for bus_id, bus_info in buses.items():
        bus_devs = devices_by_bus.get(bus_id, [])
        cameras_on_bus = [d for d in bus_devs if d.get("is_camera")]
        
        # Calculate totals
        total_yuyv_mbps = sum(c["bandwidth_est"]["yuyv_mbps"] for c in cameras_on_bus)
        total_mjpeg_mbps = sum(c["bandwidth_est"]["mjpeg_mbps"] for c in cameras_on_bus)

        # Bus capacity based on speed
        speed = bus_info["speed"]
        if speed == "5000":
            capacity_mbps = 5000.0
            type_label = "USB 3.0 (SuperSpeed)"
        elif speed == "10000":
            capacity_mbps = 10000.0
            type_label = "USB 3.1 (SuperSpeed+)"
        else:
            capacity_mbps = 480.0
            type_label = "USB 2.0 (High-Speed)"

        # Safe ceilings (typically 75% of max capacity)
        safe_ceiling = capacity_mbps * 0.75

        bus_reports.append({
            "bus": bus_id,
            "product": bus_info["product"],
            "manufacturer": bus_info["manufacturer"],
            "speed_lbl": speed,
            "type": type_label,
            "capacity_mbps": capacity_mbps,
            "safe_ceiling_mbps": safe_ceiling,
            "kernel_alloc_percent": bus_info["alloc_percent"],
            "kernel_alloc_us": bus_info["alloc_us"],
            "cameras_count": len(cameras_on_bus),
            "cameras": [
                {
                    "node": c["camera_node"],
                    "product": c.get("product", "USB Camera"),
                    "yuyv_mbps": c["bandwidth_est"]["yuyv_mbps"],
                    "mjpeg_mbps": c["bandwidth_est"]["mjpeg_mbps"],
                    "is_configured": c["bandwidth_est"]["is_configured"],
                    "formats": c["bandwidth_est"]["supported_formats"]
                } for c in cameras_on_bus
            ],
            "other_devices": [
                {
                    "product": d.get("product", "USB Device"),
                    "manufacturer": d.get("manufacturer", ""),
                    "speed": d.get("speed", "")
                } for d in bus_devs if not d.get("is_camera") and d.get("level", -1) > 0
            ],
            "est_load_yuyv_mbps": total_yuyv_mbps,
            "est_load_mjpeg_mbps": total_mjpeg_mbps
        })

    # Generate Recommendations
    warnings = []
    recommendations = []
    
    # Check for Bus Saturated
    for report in bus_reports:
        # Check USB 2.0 cameras load
        if report["capacity_mbps"] == 480.0:
            if report["cameras_count"] >= 2:
                warnings.append(
                    f"USB Bus {report['bus']} (USB 2.0) has {report['cameras_count']} cameras connected. "
                    "Sharing a USB 2.0 bus between multiple cameras frequently causes dropped frames or sync issues."
                )
            
            # If estimated MJPEG or YUYV exceeds limit
            if report["est_load_mjpeg_mbps"] > report["safe_ceiling_mbps"]:
                warnings.append(
                    f"USB Bus {report['bus']} is overloaded by cameras. "
                    f"Estimated MJPEG load ({report['est_load_mjpeg_mbps']} Mbps) exceeds safe limit ({report['safe_ceiling_mbps']} Mbps)."
                )

        # General recommendation: finding free USB 3.0 ports
        usb3_buses = [b for b in bus_reports if b["capacity_mbps"] >= 5000.0]
        free_usb2_buses = [b for b in bus_reports if b["capacity_mbps"] == 480.0 and b["cameras_count"] == 0]

        if report["cameras_count"] > 1 and report["capacity_mbps"] == 480.0:
            if usb3_buses:
                best_usb3 = min(usb3_buses, key=lambda b: b["cameras_count"])
                recommendations.append(
                    f"Move one or more cameras from Bus {report['bus']} (USB 2.0) to a USB 3.0 port on Bus {best_usb3['bus']}."
                )
            elif free_usb2_buses:
                best_usb2 = free_usb2_buses[0]
                recommendations.append(
                    f"Move a camera from Bus {report['bus']} to a separate, unused USB 2.0 host controller on Bus {best_usb2['bus']}."
                )

    output = {
        "autodarts_config_found": bool(cameras_config),
        "buses": bus_reports,
        "warnings": warnings,
        "recommendations": recommendations
    }
    
    print(json.dumps(output, indent=2))

if __name__ == "__main__":
    main()
