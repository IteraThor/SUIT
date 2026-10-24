"""Removed: replaced by camera_optimizer_view.py. Only a SUIT process started before the update imports this."""

from modules_gtk.restart_required_page import RestartRequiredPage


class CamCalibrationPage(RestartRequiredPage):
    def __init__(self, window):
        super().__init__(window, "Camera Optimizer", "cam_calibration")
