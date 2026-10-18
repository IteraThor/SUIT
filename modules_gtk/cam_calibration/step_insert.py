"""Step 1 (Insert darts): live 640x480 preview of every camera and where to put the darts."""

from __future__ import annotations
import logging
import threading
import time
from typing import TYPE_CHECKING, Dict, Optional

import numpy as np

from modules_gtk.cam_calibration import widgets as W
from modules_gtk.cam_calibration.camera_source import LIVE_SIZE, cam_key

if TYPE_CHECKING:
    from modules_gtk.cam_calibration_view import CamCalibrationView

logger = logging.getLogger("cam_librator.insert")

THUMB_INTERVAL_S = 0.5     # list thumbnails refresh slower than the large view
OPEN_ATTEMPTS = 6          # live stream open attempts before a camera shows "No picture" ...
OPEN_RETRY_S = 1.0         # ... one second apart


class InsertStep:
    """Live preview feeding the large view and the list thumbnails."""

    def __init__(self, view: CamCalibrationView):
        self.view = view
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._latest: Dict[str, np.ndarray] = {}
        self._failed: set = set()
        self.open_attempts = OPEN_ATTEMPTS
        self.open_retry_s = OPEN_RETRY_S
        self._pending = False
        self._last_thumb = 0.0

        self.placement = W.PlacementCard()
        self.notice = W.ResultBanner("warn", "", "", compact=True)

    # -- step protocol ------------------------------------------------------------------

    def enter(self):
        v = self.view
        v.set_main(v.camera_view)
        cv = v.camera_view
        cv.set_zoom(None)
        cv.set_tips([])
        cv.set_pip(None)
        cv.set_hint(None)
        cv.set_active(False)
        cv.set_dim(False)
        cv.set_status("live", "Live")

        if not v.cameras:
            v.heading.set_text("No cameras found", "Connect the cameras, then open the optimizer again.")
            self.notice.set_result("bad", "No cameras found", "SUIT could not find any camera.")
            v.set_panels(self.notice, None)
            v.action_bar.set_secondary(visible=False)
            v.action_bar.set_primary("Find darts", sensitive=False)
            cv.set_status(None)
            return

        if v.board_only:
            v.heading.set_text("Check the cameras", "Make sure nothing blocks the board.")
            if v.board_only_reason == "cameras":
                body = "Dart finding needs all 3 cameras. Tuning uses the whole board instead."
            else:
                body = "Autodarts is not calibrated for every camera. Tuning uses the whole board instead."
            self.notice.set_result("warn", "Tuning on the whole board", body)
            v.set_panels(self.notice, None)
            v.action_bar.set_primary("Start tuning", callback=self._start_board_only)
        else:
            v.heading.set_text("Put 3 darts in the 20",
                               "These are the darkest fields, so they are the hardest for the cameras.")
            v.set_panels(None, self.placement)
            v.action_bar.set_primary("Find darts", callback=self._find_darts)
        v.action_bar.set_secondary(visible=False)

        for cam in v.cameras:
            v.camera_list.set_row(cam_key(cam), "Starting the picture", "neutral", "Wait")
        v.select_camera(v.selected)
        self._show_selected_title()
        with self._lock:
            for key, frame in self._latest.items():
                v.camera_list.set_thumbnail(key, frame)
        self.start_live_stream()

    def leave(self):
        self.stop_live_stream(wait=False)

    def on_camera_selected(self, key: str):
        self._show_selected_title()
        if key in self._failed:
            self.view.camera_view.set_status("bad", "No picture")
        else:
            self.view.camera_view.set_status("live", "Live")
        with self._lock:
            frame = self._latest.get(key)
        self.view.camera_view.set_frame(frame)

    def _show_selected_title(self):
        if self.view.selected:
            self.view.camera_view.set_title(self.view.cam_title(self.view.selected))

    # -- actions ------------------------------------------------------------------------

    def _find_darts(self):
        self.view.show_step("find")

    def _start_board_only(self):
        self.view.show_step("tune")

    # -- live stream --------------------------------------------------------------------

    def start_live_stream(self):
        if self._thread is not None and self._thread.is_alive():
            if not self._stop.is_set():
                return
            # A previous stream is still closing; the new worker waits for it
        self._stop = threading.Event()
        self._failed.clear()
        prev = self._thread
        self._thread = threading.Thread(target=self._worker, args=(self._stop, prev, self.view.session),
                                        daemon=True, name="optimizer-live")
        self._thread.start()

    @property
    def thread(self) -> Optional[threading.Thread]:
        return self._thread

    def stop_live_stream(self, wait: bool = True):
        """Stop the preview. wait=True (worker threads only) blocks until cameras are released."""
        self._stop.set()
        t = self._thread
        if wait and t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=8.0)
            self.view.source.settle(0.3)

    def _worker(self, stop: threading.Event, prev: Optional[threading.Thread], session: int):
        if prev is not None and prev.is_alive():
            prev.join(timeout=8.0)
        # A search burst or a tuning run (also of an earlier session) may still hold a camera
        self.view.wait_for_cameras(own=threading.current_thread())
        streams = {}
        pending = {cam_key(cam): cam for cam in self.view.cameras}
        attempts = 0
        next_try = 0.0
        try:
            while not stop.is_set() and (streams or pending):
                # Autodarts releases the cameras a moment after it is paused, so a camera that does
                # not open yet is retried; after OPEN_ATTEMPTS failures it shows "No picture".
                if pending and time.monotonic() >= next_try:
                    attempts += 1
                    for key, cam in list(pending.items()):
                        if stop.is_set():
                            break
                        try:
                            s = self.view.source.open_live(cam, *LIVE_SIZE)
                        except Exception as e:
                            logger.debug("Failed opening live stream on %s: %s", cam.dev_node, e)
                            s = None
                        if s is not None:
                            pending.pop(key)
                            streams[key] = s
                        elif attempts >= self.open_attempts:
                            pending.pop(key)
                            self._failed.add(key)
                            self.view.ui(session, self._on_stream_failed, key)
                    next_try = time.monotonic() + self.open_retry_s
                if not streams:
                    if pending:
                        time.sleep(0.1)
                    continue
                for key, s in list(streams.items()):
                    if stop.is_set():
                        break
                    try:
                        frame = s.read()
                    except Exception:
                        frame = None
                    if frame is not None:
                        with self._lock:
                            self._latest[key] = frame
                self._schedule_update(session)
                time.sleep(0.04)    # ~25 fps loop
        finally:
            for s in streams.values():
                s.release()

    def _schedule_update(self, session: int):
        # Coalesce: at most one pending UI update, it always shows the newest frames
        if self._pending:
            return
        self._pending = True
        self.view.ui(session, self._update_ui)

    def _update_ui(self):
        self._pending = False
        if self.view.current_step != "insert":
            return
        with self._lock:
            latest = dict(self._latest)
        sel = self.view.selected
        if sel in latest:
            self.view.camera_view.set_frame(latest[sel])
        now = time.monotonic()
        thumbs = now - self._last_thumb >= THUMB_INTERVAL_S
        for key, frame in latest.items():
            row = self.view.camera_list.rows.get(key)
            if row is None:
                continue
            if thumbs:
                self.view.camera_list.set_thumbnail(key, frame)
            if row.chip.get_visible() and row.chip.has_css_class("chip-live"):
                continue
            row.update("Live picture", "live", "Live")
        if thumbs:
            self._last_thumb = now

    def _on_stream_failed(self, key: str):
        if self.view.current_step != "insert":
            return
        self.view.camera_list.set_row(key, "No picture from this camera", "bad", "No picture")
        if key == self.view.selected:
            self.view.camera_view.set_status("bad", "No picture")
