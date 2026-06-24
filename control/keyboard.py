#!/usr/bin/env python3
"""
KeyboardController — USB numpad as the primary performance control.

Layout (17-cell, 19-key with double-tall Enter):
   ┌──────┬──────┬──────┬──────┐
   │ Num  │  /   │  *   │  -   │
   ├──────┼──────┼──────┼──────┤
   │  7   │  8   │  9   │  +   │
   ├──────┼──────┼──────┼──────┤
   │  4   │  5   │  6   │ Bksp │
   ├──────┼──────┼──────┼──────┤
   │  1   │  2   │  3   │      │
   ├──────┼──────┼──────┤Enter │
   │  0   │ 000  │  .   │      │
   └──────┴──────┴──────┴──────┘

PERFORM mode (SAMPLER):
  Num     switch to MENU mode
  4-9     trigger the clip assigned to that slot (assigned in BROWSER menu)
  /       toggle V overlay
  *       cycle overlay blend mode
  -       previous FX shader
  +       next FX shader
  Enter   cycle instrument mode (SAMPLER -> SHADER -> LIVE)
  Bksp    cycle param layer: FX -> COLOUR -> BLEND -> TRAIL -> FX (see below)
  1       cycle the selected slot within the current param layer
  2       selected slot -= step
  3       selected slot += step
  0       set IN point (1st press) / OUT point (2nd press) / clear (3rd press)
  000     toggle temporal trail  (dedicated key — may be dead on some units)
  .       toggle temporal trail  (use this if 000 key is unresponsive)
  hold 0 + .     record toggle
  hold 0 + 4-9   load the preset assigned to that slot (PRESETS menu)

PERFORM mode (LIVE):
  Same as SAMPLER except 4-9 — there's no clip to trigger while the camera
  is the source, so plain 4-9 loads the preset assigned to that slot
  (PRESETS menu), same as hold-0+4-9 does in the other modes.

PERFORM mode (SHADER):
  4-9     load the generative shader assigned to that slot (SHADERS menu)
  -       previous FX shader (stacked on top of generative)
  +       next FX shader (stacked on top of generative)
  /       toggle shader blend (generative ↔ generative+clip)
  *       cycle shader blend mode
  0       next generative shader (no real clip to mark in/out on here)
  Bksp    cycle param layer: SHDR -> FX -> COLOUR -> BLEND -> TRAIL -> SHDR
  1       cycle the selected slot within the current param layer
  2       selected slot -= step
  3       selected slot += step
  hold 0 + .     record toggle
  hold 0 + 4-9   load the preset assigned to that slot (PRESETS menu)
  hold / + 4-9   load + trigger the clip assigned to that slot (BROWSER),
                 without leaving SHADER mode — useful for changing the video
                 source while blend is active

Param layers (Bksp cycles through whichever are available in the current
mode — SHDR only exists in SHADER mode; see _PARAM_LAYERS below):
  SHDR    the active generative shader's own params (SHADER mode only)
  FX      the active FX shader's own params f1-f4
  COLOUR  hue / saturation / trail decay
  BLEND   compositing: shader<->video blend amount+mode+source (SHADER) or
          overlay blend mode+opacity (SAMPLER/LIVE)
  TRAIL   temporal echo: on/off, blend type, blend mode, delay, opacity

MENU mode: Num Lock toggles the navigable menu on the SPI display; while it
is active every key routes to `self.inst.menu.handle()` and none reach the
perform handlers (see control/menu.py for the bindings).

Note: the dedicated '000' key sends KEY_KP000 (or KEY_KP00) and is mapped
directly. The triple-KP0 coalescing below is a fallback for numpads that
implement '000' by sending three rapid KEY_KP0 events instead.
"""

import logging
import threading
import time

from engine.shader import clamp01

log = logging.getLogger("kbd")

try:
    from evdev import InputDevice, categorize, ecodes, list_devices
    HAVE_EVDEV = True
except Exception:
    HAVE_EVDEV = False


# Map evdev keycodes (numpad-specific) to logical key names. Numpad keys
# have a "KP" prefix in evdev and are distinct from the top-row digits.
NUMPAD_MAP = {
    "KEY_NUMLOCK":     "NUM",
    "KEY_KPSLASH":     "/",
    "KEY_KPASTERISK":  "*",
    "KEY_KPMINUS":     "-",
    "KEY_KPPLUS":      "+",
    "KEY_KPENTER":     "ENTER",
    "KEY_BACKSPACE":   "BKSP",
    "KEY_KPDOT":       ".",
    "KEY_KP0":         "0",
    "KEY_KP00":        "000",   # dedicated 00 key (some numpads)
    "KEY_KP000":       "000",   # dedicated 000 key — single press, no triple-tap needed
    "KEY_KP1":         "1",
    "KEY_KP2":         "2",
    "KEY_KP3":         "3",
    "KEY_KP4":         "4",
    "KEY_KP5":         "5",
    "KEY_KP6":         "6",
    "KEY_KP7":         "7",
    "KEY_KP8":         "8",
    "KEY_KP9":         "9",
}

# The 000 key is detected as three rapid KP0 presses within this window.
TRIPLE_ZERO_WINDOW = 0.15   # seconds
PARAM_STEP = 0.05

# Param layers (indices). BKSP cycles the ones available in the current mode:
#   0 SHDR    generative shader params p1–p3              (SHADER mode only)
#   1 FX      the active FX shader's own params f1–f4
#   2 COLOUR  palette (p4, SHADER only) + hue / sat / trail decay
#   3 BLEND   compositing — shader↔video blend (SHADER) or overlay (SAMPLER/LIVE)
#   4 TRAIL   temporal echo — on/off, blend type, mode, delay, opacity
_PARAM_LAYERS = ("SHDR", "FX", "COLOUR", "BLEND", "TRAIL")
_BLEND_LABELS = {"mode": "MODE", "amt": "BLD AMT", "opc": "OVL OPC", "src": "SRC"}
_COLOUR_LABELS = {"hue": "HUE", "sat": "SAT", "trl_decay": "TRL OPC"}
_TRAIL_LABELS  = {"on": "TRL ON", "type": "TYPE", "mode": "MODE",
                  "delay": "DELAY", "echos": "ECHOS", "opacity": "OPACITY"}


class KeyboardController:
    def __init__(self, inst):
        self.inst   = inst
        self._stop  = threading.Event()
        self._thread= None
        self.dev    = None

        # Which parameter the 2/3 keys currently adjust. BKSP cycles the layer:
        # 0 = shader p1-p4, 1 = FX controls, 2 = COLOUR (hue/sat).
        self._param_layer = 0
        self._param_idx   = 0

        # in/out point stage: 0=waiting for IN, 1=waiting for OUT, 2=waiting for clear
        self._inout_stage = 0

        # 000 detection: track recent KP0 timestamps.
        self._kp0_history = []
        self._key0_held   = False   # True while KP0 is physically depressed

        # hold-/ + 4-9 chord: defer the / action to key-up so we can detect
        # whether it was used as a modifier before firing blend toggle.
        self._keyslash_held        = False
        self._keyslash_chord_used  = False

    # ------------------------------------------------------------- lifecycle
    def start(self):
        if not HAVE_EVDEV:
            log.warning("evdev not available — keyboard control disabled")
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="numpad")
        self._thread.start()
        log.info("numpad thread started — scanning for device")

    def stop(self):
        self._stop.set()

    def _find_numpad(self):
        """Find a device that has the KP* keycodes. Prefer SIGMACHIP
        (common USB numpad chipset) then 'pad'/'numeric' in the name,
        otherwise fall through to the last candidate (newest USB)."""
        candidates = []
        for path in list_devices():
            try:
                d = InputDevice(path)
            except Exception:
                continue
            caps = d.capabilities()
            keys = caps.get(ecodes.EV_KEY, [])
            if ecodes.KEY_KP5 in keys and ecodes.KEY_KPENTER in keys:
                candidates.append(d)
        if not candidates:
            return None
        for d in candidates:
            if "sigmachip" in (d.name or "").lower():
                return d
        for d in candidates:
            n = (d.name or "").lower()
            if "pad" in n or "numeric" in n:
                return d
        return candidates[-1]
    # ------------------------------------------------------------- main loop
    def _loop(self):
        while not self._stop.is_set():
            if self.dev is None:
                self.dev = self._find_numpad()
                if not self.dev:
                    time.sleep(2)
                    continue
                log.info("numpad connected: %s (%s)", self.dev.path, self.dev.name)
            try:
                for event in self.dev.read_loop():
                    if self._stop.is_set():
                        return
                    if event.type != ecodes.EV_KEY:
                        continue
                    key = categorize(event)
                    code = key.keycode if isinstance(key.keycode, str) \
                                       else key.keycode[0]
                    name = NUMPAD_MAP.get(code)
                    if name is None:
                        continue

                    # Track KP0 hold state using key-up events so the
                    # hold-0 + tap-dot combo can be detected below.
                    if name == "0" and key.keystate == key.key_up:
                        self._key0_held = False
                        continue

                    # Defer / action to key-up so hold-/ + 4-9 can be
                    # detected. On key-up, fire blend toggle only if the
                    # chord was not consumed by a 4-9 tap during the hold.
                    if name == "/" and key.keystate == key.key_up:
                        if not self._keyslash_chord_used:
                            self._dispatch("/")
                        self._keyslash_held       = False
                        self._keyslash_chord_used = False
                        continue

                    # All other processing is key-down only.
                    if key.keystate != key.key_down:
                        continue

                    if name == "/":
                        self._keyslash_held = True
                        continue   # action deferred to key-up

                    # hold-/ + 4-9 in SHADER mode: load+trigger clip slot
                    # without leaving SHADER mode (for blend source change).
                    if name in ("4","5","6","7","8","9") and self._keyslash_held:
                        if self.inst.mode == "SHADER":
                            self._keyslash_chord_used = True
                            self._dispatch(f"CLIPSLOT_{name}")
                            continue
                        # In other modes fall through to normal 4-9 handling;
                        # / will still fire its normal action on key-up.

                    if name == "0":
                        self._key0_held = True
                        now = time.monotonic()
                        self._kp0_history = [t for t in self._kp0_history
                                             if now - t < TRIPLE_ZERO_WINDOW]
                        self._kp0_history.append(now)
                        if len(self._kp0_history) >= 3:
                            self._kp0_history.clear()
                            self._dispatch("000")
                            continue
                        threading.Timer(TRIPLE_ZERO_WINDOW + 0.01,
                                        self._maybe_emit_single_zero,
                                        args=(now,)).start()
                        continue

                    # Hold-0 + tap-dot → record toggle.
                    # Clear kp0_history so the pending 0 timer becomes a no-op.
                    if name == "." and self._key0_held:
                        self._kp0_history.clear()
                        self._dispatch("REC")
                        continue

                    # Hold-0 + tap-4-9 → load preset slot.
                    if name in ("4","5","6","7","8","9") and self._key0_held:
                        self._kp0_history.clear()
                        self._dispatch(f"PRESET_{name}")
                        continue

                    self._dispatch(name)

            except OSError as e:
                log.warning("numpad disconnected (%s) — will reconnect", e)
                try:
                    self.dev.close()
                except Exception:
                    pass
                self.dev = None
                time.sleep(1)   # brief pause before scanning for reconnect

    def _maybe_emit_single_zero(self, ts):
        """If the KP0 timestamp `ts` is still in the history (no
        subsequent triple consumed it), emit a single '0' action.
        Then remove ts from the history so subsequent timers for the
        same burst don't double-fire."""
        try:
            self._kp0_history.remove(ts)
        except ValueError:
            # ts already consumed (by a triple-emit, which clears history)
            return
        # If there are still entries in the history newer than ts, those
        # are part of the same burst and one of their own timers will
        # handle emitting. If our ts was the most recent, we own the
        # single-zero emit.
        if any(t > ts for t in self._kp0_history):
            return
        # Clear remaining entries (they're older and irrelevant now)
        self._kp0_history.clear()
        self._dispatch("0")

    # ------------------------------------------------------------- dispatch
    def _dispatch(self, name):
        # NumLock toggles the on-screen menu. While the menu is active, EVERY
        # key routes to menu navigation and NONE reach the perform handlers —
        # so the HDMI video output is never changed by a keypress in menu mode.
        if name == "NUM":
            self.inst.menu.toggle()
            return

        if self.inst.menu.active:
            self.inst.menu.handle(name)
            return

        self._dispatch_perform(name)

    def _dispatch_perform(self, name):
        inst = self.inst
        s    = inst.sampler
        sh   = inst.shader

        # ── hold-0 + 4-9: load preset slot (any mode) ─────────────────────
        if name.startswith("PRESET_"):
            inst.load_preset_slot(int(name[-1]))
            return

        # ── hold-/ + 4-9 in SHADER mode: load+trigger clip slot ───────────
        if name.startswith("CLIPSLOT_"):
            n = int(name[-1])
            if s.slot(n):
                s.trigger()
                clip = (inst.cfg.current_clip or "").split("/")[-1]
                inst.osd.show(f"CLIP {n}: {clip.upper()[:14]}")
            else:
                inst.osd.show(f"CLIP {n}: EMPTY")
            return

        # ── SHADER mode: 4-9 load assigned generative shader ──────────────
        if inst.mode == "SHADER" and name in ("4","5","6","7","8","9"):
            n    = int(name)
            sname = inst.cfg.shader_slots.get(n)
            if sname:
                sh.load(sname)
                inst.osd.show(f"SLOT {n}: {sname.replace('.glsl','').upper()}")
            else:
                inst.osd.show(f"SLOT {n}: EMPTY")
            return

        # ── LIVE mode: 4-9 recall presets — there's no "clip" to trigger
        # while the camera is the source, so plain 4-9 does what hold-0+4-9
        # does elsewhere instead of falling through to the clip-slot path
        # below (which would kill the camera feed to play a stored clip).
        if inst.mode == "LIVE" and name in ("4","5","6","7","8","9"):
            inst.load_preset_slot(int(name))
            return

        # ── clip slots 4-9 (SAMPLER) ───────────────────────────────────────
        if name in ("4","5","6","7","8","9"):
            n = int(name)
            if s.slot(n):
                s.trigger()
                self._inout_stage = 0
            else:
                inst.osd.show(f"SLOT {n}: EMPTY")
            return

        if name == "ENTER":
            inst.cycle_mode()
        elif name == "+":
            if inst.mode == "SHADER":
                # Stack FX on top of the generative shader without replacing it.
                sh.apply_fx_overlay(+1)
                inst.osd.show(f"FX: {inst.cfg.current_fx.replace('.glsl','').upper()}")
            else:
                sh.cycle(+1, kind="fx")
        elif name == "-":
            if inst.mode == "SHADER":
                sh.apply_fx_overlay(-1)
                inst.osd.show(f"FX: {inst.cfg.current_fx.replace('.glsl','').upper()}")
            else:
                sh.cycle(-1, kind="fx")
        elif name == "/":
            if inst.mode == "SHADER":
                inst.shader_blend_toggle()
            else:
                inst.overlay_toggle()
        elif name == "*":
            if inst.mode == "SHADER":
                inst.shader_blend_cycle()
            else:
                inst.overlay_cycle_mode(+1)
        elif name == "BKSP":
            layers = self._avail_layers()
            cur = self._param_layer if self._param_layer in layers else layers[0]
            self._param_layer = layers[(layers.index(cur) + 1) % len(layers)]
            self._param_idx = 0
            inst.osd.show(f"PARAMS: {_PARAM_LAYERS[self._param_layer]}")
        elif name == "1":
            if self._param_layer == 4:          # TRAIL: temporal echo controls
                slots = self._trail_slots()
                self._param_idx = (self._param_idx + 1) % len(slots)
                inst.osd.show(f"TRAIL: {_TRAIL_LABELS[slots[self._param_idx]]}")
            elif self._param_layer == 3:        # BLEND: compositing controls
                slots = self._blend_slots()
                self._param_idx = (self._param_idx + 1) % len(slots)
                inst.osd.show(f"BLEND: {_BLEND_LABELS[slots[self._param_idx]]}")
            elif self._param_layer == 2:        # COLOUR: hue / sat / trl dec
                slots = self._colour_slots()
                self._param_idx = (self._param_idx + 1) % len(slots)
                slot = slots[self._param_idx]
                inst.osd.show(f"COLOUR: {_COLOUR_LABELS[slot]}")
            elif self._param_layer == 1:        # FX: active FX's own params (dynamic)
                lbls    = inst.shader.fx_param_labels()
                fx_keys = sorted(lbls.keys(), key=lambda k: int(k[1:]))
                self._param_idx = (self._param_idx + 1) % max(1, len(fx_keys))
                key = fx_keys[self._param_idx] if fx_keys else "f1"
                inst.osd.show(f"FX: {lbls.get(key, key.upper()).upper()}")
            else:                               # SHDR: generative params (dynamic)
                keys = self._get_shdr_keys()
                self._param_idx = (self._param_idx + 1) % max(1, len(keys))
                key = keys[self._param_idx] if keys else "p1"
                lbl = inst.shader.param_labels().get(key, key.upper())
                inst.osd.show(f"PARAM: {lbl.upper()}")
        elif name == "2":
            self._step_param(-PARAM_STEP)
        elif name == "3":
            self._step_param(+PARAM_STEP)
        elif name == "0":
            if inst.mode == "SHADER":
                # No real clip to mark in/out on — repurpose as next-shader.
                sh.cycle(+1, kind="generative")
                inst.osd.show(f"SHADER: {inst.cfg.current_shader.replace('.glsl', '').upper()}")
            else:
                # 1st press: set in, 2nd press: set out, 3rd press: clear
                stage = getattr(self, "_inout_stage", 0)
                if stage == 0:
                    s.set_in()
                    inst.osd.show("IN POINT")
                    self._inout_stage = 1
                elif stage == 1:
                    if s.set_out():
                        inst.osd.show("OUT POINT")
                        self._inout_stage = 2
                    else:
                        inst.osd.show("OUT MUST BE AFTER IN")
                else:
                    s.clear_points()
                    inst.osd.show("CLEARED IN/OUT")
                    self._inout_stage = 0
        elif name == "000":
            inst.trail_toggle()
        elif name == ".":
            inst.trail_toggle()
        elif name == "REC":
            inst.record_toggle()

    def _avail_layers(self):
        """Param layers BKSP can reach in the current mode. SHDR (generative)
        is only meaningful in SHADER mode."""
        if self.inst.mode == "SHADER":
            return [0, 1, 2, 3, 4]   # SHDR, FX, COLOUR, BLEND, TRAIL
        return [1, 2, 3, 4]          # FX, COLOUR, BLEND, TRAIL

    def _get_shdr_keys(self):
        """Sorted param keys for the current generative shader (e.g. p1..p8)."""
        return sorted(self.inst.shader.param_labels().keys(), key=lambda k: int(k[1:]))

    def _colour_slots(self):
        return ("hue", "sat", "trl_decay")

    def _blend_slots(self):
        """BLEND-layer slots for the current mode: shader↔video blend in SHADER,
        overlay self-blend in SAMPLER/LIVE."""
        if self.inst.mode == "SHADER":
            return ("mode", "amt", "src")
        return ("mode", "opc")

    def _trail_slots(self):
        return ("on", "type", "mode", "delay", "echos", "opacity")

    def sync_param_layer(self):
        """Keep the selected layer valid for the current mode (called on mode
        change) — SHDR collapses to FX when leaving SHADER mode."""
        layers = self._avail_layers()
        if self._param_layer not in layers:
            self._param_layer = layers[0]
            self._param_idx = 0

    def _step_param(self, delta):
        cfg  = self.inst.cfg
        inst = self.inst
        sign = 1.0 if delta > 0 else -1.0
        if self._param_layer == 0:        # ── SHDR: generative params (dynamic)
            keys = self._get_shdr_keys()
            if not keys:
                return
            key = keys[self._param_idx % len(keys)]
            cur = cfg.params.get(key, 0.5)
            new = clamp01(cur + delta)
            if new == cur:
                return
            inst.shader.set_param(key, new)
            lbl = inst.shader.param_labels().get(key, key.upper())
            ul  = lbl.upper()
            if ul.endswith(' X') or ul.endswith(' Y') or ul in ('X', 'Y'):
                inst.osd.show(f"{ul}: {(new - 0.5) * 200:+.0f}")
            elif ul.endswith('STARS') or ul == 'STARS':
                inst.osd.show(f"{ul}: {max(1, round(new * 500))}")
            else:
                inst.osd.show(f"{ul}: {new:.2f}")
        elif self._param_layer == 1:      # ── FX: the active FX's own params
            fx_keys = sorted(inst.shader.fx_param_labels().keys(), key=lambda k: int(k[1:]))
            key = fx_keys[self._param_idx % max(1, len(fx_keys))] if fx_keys else "f1"
            cur = cfg.fx_params.get(key, 0.5)
            new = clamp01(cur + delta)
            if new == cur:
                return
            inst.shader.set_fx_param(key, new)
            lbl = inst.shader.fx_param_labels().get(key, key.upper())
            inst.osd.show(f"{lbl.upper()}: {new:.2f}")
        elif self._param_layer == 2:      # ── COLOUR: hue / sat / trl dec
            slots = self._colour_slots()
            slot  = slots[self._param_idx % len(slots)]
            if slot == "hue":
                inst.color_adjust_hue(sign * 0.02)
            elif slot == "sat":
                inst.color_adjust_sat(sign * 0.05)
            elif slot == "trl_decay":
                cur = getattr(cfg, 'trail_mode_opacity', 0.5)
                new = round(max(0.0, min(1.0, cur + delta)), 3)
                if new == cur:
                    return
                cfg.trail_mode_opacity = new
                inst.osd.show(f"TRL OPC: {new:.2f}")
                if getattr(cfg, 'trail_on', False):
                    inst.sampler.refresh_trail()
        elif self._param_layer == 3:      # ── BLEND: compositing controls
            slots = self._blend_slots()
            slot  = slots[self._param_idx % len(slots)]
            d     = 1 if delta > 0 else -1
            if slot == "mode":
                if inst.mode == "SHADER":
                    inst.shader_blend_cycle(d)
                else:
                    inst.overlay_cycle_mode(d)
            elif slot == "amt":
                cur = getattr(cfg, 'shader_blend_amount', 0.5)
                new = round(clamp01(cur + delta), 2)
                if new == cur:
                    return
                cfg.shader_blend_amount = new
                inst.osd.show(f"BLD AMT: {new:.2f}")
                if cfg.shader_blend:
                    inst.shader.reapply()
            elif slot == "opc":
                cur = getattr(cfg, 'overlay_blend_amount', 1.0)
                new = round(clamp01(cur + delta), 2)
                if new == cur:
                    return
                cfg.overlay_blend_amount = new
                inst.osd.show(f"OVL OPC: {new:.2f}")
                if cfg.overlay_on:
                    inst.sampler.refresh_overlay()
            elif slot == "src":
                srcs = list(cfg.SHADER_BLEND_SOURCES)
                i = srcs.index(cfg.shader_blend_source) if cfg.shader_blend_source in srcs else 0
                cfg.shader_blend_source = srcs[(i + d) % len(srcs)]
                inst.osd.show(f"BLEND SRC: {cfg.shader_blend_source}")
                if cfg.shader_blend and inst.mode == "SHADER":
                    inst._start_blend_source()
        else:                             # ── TRAIL: temporal echo controls
            slots = self._trail_slots()
            slot  = slots[self._param_idx % len(slots)]
            d     = 1 if delta > 0 else -1
            if slot == "on":
                cfg.trail_on = not cfg.trail_on
                inst.osd.show(f"TRAIL: {'ON' if cfg.trail_on else 'OFF'}")
                inst.sampler.refresh_trail()
            elif slot == "type":
                types = list(cfg.TRAIL_BLEND_TYPES)
                i = types.index(cfg.trail_blend_type) if cfg.trail_blend_type in types else 0
                cfg.trail_blend_type = types[(i + d) % len(types)]
                inst.osd.show(f"TRL TYPE: {cfg.trail_blend_type.upper()}")
                if cfg.trail_on:
                    inst.sampler.refresh_trail()
            elif slot == "mode":
                modes = list(cfg.TRAIL_MODES)
                i = modes.index(cfg.trail_mode) if cfg.trail_mode in modes else 0
                cfg.trail_mode = modes[(i + d) % len(modes)]
                inst.osd.show(f"TRL MODE: {cfg.trail_mode.upper()}")
                if cfg.trail_on:
                    inst.sampler.refresh_trail()
            elif slot == "delay":
                cur = getattr(cfg, 'trail_delay_s', 2.0)
                new = round(max(0.25, min(8.0, cur + d * 0.25)), 2)
                if new == cur:
                    return
                cfg.trail_delay_s = new
                inst.osd.show(f"TRL DLY: {new:.2f}s")
                if cfg.trail_on:
                    inst.sampler.refresh_trail()
            elif slot == "echos":
                cur = getattr(cfg, 'trail_echo_count', 1)
                new = max(1, min(15, cur + d))
                if new == cur:
                    return
                cfg.trail_echo_count = new
                inst.osd.show(f"TRL ECHOS: {format(new, 'x')}")
                if cfg.trail_on:
                    inst.sampler.refresh_trail()
            elif slot == "opacity":
                cur = getattr(cfg, 'trail_mode_opacity', 0.5)
                new = round(clamp01(cur + delta), 3)
                if new == cur:
                    return
                cfg.trail_mode_opacity = new
                inst.osd.show(f"TRL OPC: {new:.2f}")
                if cfg.trail_on:
                    inst.sampler.refresh_trail()
