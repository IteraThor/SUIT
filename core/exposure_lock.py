"""Removed: the exposure lock watcher was replaced by the camera keeper (core.hbvcal.daemon).

Kept only so that an installed suit-exposure-lock.service (`python -m core.exposure_lock`) does not fail in a
restart loop after an update until SUIT is opened: started as that service, it removes its unit and the old
watcher's files, then stops its own unit (last: the stop ends this process; the unit has Restart=always, so
just exiting would bring it back). Safe to delete in a later release.
"""

if __name__ == "__main__":
    import subprocess
    from core.hbvcal import service

    unit = "suit-exposure-lock.service"
    for f in service.LEGACY_FILES:
        f.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "disable", unit], capture_output=True, timeout=10)
    (service.UNIT_DIR / unit).unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=10)
    subprocess.run(["systemctl", "--user", "stop", "--no-block", unit], capture_output=True, timeout=10)
