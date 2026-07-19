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
        # When True, load prefs.json on boot and resume the last session.
        # Normally False → boot into a clean default state. Set only by the
        # "restart in same state" path (main.py re-execs with --resume).
        self.resume      = getattr(args, "resume", False)
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
        # Actual GPU render/refresh rate, discovered from mpv's display-fps at
        # runtime (see ShaderEngine). mpv runs --video-sync=display-resample, so
        # its `frame` uniform ticks once per DISPLAY refresh, not per video
        # frame — the GPU LFOs must divide `frame` by THIS to keep real-time
        # periods. None until queried; the LFO clock falls back to fps meanwhile.
        # Runtime-only, never persisted.
        self.render_fps = None

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

        # Generative shader chain: up to 4 generative shaders stacked in
        # sequence, mirroring the FX chain below. shader_params_chain[i]
        # holds slot i's own p1-p10; shader_blend_chain[i] holds slot i's
        # blend mode/amount against whatever is below it (the previous
        # generative layer, or nothing for the bottom slot — a generative
        # shader synthesizes its image from scratch, so slot 0 never blends
        # against incoming video the way the FX chain's bottom layer does).
        # shader_edit_slot selects which slot the SHDR params screen edits.
        self.shader_chain        = []
        self.shader_params_chain = []
        self.shader_blend_chain  = []
        self.shader_edit_slot    = 0
        # Backward-compat aliases: always kept in sync via _sync_shader_compat().
        self.params             = {f"p{n}": 0.5 for n in range(1, 11)}
        self.shader_layer_blend = {"mode": "normal", "amt": 1.0}
        # FX chain: up to 3 FX shader names stacked in sequence.
        # fx_params_chain[i] holds the independent params for chain slot i.
        # fx_edit_slot selects which chain slot the params screen edits.
        # FX chain: up to 4 FX shaders applied in sequence. Empty list = no FX.
        # fx_params_chain[i] holds the independent params for chain slot i.
        # fx_edit_slot selects which chain slot the params screen edits.
        self.fx_chain        = []
        self.fx_params_chain = []
        # fx_blend_chain[i] holds {"mode": ..., "amt": ...} for chain slot i —
        # how that layer's effect composites with whatever is below it in the
        # stack (previous layer / generative / raw video). "normal" is a pure
        # pass-through so newly-added layers look like plain FX until the
        # user picks a different blend mode. See engine/shader.py FX_LAYER_BLEND_SRC.
        self.fx_blend_chain  = []
        self.fx_edit_slot    = 0
        # Backward-compat aliases: always kept in sync via _sync_fx_compat().
        self.fx_params  = {"f1": 0.5, "f2": 0.5, "f3": 0.5, "f4": 0.5, "f5": 0.5}
        self.fx_blend   = {"mode": "normal", "amt": 1.0}
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
        # Blend modes available per-FX-chain-layer (Parameter editing → FX).
        # Same palette as SHADER_BLEND_MODES plus "normal" (pure pass-through,
        # the default for every layer — see engine/shader.py FX_LAYER_BLEND_SRC).
        self.FX_LAYER_BLEND_MODES = ("normal",) + self.SHADER_BLEND_MODES
        # Source for shader blend: "clip" uses the current sampler clip,
        # "live" uses the CSI/USB camera feed
        self.shader_blend_source  = "clip"
        self.SHADER_BLEND_SOURCES = ("clip", "live")

        # ── LFOs ─────────────────────────────────────────────────────────────
        # Three LFOs that can drive any shader/FX param. They are evaluated on
        # the GPU (engine/shader.py substitutes a recur_lfo() call in place of a
        # param's literal), so a modulated param costs nothing per frame — the
        # shader is only rewritten when one of these settings changes.
        #
        # A param's assignment lives beside its value in the chain's params dict
        # under an "lfo_" key: fx_params_chain[slot]["lfo_f1"] = 0 binds LFO 1
        # to that slot's f1. None / absent = unmodulated. Keeping it in the same
        # dict means it travels with the slot through add/remove and presets for
        # free.
        self.lfos = [
            {"shape": 0, "amp": 0.5, "offset": 0.0, "period":  4.0, "bpm_sync": False, "beat": 1.0},
            {"shape": 0, "amp": 0.3, "offset": 0.0, "period":  8.0, "bpm_sync": False, "beat": 2.0},
            {"shape": 0, "amp": 0.3, "offset": 0.0, "period": 16.0, "bpm_sync": False, "beat": 4.0},
        ]
        self.lfo_bpm = 120.0
        self.LFO_SHAPES = ("SINE", "TRI", "SAW", "SQUARE", "S&H")
        # Musical divisions available when an LFO is synced to the BPM.
        self.LFO_BEATS  = (0.125, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)
        self.LFO_BEAT_LABELS = ("1/8", "1/4", "1/2", "1", "2", "4", "8", "16")

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

        # Per-clip playback settings (SAMPLER / LIVE), reached by long-pressing
        # a clip on the SAMPLER grid. Keyed by clip path; each clip remembers
        # its own orientation, zoom, speed, direction and trail so loading it
        # restores how it was last set up. Missing keys fall back to
        # CLIP_DEFAULTS below (neutral: upright at the clip's own metadata
        # rotation, no zoom, 1x forward, no trail).
        #   rotate  — degrees ADDED on top of the clip's metadata rotation
        #             (0 already plays portrait phone clips upright). 0/90/180/270.
        #   zoom    — uniform zoom factor (1.0 = none .. VIDEO_ZOOM_MAX); pushes
        #             a mismatched-aspect clip out to fill the screen.
        #   speed   — playback speed 0.1x .. 4.0x.
        #   reverse — play backwards when True.
        #   trail   — trail level 0 (off) .. CLIP_TRAIL_MAX; one slider that
        #             scales both the echo count and the delay length together.
        self.clip_settings = {}
        self.VIDEO_ROTATE_STEPS = (0, 90, 180, 270)
        self.VIDEO_ZOOM_MAX     = 4.0
        self.CLIP_TRAIL_MAX     = 5

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
        'video_scale_mode', 'clip_settings',
        'midi_target_cc',
        'lfos', 'lfo_bpm',
    ]

    # -------------------------------------------------- per-clip settings
    # Neutral defaults for a clip with nothing stored yet. bright/contrast are
    # mpv equalizer values (-100..100, 0 = neutral).
    # trail = number of echo steps (0 = off); trail_time = delay to the
    # furthest echo in seconds; trail_mode = the echo blend mode. These mirror
    # the reference build's trail controls (blend mode / steps / time).
    # trail_opc = per-echo blend opacity (0..1). Brightening modes (screen/
    # addition) accumulate toward white as echoes stack; lowering this fades
    # each echo's contribution so they don't blow out.
    CLIP_DEFAULTS = {"rotate": 0, "zoom": 1.0, "speed": 1.0,
                     "reverse": False,
                     "bright": 0, "contrast": 0,
                     "trail_on": False, "trail": 1, "trail_time": 2.0,
                     "trail_mode": "screen", "trail_opc": 0.5}

    def clip_get(self, path, key):
        """Read one per-clip setting, falling back to the neutral default."""
        if not path:
            return self.CLIP_DEFAULTS[key]
        return self.clip_settings.get(path, {}).get(key, self.CLIP_DEFAULTS[key])

    def clip_set(self, path, key, value):
        """Store one per-clip setting (no-op without a path)."""
        if not path:
            return
        self.clip_settings.setdefault(path, {})[key] = value

    def clip_lfo(self, path, target):
        """LFO index bound to a per-clip continuous target ('zoom'/'speed'),
        or None if unbound. Stored beside the value under an 'lfo_<target>'
        key so it travels with the clip."""
        if not path:
            return None
        v = self.clip_settings.get(path, {}).get("lfo_" + target)
        return None if v is None else int(v)

    def clip_set_lfo(self, path, target, idx):
        """Bind (idx) or clear (idx=None) an LFO on a per-clip target."""
        if not path:
            return
        if idx is None:
            self.clip_settings.get(path, {}).pop("lfo_" + target, None)
        else:
            self.clip_settings.setdefault(path, {})["lfo_" + target] = int(idx)

    def _sync_shader_compat(self):
        """Keep backward-compat aliases (current_shader / params /
        shader_layer_blend) in sync with shader_chain / shader_params_chain /
        shader_blend_chain — same pattern as _sync_fx_compat()."""
        n = len(self.shader_chain)
        _default_params = {f"p{i}": 0.5 for i in range(1, 11)}
        _default_blend  = {"mode": "normal", "amt": 1.0}
        if n == 0:
            self.shader_edit_slot   = 0
            self.current_shader    = None
            self.params             = self.shader_params_chain[0] if self.shader_params_chain else _default_params
            self.shader_layer_blend = self.shader_blend_chain[0] if self.shader_blend_chain else dict(_default_blend)
            return
        self.shader_edit_slot = max(0, min(self.shader_edit_slot, n - 1))
        while len(self.shader_params_chain) < n:
            self.shader_params_chain.append(dict(_default_params))
        while len(self.shader_blend_chain) < n:
            self.shader_blend_chain.append(dict(_default_blend))
        self.current_shader     = self.shader_chain[self.shader_edit_slot]
        self.params             = self.shader_params_chain[self.shader_edit_slot]
        self.shader_layer_blend = self.shader_blend_chain[self.shader_edit_slot]

    def _sync_fx_compat(self):
        """Keep backward-compat aliases in sync with fx_chain / fx_params_chain
        / fx_blend_chain."""
        n = len(self.fx_chain)
        _default_params = {"f1": 0.5, "f2": 0.5, "f3": 0.5, "f4": 0.5, "f5": 0.5}
        _default_blend  = {"mode": "normal", "amt": 1.0}
        if n == 0:
            self.fx_edit_slot = 0
            self.current_fx   = None
            self.fx_params    = self.fx_params_chain[0] if self.fx_params_chain else _default_params
            self.fx_blend     = self.fx_blend_chain[0] if self.fx_blend_chain else dict(_default_blend)
            return
        self.fx_edit_slot = max(0, min(self.fx_edit_slot, n - 1))
        # Ensure fx_params_chain / fx_blend_chain always have at least as
        # many slots as fx_chain
        while len(self.fx_params_chain) < n:
            self.fx_params_chain.append(dict(_default_params))
        while len(self.fx_blend_chain) < n:
            self.fx_blend_chain.append(dict(_default_blend))
        self.current_fx = self.fx_chain[self.fx_edit_slot]
        self.fx_params  = self.fx_params_chain[self.fx_edit_slot]
        self.fx_blend   = self.fx_blend_chain[self.fx_edit_slot]

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
        if 'shader_chain' in data and isinstance(data['shader_chain'], list):
            self.shader_chain = data['shader_chain']
        if 'shader_params_chain' in data and isinstance(data['shader_params_chain'], list):
            self.shader_params_chain = data['shader_params_chain']
        if 'shader_blend_chain' in data and isinstance(data['shader_blend_chain'], list):
            self.shader_blend_chain = data['shader_blend_chain']
        elif not self.shader_chain and data.get('current_shader'):
            # Legacy (pre-stack) prefs: promote the single current_shader/params
            # into a one-entry chain.
            self.shader_chain        = [data['current_shader']]
            self.shader_params_chain = [dict(self.params)]
            self.shader_blend_chain  = [{"mode": "normal", "amt": 1.0}]
        self._sync_shader_compat()
        if 'fx_chain' in data and isinstance(data['fx_chain'], list):
            self.fx_chain = data['fx_chain']
        if 'fx_params_chain' in data and isinstance(data['fx_params_chain'], list):
            self.fx_params_chain = data['fx_params_chain']
        if 'fx_blend_chain' in data and isinstance(data['fx_blend_chain'], list):
            self.fx_blend_chain = data['fx_blend_chain']
        if 'fx_params' in data:
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
        data['params']              = dict(self.params)
        data['shader_chain']        = list(self.shader_chain)
        data['shader_params_chain'] = [dict(p) for p in self.shader_params_chain]
        data['shader_blend_chain']  = [dict(b) for b in self.shader_blend_chain]
        data['fx_params']       = dict(self.fx_params)
        data['fx_chain']        = list(self.fx_chain)
        data['fx_params_chain'] = [dict(p) for p in self.fx_params_chain]
        data['fx_blend_chain']  = [dict(b) for b in self.fx_blend_chain]
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
        # Generative shader chain — prefer new list format, fall back to the
        # legacy single-shader keys.
        if "shader_chain" in data and isinstance(data["shader_chain"], list):
            self.shader_chain = list(data["shader_chain"])
            if "shader_params_chain" in data and isinstance(data["shader_params_chain"], list):
                self.shader_params_chain = [dict(p) for p in data["shader_params_chain"]]
            if "shader_blend_chain" in data and isinstance(data["shader_blend_chain"], list):
                self.shader_blend_chain = [dict(b) for b in data["shader_blend_chain"]]
            self.shader_edit_slot = 0
        elif data.get("shader"):
            self.shader_chain        = [data["shader"]]
            _defp = {f"p{i}": 0.5 for i in range(1, 11)}
            if "params" in data:
                _defp.update(data["params"])
            self.shader_params_chain = [_defp]
            self.shader_blend_chain  = [{"mode": "normal", "amt": 1.0}]
            self.shader_edit_slot    = 0
        self._sync_shader_compat()
        # FX chain — prefer new list format, fall back to legacy single-fx key
        if "fx_chain" in data and isinstance(data["fx_chain"], list):
            self.fx_chain = list(data["fx_chain"])
            if "fx_params_chain" in data and isinstance(data["fx_params_chain"], list):
                self.fx_params_chain = [dict(p) for p in data["fx_params_chain"]]
            if "fx_blend_chain" in data and isinstance(data["fx_blend_chain"], list):
                self.fx_blend_chain = [dict(b) for b in data["fx_blend_chain"]]
            self.fx_edit_slot = 0
        elif "fx" in data and data["fx"]:
            self.fx_chain        = [data["fx"]]
            _def = {"f1": 0.5, "f2": 0.5, "f3": 0.5, "f4": 0.5, "f5": 0.5}
            self.fx_params_chain = [dict(_def)]
            self.fx_blend_chain  = [{"mode": "normal", "amt": 1.0}]
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
            "version": 3,
            "mode":      self.current_mode,
            "shader":    self.current_shader,      # legacy compat: edit slot or None
            "params":    dict(self.params),        # legacy compat: current edit slot
            "shader_chain":        list(self.shader_chain),
            "shader_params_chain": [dict(p) for p in self.shader_params_chain],
            "shader_blend_chain":  [dict(b) for b in self.shader_blend_chain],
            "fx":        self.current_fx,          # legacy compat: slot 0 or None
            "fx_chain":  list(self.fx_chain),
            "fx_params": dict(self.fx_params),     # legacy compat: current edit slot
            "fx_params_chain": [dict(p) for p in self.fx_params_chain],
            "fx_blend_chain":  [dict(b) for b in self.fx_blend_chain],
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
