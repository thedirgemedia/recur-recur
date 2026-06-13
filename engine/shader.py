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
import logging
import threading
import time

log = logging.getLogger("shader")

# How long the knob must be still before we recompile the shader (ms)
DEBOUNCE_MS = 100
# Temp shader files live here; counter suffix ensures unique paths per apply
TMP_SHADER_DIR = "/tmp"
TMP_SHADER_PREFIX = "recur_s"
# Regex matching `#define PARAM_N <value>` lines we want to substitute
PARAM_RE = re.compile(r'^(\s*#define\s+PARAM_([1-4])\s+)([^\s/]+)', re.M)

# Blend modes for shader+clip compositing (SHADER mode, * key)
BLEND_MODE_MAP = {
    "difference": 1,
    "addition":   2,
    "multiply":   3,
    "screen":     4,
    "mix":        5,
}

# Second-pass hook: reads the generative output (gen_out, saved by the first
# shader) and the original clip (HOOKED/MAIN) and composites them.
# __BLEND_MODE__ is substituted at write time with the integer mode.
BLEND_SHADER_SRC = """\
//!DESC blend — composite generative shader with clip
//!HOOK MAIN
//!BIND HOOKED
//!BIND gen_out

#define BLEND_MODE __BLEND_MODE__
#define BLEND_AMT  __BLEND_AMT__

vec4 hook() {
    vec4 gen = gen_out_texOff(vec2(0.0));
    vec4 vid = HOOKED_texOff(vec2(0.0));
    vec3 bl;
#if BLEND_MODE == 1
    bl = abs(gen.rgb - vid.rgb);
#elif BLEND_MODE == 2
    bl = clamp(gen.rgb + vid.rgb, 0.0, 1.0);
#elif BLEND_MODE == 3
    bl = gen.rgb * vid.rgb;
#elif BLEND_MODE == 4
    bl = 1.0 - (1.0 - gen.rgb) * (1.0 - vid.rgb);
#else
    bl = mix(vid.rgb, gen.rgb, 0.5);
#endif
    // BLEND_AMT: 0 = pure video, 1 = full blend formula result
    return vec4(mix(vid.rgb, bl, BLEND_AMT), 1.0);
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
        self._tmp_counter = 0            # monotonically increasing; ensures unique paths
        self._tmp_active  = []           # base shader tmp files mpv currently has loaded
        self._color_tmp   = None         # colour-pass tmp file (appended last), or None
        self._color_sig   = None         # (hue, sat) last written — avoids rewrites
        self._label_cache    = {}  # {path: {p1:label, ...}} — avoid per-frame file reads
        self._fallback_label_path = None  # resolved fallback path when self.current is None

    # ------------------------------------------------------------- discovery
    def list_shaders(self, kind=None):
        """List .glsl files. kind='generative' returns plasma/waves/etc;
        kind='fx' returns vhs/glitch/etc. None = all."""
        d = self.cfg.shaders_dir
        if not os.path.isdir(d):
            return []
        files = sorted(f for f in os.listdir(d) if f.endswith(".glsl"))
        if kind is None:
            return files
        gen = set(getattr(self.cfg, "generative_shaders",
                          {"plasma.glsl", "waves.glsl", "tunnel.glsl",
                           "voronoi.glsl", "kaleidoscope.glsl"}))
        excl = set(getattr(self.cfg, "excluded_from_fx", set()))
        if kind == "generative":
            return [f for f in files if f in gen]
        else:  # fx
            return [f for f in files if f not in gen and f not in excl]

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
        self._apply_now()
        log.info("fx overlay -> %s (on %s)", lst[i],
                 os.path.basename(self.current) if self.current else "—")

    # ------------------------------------------------------------- params
    def set_param(self, key, value):
        """Called by GPIO/MIDI when a knob moves. Schedules a debounced
        recompile."""
        value = max(0.0, min(1.0, value))
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
            self._apply_now()

    def _snapshot(self):
        return tuple(self.cfg.params.get(k, 0.5)
                     for k in ("p1", "p2", "p3", "p4"))

    # ------------------------------------------------------------- emit
    def param_labels(self):
        """Parse the /* label */ comment from each PARAM_N line and return a
        dict like {"p1": "speed", "p2": "scale", ...}.

        Uses the currently loaded shader (self.current) when available.
        Falls back to cfg.current_fx then cfg.current_shader so the menu
        always shows meaningful labels even in SAMPLER mode (no shader active).

        The fallback path resolution (os.path.exists) is cached so the display
        render loop (20 FPS) doesn't repeatedly hit the filesystem.
        """
        defaults = {f"p{n}": f"P{n}" for n in range(1, 5)}

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
            return defaults

        if path in self._label_cache:
            return self._label_cache[path]

        try:
            with open(path) as f:
                src = f.read()
        except OSError:
            return defaults
        pat = re.compile(r'#define\s+PARAM_([1-4])\s+\S+[^\n]*/\*\s*([^*]+?)\s*\*/')
        for m in pat.finditer(src):
            defaults[f"p{m.group(1)}"] = m.group(2).strip()
        self._label_cache[path] = defaults
        return defaults

    def _read_defaults(self):
        """Apply PARAM_N defaults from the shader source to cfg.params.
        Each shader starts at its designed settings; knob movements override
        from there (the ADC poll fires within ~50ms and writes physical position)."""
        if not self.current:
            return
        try:
            with open(self.current) as f:
                src = f.read()
        except OSError:
            return
        for m in PARAM_RE.finditer(src):
            key = f"p{m.group(2)}"
            try:
                self.cfg.params[key] = float(m.group(3))
            except (ValueError, TypeError):
                pass

    def _apply_now(self):
        """Substitute PARAM_N values, write to a new unique tmp path, and
        push to mpv. A fresh path is used every call because mpv 0.40 caches
        compiled shaders in-memory by path and skips recompilation otherwise."""
        if not self.current:
            return
        try:
            with open(self.current) as f:
                src = f.read()
        except OSError as e:
            log.warning("can't read shader %s: %s", self.current, e)
            return

        vals = self.cfg.params
        def sub(m):
            n = m.group(2)
            key = f"p{n}"
            v = vals.get(key, 0.5)
            return f"{m.group(1)}{v:.4f}"
        out = PARAM_RE.sub(sub, src)

        self._tmp_counter += 1
        gen_tmp = os.path.join(TMP_SHADER_DIR,
                               f"{TMP_SHADER_PREFIX}{self._tmp_counter:06d}.glsl")
        new_active = []

        gen_set = getattr(self.cfg, "generative_shaders",
                          {"plasma.glsl", "waves.glsl", "tunnel.glsl",
                           "voronoi.glsl", "kaleidoscope.glsl"})
        is_generative = os.path.basename(self.current) in gen_set

        if getattr(self.cfg, "shader_blend", False) and is_generative:
            # ── generative + blend (composite with clip/camera) ───────────
            # Inject //!SAVE gen_out so the blend shader can read both layers.
            # Use the LAST //!BIND HOOKED so that multi-pass shaders (e.g.
            # hue_cycle, which has a state-only first pass) get the annotation
            # on the pass that actually outputs video, not the bookkeeping pass.
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

            log.debug("shaders -> %s + blend(%s)", os.path.basename(gen_tmp), mode_name)

        elif is_generative and getattr(self.cfg, "shader_fx_stack", False) \
                and self.cfg.current_fx:
            # ── generative + FX stacked ────────────────────────────────────
            # Write generative with live param substitution.
            try:
                with open(gen_tmp, "w") as f:
                    f.write(out)
            except OSError as e:
                log.warning("can't write tmp shader: %s", e)
                return
            new_active.append(gen_tmp)

            # Read FX shader source at its file defaults (params control the
            # generative layer; the FX runs at its authored defaults).
            fx_name = self.cfg.current_fx
            fx_path = fx_name if os.path.isabs(fx_name) \
                               else os.path.join(self.cfg.shaders_dir, fx_name)
            try:
                with open(fx_path) as f:
                    fx_src = f.read()
            except OSError as e:
                log.warning("can't read FX shader %s: %s", fx_path, e)
                self._finalize_shaders(new_active, is_generative)
                return

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
            log.debug("shaders -> %s + fx(%s)", os.path.basename(gen_tmp),
                      os.path.basename(fx_path))

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
        """Update hue (turns 0..1) and/or saturation (0..2) and re-push so the
        colour pass applies live in every mode."""
        if hue is not None:
            self.cfg.color_hue = hue
        if sat is not None:
            self.cfg.color_sat = sat
        self._refresh_color()
        self._push_shaders()

    def reapply(self):
        """Re-emit the current shader (e.g. after blend mode or source changed)."""
        self._apply_now()

    def _cmd_clear(self):
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
