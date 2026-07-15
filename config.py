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
        self.start_mode  = getattr(args, "mode", "SHADER")
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

        # Numpad key-to-preset slot assignments.  Maps key number (4–9) to the
        # preset filename (.json), or None if empty.
        # Assigned from the PRESETS menu page (ENTER, then a slot key 4–9).
        # Loaded by hold-0 + key 4-9 in any mode.
        self.preset_slots = {4: None, 5: None, 6: None, 7: None, 8: None, 9: None}

        # Camera capture resolution.  The IMX708 has one native low-res sensor
        # mode (1536×864); the ISP then scales down to whatever we request here.
        # Lower = less encode/decode work = less lag.  Default 640×360 keeps
        # latency low; bump to 1280×720 for quality at the cost of more lag.
        self.camera_width  = 640
        self.camera_height = 360
        self.CAMERA_RESOLUTIONS = [(320, 180), (640, 360), (1280, 720)]

        # Generative-shader params, live-tweakable.  Shaders declare as many
        # PARAM_N defines as they need (up to 8); extra slots default to 0.5.
        self.params = {f"p{n}": 0.5 for n in range(1, 11)}
        # FX chain: up to 3 FX shader names stacked in sequence.
        # fx_params_chain[i] holds the independent params for chain slot i.
        # fx_edit_slot selects which chain slot the params screen edits.
        # FX chain: up to 4 FX shaders applied in sequence. Empty list = no FX.
        # fx_params_chain[i] holds the independent params for chain slot i.
        # fx_edit_slot selects which chain slot the params screen edits.
        self.fx_chain        = []
        self.fx_params_chain = []
        self.fx_edit_slot    = 0
        # Backward-compat aliases: always kept in sync via _sync_fx_compat().
        self.fx_params  = {"f1": 0.5, "f2": 0.5, "f3": 0.5, "f4": 0.5, "f5": 0.5}
        self.current_fx = None

        # currently selected files / mode
        self.current_mode   = "SHADER"
        self.current_clip   = None
        self.current_shader = None

        # passthrough produces no visible effect — exclude it from FX cycling
        # so every + / - press lands on something visually interesting
        self.excluded_from_fx = {"passthrough.glsl"}

        # FX shaders that rotate/spin their sample point. When one of these
        # is stacked on a generative shader, the engine renders the
        # generative pass into a square buffer sized to the frame's
        # diagonal first so the rotation has margin to sample from in
        # every direction instead of going out of bounds and showing
        # black at the corners.
        self.rotating_fx = {"mirror.glsl", "rotate_zoom.glsl", "kaleido_warp.glsl"}

        # User-defined MIDI CC assignments — map target name → CC number (0–127).
        # None means "use the built-in default from midi.py".
        # Targets: p1 p2 p3 p4  blend_amt ovl_opacity trl_decay
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
        self.SHADER_BLEND_MODES  = ("mix", "screen", "addition", "multiply",
                                    "overlay", "hardlight", "softlight",
                                    "dodge", "burn", "lighten", "darken",
                                    "difference", "exclusion", "displace",
                                    "subtract", "divide", "negation",
                                    "reflect", "glow", "phoenix",
                                    "vividlight", "linearlight", "hardmix",
                                    "hue", "luminosity", "color")
        # Source for shader blend: "clip" uses the current sampler clip,
        # "live" uses the CSI/USB camera feed
        self.shader_blend_source  = "clip"
        self.SHADER_BLEND_SOURCES = ("clip", "live")

        # V-overlay state: self-blend of the current frame using overlay_mode,
        # mixed at overlay_blend_amount (OVL OPC). No time delay — echoes are
        # the trail's job now.
        self.overlay_on   = False
        self.overlay_mode = "difference"
        self.overlay_blend_amount  = 1.0   # blend opacity 0–1 (OVL OPC)
        self.OVERLAY_MODES = ("difference", "addition", "multiply",
                              "screen", "negate",
                              "subtract", "divide", "lighten", "darken",
                              "hardlight", "softlight", "dodge", "burn",
                              "phoenix", "negation", "vividlight",
                              "linearlight", "pinlight", "hardmix",
                              "grainmerge", "grainextract")

        # Whether LIVE mode appears in the ENTER-key cycle. When False,
        # cycle_mode() skips LIVE: SAMPLER → SHADER → SAMPLER.
        self.live_mode_enabled = True

        # How video is scaled when its aspect ratio differs from the output.
        #   "fit"     — letterbox/pillarbox; whole frame visible, bars on edges
        #   "fill"    — zoom until video fills screen; edges may be cropped
        #   "stretch" — stretch to fill exactly; aspect ratio not preserved
        self.video_scale_mode  = "fit"
        self.VIDEO_SCALE_MODES = ("fit", "fill", "stretch")

        # Temporal trail — echo time delay.
        # Toggle: 000 key.  Mode: menu TRAIL MODE row.  Decay: FX layer TRL DEC param.
        # Two blend types selectable from menu TRAIL TYPE row:
        #   "mode"    — tpad+blend delayed echoes chained with creative blend modes
        #   "opacity" — weighted average of live + N delayed echoes (mix);
        #               clean motion ghosts, no wash-out
        # trail_echo_count controls N for both types (1–5).
        self.trail_on          = False
        self.trail_mode        = "screen"
        self.trail_decay       = 0.93       # 0.90=short ghost, 0.93=medium, 0.97=long tail
        self.trail_delay_s     = 2.0        # delay to furthest echo in seconds
        self.trail_blend_type  = "mode"     # "mode" or "opacity"
        self.trail_echo_count  = 1          # number of delayed echoes (1–5)
        # mode-type: brightening/darkening blends (screen/addition/multiply/
        # overlay) accumulate and wash out; tame them by mixing the blend back
        # toward the original on luma. 'difference' is left at full.
        self.trail_mode_opacity = 0.5
        self.TRAIL_MODES       = ("screen", "difference", "multiply", "overlay", "addition",
                                  "subtract", "lighten", "darken", "phoenix", "negation", "divide")
        self.TRAIL_BLEND_TYPES = ("mode", "opacity")

        # Global colour control (GLSL pass applied last in every mode).
        #   color_hue: hue rotation in turns, 0..1 (= 0..360°); 0 = no shift
        #   color_sat: saturation multiplier, 0 = greyscale, 1 = normal, 2 = vivid
        self.color_hue = 0.0
        self.color_sat = 1.0
        self.COLOR_SAT_MAX = 2.0

        # composite needs SD-friendly geometry
        if self.output == "composite":
            self.width, self.height = 720, 576   # PAL; use 720x480 for NTSC
            self.fps = 25

        self._validate()

    # --------------------------------------------------------- prefs persistence
    PREFS_PATH = os.path.join(_PROJECT_ROOT, 'prefs.json')

    # Scalar cfg attributes that are directly round-tripped to prefs.json.
    _PREFS_ATTRS = [
        'overlay_on', 'overlay_mode', 'overlay_blend_amount',
        'trail_on', 'trail_mode', 'trail_decay', 'trail_delay_s',
        'trail_blend_type', 'trail_echo_count', 'trail_mode_opacity',
        'color_hue', 'color_sat',
        'shader_blend', 'shader_blend_mode', 'shader_blend_amount', 'shader_blend_source',
        'current_clip', 'current_shader', 'current_fx',
        'start_mode', 'live_mode_enabled',
        'camera_width', 'camera_height',
        'video_scale_mode',
        'midi_target_cc',
    ]

    def _sync_fx_compat(self):
        """Keep backward-compat aliases in sync with fx_chain / fx_params_chain."""
        n = len(self.fx_chain)
        _default_params = {"f1": 0.5, "f2": 0.5, "f3": 0.5, "f4": 0.5, "f5": 0.5}
        if n == 0:
            self.fx_edit_slot = 0
            self.current_fx   = None
            self.fx_params    = self.fx_params_chain[0] if self.fx_params_chain else _default_params
            return
        self.fx_edit_slot = max(0, min(self.fx_edit_slot, n - 1))
        # Ensure fx_params_chain always has at least as many slots as fx_chain
        while len(self.fx_params_chain) < n:
            self.fx_params_chain.append(dict(_default_params))
        self.current_fx = self.fx_chain[self.fx_edit_slot]
        self.fx_params  = self.fx_params_chain[self.fx_edit_slot]

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
        if 'fx_chain' in data and isinstance(data['fx_chain'], list):
            self.fx_chain = data['fx_chain']
        if 'fx_params_chain' in data and isinstance(data['fx_params_chain'], list):
            self.fx_params_chain = data['fx_params_chain']
        elif 'fx_params' in data:
            # Legacy single-slot prefs: promote to chain slot 0
            if self.fx_chain:
                while len(self.fx_params_chain) < len(self.fx_chain):
                    self.fx_params_chain.append({"f1": 0.5, "f2": 0.5, "f3": 0.5, "f4": 0.5, "f5": 0.5})
                self.fx_params_chain[0].update(data['fx_params'])
        self._sync_fx_compat()
        if 'clip_slots' in data:
            self.clip_slots = {int(k): v for k, v in data['clip_slots'].items()}
        if 'shader_slots' in data:
            self.shader_slots = {int(k): v for k, v in data['shader_slots'].items()}
        if 'preset_slots' in data:
            self.preset_slots = {int(k): v for k, v in data['preset_slots'].items()}
        if 'sampler_mode' in data:
            self._prefs_sampler_mode = data['sampler_mode']
        log.info("prefs loaded from %s", self.PREFS_PATH)

    def save_prefs(self, sampler_mode=None):
        data = {key: getattr(self, key, None) for key in self._PREFS_ATTRS}
        data['params']          = dict(self.params)
        data['fx_params']       = dict(self.fx_params)
        data['fx_chain']        = list(self.fx_chain)
        data['fx_params_chain'] = [dict(p) for p in self.fx_params_chain]
        data['clip_slots']    = {str(k): v for k, v in self.clip_slots.items()}
        data['shader_slots']  = {str(k): v for k, v in self.shader_slots.items()}
        data['preset_slots']  = {str(k): v for k, v in self.preset_slots.items()}
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
    # Fields saved/restored by the preset system (in addition to shader/params/fx).
    _PRESET_EXTRA = (
        "fx_params",
        "shader_blend", "shader_blend_mode", "shader_blend_amount", "shader_blend_source",
        "color_hue", "color_sat",
    )

    def load_preset(self, name: str) -> dict:
        path = os.path.join(self.presets_dir, name)
        if not os.path.exists(path):
            log.warning("preset not found: %s", path)
            return {}
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception as e:
            log.warning("preset load error %s: %s", name, e)
            return {}
        if "shader" in data:
            self.current_shader = data["shader"]
        if "params" in data:
            self.params.update(data["params"])
        # FX chain — prefer new list format, fall back to legacy single-fx key
        if "fx_chain" in data and isinstance(data["fx_chain"], list):
            self.fx_chain = list(data["fx_chain"])
            if "fx_params_chain" in data and isinstance(data["fx_params_chain"], list):
                self.fx_params_chain = [dict(p) for p in data["fx_params_chain"]]
            self.fx_edit_slot = 0
        elif "fx" in data and data["fx"]:
            self.fx_chain        = [data["fx"]]
            _def = {"f1": 0.5, "f2": 0.5, "f3": 0.5, "f4": 0.5, "f5": 0.5}
            self.fx_params_chain = [dict(_def)]
            if "fx_params" in data:
                self.fx_params_chain[0].update(data["fx_params"])
            self.fx_edit_slot = 0
        self._sync_fx_compat()
        for key in self._PRESET_EXTRA:
            if key in data and key not in ("fx_params",):
                setattr(self, key, data[key])
        log.info("loaded preset %s", name)
        return data

    def save_preset(self, name: str):
        path = os.path.join(self.presets_dir, name)
        data = {
            "version": 2,
            "mode":      self.current_mode,
            "shader":    self.current_shader,
            "fx":        self.current_fx,          # legacy compat: slot 0 or None
            "fx_chain":  list(self.fx_chain),
            "params":    dict(self.params),
            "fx_params": dict(self.fx_params),     # legacy compat: current edit slot
            "fx_params_chain": [dict(p) for p in self.fx_params_chain],
        }
        for key in self._PRESET_EXTRA:
            if key not in ("fx_params",):
                data[key] = getattr(self, key, None)
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            log.info("saved preset %s", name)
            return True
        except Exception as e:
            log.warning("preset save error %s: %s", name, e)
            return False
