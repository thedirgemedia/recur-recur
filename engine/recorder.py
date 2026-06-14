#!/usr/bin/env python3
"""
Recorder — capture live camera output to a clip file.

Uses mpv's stream-record property (set via IPC) to capture the raw stream that
mpv is already reading, so there is no device conflict with the live view.

For CSI cameras (rpicam-vid → FIFO → mpv) the captured stream is H.264
MPEG-TS, which ffmpeg can remux to MP4 without re-encoding. For USB V4L2
cameras the stream may be MJPEG or YUV; ffmpeg re-encodes to H.264 if a
stream-copy fails.

Workflow:
  record_start()  → set stream-record property → mpv writes raw stream to tmp
  record_stop()   → clear property → background thread remuxes tmp → clips_dir
"""

import logging
import os
import subprocess
import threading
import time
from datetime import datetime

log = logging.getLogger("recorder")


class Recorder:
    def __init__(self, cfg):
        self.cfg       = cfg
        self._recording = False
        self._tmp_path  = None
        self._final_path = None
        self._status    = ""   # "" | "REC" | "SAV"

    # ---------------------------------------------------------------- state

    @property
    def is_recording(self):
        return self._recording

    @property
    def status(self):
        return self._status

    # ---------------------------------------------------------------- control

    def start(self, sampler):
        if self._recording:
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._tmp_path   = f"/tmp/recur_rec_{ts}.ts"
        self._final_path = os.path.join(self.cfg.clips_dir, f"rec_{ts}.mp4")
        sampler._cmd_async("set_property", "stream-record", self._tmp_path)
        self._recording = True
        self._status    = "REC"
        log.info("recording started → %s", self._tmp_path)

    def stop(self, sampler):
        if not self._recording:
            return
        self._recording = False
        self._status    = "SAV"
        sampler._cmd_async("set_property", "stream-record", "")
        tmp, final = self._tmp_path, self._final_path
        self._tmp_path = self._final_path = None
        log.info("recording stopped — transcoding %s → %s", tmp, final)
        threading.Thread(target=self._transcode, args=(tmp, final),
                         daemon=True, name="rec-transcode").start()

    def teardown(self, sampler):
        """Stop recording cleanly on shutdown."""
        if self._recording:
            self.stop(sampler)

    # ---------------------------------------------------------------- transcode

    def _transcode(self, tmp, final):
        time.sleep(0.5)   # let mpv flush pending writes
        if not os.path.exists(tmp):
            log.warning("tmp recording not found: %s", tmp)
            self._status = ""
            return
        try:
            # Try a fast stream copy first (works when source is already H.264)
            r = subprocess.run(
                ["ffmpeg", "-i", tmp, "-c:v", "copy", "-an", "-y", final],
                capture_output=True, timeout=120
            )
            if r.returncode != 0:
                log.info("stream copy failed — re-encoding to H.264")
                subprocess.run(
                    ["ffmpeg", "-i", tmp,
                     "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                     "-an", "-y", final],
                    capture_output=True, timeout=600
                )
            log.info("saved clip: %s", os.path.basename(final))
        except Exception as e:
            log.error("transcode failed: %s", e)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            self._status = ""
