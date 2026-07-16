#!/usr/bin/env python3
"""
ShaderEngine v2.2 — mpv-native GLSL with live parameter retuning.

Knob retuning strategy:
  Each shader's source has `#define PARAM_1 ...` through `#define PARAM_4 ...`
  near the top. When a knob changes, we:
    1. Read the original .glsl file
    2. Substitute the new PARAM_N value
    3. Write the result to a new unique temp path
    4. Tell mpv to load that tempfile instead of the original

  mpv 0.40 caches compiled shaders by file path in-memory. Re-setting the
  same path, even after writing new content, does not trigger recompilation.
  Unique paths (counter-based) guarantee mpv always compiles fresh.

  A debounce timer (configurable) prevents shader recompilation storms when
  the knob moves continuously. Only when motion stops for `DEBOUNCE_MS` ms
  do we actually push the change to mpv. This trades immediacy for
  flicker-free playback.
"""

import os
import re
import math
import logging
import threading
import time

log = logging.getLogger("shader")

# How long the knob must be still before we recompile the shader (ms)
DEBOUNCE_MS = 100
# Temp shader files live here; counter suffix ensures unique paths per apply
TMP_SHADER_DIR = "/tmp"
TMP_SHADER_PREFIX = "recur_s"
# Regex matching `#define PARAM_N <value>` lines we want to substitute.
# Supports up to PARAM_9 so shaders can expose as many controls as they need.
PARAM_RE = re.compile(r'^(\s*#define\s+PARAM_([1-9][0-9]?)\s+)([^\s/]+)', re.M)


def clamp01(x):
    """Clamp to the [0, 1] param range. Single source of truth so
    keyboard.py/menu.py compute the same clamped value they display in the
    OSD as the one set_param/set_fx_param actually store."""
    return max(0.0, min(1.0, x))


def _subst(text, vals, prefix):
    """Substitute live values into a shader's #define PARAM_N lines. `prefix`
    is 'p' for generative params (vals=cfg.params) or 'f' for FX params."""
    def repl(m):
        return f"{m.group(1)}{vals.get(prefix + m.group(2), 0.5):.4f}"
    return PARAM_RE.sub(repl, text)

# Blend modes for shader+clip compositing (SHADER mode). Integers are
# substituted into the blend shader; the names match cfg.SHADER_BLEND_MODES.
BLEND_MODE_MAP = {
    "normal":      0,   # per-FX-layer only: pure pass-through (see FX_LAYER_BLEND_SRC)
    "difference":  1,
    "addition":    2,
    "multiply":    3,
    "screen":      4,
    "mix":         5,
    "overlay":     6,
    "hardlight":   7,
    "softlight":   8,
    "dodge":       9,
    "burn":       10,
    "lighten":    11,
    "darken":     12,
    "exclusion":  13,
    "displace":   14,   # special: warps the video by the shader (not a blend)
    "subtract":   15,
    "divide":     16,
    "negation":   17,
    "reflect":    18,
    "glow":       19,
    "phoenix":    20,
    "vividlight": 21,
    "linearlight":22,
    "hardmix":    23,
    "hue":        24,   # non-separable: hue from shader, SV from video
    "luminosity": 25,   # non-separable: value from shader, HS from video
    "color":      26,   # non-separable: HS from shader, V from video
}

# Second-pass hook: reads the generative output (gen_out, saved by the first
# shader) and the original clip (HOOKED/MAIN) and composites them. The full
# W3C/Photoshop separable blend-mode set, plus a Resolume-style "displace" that
# refracts the video by the shader's colour. __BLEND_MODE__ / __BLEND_AMT__ are
# substituted at write time.
BLEND_SHADER_SRC = """\
//!DESC blend — composite generative shader with clip
//!HOOK MAIN
//!BIND HOOKED
//!BIND gen_out

#define BLEND_MODE __BLEND_MODE__
#define BLEND_AMT  __BLEND_AMT__

// per-channel blend: b = base (video), s = blend layer (shader)
// Modes 1-13: W3C/Photoshop separable set. 15-23: extended separable set.
// Non-separable HSV modes (24-26) and displace (14) are handled in hook().
float bmode(float b, float s) {
#if   BLEND_MODE == 1
    return abs(b - s);                                               // difference
#elif BLEND_MODE == 2
    return min(b + s, 1.0);                                          // addition
#elif BLEND_MODE == 3
    return b * s;                                                    // multiply
#elif BLEND_MODE == 4
    return 1.0 - (1.0 - b) * (1.0 - s);                             // screen
#elif BLEND_MODE == 6
    return b < 0.5 ? 2.0*b*s : 1.0 - 2.0*(1.0-b)*(1.0-s);          // overlay
#elif BLEND_MODE == 7
    return s < 0.5 ? 2.0*b*s : 1.0 - 2.0*(1.0-b)*(1.0-s);          // hardlight
#elif BLEND_MODE == 8
    return (s <= 0.5)
        ? b - (1.0 - 2.0*s) * b * (1.0 - b)                        // softlight
        : b + (2.0*s - 1.0) *
          ((b <= 0.25 ? ((16.0*b - 12.0)*b + 4.0)*b : sqrt(b)) - b);
#elif BLEND_MODE == 9
    return s >= 1.0 ? 1.0 : min(1.0, b / (1.0 - s));               // dodge
#elif BLEND_MODE == 10
    return s <= 0.0 ? 0.0 : 1.0 - min(1.0, (1.0 - b) / s);         // burn
#elif BLEND_MODE == 11
    return max(b, s);                                                // lighten
#elif BLEND_MODE == 12
    return min(b, s);                                                // darken
#elif BLEND_MODE == 13
    return b + s - 2.0*b*s;                                         // exclusion
#elif BLEND_MODE == 15
    return max(b - s, 0.0);                                          // subtract
#elif BLEND_MODE == 16
    return s <= 0.0 ? 1.0 : min(b / s, 1.0);                        // divide
#elif BLEND_MODE == 17
    return clamp(abs(1.0 - b - s), 0.0, 1.0);                       // negation
#elif BLEND_MODE == 18
    return s >= 1.0 ? 1.0 : min(b*b / (1.0 - s), 1.0);             // reflect
#elif BLEND_MODE == 19
    return b >= 1.0 ? 1.0 : min(s*s / (1.0 - b), 1.0);             // glow
#elif BLEND_MODE == 20
    return 1.0 - abs(b - s);                                         // phoenix
#elif BLEND_MODE == 21
    return s < 0.5                                                   // vividlight
        ? (s <= 0.0 ? 0.0 : 1.0 - min(1.0, (1.0-b)/(2.0*s)))
        : (s >= 1.0 ? 1.0 : min(1.0, b/(2.0*(1.0-s))));
#elif BLEND_MODE == 22
    return clamp(b + 2.0*s - 1.0, 0.0, 1.0);                        // linearlight
#elif BLEND_MODE == 23
    return b + s >= 1.0 ? 1.0 : 0.0;                                // hardmix
#else
    return mix(b, s, 0.5);                                           // mix (normal)
#endif
}

// RGB <-> HSV helpers for non-separable blend modes (24-26)
vec3 rgb2hsv(vec3 c) {
    float cmax = max(c.r, max(c.g, c.b));
    float cmin = min(c.r, min(c.g, c.b));
    float d = cmax - cmin;
    float h = 0.0;
    if (d > 0.0001) {
        if      (cmax == c.r) h = mod((c.g - c.b) / d, 6.0) / 6.0;
        else if (cmax == c.g) h = ((c.b - c.r) / d + 2.0) / 6.0;
        else                  h = ((c.r - c.g) / d + 4.0) / 6.0;
    }
    return vec3(h, cmax > 0.0001 ? d / cmax : 0.0, cmax);
}
vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

vec4 hook() {
    vec4 gen = gen_out_texOff(vec2(0.0));
#if BLEND_MODE == 14
    // Displace: offset the video by the shader's R/G channels.
    vec2 disp = (gen.rg - 0.5) * 2.0 * BLEND_AMT * 48.0;
    return vec4(HOOKED_texOff(disp).rgb, 1.0);
#else
    vec3 vid = HOOKED_texOff(vec2(0.0)).rgb;
#if BLEND_MODE == 24 || BLEND_MODE == 25 || BLEND_MODE == 26
    vec3 vh = rgb2hsv(vid);
    vec3 gh = rgb2hsv(gen.rgb);
#if   BLEND_MODE == 24
    vec3 bl = hsv2rgb(vec3(gh.x, vh.y, vh.z));                      // hue from shader
#elif BLEND_MODE == 25
    vec3 bl = hsv2rgb(vec3(vh.x, vh.y, gh.z));                      // luminosity from shader
#else
    vec3 bl = hsv2rgb(vec3(gh.x, gh.y, vh.z));                      // color (HS) from shader
#endif
#else
    vec3 bl = vec3(bmode(vid.r, gen.r),
                   bmode(vid.g, gen.g),
                   bmode(vid.b, gen.b));
#endif
    return vec4(mix(vid, bl, BLEND_AMT), 1.0);
#endif
}
"""

# Per-layer composite: blends one chain layer's effect against whatever was
# below it in the stack. Used for BOTH the FX chain and the generative-shader
# chain (see _layer_composite_src / _write_composite_pass) — the mechanics are
# identical, just with different bind names for "this layer's effect"
# (SRCNAME) and "what's below" (BELOWNAME, "HOOKED" for FX / progressive
# generative folding, or a named accumulator when the whole computation must
# avoid touching MAIN — see _write_shader_chain). Mode 0 ("normal") is a pure
# pass-through of the effect layer — the default, so a freshly-added layer
# looks unchanged until the user picks another blend mode.
_LAYER_BLEND_BODY = """\
#define BLEND_MODE __BLEND_MODE__
#define BLEND_AMT  __BLEND_AMT__

// per-channel blend: b = base (layer below), s = blend layer (this effect)
float bmode(float b, float s) {
#if   BLEND_MODE == 0
    return s;                                                         // normal (pass-through)
#elif BLEND_MODE == 1
    return abs(b - s);                                               // difference
#elif BLEND_MODE == 2
    return min(b + s, 1.0);                                          // addition
#elif BLEND_MODE == 3
    return b * s;                                                    // multiply
#elif BLEND_MODE == 4
    return 1.0 - (1.0 - b) * (1.0 - s);                             // screen
#elif BLEND_MODE == 6
    return b < 0.5 ? 2.0*b*s : 1.0 - 2.0*(1.0-b)*(1.0-s);          // overlay
#elif BLEND_MODE == 7
    return s < 0.5 ? 2.0*b*s : 1.0 - 2.0*(1.0-b)*(1.0-s);          // hardlight
#elif BLEND_MODE == 8
    return (s <= 0.5)
        ? b - (1.0 - 2.0*s) * b * (1.0 - b)                        // softlight
        : b + (2.0*s - 1.0) *
          ((b <= 0.25 ? ((16.0*b - 12.0)*b + 4.0)*b : sqrt(b)) - b);
#elif BLEND_MODE == 9
    return s >= 1.0 ? 1.0 : min(1.0, b / (1.0 - s));               // dodge
#elif BLEND_MODE == 10
    return s <= 0.0 ? 0.0 : 1.0 - min(1.0, (1.0 - b) / s);         // burn
#elif BLEND_MODE == 11
    return max(b, s);                                                // lighten
#elif BLEND_MODE == 12
    return min(b, s);                                                // darken
#elif BLEND_MODE == 13
    return b + s - 2.0*b*s;                                         // exclusion
#elif BLEND_MODE == 15
    return max(b - s, 0.0);                                          // subtract
#elif BLEND_MODE == 16
    return s <= 0.0 ? 1.0 : min(b / s, 1.0);                        // divide
#elif BLEND_MODE == 17
    return clamp(abs(1.0 - b - s), 0.0, 1.0);                       // negation
#elif BLEND_MODE == 18
    return s >= 1.0 ? 1.0 : min(b*b / (1.0 - s), 1.0);             // reflect
#elif BLEND_MODE == 19
    return b >= 1.0 ? 1.0 : min(s*s / (1.0 - b), 1.0);             // glow
#elif BLEND_MODE == 20
    return 1.0 - abs(b - s);                                         // phoenix
#elif BLEND_MODE == 21
    return s < 0.5                                                   // vividlight
        ? (s <= 0.0 ? 0.0 : 1.0 - min(1.0, (1.0-b)/(2.0*s)))
        : (s >= 1.0 ? 1.0 : min(1.0, b/(2.0*(1.0-s))));
#elif BLEND_MODE == 22
    return clamp(b + 2.0*s - 1.0, 0.0, 1.0);                        // linearlight
#elif BLEND_MODE == 23
    return b + s >= 1.0 ? 1.0 : 0.0;                                // hardmix
#else
    return mix(b, s, 0.5);                                           // mix
#endif
}

vec3 rgb2hsv(vec3 c) {
    float cmax = max(c.r, max(c.g, c.b));
    float cmin = min(c.r, min(c.g, c.b));
    float d = cmax - cmin;
    float h = 0.0;
    if (d > 0.0001) {
        if      (cmax == c.r) h = mod((c.g - c.b) / d, 6.0) / 6.0;
        else if (cmax == c.g) h = ((c.b - c.r) / d + 2.0) / 6.0;
        else                  h = ((c.r - c.g) / d + 4.0) / 6.0;
    }
    return vec3(h, cmax > 0.0001 ? d / cmax : 0.0, cmax);
}
vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

vec4 hook() {
    vec4 eff = SRCNAME_texOff(vec2(0.0));
#if BLEND_MODE == 14
    // Displace: warp the layer below by this effect's R/G channels.
    vec2 disp = (eff.rg - 0.5) * 2.0 * BLEND_AMT * 48.0;
    return vec4(BELOWNAME_texOff(disp).rgb, 1.0);
#else
    vec3 below = BELOWNAME_texOff(vec2(0.0)).rgb;
#if BLEND_MODE == 24 || BLEND_MODE == 25 || BLEND_MODE == 26
    vec3 bh = rgb2hsv(below);
    vec3 eh = rgb2hsv(eff.rgb);
#if   BLEND_MODE == 24
    vec3 bl = hsv2rgb(vec3(eh.x, bh.y, bh.z));                      // hue from effect
#elif BLEND_MODE == 25
    vec3 bl = hsv2rgb(vec3(bh.x, bh.y, eh.z));                      // luminosity from effect
#else
    vec3 bl = hsv2rgb(vec3(eh.x, eh.y, bh.z));                      // color (HS) from effect
#endif
#else
    vec3 bl = vec3(bmode(below.r, eff.r),
                   bmode(below.g, eff.g),
                   bmode(below.b, eff.b));
#endif
    return vec4(mix(below, bl, BLEND_AMT), 1.0);
#endif
}
"""

# NOTE: A GLSL temporal trail for SHADER mode was attempted but removed. It
# needs cross-frame feedback (this frame reading last frame's accumulator),
# which the Pi 5 V3D / libplacebo renderer does not persist — verified
# empirically with controlled render tests. The lavfi trail (engine.sampler)
# runs in the vf chain *before* the generative shader, which overwrites it, so
# the trail is a SAMPLER / LIVE feature only.

# Global colour control: a single MAIN-hook pass appended LAST in every mode so
# it transforms the final picture (video, generative shader, or camera alike).
# __HUE__ (turns, 0..1 = 0..360°) rotates hue; __SAT__ (0=grey, 1=normal, 2=
# vivid) scales saturation. Substituted at write time; only added when non-
# neutral so the default path stays shader-free.
COLOR_SHADER_SRC = """\
//!DESC colour — hue / saturation
//!HOOK MAIN
//!BIND HOOKED

#define HUE __HUE__
#define SAT __SAT__

vec3 rgb2hsv(vec3 c) {
    vec4 K = vec4(0.0, -1.0/3.0, 2.0/3.0, -1.0);
    vec4 p = mix(vec4(c.bg, K.wz), vec4(c.gb, K.xy), step(c.b, c.g));
    vec4 q = mix(vec4(p.xyw, c.r), vec4(c.r, p.yzx), step(p.x, c.r));
    float d = q.x - min(q.w, q.y);
    return vec3(abs(q.z + (q.w - q.y) / (6.0*d + 1e-10)),
                d / (q.x + 1e-10), q.x);
}
vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}
vec4 hook() {
    vec3 c   = HOOKED_texOff(vec2(0.0)).rgb;
    vec3 hsv = rgb2hsv(c);
    hsv.x = fract(hsv.x + HUE);
    hsv.y = clamp(hsv.y * SAT, 0.0, 1.0);
    return vec4(hsv2rgb(hsv), 1.0);
}
"""


class ShaderEngine:
    def __init__(self, cfg, sampler):
        self.cfg     = cfg
        self.sampler = sampler
        self.current = None              # abs path of the EDITED chain slot's shader
                                          # (cfg.shader_chain[shader_edit_slot]) — for
                                          # labels/logging; rendering reads the whole
                                          # chain directly (see _sync_current)
        self._pending = threading.Event()
        self._debounce_thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()    # guards _apply_now/set_color/_cmd_clear —
                                          # these run from both the main thread and
                                          # the debounce thread and mutate shared
                                          # tmp-file state (_tmp_active/_tmp_counter/
                                          # _color_tmp), so they must not interleave
        self._tmp_counter = 0            # monotonically increasing; ensures unique paths
        self._tmp_active  = []           # base shader tmp files mpv currently has loaded
        self._color_tmp   = None         # colour-pass tmp file (appended last), or None
        self._color_sig   = None         # (hue, sat) last written — avoids rewrites
        self._label_cache    = {}  # {path: {p1:label, ...}} — avoid per-frame file reads
        self._fallback_label_path = None  # resolved fallback path when self.current is None
        self._gen_cache      = {}  # {basename: bool} — cached DESC scan results
        self._sweep_stale_tmp_shaders()

    def _sweep_stale_tmp_shaders(self):
        """Remove tmp shader files left behind by an unclean shutdown (crash,
        kill -9, power loss) of a previous run — they're never cleaned up by
        the new process since _tmp_counter restarts from 0 each session."""
        import glob
        for path in glob.glob(os.path.join(TMP_SHADER_DIR, f"{TMP_SHADER_PREFIX}*.glsl")):
            try:
                os.unlink(path)
            except OSError:
                pass

    # ------------------------------------------------------------- discovery

    def _is_generative(self, basename):
        """Return True if the shader's //!DESC line contains '(generative)'.
        Results are cached so the files are only read once per session."""
        if basename in self._gen_cache:
            return self._gen_cache[basename]
        path = os.path.join(self.cfg.shaders_dir, basename)
        result = False
        try:
            with open(path) as f:
                for line in f:
                    if line.startswith("//!DESC"):
                        result = "(generative)" in line
                        break
        except OSError:
            pass
        self._gen_cache[basename] = result
        return result

    def list_shaders(self, kind=None):
        """List .glsl files. kind='generative' returns shaders whose //!DESC
        contains '(generative)'; kind='fx' returns the rest. None = all."""
        d = self.cfg.shaders_dir
        if not os.path.isdir(d):
            return []
        files = sorted(f for f in os.listdir(d) if f.endswith(".glsl"))
        if kind is None:
            return files
        excl = set(getattr(self.cfg, "excluded_from_fx", set()))
        if kind == "generative":
            return [f for f in files if self._is_generative(f)]
        else:  # fx
            return [f for f in files if not self._is_generative(f) and f not in excl]

    # ------------------------------------------------------------- loading
    def _sync_current(self):
        """Refresh self.current (abs path of the CHAIN SLOT currently being
        edited) after cfg.shader_chain / shader_edit_slot changes — used by
        param_labels()/apply_fx_overlay's logging, not by rendering (which
        keys off cfg.shader_chain directly, since there can be several)."""
        name = self.cfg.current_shader
        self.current = os.path.join(self.cfg.shaders_dir, name) if name else None

    def load(self, shader):
        """Set the active shader, REPLACING the whole stack with just this
        one (or clearing it entirely for None). Used where "load" means
        "this is now THE shader" — menu picks, presets, mode-restore — as
        opposed to shader_chain_toggle()'s add/remove-from-the-stack
        semantics (grid tap). The shader's PARAM_N defaults are kept;
        subsequent knob moves substitute live values."""
        self._fallback_label_path = None   # invalidate on any load/clear
        cfg = self.cfg
        if shader is None:
            cfg.shader_chain        = []
            cfg.shader_params_chain = []
            cfg.shader_blend_chain  = []
            cfg.shader_edit_slot    = 0
            cfg._sync_shader_compat()
            self._sync_current()
            # Re-apply fx chain if any (persists across mode transitions);
            # otherwise clear all shaders from mpv.
            self._apply_now()
            log.info("shader cleared")
            return
        if not os.path.isabs(shader):
            shader = os.path.join(cfg.shaders_dir, shader)
        if not os.path.exists(shader):
            log.warning("shader not found: %s", shader)
            return
        basename = os.path.basename(shader)
        cfg.shader_chain        = [basename]
        cfg.shader_params_chain = [{f"p{i}": 0.5 for i in range(1, 11)}]
        cfg.shader_blend_chain  = [{"mode": "normal", "amt": 1.0}]
        cfg.shader_edit_slot    = 0
        cfg._sync_shader_compat()
        self._sync_current()
        self._read_shader_defaults(0)
        self._apply_now()
        log.info("shader -> %s", basename)

    def shader_chain_toggle(self, name):
        """Toggle a generative shader in the stack (grid tap — mirrors
        fx_chain_toggle exactly). If already present: remove it. If not
        present: add to end (max 4 slots, oldest evicted)."""
        cfg = self.cfg
        MAX_CHAIN = 4
        _def_p = {f"p{i}": 0.5 for i in range(1, 11)}
        _def_b = {"mode": "normal", "amt": 1.0}
        try:
            pos = cfg.shader_chain.index(name)
            # Already in stack — remove it
            cfg.shader_chain.pop(pos)
            if pos < len(cfg.shader_params_chain):
                cfg.shader_params_chain.pop(pos)
            if pos < len(cfg.shader_blend_chain):
                cfg.shader_blend_chain.pop(pos)
            cfg.shader_edit_slot = max(0, min(cfg.shader_edit_slot, len(cfg.shader_chain) - 1))
        except ValueError:
            # Not in stack — add to end
            if len(cfg.shader_chain) >= MAX_CHAIN:
                cfg.shader_chain.pop(0)
                if cfg.shader_params_chain:
                    cfg.shader_params_chain.pop(0)
                if cfg.shader_blend_chain:
                    cfg.shader_blend_chain.pop(0)
            cfg.shader_chain.append(name)
            while len(cfg.shader_params_chain) < len(cfg.shader_chain):
                cfg.shader_params_chain.append(dict(_def_p))
            cfg.shader_edit_slot = len(cfg.shader_chain) - 1
        cfg._sync_shader_compat()
        self._fallback_label_path = None
        self._sync_current()
        if cfg.shader_chain:
            self._read_shader_defaults(cfg.shader_edit_slot)
        self._apply_now()
        chain_str = " > ".join(n.replace(".glsl", "") for n in cfg.shader_chain) if cfg.shader_chain else "—"
        log.info("shader stack: [%s]", chain_str)

    def push_fx(self, fx, slot=None):
        """Set an FX in the given chain slot (default: fx_edit_slot). Applies
        the chain to the current output without loading as a gen shader."""
        cfg = self.cfg
        if slot is None:
            slot = cfg.fx_edit_slot
        _def = {"f1": 0.5, "f2": 0.5, "f3": 0.5, "f4": 0.5, "f5": 0.5}
        while len(cfg.fx_chain) <= slot:
            cfg.fx_chain.append(None)
        while len(cfg.fx_params_chain) <= slot:
            cfg.fx_params_chain.append(dict(_def))
        cfg.fx_chain[slot] = fx
        cfg.shader_fx_stack = True
        cfg._sync_fx_compat()
        self._fallback_label_path = None
        self._read_fx_defaults(fx)
        self._apply_now()

    def fx_chain_toggle(self, fx_name):
        """Toggle an FX in the chain (web-style). If already present: remove it.
        If not present: add to end (max 4 slots, oldest removed). Also sets
        shader_fx_stack=True when chain is non-empty."""
        cfg = self.cfg
        MAX_CHAIN = 4
        _def = {"f1": 0.5, "f2": 0.5, "f3": 0.5, "f4": 0.5, "f5": 0.5}
        try:
            pos = cfg.fx_chain.index(fx_name)
            # Already in chain — remove it
            cfg.fx_chain.pop(pos)
            if pos < len(cfg.fx_params_chain):
                cfg.fx_params_chain.pop(pos)
            if pos < len(cfg.fx_blend_chain):
                cfg.fx_blend_chain.pop(pos)
            cfg.fx_edit_slot = max(0, min(cfg.fx_edit_slot, len(cfg.fx_chain) - 1))
        except ValueError:
            # Not in chain — add to end
            if len(cfg.fx_chain) >= MAX_CHAIN:
                cfg.fx_chain.pop(0)
                if cfg.fx_params_chain:
                    cfg.fx_params_chain.pop(0)
                if cfg.fx_blend_chain:
                    cfg.fx_blend_chain.pop(0)
            cfg.fx_chain.append(fx_name)
            while len(cfg.fx_params_chain) < len(cfg.fx_chain):
                cfg.fx_params_chain.append(dict(_def))
            cfg.fx_edit_slot = len(cfg.fx_chain) - 1
        cfg.shader_fx_stack = bool(cfg.fx_chain)
        cfg._sync_fx_compat()
        self._fallback_label_path = None
        if cfg.fx_chain:
            self._read_fx_defaults(cfg.fx_chain[cfg.fx_edit_slot])
        self._apply_now()
        chain_str = " > ".join(f.replace(".glsl", "") for f in cfg.fx_chain) if cfg.fx_chain else "—"
        log.info("fx chain: [%s]", chain_str)

    def cycle(self, direction=1, kind=None):
        """Cycle to next/previous shader. For kind='fx' cycles the current chain
        slot; for kind='generative' cycles the loaded gen shader."""
        lst = self.list_shaders(kind)
        if not lst:
            if kind != "fx":
                self.load(None)
            return
        if kind == "fx":
            cfg  = self.cfg
            cur  = cfg.current_fx
            i    = lst.index(cur) if cur in lst else -1
            i    = (i + direction) % len(lst)
            new_fx = lst[i]
            slot   = cfg.fx_edit_slot
            _def   = {"f1": 0.5, "f2": 0.5, "f3": 0.5, "f4": 0.5, "f5": 0.5}
            if slot < len(cfg.fx_chain):
                cfg.fx_chain[slot] = new_fx
            else:
                cfg.fx_chain.append(new_fx)
                cfg.fx_params_chain.append(dict(_def))
            cfg.shader_fx_stack = True
            cfg._sync_fx_compat()
            self._fallback_label_path = None
            self._read_fx_defaults(new_fx)
            self._apply_now()
            return
        if kind == "generative":
            # Cycles the currently-EDITED stack slot's shader — mirrors the
            # kind='fx' branch above (used by the SETTINGS menu's GEN row).
            cfg = self.cfg
            cur = cfg.current_shader
            i   = lst.index(cur) if cur in lst else -1
            i   = (i + direction) % len(lst)
            new_shader = lst[i]
            slot = cfg.shader_edit_slot
            _def_p = {f"p{n}": 0.5 for n in range(1, 11)}
            if slot < len(cfg.shader_chain):
                cfg.shader_chain[slot] = new_shader
            else:
                cfg.shader_chain.append(new_shader)
                cfg.shader_params_chain.append(dict(_def_p))
            cfg._sync_shader_compat()
            self._fallback_label_path = None
            self._sync_current()
            self._read_shader_defaults(cfg.shader_edit_slot)
            self._apply_now()
            return
        cur = self.cfg.current_shader
        if cur in lst:
            i = (lst.index(cur) + direction) % len(lst)
        else:
            i = 0
        self.load(lst[i])

    def apply_fx_overlay(self, direction=1):
        """Cycle the FX in the current chain slot on top of the generative shader.
        Does NOT change self.current. Called by +/- in SHADER mode."""
        lst = self.list_shaders(kind="fx")
        if not lst:
            return
        cfg  = self.cfg
        _def = {"f1": 0.5, "f2": 0.5, "f3": 0.5, "f4": 0.5, "f5": 0.5}
        if not cfg.fx_chain:
            # No chain yet — start with first or last FX
            i = 0 if direction > 0 else len(lst) - 1
            cfg.fx_chain.append(lst[i])
            cfg.fx_params_chain.append(dict(_def))
            cfg.fx_edit_slot = 0
        else:
            slot   = max(0, min(cfg.fx_edit_slot, len(cfg.fx_chain) - 1))
            cur    = cfg.fx_chain[slot]
            i      = lst.index(cur) if cur in lst else -1
            i      = (i + direction) % len(lst)
            cfg.fx_chain[slot] = lst[i]
            while len(cfg.fx_params_chain) < len(cfg.fx_chain):
                cfg.fx_params_chain.append(dict(_def))
        cfg.shader_fx_stack = True
        cfg._sync_fx_compat()
        self._fallback_label_path = None
        self._read_fx_defaults(cfg.current_fx)
        self._apply_now()
        log.info("fx overlay -> %s [slot %d] (on %s)", cfg.current_fx,
                 cfg.fx_edit_slot,
                 os.path.basename(self.current) if self.current else "—")

    # ------------------------------------------------------------- params
    def set_param(self, key, value):
        """Called by GPIO/MIDI when a knob moves. Schedules a debounced
        recompile."""
        value = clamp01(value)
        if self.cfg.params.get(key) == value:
            return
        self.cfg.params[key] = value
        if self.current is None:
            return
        self._pending.set()
        if self._debounce_thread is None or not self._debounce_thread.is_alive():
            self._debounce_thread = threading.Thread(
                target=self._debounce_loop, daemon=True,
                name="shader-debounce")
            self._debounce_thread.start()

    def _debounce_loop(self):
        """Wait for DEBOUNCE_MS of quiet, then commit the latest values."""
        while not self._stop.is_set():
            if not self._pending.wait(timeout=1.0):
                return
            last_check = self._snapshot()
            while not self._stop.is_set():
                time.sleep(DEBOUNCE_MS / 1000.0)
                cur = self._snapshot()
                if cur == last_check:
                    break
                last_check = cur
            self._pending.clear()
            self._commit_pending()

    def _commit_pending(self):
        """Apply whatever changed (shader params and/or colour) since the
        last commit. Also handles fx-chain-only (SAMPLER/LIVE) when there
        is no gen shader but there are FX in the chain."""
        with self._lock:
            if self.cfg.shader_chain or self.cfg.fx_chain:
                self._apply_now_locked()
            else:
                self._refresh_color()
                self._push_shaders()

    def set_fx_param(self, key, value):
        """Adjust an FX param (f1–f4) in the current edit slot. Debounced."""
        value = clamp01(value)
        cfg  = self.cfg
        slot = max(0, min(cfg.fx_edit_slot, len(cfg.fx_params_chain) - 1)) \
               if cfg.fx_params_chain else 0
        # Ensure chain slot exists
        if slot >= len(cfg.fx_params_chain):
            cfg.fx_params_chain.append({"f1": 0.5, "f2": 0.5, "f3": 0.5, "f4": 0.5, "f5": 0.5})
        params = cfg.fx_params_chain[slot]
        if params.get(key) == value:
            return
        params[key] = value
        cfg._sync_fx_compat()   # keep fx_params alias in sync
        if not cfg.current_fx:
            return
        self._pending.set()
        if self._debounce_thread is None or not self._debounce_thread.is_alive():
            self._debounce_thread = threading.Thread(
                target=self._debounce_loop, daemon=True, name="shader-debounce")
            self._debounce_thread.start()

    def cycle_fx_blend_mode(self, direction=1):
        """Cycle the current FX chain edit slot's blend mode (how that layer
        composites with whatever is below it). Applies immediately."""
        cfg   = self.cfg
        modes = list(cfg.FX_LAYER_BLEND_MODES)
        cur   = cfg.fx_blend.get("mode", "normal")
        i     = modes.index(cur) if cur in modes else 0
        cfg.fx_blend["mode"] = modes[(i + direction) % len(modes)]
        self._apply_now()

    def set_fx_blend_amount(self, amount):
        """Set the current FX chain edit slot's blend amount (0-1). Debounced
        like set_fx_param."""
        cfg = self.cfg
        amount = clamp01(amount)
        if cfg.fx_blend.get("amt", 1.0) == amount:
            return
        cfg.fx_blend["amt"] = amount
        self._pending.set()
        if self._debounce_thread is None or not self._debounce_thread.is_alive():
            self._debounce_thread = threading.Thread(
                target=self._debounce_loop, daemon=True, name="shader-debounce")
            self._debounce_thread.start()

    def fx_row_keys(self):
        """Ordered list of selectable rows for the FX params screen: this
        layer's own f-params, then its blend mode and amount."""
        fkeys = sorted(self.fx_param_labels().keys(), key=lambda k: int(k[1:]))
        return fkeys + ["__blend_mode__", "__blend_amt__"]

    def cycle_shader_layer_blend_mode(self, direction=1):
        """Cycle the current SHDR chain edit slot's blend mode (how that
        layer composites with whatever is below it in the stack). Applies
        immediately. Meaningless (but harmless) on slot 0, which never
        composites — see _write_shader_chain."""
        cfg   = self.cfg
        modes = list(cfg.FX_LAYER_BLEND_MODES)   # same palette, shared with FX layers
        cur   = cfg.shader_layer_blend.get("mode", "normal")
        i     = modes.index(cur) if cur in modes else 0
        cfg.shader_layer_blend["mode"] = modes[(i + direction) % len(modes)]
        self._apply_now()

    def set_shader_layer_blend_amount(self, amount):
        """Set the current SHDR chain edit slot's blend amount (0-1).
        Debounced like set_param."""
        cfg = self.cfg
        amount = clamp01(amount)
        if cfg.shader_layer_blend.get("amt", 1.0) == amount:
            return
        cfg.shader_layer_blend["amt"] = amount
        self._pending.set()
        if self._debounce_thread is None or not self._debounce_thread.is_alive():
            self._debounce_thread = threading.Thread(
                target=self._debounce_loop, daemon=True, name="shader-debounce")
            self._debounce_thread.start()

    def shader_row_keys(self):
        """Ordered list of selectable rows for the SHDR params screen: the
        edited slot's own p-params, then (only for slot > 0 — slot 0 never
        composites, so its blend would be permanently inert) its blend mode
        and amount."""
        pkeys = sorted(self.param_labels().keys(), key=lambda k: int(k[1:]))
        if self.cfg.shader_edit_slot > 0:
            return pkeys + ["__blend_mode__", "__blend_amt__"]
        return pkeys

    def _snapshot(self):
        chain_params = tuple(
            tuple(p.get(k, 0.5) for k in ("f1", "f2", "f3", "f4", "f5"))
            for p in self.cfg.fx_params_chain
        )
        chain_blend = tuple(
            (b.get("mode", "normal"), round(b.get("amt", 1.0), 4))
            for b in self.cfg.fx_blend_chain
        )
        shader_chain_params = tuple(
            tuple(p.get(f"p{n}", 0.5) for n in range(1, 11))
            for p in self.cfg.shader_params_chain
        )
        shader_chain_blend = tuple(
            (b.get("mode", "normal"), round(b.get("amt", 1.0), 4))
            for b in self.cfg.shader_blend_chain
        )
        return (shader_chain_params,
                chain_params,
                chain_blend,
                shader_chain_blend,
                getattr(self.cfg, 'color_hue', 0.0),
                getattr(self.cfg, 'color_sat', 1.0))

    def fx_param_labels(self):
        """Param labels for the current FX shader, keyed f1–f4."""
        return self._labels_for(self.cfg.current_fx, "f")

    def _labels_for(self, shader, prefix):
        """{prefix1: label, …} parsed dynamically from the shader's PARAM_N defines."""
        fallback = {f"{prefix}{n}": f"{prefix.upper()}{n}" for n in range(1, 5)}
        if not shader:
            return fallback
        path = shader if os.path.isabs(shader) \
                       else os.path.join(self.cfg.shaders_dir, shader)
        ckey = (path, prefix)
        if ckey in self._label_cache:
            return self._label_cache[ckey]
        try:
            with open(path) as f:
                src = f.read()
        except OSError:
            return fallback
        labels = {}
        pat_lbl = re.compile(r'#define\s+PARAM_([1-9][0-9]?)\s+\S+[^\n]*/\*\s*([^*]+?)\s*\*/')
        for m in pat_lbl.finditer(src):
            labels[f"{prefix}{m.group(1)}"] = m.group(2).strip()
        pat_def = re.compile(r'#define\s+PARAM_([1-9][0-9]?)\b')
        for m in pat_def.finditer(src):
            k = f"{prefix}{m.group(1)}"
            if k not in labels:
                labels[k] = f"{prefix.upper()}{m.group(1)}"
        if not labels:
            labels = fallback
        self._label_cache[ckey] = labels
        return labels

    # ------------------------------------------------------------- emit
    def param_labels(self):
        """Parse PARAM_N defines from the shader source and return a dict like
        {"p1": "speed", "p2": "scale", ...} containing ONLY the params that
        actually exist in the shader.  Supports PARAM_1 through PARAM_9.

        Uses the currently loaded shader (self.current) when available.
        Falls back to cfg.current_fx then cfg.current_shader so the menu
        always shows meaningful labels even in SAMPLER mode (no shader active).

        The fallback path resolution (os.path.exists) is cached so the display
        render loop (20 FPS) doesn't repeatedly hit the filesystem.
        """
        # Fast path: shader is loaded.
        path = self.current
        if path:
            self._fallback_label_path = None   # invalidate fallback cache
        else:
            # Use cached fallback path — only stat() the filesystem once per
            # fx/shader change, not every render frame.
            path = self._fallback_label_path
            if path is None:
                for candidate in (self.cfg.current_fx, self.cfg.current_shader):
                    if candidate:
                        if not os.path.isabs(candidate):
                            candidate = os.path.join(self.cfg.shaders_dir, candidate)
                        if os.path.exists(candidate):
                            path = candidate
                            break
                self._fallback_label_path = path   # cache (None = no file found)

        if not path:
            return {f"p{n}": f"P{n}" for n in range(1, 5)}  # minimal fallback

        if path in self._label_cache:
            return self._label_cache[path]

        try:
            with open(path) as f:
                src = f.read()
        except OSError:
            return {f"p{n}": f"P{n}" for n in range(1, 5)}

        labels = {}
        # First pass: defines with a /* label */ comment
        pat_lbl = re.compile(r'#define\s+PARAM_([1-9][0-9]?)\s+\S+[^\n]*/\*\s*([^*]+?)\s*\*/')
        for m in pat_lbl.finditer(src):
            labels[f"p{m.group(1)}"] = m.group(2).strip()
        # Second pass: defines without a label — record them with a default "Pn" name
        pat_def = re.compile(r'#define\s+PARAM_([1-9][0-9]?)\b')
        for m in pat_def.finditer(src):
            k = f"p{m.group(1)}"
            if k not in labels:
                labels[k] = f"P{m.group(1)}"

        self._label_cache[path] = labels
        return labels

    def _read_shader_defaults(self, slot):
        """Seed shader-chain slot `slot`'s params from its shader's authored
        defaults. Each shader starts at its designed settings; knob/menu
        movement overrides from there."""
        cfg = self.cfg
        if slot < 0 or slot >= len(cfg.shader_chain) or not cfg.shader_chain[slot]:
            return
        name = cfg.shader_chain[slot]
        path = name if os.path.isabs(name) else os.path.join(cfg.shaders_dir, name)
        if slot >= len(cfg.shader_params_chain):
            cfg.shader_params_chain.append({f"p{i}": 0.5 for i in range(1, 11)})
        self._read_param_defaults(path, cfg.shader_params_chain[slot], "p")
        cfg._sync_shader_compat()

    def _read_param_defaults(self, path, target, prefix):
        try:
            with open(path) as f:
                src = f.read()
        except OSError:
            return
        for m in PARAM_RE.finditer(src):
            try:
                target[prefix + m.group(2)] = float(m.group(3))
            except (ValueError, TypeError):
                pass

    def _read_fx_defaults(self, fx):
        """Seed the current edit slot's params from an FX shader's authored defaults."""
        if not fx:
            return
        cfg  = self.cfg
        path = fx if os.path.isabs(fx) else os.path.join(cfg.shaders_dir, fx)
        slot = max(0, min(cfg.fx_edit_slot, len(cfg.fx_params_chain) - 1)) \
               if cfg.fx_params_chain else 0
        if slot >= len(cfg.fx_params_chain):
            cfg.fx_params_chain.append({"f1": 0.5, "f2": 0.5, "f3": 0.5, "f4": 0.5, "f5": 0.5})
        self._read_param_defaults(path, cfg.fx_params_chain[slot], "f")
        cfg._sync_fx_compat()

    # ------------------------------------------------------------- layer-chain primitives
    #
    # Shared by both the FX chain and the generative-shader chain: writing a
    # chain of N shaders comes down to, per layer, an "effect" pass (params
    # substituted, optionally geometry-preprocessed) and — for any layer that
    # needs to composite against what's below it — a "composite" pass built
    # from _LAYER_BLEND_BODY. The two chains differ only in which layers get
    # a composite pass at all and what "below" means for it; see
    # _write_fx_shaders / _write_shader_chain.

    def _insert_save(self, src, save_name):
        """Insert //!SAVE {save_name} right after //!BIND HOOKED, so this
        pass's output is stashed under that name WITHOUT becoming the new
        MAIN — some later pass must consume it."""
        marker = "//!BIND HOOKED\n"
        pos = src.rfind(marker)
        if pos != -1:
            src = src[:pos] + src[pos:].replace(marker, marker + f"//!SAVE {save_name}\n", 1)
        return src

    def _write_effect_pass(self, name, params, prefix, save_name=None, pre_process=None):
        """Write one shader's effect pass (its own PARAM_N values substituted).
        save_name: if given, the pass gets //!SAVE {save_name} (doesn't touch
        MAIN); if None, its output becomes the new MAIN directly.
        pre_process: optional callable(src) -> src for geometry hacks
        (square-mode) applied before the save marker is inserted.
        Returns the tmp path, or None on read/write failure."""
        path = name if os.path.isabs(name) else os.path.join(self.cfg.shaders_dir, name)
        try:
            with open(path) as f:
                src = _subst(f.read(), params, prefix)
        except OSError as e:
            log.warning("can't read shader %s: %s", path, e)
            return None
        if pre_process:
            src = pre_process(src)
        if save_name:
            src = self._insert_save(src, save_name)
        self._tmp_counter += 1
        tmp = os.path.join(TMP_SHADER_DIR, f"{TMP_SHADER_PREFIX}{self._tmp_counter:06d}.glsl")
        try:
            with open(tmp, "w") as f:
                f.write(src)
        except OSError as e:
            log.warning("can't write tmp shader: %s", e)
            return None
        return tmp

    def _layer_composite_src(self, src_bind, below_bind, mode_int, amt, out_save=None):
        """Assemble a composite-pass shader source: reads src_bind (this
        layer's effect) and below_bind (whatever's below it — "HOOKED" for
        the implicit running MAIN, or a named accumulator), blends them by
        mode_int/amt, and either becomes the new MAIN (out_save=None) or is
        stashed under out_save without touching MAIN."""
        header = ("//!DESC layer blend — composite one layer with the layer below it\n"
                  "//!HOOK MAIN\n")
        header += "//!BIND HOOKED\n" if below_bind == "HOOKED" else f"//!BIND {below_bind}\n"
        header += f"//!BIND {src_bind}\n"
        if out_save:
            header += f"//!SAVE {out_save}\n"
        body = (_LAYER_BLEND_BODY
                .replace("SRCNAME", src_bind)
                .replace("BELOWNAME", below_bind)
                .replace("__BLEND_MODE__", str(mode_int))
                .replace("__BLEND_AMT__", f"{amt:.4f}"))
        return header + "\n" + body

    def _write_composite_pass(self, src_bind, below_bind, blend, out_save=None):
        mode_int = BLEND_MODE_MAP.get(blend.get("mode", "normal"), 0)
        amt      = blend.get("amt", 1.0)
        src = self._layer_composite_src(src_bind, below_bind, mode_int, amt, out_save)
        self._tmp_counter += 1
        tmp = os.path.join(TMP_SHADER_DIR, f"{TMP_SHADER_PREFIX}{self._tmp_counter:06d}.glsl")
        try:
            with open(tmp, "w") as f:
                f.write(src)
        except OSError as e:
            log.warning("can't write composite shader: %s", e)
            return None
        return tmp

    def _make_fx_square_preprocessor(self, w, h, side):
        """FX-shader square-mode hack: the shader has its own SQUARE_SRC/
        NATIVE_ASPECT/SQ_SCALE_X/Y defines that map an oversized side×side
        input back down to native w×h (used when this FX rotates/spins its
        sample point and needs margin beyond the frame edge)."""
        native_aspect = w / h
        def _pre(src):
            src = src.replace("#define SQUARE_SRC 0", "#define SQUARE_SRC 1", 1)
            src = re.sub(r"#define NATIVE_ASPECT [\d.]+",
                         f"#define NATIVE_ASPECT {native_aspect:.6f}", src, count=1)
            src = re.sub(r"#define SQ_SCALE_X [\d.]+",
                         f"#define SQ_SCALE_X {w / side:.6f}", src, count=1)
            src = re.sub(r"#define SQ_SCALE_Y [\d.]+",
                         f"#define SQ_SCALE_Y {h / side:.6f}", src, count=1)
            src = src.replace("//!BIND HOOKED\n",
                              f"//!BIND HOOKED\n//!WIDTH {w}\n//!HEIGHT {h}\n", 1)
            return src
        return _pre

    def _make_gen_square_preprocessor(self, h, side):
        """Generative-shader square-mode hack: render into an oversized
        side×side canvas (aspect-ratio expression overridden to match) so a
        rotating FX stacked on top has margin to sample beyond native
        bounds without hitting black corners."""
        def _pre(src):
            src = src.replace("HOOKED_size / HOOKED_size.y", f"vec2({side / h:.6f})")
            src = src.replace("//!BIND HOOKED\n",
                              f"//!BIND HOOKED\n//!WIDTH {side}\n//!HEIGHT {side}\n", 1)
            return src
        return _pre

    def _write_fx_shaders(self, chain, square_mode=False, bottom_has_source=True):
        """Write tmp shader files for each entry in chain. Returns list of paths.

        Each layer normally becomes TWO passes: its own effect (saved aside
        as fx_layer_out without touching the picture yet) followed by a
        composite pass that blends that effect against whatever was below it
        (the previous layer, the generative stack, or the raw video/camera)
        using the layer's own blend mode/amount (cfg.fx_blend_chain[i]). The
        bottom-most layer (i==0) skips the composite pass — applying its
        effect directly, exactly like before — when bottom_has_source is
        False: with no real clip/camera loaded yet there's nothing
        meaningful underneath to blend with.

        square_mode: if True, adjusts the FIRST shader to map back to native size.
        """
        cfg  = self.cfg
        w    = getattr(cfg, 'width',  1280)
        h    = getattr(cfg, 'height', 720)
        side = int(math.ceil(math.hypot(w, h))) if square_mode else None
        tmps = []
        for i, fx_name in enumerate(chain):
            if not fx_name:
                continue
            params = cfg.fx_params_chain[i] if i < len(cfg.fx_params_chain) else cfg.fx_params
            blend  = cfg.fx_blend_chain[i] if i < len(cfg.fx_blend_chain) else {"mode": "normal", "amt": 1.0}
            do_blend = bottom_has_source if i == 0 else True

            pre = self._make_fx_square_preprocessor(w, h, side) if (i == 0 and square_mode and side) else None
            eff_tmp = self._write_effect_pass(
                fx_name, params, "f",
                save_name="fx_layer_out" if do_blend else None,
                pre_process=pre)
            if eff_tmp is None:
                continue
            tmps.append(eff_tmp)

            if do_blend:
                comp_tmp = self._write_composite_pass("fx_layer_out", "HOOKED", blend)
                if comp_tmp:
                    tmps.append(comp_tmp)
        return tmps

    def _write_shader_chain(self, chain, square_mode=False, final_save_name=None):
        """Write the generative-shader stack (cfg.shader_chain). Returns list
        of tmp paths.

        Layer 0 (the bottom of the stack) never composites — a generative
        shader synthesizes its picture from scratch (ignoring HOOKED
        entirely), so there's nothing meaningful beneath it the way there is
        for the FX chain's raw video. Layers 1+ composite against the folded
        result of the layers below them via their own blend mode/amount
        (cfg.shader_blend_chain[i]), using a shared "gen_acc" accumulator
        name (consumed immediately by each layer's own composite pass, so
        reusing the name across layers is safe — same trick as fx_layer_out).

        final_save_name: when set (the existing shader_blend generative<->
        video compositing is on), the WHOLE stack's computation is kept off
        to the side — every pass here SAVEs instead of touching MAIN — so
        the raw video/camera stays pristine for the later shader-blend
        composite pass (BLEND_SHADER_SRC) to read against. When None, the
        last pass becomes the new MAIN directly, exactly like a single
        generative shader does today.
        """
        cfg = self.cfg
        active = [(i, name) for i, name in enumerate(chain) if name]
        if not active:
            return []
        preserve_main = final_save_name is not None
        last_idx = active[-1][0]
        w = getattr(cfg, 'width', 1280); h = getattr(cfg, 'height', 720)
        side = int(math.ceil(math.hypot(w, h))) if square_mode else None

        tmps = []
        for i, name in active:
            is_last = (i == last_idx)
            params = cfg.shader_params_chain[i] if i < len(cfg.shader_params_chain) else cfg.params
            blend  = cfg.shader_blend_chain[i] if i < len(cfg.shader_blend_chain) else {"mode": "normal", "amt": 1.0}
            pre = self._make_gen_square_preprocessor(h, side) if (square_mode and side) else None

            if i == 0:
                # Bottom layer: no "below" to composite against, ever.
                if is_last:
                    save_name = final_save_name if preserve_main else None
                else:
                    save_name = "gen_acc"   # more layers follow; hand off directly
                eff_tmp = self._write_effect_pass(name, params, "p", save_name=save_name, pre_process=pre)
                if eff_tmp:
                    tmps.append(eff_tmp)
                continue

            eff_tmp = self._write_effect_pass(name, params, "p", save_name="chain_eff", pre_process=pre)
            if eff_tmp is None:
                continue
            tmps.append(eff_tmp)

            if is_last:
                out_save = final_save_name if preserve_main else None
            else:
                out_save = "gen_acc"
            comp_tmp = self._write_composite_pass("chain_eff", "gen_acc", blend, out_save=out_save)
            if comp_tmp:
                tmps.append(comp_tmp)
        return tmps

    def _apply_fx_chain_only(self):
        """Apply fx_chain to the video with no gen shader (SAMPLER/LIVE mode)."""
        chain = [fx for fx in self.cfg.fx_chain if fx]
        bottom_has_source = getattr(self.sampler, "_active_source", None) in ("clip", "camera")
        new_active = self._write_fx_shaders(chain, bottom_has_source=bottom_has_source)
        self._finalize_shaders(new_active)

    def _apply_now(self):
        """Substitute PARAM_N values, write to a new unique tmp path, and
        push to mpv. A fresh path is used every call because mpv 0.40 caches
        compiled shaders in-memory by path and skips recompilation otherwise.

        Runs under self._lock since this is called from both the main thread
        (load/apply_fx_overlay/reapply) and the debounce thread — without it,
        an interleaved run could corrupt _tmp_counter/_tmp_active and leave
        mpv pointed at a half-written or already-deleted tmp file."""
        with self._lock:
            self._apply_now_locked()

    def _apply_now_locked(self):
        chain = [name for name in self.cfg.shader_chain if name]

        # ── no generative shader: apply fx chain to video (SAMPLER/LIVE) ──
        if not chain:
            if self.cfg.fx_chain:
                self._apply_fx_chain_only()
            else:
                self._refresh_color()
                self._push_shaders()
            return

        active_fx  = [fx for fx in self.cfg.fx_chain if fx]
        new_active = []

        if getattr(self.cfg, "shader_blend", False):
            # ── generative stack + blend-with-video [+ fx chain] ───────────
            gen_tmps = self._write_shader_chain(chain, final_save_name="gen_out")
            if not gen_tmps:
                return
            new_active.extend(gen_tmps)

            self._tmp_counter += 1
            blend_tmp = os.path.join(TMP_SHADER_DIR,
                                     f"{TMP_SHADER_PREFIX}{self._tmp_counter:06d}.glsl")
            mode_name    = getattr(self.cfg, "shader_blend_mode",   "difference")
            mode_int     = BLEND_MODE_MAP.get(mode_name, 1)
            blend_amount = getattr(self.cfg, "shader_blend_amount", 0.5)
            blend_src = (BLEND_SHADER_SRC
                         .replace("__BLEND_MODE__", str(mode_int))
                         .replace("__BLEND_AMT__",  f"{blend_amount:.4f}"))
            try:
                with open(blend_tmp, "w") as f:
                    f.write(blend_src)
            except OSError as e:
                log.warning("can't write blend shader: %s", e)
                return
            new_active.append(blend_tmp)

            if getattr(self.cfg, "shader_fx_stack", False) and active_fx:
                fx_tmps = self._write_fx_shaders(active_fx)
                new_active.extend(fx_tmps)
                log.debug("shaders -> %d-layer stack + blend(%s) + %d fx",
                          len(chain), mode_name, len(fx_tmps))
            else:
                log.debug("shaders -> %d-layer stack + blend(%s)", len(chain), mode_name)

        elif getattr(self.cfg, "shader_fx_stack", False) and active_fx:
            # ── generative stack + fx chain (no blend) ─────────────────────
            # Rotating FX need the stack to render into a diagonal-sized
            # square so corner samples don't fall outside the frame. Only
            # the FIRST rotating fx triggers this; subsequent FX run native.
            first_fx    = active_fx[0]
            square_mode = first_fx in getattr(self.cfg, "rotating_fx", set())

            gen_tmps = self._write_shader_chain(chain, square_mode=square_mode)
            if not gen_tmps:
                return
            new_active.extend(gen_tmps)

            fx_tmps = self._write_fx_shaders(active_fx, square_mode=square_mode)
            new_active.extend(fx_tmps)
            log.debug("shaders -> %d-layer stack + %d fx%s",
                      len(chain), len(fx_tmps), " [square]" if square_mode else "")

        else:
            # ── generative stack alone ──────────────────────────────────────
            gen_tmps = self._write_shader_chain(chain)
            if not gen_tmps:
                return
            new_active.extend(gen_tmps)

        self._finalize_shaders(new_active)

    def _finalize_shaders(self, new_active):
        """Set the base shader list, append the colour pass, push to mpv.

        Note: SHADER-mode trails are NOT possible here — they require cross-frame
        GLSL feedback, which the Pi 5 V3D driver does not persist (verified). So
        no trail shader is injected; the trail is a SAMPLER/LIVE feature only.
        """
        old = self._tmp_active
        self._tmp_active = new_active
        self._refresh_color()
        self._push_shaders()
        for path in old:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _push_shaders(self):
        """Push base shaders + the colour tail (if any) to mpv."""
        final = list(self._tmp_active)
        if self._color_tmp:
            final.append(self._color_tmp)
        self.sampler._cmd_async("set_property", "glsl-shaders", final)

    def _refresh_color(self):
        """Write the colour-pass tmp from cfg.color_hue/color_sat, or drop it
        when colour is neutral (hue 0, sat 1). Leaves base shaders untouched."""
        hue = getattr(self.cfg, 'color_hue', 0.0)
        sat = getattr(self.cfg, 'color_sat', 1.0)
        sig = (round(hue, 5), round(sat, 5))
        if sig == self._color_sig:
            return   # unchanged — keep the existing colour tmp (no recompile)
        self._color_sig = sig
        old = self._color_tmp
        if abs(hue) < 1e-4 and abs(sat - 1.0) < 1e-4:
            self._color_tmp = None
        else:
            src = (COLOR_SHADER_SRC
                   .replace("__HUE__", f"{hue:.5f}")
                   .replace("__SAT__", f"{sat:.5f}"))
            self._tmp_counter += 1
            path = os.path.join(TMP_SHADER_DIR,
                                f"{TMP_SHADER_PREFIX}col{self._tmp_counter:06d}.glsl")
            try:
                with open(path, "w") as f:
                    f.write(src)
                self._color_tmp = path
            except OSError as e:
                log.warning("colour shader write failed: %s", e)
                return
        if old and old != self._color_tmp:
            try:
                os.unlink(old)
            except OSError:
                pass

    def set_color(self, hue=None, sat=None):
        """Update hue (turns 0..1) and/or saturation (0..2). Debounced like
        set_param/set_fx_param — holding a colour key down no longer does a
        full tmp-file write + IPC round trip on every single keypress."""
        if hue is not None:
            self.cfg.color_hue = hue
        if sat is not None:
            self.cfg.color_sat = sat
        self._pending.set()
        if self._debounce_thread is None or not self._debounce_thread.is_alive():
            self._debounce_thread = threading.Thread(
                target=self._debounce_loop, daemon=True, name="shader-debounce")
            self._debounce_thread.start()

    def reapply(self):
        """Re-emit the current shader (e.g. after blend mode or source changed)."""
        self._apply_now()

    def _cmd_clear(self):
        with self._lock:
            old = self._tmp_active
            self._tmp_active = []
            self._refresh_color()
            self._push_shaders()
        for path in old:
            try:
                os.unlink(path)
            except OSError:
                pass

    # ------------------------------------------------------------- lifecycle
    def start(self, shader=None):
        shader = shader or self.cfg.current_shader or self._first_shader()
        if shader:
            self.load(shader)

    def _first_shader(self):
        lst = self.list_shaders()
        return lst[0] if lst else None

    def stop(self):
        self._stop.set()
        self._pending.set()  # wake debounce thread so it exits
        self._cmd_clear()
