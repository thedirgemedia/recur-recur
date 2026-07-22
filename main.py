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

from engine.sampler   import SamplerEngine
from engine.shader    import ShaderEngine
from engine.mixer     import MixerEngine
from engine.usb       import UsbManager
from engine.recorder  import Recorder
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
        self.cfg              = cfg
        self.running          = False
        self._restart_pending = False
        self._restart_resume  = False
        self._mode            = "SAMPLER"
        self._lock   = threading.Lock()

        # engines
        self.sampler = SamplerEngine(cfg)
        self.shader  = ShaderEngine(cfg, self.sampler)
        self.mixer   = MixerEngine(cfg)
        self.osd     = OSD(cfg)
        self.osd.attach(self.sampler)

        # on-demand USB import (mount removable drives → copy to internal)
        self.usb      = UsbManager(cfg)

        # live camera recording
        self.recorder = Recorder(cfg)

        # navigable on-screen menu (SPI display)
        self.menu    = Menu(self)

        # controllers
        self.kb      = KeyboardController(self)
        self.midi    = MidiController(self)
        self.gpio    = GpioController(self)
        self.display = DisplayController(self)

        # Boot normally starts from a clean default state — prefs.json is NOT
        # loaded, so every field keeps its Config default (no shader/FX chain,
        # no blend/trail/overlay, neutral colour, empty slots → _auto_assign()
        # refills them from the clip/shader scan, exactly like a fresh install).
        # The one exception is a "restart in same state" (SETTINGS → RESTART
        # SAME): that re-execs with --resume, which sets cfg.resume, so the
        # session that was just saved on teardown is loaded back verbatim.
        if getattr(cfg, "resume", False):
            cfg.load_prefs()

        # Per-mode volatile state — saved before leaving a mode, restored on
        # re-entry. Initialized from prefs when resuming, otherwise from Config
        # defaults (a clean, effect-free first pass).
        self._mode_states = {
            "SHADER": {
                "shader_blend":        getattr(cfg, "shader_blend",    False),
                "shader_fx_stack":     getattr(cfg, "shader_fx_stack", False),
                "shader_chain":        list(getattr(cfg, "shader_chain", [])),
                "shader_params_chain": [dict(p) for p in getattr(cfg, "shader_params_chain", [])],
                "shader_blend_chain":  [dict(b) for b in getattr(cfg, "shader_blend_chain", [])],
                "shader_edit_slot":    getattr(cfg, "shader_edit_slot", 0),
            }
        }

        signal.signal(signal.SIGINT,  self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

    # ------------------------------------------------------------------ mode
    @property
    def mode(self):
        return self._mode

    def _save_mode_state(self, mode):
        """Snapshot volatile SHADER-mode fields before leaving that mode."""
        if mode == "SHADER":
            self._mode_states["SHADER"] = {
                "shader_blend":        self.cfg.shader_blend,
                "shader_fx_stack":     self.cfg.shader_fx_stack,
                "shader_chain":        list(self.cfg.shader_chain),
                "shader_params_chain": [dict(p) for p in self.cfg.shader_params_chain],
                "shader_blend_chain":  [dict(b) for b in self.cfg.shader_blend_chain],
                "shader_edit_slot":    self.cfg.shader_edit_slot,
            }

    def _restore_mode_state(self, mode):
        """Restore snapshotted fields when re-entering a mode."""
        if mode == "SHADER":
            state = self._mode_states.get("SHADER", {})
            self.cfg.shader_blend    = state.get("shader_blend",    False)
            self.cfg.shader_fx_stack = state.get("shader_fx_stack", False)
            # Restore the whole generative stack so _apply_mode() picks it
            # up. (shader.load(None) in SAMPLER entry clears shader_chain.)
            if state.get("shader_chain"):
                self.cfg.shader_chain        = list(state["shader_chain"])
                self.cfg.shader_params_chain = [dict(p) for p in state.get("shader_params_chain", [])]
                self.cfg.shader_blend_chain  = [dict(b) for b in state.get("shader_blend_chain", [])]
                self.cfg.shader_edit_slot    = state.get("shader_edit_slot", 0)
                self.cfg._sync_shader_compat()

    def set_mode(self, name: str):
        name = name.upper()
        if name not in self.MODES:
            log.warning("unknown mode %s", name)
            return
        with self._lock:
            self._save_mode_state(self._mode)   # snapshot before leaving
            log.info("mode → %s", name)
            self._mode = name
            self.cfg.current_mode = name
            self.osd.show(f"MODE: {name}")
            self._restore_mode_state(name)      # restore before applying
            self._apply_mode()
        kb = getattr(self, "kb", None)
        if kb is not None:
            kb.sync_param_layer()   # SHDR layer only valid in SHADER mode

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
            self.shader.load(None)
            if self.cfg.current_clip:
                self.sampler.start_playback()   # no-op if clip already active
            else:
                # No clip yet: load a blank source so mpv initialises its DRM
                # output immediately rather than staying idle (idle mode with
                # --gpu-context=drm defers KMS init until content is loaded,
                # which leaves the HDMI output dark until the first mode change).
                self.sampler.play_blank()
            # SHADER mode forces loop-file=inf to keep frames flowing; restore
            # the sampler's real loop mode (oneshot/playlist/etc.) on return.
            self.sampler._apply_loop_mode()
            self.sampler.refresh_overlay()
            self.sampler.refresh_trail()
        elif self._mode == "SHADER":
            # shader_blend and shader_fx_stack were restored by _restore_mode_state()
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
            # The whole generative stack (+ its per-slot params/blends) was
            # already restored into cfg.shader_chain etc. by
            # _restore_mode_state() — just validate it against what's still
            # on disk (files can change between sessions) and push it to
            # mpv, rather than going through shader.load()/_read_shader_defaults()
            # which would reset params to authored defaults.
            gens = self.shader.list_shaders(kind="generative")
            valid_chain = [s for s in self.cfg.shader_chain if s in gens]
            if valid_chain != self.cfg.shader_chain:
                keep_idx = [i for i, s in enumerate(self.cfg.shader_chain) if s in gens]
                self.cfg.shader_params_chain = [self.cfg.shader_params_chain[i]
                                                for i in keep_idx if i < len(self.cfg.shader_params_chain)]
                self.cfg.shader_blend_chain  = [self.cfg.shader_blend_chain[i]
                                                for i in keep_idx if i < len(self.cfg.shader_blend_chain)]
                self.cfg.shader_chain     = valid_chain
                self.cfg.shader_edit_slot = 0
                self.cfg._sync_shader_compat()
            if not self.cfg.shader_chain and gens:
                self.shader.load(gens[0])
            else:
                self.shader.reapply()
        elif self._mode == "LIVE":
            self.shader.load(None)
            self.sampler.play_camera()
            self.sampler.refresh_overlay()
            self.sampler.refresh_trail()

    def _start_blend_source(self):
        """Start the blend video source. 'clip' uses the loaded sampler clip;
        'live' starts the camera. If 'live' is configured but the camera isn't
        already running, fall back to 'clip' rather than launching it silently."""
        if self.cfg.shader_blend_source == "live":
            self.sampler.play_camera()
        else:
            if self.cfg.current_clip:
                self.sampler.start_playback()
            else:
                self.sampler.play_blank()

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
            if self._mode == "SHADER":
                self.shader.load(gen_shader)
            else:
                # Not in SHADER mode yet — stage it into the mode-state cache
                # (mirrors apply_preset) so switching into SHADER mode later
                # picks it up, replacing whatever stack was there before.
                ms = self._mode_states.setdefault("SHADER", {})
                ms["shader_chain"]        = [gen_shader]
                ms["shader_params_chain"] = [{f"p{n}": 0.5 for n in range(1, 11)}]
                ms["shader_blend_chain"]  = [{"mode": "normal", "amt": 1.0}]
                ms["shader_edit_slot"]    = 0
                self.cfg.current_shader = gen_shader   # cosmetic: SHADERS page "▶" marker
        if clip_idx is not None:
            self.sampler.load(clip_idx)
            self.sampler.trigger()

    # ------------------------------------------------------------------ presets
    def load_preset_slot(self, slot: int):
        """Load the whole-state preset on numpad/note key `slot`.

        The global shortcuts — hold-0 + 4-9 in any mode, and MIDI notes — are
        not looking at a screen, so they always address PAGE ONE of the preset
        store: key 7 is the first preset, key 3 the ninth, matching where those
        keys sit on the LIVE grid. Presets past the first nine are reachable by
        paging that grid.
        """
        from control.display import _GRID_SLOTS
        try:
            index = _GRID_SLOTS.index(slot)
        except ValueError:
            return
        cfg = self.cfg
        name = cfg.preset_name("whole", index)[:-5]
        if not cfg.preset_exists("whole", index):
            self.osd.show(f"{name}: EMPTY")
            return
        data = cfg.load_preset_at("whole", index)
        if data:
            self.apply_preset(data)
            self.osd.show(f"PRESET: {name}")

    def apply_preset(self, data: dict):
        """Apply a loaded preset dict to the live instrument state.

        Switches to the saved mode if it differs from the current one.
        cfg.load_preset() (which produced `data`) already populated
        cfg.shader_chain / shader_params_chain / shader_blend_chain directly
        from the file, so this just decides how/when to push that into
        _mode_states and onto the live output.
        """
        cfg         = self.cfg
        target_mode = data.get("mode", "").upper() or None

        if target_mode == "SHADER":
            # Prime the cache so set_mode/_restore_mode_state picks up the
            # preset's whole generative stack (bypassing shader.load()'s
            # default-param reset).
            ms = self._mode_states.setdefault("SHADER", {})
            ms["shader_chain"]        = list(cfg.shader_chain)
            ms["shader_params_chain"] = [dict(p) for p in cfg.shader_params_chain]
            ms["shader_blend_chain"]  = [dict(b) for b in cfg.shader_blend_chain]
            ms["shader_edit_slot"]    = cfg.shader_edit_slot
            if "shader_blend" in data:
                ms["shader_blend"] = data["shader_blend"]

        # Switch mode (calls _restore_mode_state → _apply_mode, loads the stack).
        if target_mode and target_mode != self.mode:
            self.set_mode(target_mode)
        elif target_mode == "SHADER" and self.mode == "SHADER":
            # Already in SHADER — cfg's shader_chain etc. are already the
            # preset's values; just push them live.
            self.shader.reapply()
        # else: non-SHADER preset or no mode field — cfg fields are already
        # updated (by cfg.load_preset()) for the next SHADER-mode entry.

        # FX
        if data.get("fx"):
            cfg.current_fx = data["fx"]
        if "fx_params" in data:
            cfg.fx_params.update(data["fx_params"])
        # Blend — update cfg and keep mode-state cache in sync.
        if "shader_blend" in data:
            cfg.shader_blend = data["shader_blend"]
            self._mode_states.setdefault("SHADER", {})["shader_blend"] = data["shader_blend"]
        for key in ("shader_blend_mode", "shader_blend_amount", "shader_blend_source"):
            if key in data:
                setattr(cfg, key, data[key])
        # Colour — takes effect immediately in all modes.
        self.shader.set_color(
            hue=data.get("color_hue", cfg.color_hue),
            sat=data.get("color_sat", cfg.color_sat))
        log.info("preset applied: mode=%s shader=%s", target_mode, data.get("shader", "—"))

    def apply_shader_preset(self, data: dict):
        """Push a loaded generative-stack preset onto the live output.

        cfg.load_shader_preset() has already written the chain into cfg; this
        only decides how to get it on screen. The stack is what SHADER mode
        draws, so — exactly as apply_preset() does — the SHADER mode-state
        cache is primed first, otherwise _restore_mode_state() would overwrite
        the preset with the stale cached stack next time SHADER is entered.
        Never touches the FX chain.
        """
        cfg = self.cfg
        ms  = self._mode_states.setdefault("SHADER", {})
        ms["shader_chain"]        = list(cfg.shader_chain)
        ms["shader_params_chain"] = [dict(p) for p in cfg.shader_params_chain]
        ms["shader_blend_chain"]  = [dict(b) for b in cfg.shader_blend_chain]
        ms["shader_edit_slot"]    = cfg.shader_edit_slot
        if data.get("shader_blend") is not None:
            ms["shader_blend"] = data["shader_blend"]
        if self.mode == "SHADER":
            self.shader.reapply()
        log.info("shader preset applied: %d layers", len(cfg.shader_chain))

    def apply_fx_preset(self, data: dict):
        """Push a loaded FX-stack preset onto the live output.

        FX are post-stage filters over whatever is on screen, so this applies
        in every mode (SHADER / SAMPLER / LIVE) and needs no mode-state
        priming. Never touches the generative stack.
        """
        cfg = self.cfg
        if cfg.fx_chain:
            slot = min(cfg.fx_edit_slot, len(cfg.fx_chain) - 1)
            try:
                self.shader._read_fx_defaults(cfg.fx_chain[slot])
            except Exception as e:
                log.warning("fx preset defaults: %s", e)
        self.shader.reapply()
        log.info("fx preset applied: %d layers", len(cfg.fx_chain))

    # ------------------------------------------------------------------ shader blend (SHADER mode)
    def shader_blend_toggle(self):
        """Toggle video+shader blend. / key in SHADER mode."""
        self.cfg.shader_blend = not self.cfg.shader_blend
        state = self.cfg.shader_blend
        if state:
            # If source is set to "live" but the camera isn't already running,
            # fall back to "clip" so enabling blend doesn't unexpectedly launch
            # the camera. Switch to "live" explicitly via BLEND layer → SRC.
            if (self.cfg.shader_blend_source == "live"
                    and self.sampler._active_source != "camera"):
                self.cfg.shader_blend_source = "clip"
            src = self.cfg.shader_blend_source
            log.info("shader blend -> ON (%s, source=%s)",
                     self.cfg.shader_blend_mode, src)
            self._start_blend_source()
            self.osd.show(f"BLEND ON: {self.cfg.shader_blend_mode} [{src}]")
        else:
            log.info("shader blend -> OFF")
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

    def shader_blend_cycle(self, direction=1):
        """Cycle shader blend mode. / key in SHADER mode; also the BLEND
        param-layer's mode slot (keyboard 2/3, MIDI shader_blend_cycle)."""
        modes = self.cfg.SHADER_BLEND_MODES
        i = modes.index(self.cfg.shader_blend_mode) \
            if self.cfg.shader_blend_mode in modes else 0
        self.cfg.shader_blend_mode = modes[(i + direction) % len(modes)]
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

    def video_scale_cycle(self, direction=1):
        modes = self.cfg.VIDEO_SCALE_MODES
        i = list(modes).index(self.cfg.video_scale_mode) if self.cfg.video_scale_mode in modes else 0
        self.cfg.video_scale_mode = modes[(i + direction) % len(modes)]
        self.sampler.apply_video_scale()
        self.osd.show(f"SCALE: {self.cfg.video_scale_mode.upper()}")

    def record_toggle(self):
        """Start or stop recording the live camera to a clip file.
        Uses mpv stream-record (no device conflict). On stop, ffmpeg remuxes
        to MP4 in the background; the clip appears in BROWSER when done."""
        if self.sampler._active_source != 'camera':
            self.osd.show("RECORD: no camera active")
            return
        if self.recorder.is_recording:
            self.recorder.stop(self.sampler)
            self.osd.show("REC STOP — saving…")
        else:
            self.recorder.start(self.sampler)
            self.osd.show("REC")

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

        # Best-effort: unbind the framebuffer text console from HDMI so mpv
        # gets a clean display.  This succeeds if the process is running as
        # root (e.g. via ExecStartPre in the systemd unit) but silently fails
        # for an unprivileged user — vtcon1/bind is owned by root and requires
        # CAP_DAC_OVERRIDE, not just CAP_SYS_ADMIN.  mpv's DRM scan-out will
        # override the fbcon display regardless, so this is cosmetic only.
        for vtcon in ("/sys/class/vtconsole/vtcon1/bind",
                      "/sys/class/vtconsole/vtcon0/bind"):
            try:
                with open(vtcon) as f:
                    if f.read().strip() == "1":
                        with open(vtcon, "w") as fw:
                            fw.write("0")
            except Exception:
                pass

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

        self.sampler.apply_video_scale()
        self.sampler.apply_video_zoom()

        log.info("ready. numpad ENTER puts the active tab's mode on screen, "
                 "'.' opens SETTINGS.")
        try:
            while self.running:
                time.sleep(0.1)
        except KeyboardInterrupt:
            pass
        finally:
            self._teardown()
            if self._restart_pending:
                # _teardown() has just saved prefs.json. A plain restart drops
                # --resume so the new process boots to defaults; "restart in
                # same state" adds it so the saved session is loaded back.
                argv = [a for a in sys.argv if a != "--resume"]
                if self._restart_resume:
                    argv.append("--resume")
                log.info("restarting… (resume=%s)", self._restart_resume)
                os.execv(sys.executable, [sys.executable] + argv)

    def _shutdown(self, *_):
        self.running = False

    def restart(self, resume=False):
        """Re-exec the app. resume=False (default) boots to a clean default
        state; resume=True reloads prefs.json so it comes back exactly as it
        is now (state was saved on teardown)."""
        self._restart_resume  = resume
        self._restart_pending = True
        self.running = False

    def _teardown(self):
        log.info("shutting down…")
        try:
            self.recorder.teardown(self.sampler)
        except Exception as e:
            log.debug("recorder teardown error: %s", e)
        try:
            self.usb.unmount_all()
        except Exception as e:
            log.debug("usb unmount error: %s", e)
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
    # Internal: set by "restart in same state" so the re-exec'd process loads
    # prefs.json and resumes the session instead of booting to defaults.
    p.add_argument("--resume",     action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    cfg = Config(args)
    RecurInstrument(cfg).run()
