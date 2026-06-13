#!/usr/bin/env python3
"""
recur-recur — a r_e_c_u_r-inspired video instrument for Raspberry Pi 5
modes: SAMPLER | SHADER | LIVE
  +/- cycles FX shaders in every mode; FX mode is not a separate stop.
control: keyboard / USB MIDI / GPIO knobs
output: HDMI or composite (via HAT/adapter)
"""

import os
import sys
import time
import signal
import threading
import argparse
import logging

from engine.sampler import SamplerEngine
from engine.shader  import ShaderEngine
from engine.mixer   import MixerEngine
from control.keyboard import KeyboardController
from control.midi     import MidiController
from control.gpio     import GpioController
from control.osd      import OSD
from control.display  import DisplayController
from control.menu     import Menu
from config           import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("recur")


class RecurInstrument:
    MODES = ["SAMPLER", "SHADER", "LIVE"]

    def __init__(self, cfg: Config):
        self.cfg     = cfg
        self.running = False
        self._mode   = "SAMPLER"
        self._lock   = threading.Lock()

        # engines
        self.sampler = SamplerEngine(cfg)
        self.shader  = ShaderEngine(cfg, self.sampler)
        self.mixer   = MixerEngine(cfg)
        self.osd     = OSD(cfg)
        self.osd.attach(self.sampler)

        # navigable on-screen menu (SPI display)
        self.menu    = Menu(self)

        # controllers
        self.kb      = KeyboardController(self)
        self.midi    = MidiController(self)
        self.gpio    = GpioController(self)
        self.display = DisplayController(self)

        cfg.load_prefs()

        signal.signal(signal.SIGINT,  self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

    # ------------------------------------------------------------------ mode
    @property
    def mode(self):
        return self._mode

    def set_mode(self, name: str):
        name = name.upper()
        if name not in self.MODES:
            log.warning("unknown mode %s", name)
            return
        with self._lock:
            log.info("mode → %s", name)
            self._mode = name
            self.osd.show(f"MODE: {name}")
            self._apply_mode()

    def cycle_mode(self, direction=1):
        modes = [m for m in self.MODES
                 if m != "LIVE" or getattr(self.cfg, 'live_mode_enabled', True)]
        cur = self._mode if self._mode in modes else modes[0]
        idx = modes.index(cur)
        self.set_mode(modes[(idx + direction) % len(modes)])

    def _apply_mode(self):
        """v2 architecture: mpv owns the screen at all times.
          SAMPLER: clip plays; +/- selects the active FX shader (applied live)
          SHADER : generative shader — keeps whatever source is already loaded
                   (generative shader completely overrides the video picture, so
                   the clip can keep playing at its current position behind it)
          LIVE   : camera capture

        FX mode has been removed as a discrete stop: pressing +/- now applies
        the selected FX shader from SAMPLER mode directly.

        Video persistence: start_playback() is idempotent when a clip is already
        loaded ('_active_source == clip'), so mode transitions never restart the
        clip.  SHADER mode (blend=off) also preserves a playing clip; returning
        to SAMPLER reveals it at the same timecode.
        """
        if self._mode == "SAMPLER":
            self.cfg.shader_blend = False
            self.shader.load(None)
            self.sampler.start_playback()   # no-op if clip already active
            # SHADER mode forces loop-file=inf to keep frames flowing; restore
            # the sampler's real loop mode (oneshot/playlist/etc.) on return.
            self.sampler._apply_loop_mode()
            self.sampler.refresh_overlay()
            self.sampler.refresh_trail()
        elif self._mode == "SHADER":
            self.cfg.shader_fx_stack = False   # start clean; +/- re-enables
            if self.cfg.shader_blend:
                self._start_blend_source()
            else:
                active = self.sampler._active_source
                if active == 'camera':
                    # Camera can't stay — stop it and load the clip (or blank).
                    self.sampler._stop_cam_proc()
                    if self.cfg.current_clip:
                        self.sampler.start_playback()
                    else:
                        self.sampler.play_blank()
                elif active != 'clip':
                    # Nothing usable loaded yet → load blank as a substrate.
                    self.sampler.play_blank()
                # If active == 'clip': keep it playing — the generative shader
                # fully overrides the picture so there is no visible difference,
                # but the playhead position is preserved for when we leave.
                # Overlay VF has no effect against a pure-generative source.
                self.sampler._cmd_async("vf", "remove", "@overlay")
                # Trail is kept / restored according to cfg.trail_on so the
                # 000 toggle works the same in every mode.
                self.sampler.refresh_trail()
            # Generative shaders animate via mpv's `frame` uniform, which only
            # advances while mpv renders new frames. Force the (hidden) source
            # to loop and play so it never pauses at EOF — otherwise oneshot /
            # playlist modes freeze the shader on the last frame. A live camera
            # streams continuously and has no loop concept, so skip it there.
            if self.sampler._active_source != 'camera':
                self.sampler._cmd_async("set_property", "loop-file", "inf")
            self.sampler.resume()
            # Default to first generative shader if current one isn't in the set.
            cur  = self.cfg.current_shader
            gens = self.shader.list_shaders(kind="generative")
            if cur not in gens:
                cur = gens[0] if gens else None
            self.shader.load(cur)
        elif self._mode == "LIVE":
            self.shader.load(None)
            self.sampler.play_camera()
            self.sampler.refresh_overlay()
            self.sampler.refresh_trail()

    def _start_blend_source(self):
        """Start whichever video source is selected for shader blending."""
        if self.cfg.shader_blend_source == "live":
            self.sampler.play_camera()
        else:
            self.sampler.start_playback()

    # ------------------------------------------------------------------ menu deferred load
    def apply_menu_selection(self, clip_idx=None, gen_shader=None):
        """Load a clip/shader the user picked on the BROWSER/SHADERS menu pages.

        Called when the menu closes. Only the clip/shader *pick* is deferred to
        menu-close so browsing never yanks the live output; every other menu
        setting (overlay, trail, blend, params, mode) applies live while the
        menu is open.

        clip_idx   — a BROWSER selection to load as the live clip, or None.
        gen_shader — a SHADERS selection to make the active generative, or None.
        """
        if gen_shader is not None:
            self.cfg.current_shader = gen_shader
            if self._mode == "SHADER":
                self.shader.load(gen_shader)
        if clip_idx is not None:
            self.sampler.load(clip_idx)
            self.sampler.trigger()

    # ------------------------------------------------------------------ shader blend (SHADER mode)
    def shader_blend_toggle(self):
        """Toggle video+shader blend. * key in SHADER mode."""
        self.cfg.shader_blend = not self.cfg.shader_blend
        state = self.cfg.shader_blend
        src   = self.cfg.shader_blend_source
        log.info("shader blend -> %s (%s, source=%s)",
                 "ON" if state else "OFF", self.cfg.shader_blend_mode, src)
        if state:
            self._start_blend_source()
            self.osd.show(f"BLEND ON: {self.cfg.shader_blend_mode} [{src}]")
        else:
            self.sampler.play_blank()
            self.osd.show("BLEND OFF")
        self.shader.reapply()

    def shader_blend_source_cycle(self):
        """Cycle shader blend source (clip → live → clip…). Menu and settings."""
        srcs = self.cfg.SHADER_BLEND_SOURCES
        i = srcs.index(self.cfg.shader_blend_source) \
            if self.cfg.shader_blend_source in srcs else 0
        self.cfg.shader_blend_source = srcs[(i + 1) % len(srcs)]
        src = self.cfg.shader_blend_source
        log.info("shader blend source -> %s", src)
        if self.cfg.shader_blend and self._mode == "SHADER":
            self._start_blend_source()   # hot-swap source without toggling blend

    def shader_blend_adjust_amount(self, delta):
        """Nudge the blend mix strength (0 = all video, 1 = full blend effect)."""
        cur = getattr(self.cfg, "shader_blend_amount", 0.5)
        self.cfg.shader_blend_amount = max(0.0, min(1.0, round(cur + delta, 3)))
        self.osd.show(f"BLEND AMT: {self.cfg.shader_blend_amount:.2f}")
        if self.cfg.shader_blend:
            self.shader.reapply()

    def shader_blend_cycle(self):
        """Cycle shader blend mode. / key in SHADER mode."""
        modes = self.cfg.SHADER_BLEND_MODES
        i = modes.index(self.cfg.shader_blend_mode) \
            if self.cfg.shader_blend_mode in modes else 0
        self.cfg.shader_blend_mode = modes[(i + 1) % len(modes)]
        log.info("shader blend mode -> %s", self.cfg.shader_blend_mode)
        self.osd.show(f"BLEND: {self.cfg.shader_blend_mode}")
        if self.cfg.shader_blend:
            self.shader.reapply()

    # ------------------------------------------------------------------ overlay
    def overlay_toggle(self):
        """Toggle the V-overlay (time-delayed self-blend).
        Works in SAMPLER and LIVE modes. Blocked in SHADER mode
        because the shader pipeline occupies the vf chain."""
        if self._mode == "SHADER":
            log.info("overlay toggle ignored in SHADER mode")
            return
        self.cfg.overlay_on = not self.cfg.overlay_on
        log.info("overlay -> %s (%s)",
                 "ON" if self.cfg.overlay_on else "OFF",
                 self.cfg.overlay_mode)
        self.osd.show(f"OVERLAY {'ON' if self.cfg.overlay_on else 'OFF'} "
                      f"({self.cfg.overlay_mode})")
        self.sampler.refresh_overlay()

    def overlay_cycle_mode(self, direction=1):
        """Cycle to next/previous blend mode."""
        modes = self.cfg.OVERLAY_MODES
        i = modes.index(self.cfg.overlay_mode) if self.cfg.overlay_mode in modes else 0
        i = (i + direction) % len(modes)
        self.cfg.overlay_mode = modes[i]
        log.info("overlay mode -> %s", self.cfg.overlay_mode)
        self.osd.show(f"BLEND: {self.cfg.overlay_mode}")
        if self.cfg.overlay_on and self._mode in ("SAMPLER", "LIVE"):
            self.sampler.refresh_overlay()

    # ------------------------------------------------------------------ trail
    def _auto_assign(self):
        """Fill any empty clip/shader slots with available items.

        Only fills slots that are None — existing user assignments are kept.
        Runs once at startup after the clip scan so first-run users get all
        their content immediately reachable on keys 4–9.
        """
        clips = self.sampler.clips
        used  = {v for v in self.cfg.clip_slots.values() if v}
        avail = [c for c in clips if c not in used]
        ai = 0
        for slot in sorted(self.cfg.clip_slots):
            if self.cfg.clip_slots[slot] is None and ai < len(avail):
                self.cfg.clip_slots[slot] = avail[ai]
                ai += 1

        shaders = self.shader.list_shaders(kind="generative")
        used    = {v for v in self.cfg.shader_slots.values() if v}
        avail   = [s for s in shaders if s not in used]
        ai = 0
        for slot in sorted(self.cfg.shader_slots):
            if self.cfg.shader_slots[slot] is None and ai < len(avail):
                self.cfg.shader_slots[slot] = avail[ai]
                ai += 1

    def trail_toggle(self):
        """Toggle 2-second temporal echo trail. SAMPLER / LIVE only — a trail on
        the generative shader needs cross-frame GPU feedback the Pi 5 V3D driver
        does not support, so it is unavailable in SHADER mode."""
        if self._mode == "SHADER":
            log.info("trail toggle ignored in SHADER mode (no GPU feedback)")
            self.osd.show("TRAIL N/A IN SHADER")
            return
        self.cfg.trail_on = not self.cfg.trail_on
        state = self.cfg.trail_on
        log.info("trail -> %s (%s)", "ON" if state else "OFF", self.cfg.trail_mode)
        self.osd.show(f"TRAIL {'ON' if state else 'OFF'} ({self.cfg.trail_mode})")
        self.sampler.refresh_trail()

    def trail_cycle_mode(self, direction=1):
        """Cycle trail blend mode."""
        modes = self.cfg.TRAIL_MODES
        i = modes.index(self.cfg.trail_mode) if self.cfg.trail_mode in modes else 0
        self.cfg.trail_mode = modes[(i + direction) % len(modes)]
        log.info("trail mode -> %s", self.cfg.trail_mode)
        if self.cfg.trail_on:
            self.sampler.refresh_trail()

    def trail_cycle_blend_type(self):
        """Toggle trail blend type between 'mode' (lagfun decay) and 'opacity' (clean dissolve)."""
        types = self.cfg.TRAIL_BLEND_TYPES
        cur = getattr(self.cfg, 'trail_blend_type', 'mode')
        i = types.index(cur) if cur in types else 0
        self.cfg.trail_blend_type = types[(i + 1) % len(types)]
        log.info("trail blend type -> %s", self.cfg.trail_blend_type)
        if self.cfg.trail_on:
            self.sampler.refresh_trail()

    # ------------------------------------------------------------------ colour
    def color_adjust_hue(self, delta):
        """Rotate global hue. delta in turns; wraps 0..1. Works in all modes."""
        new = (self.cfg.color_hue + delta) % 1.0
        self.shader.set_color(hue=new)
        self.osd.show(f"HUE: {new * 360:.0f}°")

    def color_adjust_sat(self, delta):
        """Scale global saturation. 0=grey, 1=normal, up to COLOR_SAT_MAX."""
        hi  = getattr(self.cfg, 'COLOR_SAT_MAX', 2.0)
        new = max(0.0, min(hi, round(self.cfg.color_sat + delta, 3)))
        self.shader.set_color(sat=new)
        self.osd.show(f"SAT: {new:.2f}")

    # ------------------------------------------------------------------ run
    def run(self):
        self.running = True
        log.info("recur-recur starting — output: %s", self.cfg.output)

        # Pin Python control threads to cores 0-1 so media processes get
        # exclusive use of cores 2-3. Best-effort, silent on failure.
        try:
            from control.process_priority import boost_self_python
            boost_self_python()
        except Exception:
            pass

        self.sampler.start()
        self._auto_assign()   # fill empty slots after clip scan, before mode set
        self.kb.start()
        self.midi.start()
        self.gpio.start()
        self.osd.start()
        self.display.start()

        self.set_mode(self.cfg.start_mode)

        # restore sampler playback mode saved in prefs
        saved_sm = getattr(self.cfg, '_prefs_sampler_mode', None)
        if saved_sm:
            self.sampler.set_mode(saved_sm)
            del self.cfg._prefs_sampler_mode

        log.info("ready. numpad ENTER cycles modes, NumLock opens the menu.")
        try:
            while self.running:
                time.sleep(0.1)
        except KeyboardInterrupt:
            pass
        finally:
            self._teardown()

    def _shutdown(self, *_):
        self.running = False

    def _teardown(self):
        log.info("shutting down…")
        try:
            self.cfg.save_prefs(sampler_mode=self.sampler.mode)
        except Exception as e:
            log.warning("auto-save prefs failed: %s", e)
        for obj in [self.sampler, self.shader, self.mixer,
                    self.kb, self.midi, self.gpio, self.osd, self.display]:
            try:
                obj.stop()
            except Exception as e:
                log.debug("stop error: %s", e)
        log.info("bye")


def parse_args():
    p = argparse.ArgumentParser(description="recur-recur video instrument")
    p.add_argument("--output",     default="hdmi",    choices=["hdmi", "composite"])
    p.add_argument("--mode",       default="SAMPLER",  choices=["SAMPLER","SHADER","LIVE"])
    p.add_argument("--clips-dir",  default="clips/")
    p.add_argument("--shaders-dir",default="shaders/")
    p.add_argument("--resolution", default="1280x720")
    p.add_argument("--no-midi",    action="store_true")
    p.add_argument("--no-gpio",    action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    cfg = Config(args)
    RecurInstrument(cfg).run()
