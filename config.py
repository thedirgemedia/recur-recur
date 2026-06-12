#!/usr/bin/env python3
"""Central configuration for recur-recur."""

import os
import json
import logging

log = logging.getLogger("config")


_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def _abs(path: str) -> str:
    """Resolve path relative to project root, not CWD."""
    return path if os.path.isabs(path) else os.path.join(_PROJECT_ROOT, path)


class Config:
    def __init__(self, args=None):
        # output: 'hdmi' or 'composite'
        self.output      = getattr(args, "output", "hdmi")
        self.start_mode  = getattr(args, "mode", "SAMPLER")
        self.clips_dir   = _abs(getattr(args, "clips_dir",   "clips/"))
        self.shaders_dir = _abs(getattr(args, "shaders_dir", "shaders/"))
        self.presets_dir = _abs("presets/")

        res = getattr(args, "resolution", "1280x720")
        self.width, self.height = (int(x) for x in res.split("x"))

        self.use_midi = not getattr(args, "no_midi", False)
        self.use_gpio = not getattr(args, "no_gpio", False)

        # mpv IPC socket — how we drive the video player
        self.mpv_socket = "/tmp/recur-mpv.sock"
        self.fps        = 30

        # Numpad key-to-clip slot assignments.  Maps key number (4–9) to the
        # absolute path of the assigned clip, or None if the slot is empty.
        # Assigned from the BROWSER menu (ENTER, then a slot key 4–9).
        # Used by SAMPLER/LIVE modes and by playlist play mode.
        self.clip_slots = {4: None, 5: None, 6: None, 7: None, 8: None, 9: None}

        # Numpad key-to-shader slot assignments.  Maps key number (4–9) to the
        # basename of the assigned generative shader (.glsl), or None if empty.
        # Assigned from the SHADERS menu page (ENTER, then a slot key 4–9).
        # Used by SHADER mode keys 4-9.
        self.shader_slots = {4: None, 5: None, 6: None, 7: None, 8: None, 9: None}

        # Camera capture resolution.  The IMX708 has one native low-res sensor
        # mode (1536×864); the ISP then scales down to whatever we request here.
        # Lower = less encode/decode work = less lag.  Default 640×360 keeps
        # latency low; bump to 1280×720 for quality at the cost of more lag.
        self.camera_width  = 640
        self.camera_height = 360
        self.CAMERA_RESOLUTIONS = [(320, 180), (640, 360), (1280, 720)]

        # shader params, live-tweakable (mirrors recur's 4-knob layout)
        self.params = {"p1": 0.5, "p2": 0.5, "p3": 0.5, "p4": 0.5}

        # currently selected files
        self.current_clip   = None
        self.current_shader = None
        self.current_fx     = "glitch.glsl"

        # generative shaders (used by SHADER mode); the rest are FX
        self.generative_shaders = {"plasma.glsl", "waves.glsl", "tunnel.glsl",
                                   "voronoi.glsl", "kaleidoscope.glsl"}

        # passthrough produces no visible effect — exclude it from FX cycling
        # so every + / - press lands on something visually interesting
        self.excluded_from_fx = {"passthrough.glsl"}

        # User-defined MIDI CC assignments — map target name → CC number (0–127).
        # None means "use the built-in default from midi.py".
        # Targets: p1 p2 p3 p4  blend_amt ovl_frames trl_decay
        #          overlay_toggle overlay_cycle
        #          shader_blend_toggle shader_blend_cycle
        #          shader_next shader_prev trail_toggle
        #          mode_sampler mode_shader mode_live
        self.midi_target_cc: dict = {}

        # SHADER mode: FX stacked on top of the generative shader.
        # Set True when +/- is pressed in SHADER mode; cleared on mode entry.
        self.shader_fx_stack = False

        # SHADER mode blend — overlay the generative shader with a video source
        # (toggled by *, blend mode cycled by /, blend source from menu)
        self.shader_blend        = False
        self.shader_blend_mode   = "difference"
        self.shader_blend_amount = 0.5          # 0 = all video, 1 = full blend effect
        self.SHADER_BLEND_MODES  = ("difference", "addition", "multiply",
                                    "screen", "mix")
        # Source for shader blend: "clip" uses the current sampler clip,
        # "live" uses the CSI/USB camera feed
        self.shader_blend_source  = "clip"
        self.SHADER_BLEND_SOURCES = ("clip", "live")

        # V-overlay state (self-blend with time-shifted version of clip)
        self.overlay_on   = False
        self.overlay_mode = "difference"   # difference | addition | multiply | screen | negate
        self.overlay_offset_frames = 8     # how many frames back to mix with
        self.overlay_blend_amount  = 1.0   # blend opacity 0–1 (OVL OPC)
        self.OVERLAY_MODES = ("difference", "addition", "multiply",
                              "screen", "negate")

        # Whether LIVE mode appears in the ENTER-key cycle. When False,
        # cycle_mode() skips LIVE: SAMPLER → SHADER → SAMPLER.
        self.live_mode_enabled = True

        # Temporal trail — echo time delay.
        # Toggle: 000 key.  Mode: menu TRAIL MODE row.  Decay: FX layer TRL DEC param.
        # Two blend types selectable from menu TRAIL TYPE row:
        #   "mode"    — tpad+lagfun decay blended on luma only (current, creative modes)
        #   "opacity" — 4 discrete echo steps (trail_step_opacities), plain dissolve blend
        self.trail_on         = False
        self.trail_mode       = "screen"   # screen | difference | multiply | overlay | addition
        self.trail_decay      = 0.93       # 0.90=short ghost, 0.93=medium, 0.97=long tail
        self.trail_delay_s    = 2.0        # echo delay in seconds (frames = delay * fps)
        self.trail_blend_type = "mode"     # "mode" or "opacity"
        # opacity-mode: 4-step opacities for echoes at 1×, 2×, 3×, 4× step offsets
        # (most-recent to oldest; step = trail_delay_s / 4)
        self.trail_step_opacities = (0.75, 0.50, 0.25, 0.15)
        self.TRAIL_MODES       = ("screen", "difference", "multiply", "overlay", "addition")
        self.TRAIL_BLEND_TYPES = ("mode", "opacity")

        # composite needs SD-friendly geometry
        if self.output == "composite":
            self.width, self.height = 720, 576   # PAL; use 720x480 for NTSC
            self.fps = 25

        self._validate()

    # --------------------------------------------------------- prefs persistence
    PREFS_PATH = os.path.join(_PROJECT_ROOT, 'prefs.json')

    # Scalar cfg attributes that are directly round-tripped to prefs.json.
    _PREFS_ATTRS = [
        'overlay_on', 'overlay_mode', 'overlay_offset_frames', 'overlay_blend_amount',
        'trail_on', 'trail_mode', 'trail_decay', 'trail_delay_s',
        'trail_blend_type', 'trail_step_opacities',
        'shader_blend', 'shader_blend_mode', 'shader_blend_amount', 'shader_blend_source',
        'current_clip', 'current_shader', 'current_fx',
        'start_mode', 'live_mode_enabled',
        'camera_width', 'camera_height',
        'midi_target_cc',
    ]

    def load_prefs(self):
        try:
            with open(self.PREFS_PATH) as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            log.warning("prefs load failed: %s", e)
            return
        for key in self._PREFS_ATTRS:
            if key in data and data[key] is not None:
                setattr(self, key, data[key])
        if 'params' in data:
            self.params.update(data['params'])
        if 'clip_slots' in data:
            self.clip_slots = {int(k): v for k, v in data['clip_slots'].items()}
        if 'shader_slots' in data:
            self.shader_slots = {int(k): v for k, v in data['shader_slots'].items()}
        if 'sampler_mode' in data:
            self._prefs_sampler_mode = data['sampler_mode']
        log.info("prefs loaded from %s", self.PREFS_PATH)

    def save_prefs(self, sampler_mode=None):
        data = {key: getattr(self, key, None) for key in self._PREFS_ATTRS}
        data['params']       = dict(self.params)
        data['clip_slots']   = {str(k): v for k, v in self.clip_slots.items()}
        data['shader_slots'] = {str(k): v for k, v in self.shader_slots.items()}
        if sampler_mode is not None:
            data['sampler_mode'] = sampler_mode
        try:
            with open(self.PREFS_PATH, 'w') as f:
                json.dump(data, f, indent=2)
            log.info("prefs saved to %s", self.PREFS_PATH)
            return True
        except Exception as e:
            log.warning("prefs save failed: %s", e)
            return False

    def _validate(self):
        for d in (self.clips_dir, self.shaders_dir, self.presets_dir):
            os.makedirs(d, exist_ok=True)

    # -------------------------------------------------- presets (recur-style)
    def load_preset(self, name: str) -> dict:
        path = os.path.join(self.presets_dir, name)
        if not os.path.exists(path):
            log.warning("preset not found: %s", path)
            return {}
        with open(path) as f:
            data = json.load(f)
        self.current_shader = data.get("shader", self.current_shader)
        self.current_fx     = data.get("fx", self.current_fx)
        self.params.update(data.get("params", {}))
        log.info("loaded preset %s", name)
        return data

    def save_preset(self, name: str):
        path = os.path.join(self.presets_dir, name)
        data = {
            "shader": self.current_shader,
            "fx":     self.current_fx,
            "params": self.params,
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        log.info("saved preset %s", name)
