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
SPEED_STEP = 0.1   # step size for sampler speed (0.1–4.0 range)

# Numpad key → grid position (top-left=0 … bottom-right=8), matching display layout:
#   7 8 9   →   0 1 2
#   4 5 6   →   3 4 5
#   1 2 3   →   6 7 8
_GRID_KEY_TO_POS = {7: 0, 8: 1, 9: 2, 4: 3, 5: 4, 6: 5, 1: 6, 2: 7, 3: 8}

# Param layers (indices). BKSP cycles the ones available in the current mode:
#   0 SHDR    generative shader params p1–p3              (SHADER mode only)
#   1 FX      the active FX shader's own params f1–f4
#   2 COLOUR  palette (p4, SHADER only) + hue / sat / trail opacity / trail decay
#   3 BLEND   compositing — shader↔video blend (SHADER) or overlay (SAMPLER/LIVE)
#   4 TRAIL   temporal echo — on/off, blend type, mode, delay, opacity
#   5 SPEED   sampler playback speed + direction
_PARAM_LAYERS = ("SHDR", "FX", "COLOUR", "BLEND", "TRAIL", "SPEED")
_BLEND_LABELS = {"mode": "MODE", "amt": "BLD AMT", "opc": "OVL OPC", "src": "SRC"}
_COLOUR_LABELS = {"hue": "HUE", "sat": "SAT", "trl_opc": "TRL OPC", "trl_decay": "TRL DEC"}
_TRAIL_LABELS  = {"on": "TRL ON", "type": "TYPE", "mode": "MODE",
                  "delay": "DELAY", "echos": "ECHOS", "opacity": "OPACITY"}
_SPEED_LABELS  = {"speed": "SPEED", "dir": "DIR"}


class KeyboardController:
    def __init__(self, inst):
        self.inst   = inst
        self._stop  = threading.Event()
        self._thread= None
        self.dev    = None

        self._param_layer = 0
        self._param_idx   = 0

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

                    # Key-down only.
                    if key.keystate != key.key_down:
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

    # ------------------------------------------------------------- dispatch
    def _dispatch(self, name):
        # Top-row keys always select display tabs.
        _disp = getattr(self.inst, "display", None)
        if name == "NUM":
            if _disp:
                _disp.set_tab(0)   # SHADER
            return
        if name == "/":
            if _disp:
                _disp.set_tab(1)   # SAMPLER
            return
        if name == "*":
            if _disp:
                _disp.set_tab(2)   # LIVE
            return
        if name == "-":
            if _disp:
                _disp.set_tab(3)   # FX
            return
        if name == ".":
            if _disp:
                _disp.set_tab(4)   # SETTINGS
            return

        if self.inst.menu.active:
            self.inst.menu.handle(name)
            return

        # Grid first screen: 1-9 select slot; ENTER pushes staged; 0 toggles staged.
        if _disp and _disp.is_grid_screen():
            tab = _disp._active_tab
            if name in ("1","2","3","4","5","6","7","8","9"):
                self._grid_select(name, _disp)
                return
            if name == "+":
                if tab == 3:
                    _disp.fx_grid_page(+1)
                elif tab == 0:
                    _disp.shader_grid_page(+1)
                return
            if name == "BKSP":
                if tab == 3:
                    _disp.fx_grid_page(-1)
                elif tab == 0:
                    _disp.shader_grid_page(-1)
                return
            if name == "ENTER":
                if _disp._staged:
                    self._push_staged(_disp)
                return
            if name == "0":
                staged = _disp.toggle_staged()
                if not staged:
                    _disp._grid_pending = [None, None, None, None, None]
                self.inst.osd.show("STAGED" if staged else "LIVE")
                return
            if name == "000":
                self.inst.trail_toggle()
                return
            return

        self._dispatch_perform(name)

    # --------------------------------------------------------- grid selection
    def _grid_select(self, key, _disp):
        """Handle a 1-9 keypress on a grid first-screen.

        Keys map to slot numbers directly (key "7" → slot 7, displayed top-left).

        Behaviour:
          • Pressing the key of the CURRENTLY ACTIVE item → drill into params screen.
          • LIVE mode: load immediately (no staging for presets).
          • STAGED mode: stage the slot (show amber) — ENTER pushes to output.
          • LIVE mode: load immediately.
        """
        slot = int(key)
        inst = self.inst
        tab  = _disp._active_tab

        if tab == 0:   # SHADER_GRID: load generative shader from current page
            from control.display import _GRID_SLOTS as _GS
            try:
                pos = _GS.index(slot)
            except ValueError:
                return
            sh_list = inst.shader.list_shaders(kind="generative")
            idx     = _disp._shader_grid_offset + pos
            if idx >= len(sh_list):
                return
            sname = sh_list[idx]
            if sname == (inst.cfg.current_shader or "") or sname == (_disp._grid_pending[0] or ""):
                _disp.go_to_params_screen()
                inst.osd.show("PARAMS")
                return
            if _disp._staged:
                _disp._grid_pending[0] = sname
                inst.osd.show(f"STAGED: {sname.replace('.glsl','').upper()}")
            else:
                inst.shader.load(sname)
                inst.osd.show(f"SHADER: {sname.replace('.glsl','').upper()}")
            return

        if tab == 3:   # FX_GRID: toggle FX in the chain
            from control.display import _GRID_SLOTS as _GS
            try:
                pos = _GS.index(slot)
            except ValueError:
                return
            fx_list = inst.shader.list_shaders(kind="fx")
            offset  = _disp._fx_grid_offset
            idx     = offset + pos
            if idx < len(fx_list):
                name_fx  = fx_list[idx]
                fx_chain = inst.cfg.fx_chain
                if name_fx in fx_chain:
                    chain_pos = fx_chain.index(name_fx)
                    if inst.cfg.fx_edit_slot == chain_pos:
                        # Second press on the selected chain slot → drill into params
                        self._param_layer = 1
                        self._param_idx   = 0
                        _disp.go_to_params_screen()
                        inst.osd.show("FX PARAMS")
                        return
                    # Different slot already in chain → select it for editing
                    inst.cfg.fx_edit_slot = chain_pos
                    inst.cfg._sync_fx_compat()
                    inst.osd.show(f"FX [{chain_pos+1}]: {name_fx.replace('.glsl','').upper()}")
                else:
                    # Not in chain → add (toggle)
                    inst.shader.fx_chain_toggle(name_fx)
                    chain_str = " > ".join(f.replace(".glsl","").upper()
                                           for f in inst.cfg.fx_chain) if inst.cfg.fx_chain else "—"
                    inst.osd.show(f"FX: {chain_str}")
            return

        if tab == 4:   # SETTINGS_GRID: open menu page
            _SETTINGS_PAGES = ("BROWSER", "SHADERS", "PRESETS", "SETTINGS", "MIDI", "IMPORT")
            from control.display import _GRID_SLOTS as _GS
            try:
                pos = _GS.index(slot)
            except ValueError:
                return
            if pos < len(_SETTINGS_PAGES):
                page_name = _SETTINGS_PAGES[pos]
                from control.menu import PAGES
                menu = inst.menu
                menu.page   = list(PAGES).index(page_name)
                menu.sel    = 0
                menu.active = True
                menu._cancel_edits()
                if page_name == "BROWSER":
                    threading.Thread(target=inst.sampler.rescan_clips, daemon=True).start()
            return

        # SAMPLER: pressing the already-active clip slot drills into speed params.
        active_slot = self._active_slot_for_tab(tab, inst)
        if active_slot == slot and tab == 1:
            self._param_layer = 5   # SPEED layer
            self._param_idx   = 0
            _disp.go_to_params_screen()
            spd = getattr(inst.sampler, "speed", 1.0)
            inst.osd.show(f"SPEED: {spd:.2f}x")
            return

        # LIVE tab: always load immediately (no staging for presets).
        if tab == 2:
            inst.load_preset_slot(slot)
            return

        # STAGED mode: stage without loading.
        if _disp._staged:
            _disp._grid_pending[tab] = slot
            name = self._slot_display_name(tab, slot, inst)
            inst.osd.show(f"STAGED: {name}")
            return

        # LIVE mode: load immediately.
        self._load_slot(tab, slot, inst)

    def _active_slot_for_tab(self, tab, inst):
        """Return the slot number of the currently loaded item, or None."""
        cfg = inst.cfg
        if tab == 0:
            cur = cfg.current_shader
            return next((k for k, v in cfg.shader_slots.items() if v == cur), None)
        if tab == 1:
            cur = cfg.current_clip
            return next((k for k, v in cfg.clip_slots.items() if v == cur), None)
        return None

    def _slot_display_name(self, tab, slot, inst):
        cfg = inst.cfg
        if tab == 0:
            n = cfg.shader_slots.get(slot) or ""
            return n.replace(".glsl", "").upper() or f"SLOT {slot}"
        if tab == 1:
            import os
            p = cfg.clip_slots.get(slot) or ""
            return os.path.splitext(os.path.basename(p))[0].upper() if p else f"SLOT {slot}"
        return f"SLOT {slot}"

    def _load_slot(self, tab, slot, inst):
        """Immediately load the item in the given tab slot.

        For tab 0, slot is a shader filename (from paged grid).
        """
        if tab == 0:
            # slot is the shader filename stored by the paged grid
            inst.shader.load(slot)
            inst.osd.show(f"SHADER: {slot.replace('.glsl','').upper()}")
        elif tab == 1:
            if inst.sampler.slot(slot):
                inst.sampler.trigger()
            else:
                inst.osd.show(f"SLOT {slot}: EMPTY")
        elif tab == 2:
            inst.load_preset_slot(slot)

    def _push_staged(self, _disp):
        """Push all staged (pending) grid selections to the live output."""
        inst = self.inst
        pushed = False
        for tab in range(3):   # SHADER, SAMPLER, LIVE
            slot = _disp._grid_pending[tab]
            if slot is not None:
                self._load_slot(tab, slot, inst)
                _disp._grid_pending[tab] = None
                pushed = True
        if not pushed:
            inst.osd.show("NOTHING STAGED")

    def _dispatch_perform(self, name):
        """Handle keys on the params sub-screen (second screen of each tab).

        ENTER  push staged selections
        +      increase selected param
        BKSP   decrease selected param
        1-9    select param by grid position (7=top-left … 3=bottom-right)
        Tab key (NUM/slash/asterisk/minus/dot)  exit back to grid (handled
               in _dispatch before this method is reached)
        """
        inst  = self.inst
        _disp = getattr(inst, "display", None)

        if name == "ENTER":
            if _disp and _disp._staged:
                self._push_staged(_disp)
            return

        if name == "+":
            self._step_param(+PARAM_STEP)
        elif name == "BKSP":
            self._step_param(-PARAM_STEP)
        elif name in ("1","2","3","4","5","6","7","8","9"):
            n   = int(name)
            # Map numpad key to grid position so keys match the visual layout
            pos = _GRID_KEY_TO_POS.get(n, n - 1)
            self._select_param_by_number(pos + 1)
        elif name == "0":
            if _disp:
                staged = _disp.toggle_staged()
                if not staged:
                    _disp._grid_pending = [None, None, None, None, None]
                inst.osd.show("STAGED" if staged else "LIVE")
        elif name == "000":
            inst.trail_toggle()
        elif name == "REC":
            inst.record_toggle()

    def _select_param_by_number(self, n):
        """Select param n (1-based) within the current layer, show OSD."""
        inst = self.inst
        if self._param_layer == 1:   # FX params
            lbls    = inst.shader.fx_param_labels()
            fx_keys = sorted(lbls.keys(), key=lambda k: int(k[1:]))
            if not fx_keys:
                return
            self._param_idx = min(n - 1, len(fx_keys) - 1)
            key = fx_keys[self._param_idx]
            inst.osd.show(f"FX: {lbls.get(key, key.upper()).upper()}")
        elif self._param_layer == 2:   # COLOUR
            slots = self._colour_slots()
            self._param_idx = min(n - 1, len(slots) - 1)
            inst.osd.show(f"COLOUR: {_COLOUR_LABELS[slots[self._param_idx]]}")
        elif self._param_layer == 3:   # BLEND
            slots = self._blend_slots()
            self._param_idx = min(n - 1, len(slots) - 1)
            inst.osd.show(f"BLEND: {_BLEND_LABELS[slots[self._param_idx]]}")
        elif self._param_layer == 4:   # TRAIL
            slots = self._trail_slots()
            self._param_idx = min(n - 1, len(slots) - 1)
            inst.osd.show(f"TRAIL: {_TRAIL_LABELS[slots[self._param_idx]]}")
        elif self._param_layer == 5:   # SPEED
            slots = self._speed_slots()
            self._param_idx = min(n - 1, len(slots) - 1)
            slot = slots[self._param_idx]
            if slot == "speed":
                spd = getattr(inst.sampler, "speed", 1.0)
                inst.osd.show(f"SPEED: {spd:.2f}x")
            else:
                inst.osd.show("DIR: REVERSE")
        else:                          # SHDR generative params
            keys = self._get_shdr_keys()
            if not keys:
                return
            self._param_idx = min(n - 1, len(keys) - 1)
            key = keys[self._param_idx]
            lbl = inst.shader.param_labels().get(key, key.upper())
            inst.osd.show(f"PARAM: {lbl.upper()}")

    def _avail_layers(self):
        """Param layers valid for the current mode."""
        if self.inst.mode == "SHADER":
            return [0, 1, 2, 3, 4, 5]
        return [1, 2, 3, 4, 5]

    def _get_shdr_keys(self):
        """Sorted param keys for the current generative shader (e.g. p1..p8)."""
        return sorted(self.inst.shader.param_labels().keys(), key=lambda k: int(k[1:]))

    def _colour_slots(self):
        return ("hue", "sat", "trl_opc", "trl_decay")

    def _blend_slots(self):
        """BLEND-layer slots for the current mode: shader↔video blend in SHADER,
        overlay self-blend in SAMPLER/LIVE."""
        if self.inst.mode == "SHADER":
            return ("mode", "amt", "src")
        return ("mode", "opc")

    def _trail_slots(self):
        return ("on", "type", "mode", "delay", "echos", "opacity")

    def _speed_slots(self):
        return ("speed", "dir")

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
        elif self._param_layer == 2:      # ── COLOUR: hue / sat / trl opc / trl dec
            slots = self._colour_slots()
            slot  = slots[self._param_idx % len(slots)]
            if slot == "hue":
                inst.color_adjust_hue(sign * 0.02)
            elif slot == "sat":
                inst.color_adjust_sat(sign * 0.05)
            elif slot == "trl_opc":
                cur = getattr(cfg, 'trail_mode_opacity', 0.5)
                new = round(max(0.0, min(1.0, cur + delta)), 3)
                if new == cur:
                    return
                cfg.trail_mode_opacity = new
                inst.osd.show(f"TRL OPC: {new:.2f}")
                if getattr(cfg, 'trail_on', False):
                    inst.sampler.refresh_trail()
            elif slot == "trl_decay":
                cur = round(getattr(cfg, 'trail_decay', 0.93), 3)
                new = round(max(0.80, min(0.99, cur + delta)), 3)
                if new == cur:
                    return
                cfg.trail_decay = new
                inst.osd.show(f"TRL DEC: {new:.2f}")
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
        elif self._param_layer == 4:      # ── TRAIL: temporal echo controls
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
                new = max(1, min(5, cur + d))
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
        else:                             # ── SPEED: sampler playback speed / direction
            slots = self._speed_slots()
            slot  = slots[self._param_idx % len(slots)]
            if slot == "speed":
                step = SPEED_STEP * (1 if delta > 0 else -1)
                inst.sampler.nudge_speed(step)
                spd = inst.sampler.speed
                inst.osd.show(f"SPEED: {spd:.2f}x")
            elif slot == "dir":
                inst.sampler.reverse()
                inst.osd.show("REVERSE")
