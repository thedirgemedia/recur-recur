#!/usr/bin/env python3
"""
OSD — lightweight on-screen status overlay (mode, clip name, rec indicator).

Routes messages through the SamplerEngine's existing persistent socket
(no per-message connect/send/close), so it's effectively free.
"""

import logging

log = logging.getLogger("osd")


class OSD:
    def __init__(self, cfg):
        self.cfg     = cfg
        self.sampler = None      # injected by the instrument after init

    def attach(self, sampler):
        """Called by the instrument so OSD can reuse the open mpv socket."""
        self.sampler = sampler

    def start(self):
        log.debug("osd ready")

    def show(self, text, duration=1.5):
        # Main-output text overlays are disabled — all status is shown on the
        # 3.5" SPI display instead. Keep a debug log line for diagnostics only.
        log.debug("OSD: %s", text)

    def stop(self):
        pass
