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
        self.current = None              # absolute path of the SOURCE .glsl
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
    def load(self, shader):
        """Set the active shader. None to clear. The shader's PARAM_N
        defaults are kept; subsequent knob moves substitute live values."""
        self._fallback_label_path = None   # invalidate on any load/clear
        if shader is None:
            self.current = None
            self.cfg.current_shader = None
            self._cmd_clear()
            log.info("shader cleared")
            return
        if not os.path.isabs(shader):
            shader = os.path.join(self.cfg.shaders_dir, shader)
        if not os.path.exists(shader):
            log.warning("shader not found: %s", shader)
            return
        self.current = shader
        self.cfg.current_shader = os.path.basename(shader)
        self._read_defaults()
        self._apply_now()
        log.info("shader -> %s", os.path.basename(shader))

    def push_fx(self, fx):
        self.cfg.current_fx = fx
        self.load(fx)

    def cycle(self, direction=1, kind=None):
        """Cycle to next/previous shader (optionally restricted to a kind)."""
        lst = self.list_shaders(kind)
        if not lst:
            self.load(None); return
        cur = self.cfg.current_shader
        if cur in lst:
            i = (lst.index(cur) + direction) % len(lst)
        else:
            i = 0
        self.load(lst[i])
        if kind == "fx":
            self.cfg.current_fx = lst[i]

    def apply_fx_overlay(self, direction=1):
        """Cycle the FX list and stack the result on top of the current
        generative shader.  Does NOT change self.current — the generative
        shader stays active and its params remain tunable via knobs.

        Called by +/- in SHADER mode instead of cycle() so the generative
        layer is never displaced.
        """
        lst = self.list_shaders(kind="fx")
        if not lst:
            return
        cur = self.cfg.current_fx
        if cur in lst:
            i = (lst.index(cur) + direction) % len(lst)
        else:
            i = 0
        self.cfg.current_fx    = lst[i]
        self.cfg.shader_fx_stack = True
        self._read_fx_defaults(lst[i])   # seed f1–f4 from the new FX's defaults
        self._apply_now()
        log.info("fx overlay -> %s (on %s)", lst[i],
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
        last commit. Colour must still apply even with no base shader
        active (it's a global pass), so it's refreshed independently of
        _apply_now_locked, which is a no-op when self.current is None."""
        with self._lock:
            if self.current:
                self._apply_now_locked()
            else:
                self._refresh_color()
                self._push_shaders()

    def set_fx_param(self, key, value):
        """Adjust an FX param (f1–f4). Schedules a debounced recompile if an FX
        is active in the chain."""
        value = clamp01(value)
        if self.cfg.fx_params.get(key) == value:
            return
        self.cfg.fx_params[key] = value
        if not self.cfg.current_fx:
            return
        self._pending.set()
        if self._debounce_thread is None or not self._debounce_thread.is_alive():
            self._debounce_thread = threading.Thread(
                target=self._debounce_loop, daemon=True, name="shader-debounce")
            self._debounce_thread.start()

    def _snapshot(self):
        return (tuple(self.cfg.params.get(f"p{n}", 0.5) for n in range(1, 11)),
                tuple(self.cfg.fx_params.get(k, 0.5) for k in sorted(self.cfg.fx_params, key=lambda k: int(k[1:]))),
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

    def _read_defaults(self):
        """Apply the loaded shader's PARAM_N defaults — into cfg.params for a
        generative shader, or cfg.fx_params for an FX shader. Each shader starts
        at its designed settings; knob/menu movement overrides from there."""
        if not self.current:
            return
        is_gen = self._is_generative(os.path.basename(self.current))
        target, prefix = ((self.cfg.params, "p") if is_gen
                          else (self.cfg.fx_params, "f"))
        self._read_param_defaults(self.current, target, prefix)

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
        """Seed cfg.fx_params from an FX shader's authored defaults (used when
        the FX changes without going through load(), e.g. stacked in SHADER)."""
        if not fx:
            return
        path = fx if os.path.isabs(fx) else os.path.join(self.cfg.shaders_dir, fx)
        self._read_param_defaults(path, self.cfg.fx_params, "f")

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
        if not self.current:
            return
        try:
            with open(self.current) as f:
                src = f.read()
        except OSError as e:
            log.warning("can't read shader %s: %s", self.current, e)
            return

        is_generative = self._is_generative(os.path.basename(self.current))

        # self.current is the generative (SHADER mode) or the FX shader
        # (SAMPLER/LIVE) — substitute the matching param set.
        out = _subst(src, self.cfg.params if is_generative else self.cfg.fx_params,
                     "p" if is_generative else "f")

        self._tmp_counter += 1
        gen_tmp = os.path.join(TMP_SHADER_DIR,
                               f"{TMP_SHADER_PREFIX}{self._tmp_counter:06d}.glsl")
        new_active = []

        if getattr(self.cfg, "shader_blend", False) and is_generative:
            # ── generative + blend [+ optional FX] ────────────────────────
            # Pass 1: generative shader saves its output as gen_out.
            # Pass 2: blend shader reads gen_out + MAIN (video) and composites.
            # Pass 3 (optional): FX shader runs on top of the blended result.
            marker = "//!BIND HOOKED\n"
            pos = out.rfind(marker)
            if pos != -1:
                out = out[:pos] + out[pos:].replace(marker,
                                                    marker + "//!SAVE gen_out\n",
                                                    1)
            try:
                with open(gen_tmp, "w") as f:
                    f.write(out)
            except OSError as e:
                log.warning("can't write tmp shader: %s", e)
                return
            new_active.append(gen_tmp)

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

            # FX stacked on top of the blend composite (pass 3)
            if getattr(self.cfg, "shader_fx_stack", False) and self.cfg.current_fx:
                fx_name = self.cfg.current_fx
                fx_path = fx_name if os.path.isabs(fx_name) \
                                   else os.path.join(self.cfg.shaders_dir, fx_name)
                try:
                    with open(fx_path) as f:
                        fx_src = _subst(f.read(), self.cfg.fx_params, "f")
                except OSError as e:
                    log.warning("can't read FX shader %s: %s", fx_path, e)
                else:
                    self._tmp_counter += 1
                    fx_tmp = os.path.join(TMP_SHADER_DIR,
                                          f"{TMP_SHADER_PREFIX}{self._tmp_counter:06d}.glsl")
                    try:
                        with open(fx_tmp, "w") as f:
                            f.write(fx_src)
                        new_active.append(fx_tmp)
                        log.debug("shaders -> %s + blend(%s) + fx(%s)",
                                  os.path.basename(gen_tmp), mode_name,
                                  os.path.basename(fx_path))
                    except OSError as e:
                        log.warning("can't write FX tmp shader: %s", e)
            else:
                log.debug("shaders -> %s + blend(%s)",
                          os.path.basename(gen_tmp), mode_name)

        elif is_generative and getattr(self.cfg, "shader_fx_stack", False) \
                and self.cfg.current_fx:
            # ── generative + FX stacked ────────────────────────────────────
            # Rotating FX (mirror/rotate_zoom/kaleido_warp) need margin to
            # sample from or they show black where the rotated sample falls
            # outside the frame. A width*width square only adds margin on
            # the vertical axis (the horizontal axis is already at its
            # native max) so it still clips during rotation — the square
            # has to be sized to the frame's diagonal to guarantee no
            # corner ever clips at any rotation angle. Render the
            # generative pass into that diagonal*diagonal square buffer,
            # then have the FX pass map both axes back down to the
            # native frame when it samples.
            fx_name = self.cfg.current_fx
            square_mode = fx_name in getattr(self.cfg, "rotating_fx", set())

            if square_mode:
                w, h = self.cfg.width, self.cfg.height
                side = int(math.ceil(math.hypot(w, h)))
                out = out.replace("HOOKED_size / HOOKED_size.y",
                                   f"vec2({side / h:.6f})")
                out = out.replace("//!BIND HOOKED\n",
                                   f"//!BIND HOOKED\n//!WIDTH {side}\n"
                                   f"//!HEIGHT {side}\n", 1)

            # Write generative with live param substitution.
            try:
                with open(gen_tmp, "w") as f:
                    f.write(out)
            except OSError as e:
                log.warning("can't write tmp shader: %s", e)
                return
            new_active.append(gen_tmp)

            # Stack the FX with its own live params (cfg.fx_params), so the
            # generative (cfg.params) and the FX are tuned independently.
            fx_path = fx_name if os.path.isabs(fx_name) \
                               else os.path.join(self.cfg.shaders_dir, fx_name)
            try:
                with open(fx_path) as f:
                    fx_src = _subst(f.read(), self.cfg.fx_params, "f")
            except OSError as e:
                log.warning("can't read FX shader %s: %s", fx_path, e)
                self._finalize_shaders(new_active, is_generative)
                return

            if square_mode:
                native_aspect = w / h
                fx_src = fx_src.replace("#define SQUARE_SRC 0",
                                         "#define SQUARE_SRC 1", 1)
                fx_src = re.sub(r"#define NATIVE_ASPECT [\d.]+",
                                 f"#define NATIVE_ASPECT {native_aspect:.6f}",
                                 fx_src, count=1)
                fx_src = re.sub(r"#define SQ_SCALE_X [\d.]+",
                                 f"#define SQ_SCALE_X {w / side:.6f}",
                                 fx_src, count=1)
                fx_src = re.sub(r"#define SQ_SCALE_Y [\d.]+",
                                 f"#define SQ_SCALE_Y {h / side:.6f}",
                                 fx_src, count=1)
                fx_src = fx_src.replace(
                    "//!BIND HOOKED\n",
                    f"//!BIND HOOKED\n//!WIDTH {w}\n"
                    f"//!HEIGHT {h}\n", 1)

            self._tmp_counter += 1
            fx_tmp = os.path.join(TMP_SHADER_DIR,
                                  f"{TMP_SHADER_PREFIX}{self._tmp_counter:06d}.glsl")
            try:
                with open(fx_tmp, "w") as f:
                    f.write(fx_src)
            except OSError as e:
                log.warning("can't write FX tmp shader: %s", e)
                self._finalize_shaders(new_active, is_generative)
                return
            new_active.append(fx_tmp)
            log.debug("shaders -> %s + fx(%s)%s", os.path.basename(gen_tmp),
                      os.path.basename(fx_path),
                      " [square]" if square_mode else "")

        else:
            # ── single shader ──────────────────────────────────────────────
            try:
                with open(gen_tmp, "w") as f:
                    f.write(out)
            except OSError as e:
                log.warning("can't write tmp shader: %s", e)
                return
            new_active.append(gen_tmp)

        self._finalize_shaders(new_active, is_generative)

    def _finalize_shaders(self, new_active, is_generative=False):
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
