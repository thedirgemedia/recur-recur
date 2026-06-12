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

PERFORM mode (SAMPLER / LIVE):
  Num     switch to MENU mode
  4-9     trigger the clip assigned to that slot (assigned in BROWSER menu)
  /       toggle V overlay
  *       cycle overlay blend mode
  -       previous FX shader
  +       next FX shader
  Enter   cycle instrument mode (SAMPLER -> SHADER -> LIVE)
  1       cycle selected param (p1 -> p2 -> p3 -> p4 -> p1)
  2       selected param -= 0.1
  3       selected param += 0.1
  0       set IN point (1st press) / OUT point (2nd press) / clear (3rd press)
  000     toggle temporal trail  (dedicated key — may be dead on some units)
  .       toggle temporal trail  (use this if 000 key is unresponsive)
  Bksp    toggle param view: shader p1-p4 ↔ FX controls (blend amt, ovl frames)

PERFORM mode (SHADER):
  4-9     load the generative shader assigned to that slot (SHADERS menu)
  -       previous FX shader (stacked on top of generative)
  +       next FX shader (stacked on top of generative)
  /       toggle shader blend (generative ↔ generative+clip)
  *       cycle shader blend mode
  1       cycle selected param (p1 -> p2 -> p3 -> p4)
  2       selected param -= 0.1
  3       selected param += 0.1

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
PARAM_STEP = 0.1

# FX-layer slots exposed when BKSP toggles to FX param view
_FX_PARAMS = ("blend_amt", "ovl_frames", "trl_decay", None)
_FX_LABELS = {"blend_amt": "BLD AMT", "ovl_frames": "OVL FRM", "trl_decay": "TRL DEC"}


class KeyboardController:
    def __init__(self, inst):
        self.inst   = inst
        self._stop  = threading.Event()
        self._thread= None
        self.dev    = None

        # Which parameter the 2/3 keys currently adjust.
        # Layer 0 = shader p1-p4; layer 1 = FX controls (BKSP toggles).
        self._param_layer = 0
        self._param_keys  = ("p1", "p2", "p3", "p4")
        self._param_idx   = 0

        # in/out point stage: 0=waiting for IN, 1=waiting for OUT, 2=waiting for clear
        self._inout_stage = 0

        # 000 detection: track recent KP0 timestamps.
        self._kp0_history = []

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
                    if key.keystate != key.key_down:
                        continue
                    code = key.keycode if isinstance(key.keycode, str) \
                                       else key.keycode[0]
                    name = NUMPAD_MAP.get(code)
                    if name is None:
                        continue

                    if name == "0":
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

        # ── clip slots 4-9 (SAMPLER / LIVE) ───────────────────────────────
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
            self._param_layer = 1 - self._param_layer
            self._param_idx = 0
            inst.osd.show("PARAMS: FX" if self._param_layer else "PARAMS: SHDR")
        elif name == "1":
            if self._param_layer == 0:
                cfg         = inst.cfg
                _overlay_on   = getattr(cfg, 'overlay_on',   False)
                _blend_active = _overlay_on or getattr(cfg, 'shader_blend', False)
                if _overlay_on:
                    max_idx = 6
                elif _blend_active:
                    max_idx = 5
                else:
                    max_idx = 4
                self._param_idx = (self._param_idx + 1) % max_idx
                if self._param_idx < 4:
                    inst.osd.show(f"PARAM: {self._param_keys[self._param_idx].upper()}")
                    log.info("selected param -> %s", self._param_keys[self._param_idx])
                elif self._param_idx == 4:
                    lbl = "BLD AMT" if getattr(cfg, 'shader_blend', False) else "OVL DLY"
                    inst.osd.show(f"PARAM: {lbl}")
                    log.info("selected param -> blend/ovl")
                else:
                    inst.osd.show("PARAM: OVL OPC")
                    log.info("selected param -> ovl opacity")
            else:
                n = sum(1 for k in _FX_PARAMS if k is not None)
                self._param_idx = (self._param_idx + 1) % n
                inst.osd.show(f"FX: {_FX_LABELS.get(_FX_PARAMS[self._param_idx], '?')}")
        elif name == "2":
            self._step_param(-PARAM_STEP)
        elif name == "3":
            self._step_param(+PARAM_STEP)
        elif name == "0":
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

    def _step_param(self, delta):
        if self._param_layer == 0:
            cfg           = self.inst.cfg
            _overlay_on   = getattr(cfg, 'overlay_on',   False)
            _blend_active = _overlay_on or getattr(cfg, 'shader_blend', False)
            # clamp idx when active params shrink (e.g. overlay turned off)
            if self._param_idx >= 6:
                self._param_idx = 0
            elif self._param_idx == 5 and not _overlay_on:
                self._param_idx = 0
            elif self._param_idx >= 4 and not _blend_active:
                self._param_idx = 0
            if self._param_idx < 4:
                key = self._param_keys[self._param_idx]
                cur = cfg.params.get(key, 0.5)
                new = max(0.0, min(1.0, cur + delta))
                if new == cur:
                    return
                self.inst.shader.set_param(key, new)
                self.inst.osd.show(f"{key.upper()}: {new:.2f}")
            elif self._param_idx == 4:
                # p5: blend amount or overlay decay
                if getattr(cfg, 'shader_blend', False):
                    cur = getattr(cfg, 'shader_blend_amount', 0.5)
                    new = max(0.0, min(1.0, round(cur + delta, 2)))
                    if new == cur:
                        return
                    cfg.shader_blend_amount = new
                    self.inst.osd.show(f"BLD AMT: {new:.2f}")
                    self.inst.shader.reapply()
                else:
                    cur = getattr(cfg, 'overlay_offset_frames', 8)
                    new = max(1, min(32, cur + (1 if delta > 0 else -1)))
                    if new == cur:
                        return
                    cfg.overlay_offset_frames = new
                    self.inst.osd.show(f"OVL DLY: {new}fr")
                    self.inst.sampler.refresh_overlay()
            else:
                # p6: overlay opacity
                cur = getattr(cfg, 'overlay_blend_amount', 1.0)
                new = max(0.0, min(1.0, round(cur + delta, 2)))
                if new == cur:
                    return
                cfg.overlay_blend_amount = new
                self.inst.osd.show(f"OVL OPC: {new:.2f}")
                self.inst.sampler.refresh_overlay()
        else:
            key = _FX_PARAMS[self._param_idx]
            if key is None:
                return
            cfg = self.inst.cfg
            if key == "blend_amt":
                cur = getattr(cfg, 'shader_blend_amount', 0.5)
                new = max(0.0, min(1.0, cur + delta))
                if new == cur:
                    return
                cfg.shader_blend_amount = new
                self.inst.osd.show(f"BLD AMT: {new:.2f}")
                if getattr(cfg, 'shader_blend', False):
                    self.inst.shader.reapply()
            elif key == "ovl_frames":
                cur = getattr(cfg, 'overlay_offset_frames', 8)
                new = max(1, min(32, cur + (1 if delta > 0 else -1)))
                if new == cur:
                    return
                cfg.overlay_offset_frames = new
                self.inst.osd.show(f"OVL FRM: {new}")
                if getattr(cfg, 'overlay_on', False):
                    self.inst.sampler.refresh_overlay()
            elif key == "trl_decay":
                cur = getattr(cfg, 'trail_decay', 0.93)
                new = round(max(0.80, min(0.99, cur + delta)), 3)
                if new == cur:
                    return
                cfg.trail_decay = new
                self.inst.osd.show(f"TRL DEC: {new:.2f}")
                if getattr(cfg, 'trail_on', False):
                    self.inst.sampler.refresh_trail()
