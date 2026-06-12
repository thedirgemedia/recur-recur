#!/usr/bin/env python3
"""
MixerEngine — owns the output target (HDMI vs composite) and an optional
FFmpeg recording sidecar that captures the rendered output to disk while
it's being displayed.

On Pi 5 there is no built-in composite jack, so 'composite' assumes either:
  - an HDMI→composite converter on the HDMI port, or
  - a composite HAT on the DSI/GPIO header.
Either way the render target is the same framebuffer; we just set SD geometry
(handled in Config) and tag the recording accordingly.
"""

import os
import time
import logging
import subprocess

log = logging.getLogger("mixer")


class MixerEngine:
    def __init__(self, cfg):
        self.cfg       = cfg
        self.recording = False
        self.rec_proc  = None

    # ------------------------------------------------------------- recording
    def start_recording(self, outfile=None):
        if self.recording:
            log.info("already recording")
            return
        if outfile is None:
            ts = time.strftime("%Y%m%d-%H%M%S")
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            outfile = os.path.join(root, "recordings", f"recur-{ts}.mkv")
        os.makedirs(os.path.dirname(outfile), exist_ok=True)

        # Capture the KMS output. kmsgrab needs CAP_SYS_ADMIN (granted to the
        # service via AmbientCapabilities) and the VC4 display device — on
        # Pi 5 that is card1, not the default card0 (V3D, no display).
        cmd = [
            "ffmpeg", "-y",
            "-f", "kmsgrab",
            "-device", "/dev/dri/card1",
            "-framerate", str(self.cfg.fps),
            "-i", "-",
            "-vf", "hwdownload,format=bgr0",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            outfile,
        ]
        log.info("recording → %s", outfile)
        try:
            self.rec_proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL,
                stderr=open("/tmp/ffmpeg-rec.err", "w"),
            )
        except FileNotFoundError:
            log.error("ffmpeg not found — install it to enable recording")
            return
        # kmsgrab fails fast when it can't open the device or lacks
        # CAP_SYS_ADMIN — surface that instead of pretending to record.
        time.sleep(0.5)
        if self.rec_proc.poll() is not None:
            log.error("recording failed to start (see /tmp/ffmpeg-rec.err)")
            self.rec_proc = None
            return
        self.recording = True

    def stop_recording(self):
        if not self.recording:
            return
        log.info("stopping recording")
        if self.rec_proc:
            self.rec_proc.terminate()
            try: self.rec_proc.wait(timeout=3)
            except Exception: self.rec_proc.kill()
            self.rec_proc = None
        self.recording = False

    def toggle_recording(self):
        self.stop_recording() if self.recording else self.start_recording()

    def stop(self):
        self.stop_recording()
