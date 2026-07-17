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

Top row NUM / / / * / - always selects a display tab: SHADER, FX, SAMPLER,
LIVE — in every context, even over an open menu page. Pressing the key of the
tab you're already on cycles that tab's sub-screens (a 3x3 slot GRID, then a
PARAMS screen for tabs that have one).

The . key is the odd one out — one press, meaning set by how deep you are
(see _dot_press). At the top level (a grid screen, no menu) it's the
SETTINGS tab key, exactly like the other four are for their tab. Anywhere
deeper it goes UP one level instead: cancels an in-progress menu sub-action,
else closes an open menu page, else exits param edit mode, else drops a
params screen back to its grid. It is checked before the menu.active branch
in _dispatch, so it works over an open menu page too.

Grid screens (keys 7 8 9 / 4 5 6 / 1 2 3, matching their on-screen position):
  SHADER grid — tap: load/unload the one active generative shader (tap the
                loaded one again to unload). Hold: open its params screen,
                loading it first if it wasn't already active.
  FX grid     — tap: toggle the FX in/out of the chain (up to 4 at once) —
                never changes screens, whether adding or removing. Hold:
                open that FX's params screen (adding it to the chain first
                if it wasn't already there, without removing anything else).
  SAMPLER/LIVE/SETTINGS grids — unchanged: tap triggers a clip/preset or
                opens a menu page; pressing the already-playing clip's key
                opens a SPEED params screen instead.
  0           — toggle STAGED (amber, picks wait for Enter) vs LIVE (green,
                picks apply immediately)
  Enter       — push staged picks if any are waiting; otherwise put the active
                tab's mode on the screen (SHADER/SAMPLER/LIVE tab + Enter
                switches the instrument to that mode — LIVE starts the camera).
                FX and SETTINGS aren't modes, so Enter there just says so.
  +/Bksp      — page the SHADER/FX grid (9 items per page)

Params screens (reached via hold, or by pressing an already-open tab's key
again):
  +/Bksp      — scroll the highlighted parameter up/down the list (not in
                edit mode)
  1-9         — jump to a parameter by its on-screen grid position
  Enter       — toggle edit mode on the highlighted parameter
  +/Bksp (in edit mode) — step that parameter's value down/up
  Enter (in edit mode)  — exit edit mode, back to scrolling the list
  On the FX params screen, the layer's own f-params are followed by two
  extra rows: BLEND (its blend mode against whatever is below it in the
  stack) and BLD AMT (that blend's strength).

MENU mode: reached via the SETTINGS tab; while a menu page is open every key
routes to `self.inst.menu.handle()` and none reach the perform handlers
(see control/menu.py for the bindings) — except the tab keys and '.', which
keyboard.py intercepts everywhere, menu included (see above).

Note: the '000' key is not usable on this numpad. It has no keycode of its own
— it emits three rapid KEY_KP0 presses, arriving as three plain '0's. Nothing
binds it, so it just toggles STAGED an odd number of times.
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
    # There is no KEY_KP00/KEY_KP000 in Linux input-event-codes.h; entries for
    # them sat here for a long time and could never match. See the module
    # docstring for what the 000 key actually sends.
    "KEY_KP0":         "0",
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

# This numpad's 000 key has no keycode of its own: it fires three rapid KEY_KP0
# presses (measured 16ms down-to-down), so it arrives as three plain "0"s and is
# not usable as a distinct key without coalescing them. Nothing binds it.
PARAM_STEP = 0.05
SPEED_STEP = 0.1   # step size for sampler speed (0.1–4.0 range)

# How long a grid key must be held before it's treated as a hold rather than
# a tap (SHADER/FX grids only — see _HOLD_TABS).
HOLD_THRESHOLD = 0.4   # seconds
_HOLD_TABS = (0, 1)    # SHADER, FX — the only grids with tap/hold semantics

# Display tab → instrument mode, for the 000 key. FX (1) and SETTINGS (4) are
# absent: neither is a mode, they act on whatever mode is already running.
_TAB_MODES = {0: "SHADER", 2: "SAMPLER", 3: "LIVE"}

# Numpad key → grid position (top-left=0 … bottom-right=8), matching display layout:
#   7 8 9   →   0 1 2
#   4 5 6   →   3 4 5
#   1 2 3   →   6 7 8
_GRID_KEY_TO_POS = {7: 0, 8: 1, 9: 2, 4: 3, 5: 4, 6: 5, 1: 6, 2: 7, 3: 8}

# Param "layers" (indices) a params screen can show. Reached directly by tab/
# grid navigation now (see module docstring) rather than by cycling a key:
#   0 SHDR    generative shader params p1–p9               (SHADER tab)
#   1 FX      the edited FX chain slot's own f-params + its blend mode/amount (FX tab)
#   2 COLOUR  palette (p4, SHADER only) + hue / sat / trail opacity / trail decay
#             — currently unreachable, no key/grid selects this layer (see MANUAL.md)
#   3 BLEND   compositing — shader↔video blend (SHADER) or overlay (SAMPLER/LIVE)
#             — currently unreachable, no key/grid selects this layer (see MANUAL.md)
#   4 TRAIL   temporal echo — on/off, blend type, mode, delay, opacity
#             — currently unreachable, no key/grid selects this layer (see MANUAL.md)
#   5 SPEED   sampler playback speed + direction (SAMPLER tab, drill into the playing clip)
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

        self._param_layer   = 0
        self._param_idx     = 0
        self._editing_param = False   # True while Enter has "entered" the highlighted param

        # Hold-vs-tap detection for the SHADER/FX grids (see _on_key_down).
        self._hold_timers = {}   # key name -> pending threading.Timer

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

                    if key.keystate == key.key_down:
                        self._on_key_down(name)
                    elif key.keystate == key.key_up:
                        self._on_key_up(name)
                    # key.key_hold (OS auto-repeat while held) is ignored —
                    # hold detection uses our own timer, not repeat events.

            except OSError as e:
                log.warning("numpad disconnected (%s) — will reconnect", e)
                try:
                    self.dev.close()
                except Exception:
                    pass
                self.dev = None
                time.sleep(1)   # brief pause before scanning for reconnect

    # --------------------------------------------------------- hold vs tap
    def _on_key_down(self, name):
        """Grid-cell keys (1-9) on the SHADER/FX grids get a hold timer so a
        long-press can be distinguished from a tap (see module docstring).
        Every other key/context dispatches immediately, unchanged."""
        inst  = self.inst
        _disp = getattr(inst, "display", None)
        uses_hold = (name in ("1", "2", "3", "4", "5", "6", "7", "8", "9")
                     and not inst.menu.active
                     and _disp is not None
                     and _disp.is_grid_screen()
                     and _disp._active_tab in _HOLD_TABS)
        if uses_hold:
            timer = threading.Timer(HOLD_THRESHOLD, self._fire_hold, args=(name,))
            timer.daemon = True
            self._hold_timers[name] = timer
            timer.start()
        else:
            self._dispatch(name)

    def _on_key_up(self, name):
        """If a hold timer is still pending for this key, the key was
        released before the threshold — treat it as a tap. If no timer is
        pending, either this key never used hold tracking (already
        dispatched on key-down) or the hold already fired — nothing to do."""
        timer = self._hold_timers.pop(name, None)
        if timer is not None:
            timer.cancel()
            self._dispatch(name)

    def _fire_hold(self, name):
        self._hold_timers.pop(name, None)
        _disp = getattr(self.inst, "display", None)
        if _disp is not None:
            self._grid_hold(name, _disp)

    def _dot_press(self):
        """'.' — a single press whose meaning depends on how deep you are.
        Step back one level if there is one, otherwise open SETTINGS:
          menu sub-action (assign/edit/confirm/USB browse) -> cancel it
          menu page open                                   -> close the menu
          params screen in edit mode                       -> exit edit mode
          params screen                                     -> back to the grid
          grid screen (top level)                           -> open/cycle SETTINGS
        """
        inst  = self.inst
        menu  = inst.menu
        _disp = getattr(inst, "display", None)

        if menu.active:
            if menu._midi_editing or menu._assigning or menu._confirm_delete:
                menu._cancel_edits()
                return
            from control.menu import PAGES
            if menu.page == PAGES.index("IMPORT") and menu._usb_dev:
                menu._usb_eject()
                return
            menu.toggle()   # closes the menu, applying any staged pick
            if _disp:
                _disp.go_to_grid_screen()
            return

        if self._editing_param:
            self._editing_param = False
            return

        if _disp and not _disp.is_grid_screen():
            _disp.go_to_grid_screen()
            return

        # Already at the top level — nothing to back out of, so this is the
        # SETTINGS tab key (and cycles its sub-screens when already there).
        self._editing_param = False
        if _disp:
            _disp.set_tab(4)

    def _force_tab_mode(self):
        """ENTER on a grid screen: put the active tab's mode on the screen.

        The tab keys only change what the SPI display shows; this is what
        commits that choice to the instrument, so LIVE tab + ENTER starts the
        camera. set_mode() ignores live_mode_enabled — that flag only filters
        the GPIO/MIDI cycle, and this press is explicit.
        """
        _disp = getattr(self.inst, "display", None)
        if _disp is None:
            return
        mode = _TAB_MODES.get(_disp._active_tab)
        if mode is None:
            self.inst.osd.show("NO MODE FOR THIS TAB")
            return
        if mode == self.inst.mode:
            self.inst.osd.show(f"MODE: {mode}")
            return
        self.inst.set_mode(mode)   # shows its own OSD

    # ------------------------------------------------------------- dispatch
    def _dispatch(self, name):
        # Top-row keys always select display tabs. '.' is context-dependent
        # (back out a level, else open SETTINGS — see _dot_press) and is
        # checked here, ahead of the menu.active branch below, so it stays
        # reachable while a menu page is open.
        _disp = getattr(self.inst, "display", None)
        if name == ".":
            self._dot_press()
            return
        if name in ("NUM", "/", "*", "-"):
            # Any tab-bar keypress leaves behind a fresh (non-editing) params
            # screen so you never land back on a stale edit-mode from a
            # different context.
            self._editing_param = False
        if name == "NUM":
            if _disp:
                _disp.set_tab(0)   # SHADER
            return
        if name == "/":
            if _disp:
                _disp.set_tab(1)   # FX
            return
        if name == "*":
            if _disp:
                _disp.set_tab(2)   # SAMPLER
            return
        if name == "-":
            if _disp:
                _disp.set_tab(3)   # LIVE
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
                if tab == 1:
                    _disp.fx_grid_page(+1)
                elif tab == 0:
                    _disp.shader_grid_page(+1)
                return
            if name == "BKSP":
                if tab == 1:
                    _disp.fx_grid_page(-1)
                elif tab == 0:
                    _disp.shader_grid_page(-1)
                return
            if name == "ENTER":
                # Staged picks waiting: push them (that's what staging is for).
                # Otherwise ENTER commits the active tab to the instrument mode,
                # which is the only numpad route into SHADER/SAMPLER/LIVE.
                if _disp._staged and any(_disp._grid_pending[t] is not None
                                         for t in (0, 2, 3)):
                    self._push_staged(_disp)
                else:
                    self._force_tab_mode()
                return
            if name == "0":
                staged = _disp.toggle_staged()
                if not staged:
                    _disp._grid_pending = [None, None, None, None, None]
                self.inst.osd.show("STAGED" if staged else "LIVE")
                return
            return

        self._dispatch_perform(name)

    # --------------------------------------------------------- grid selection
    def _grid_select(self, key, _disp):
        """Handle a tap (1-9) on a grid first-screen.

        Keys map to slot numbers directly (key "7" → slot 7, displayed top-left).
        SHADER/FX grids use tap-to-toggle semantics (see module docstring);
        SAMPLER/LIVE/SETTINGS grids keep their original load/trigger/open
        behaviour, unaffected by hold detection.
        """
        slot = int(key)
        inst = self.inst
        tab  = _disp._active_tab

        if tab in (0, 1):   # SHADER_GRID / FX_GRID: tap ONLY toggles stack
                            # membership — never changes screens. Opening
                            # params is hold's job (see _grid_hold), even for
                            # a newly-added item. Both grids are now real
                            # stacks (up to 4) with identical semantics.
            kind, offset_attr, toggle_fn, chain_attr, tag = (
                ("generative", "_shader_grid_offset", inst.shader.shader_chain_toggle,
                 "shader_chain", "SHADER")
                if tab == 0 else
                ("fx", "_fx_grid_offset", inst.shader.fx_chain_toggle,
                 "fx_chain", "FX")
            )
            name = self._resolve_grid_item(slot, _disp, kind, offset_attr)
            if name is None:
                return
            toggle_fn(name)
            chain = getattr(inst.cfg, chain_attr)
            chain_str = " > ".join(n.replace(".glsl", "").upper() for n in chain) if chain else "—"
            inst.osd.show(f"{tag}: {chain_str}")
            return

        if tab == 4:   # SETTINGS_GRID: open menu page
            _SETTINGS_PAGES = ("BROWSER", "SHADERS", "PRESETS", "SETTINGS", "MIDI", "IMPORT")
            from control.display import _GRID_SLOTS as _GS
            try:
                pos = _GS.index(slot)
            except ValueError:
                return
            if pos < len(_SETTINGS_PAGES):
                # open_page runs the page's entry hooks (clip rescan, USB drive
                # scan); don't set menu.page directly here.
                inst.menu.open_page(_SETTINGS_PAGES[pos])
            return

        # SAMPLER: pressing the already-active clip slot drills into speed params.
        active_slot = self._active_slot_for_tab(tab, inst)
        if active_slot == slot and tab == 2:
            self._param_layer   = 5   # SPEED layer
            self._param_idx     = 0
            self._editing_param = False
            _disp.go_to_params_screen()
            spd = getattr(inst.sampler, "speed", 1.0)
            inst.osd.show(f"SPEED: {spd:.2f}x")
            return

        # LIVE tab: always load immediately (no staging for presets).
        if tab == 3:
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

    def _resolve_grid_item(self, slot, _disp, kind, offset_attr):
        """Map a numpad key on a paginated grid (SHADER or FX) to the shader
        filename at that grid position, or None if the slot is empty/out of
        range. Shared by the tap and hold handlers for both grids."""
        from control.display import _GRID_SLOTS as _GS
        try:
            pos = _GS.index(slot)
        except ValueError:
            return None
        lst    = self.inst.shader.list_shaders(kind=kind)
        offset = getattr(_disp, offset_attr)
        idx    = offset + pos
        return lst[idx] if idx < len(lst) else None

    def _grid_hold(self, key, _disp):
        """Handle a hold (long-press) on a SHADER/FX grid cell: jump straight
        to that item's params screen. If it's already in the stack, this
        only SELECTS it for editing (no membership change); if not, it's
        added first (auto-activate — see module docstring). Bypasses STAGED
        mode — holding to configure something is a workshop action, not a
        performance change."""
        slot = int(key)
        inst = self.inst
        tab  = _disp._active_tab

        if tab not in (0, 1):
            return
        if tab == 0:
            kind, offset_attr, toggle_fn = "generative", "_shader_grid_offset", inst.shader.shader_chain_toggle
            chain_attr, edit_slot_attr   = "shader_chain", "shader_edit_slot"
            sync_fn, param_layer         = inst.cfg._sync_shader_compat, 0
        else:
            kind, offset_attr, toggle_fn = "fx", "_fx_grid_offset", inst.shader.fx_chain_toggle
            chain_attr, edit_slot_attr   = "fx_chain", "fx_edit_slot"
            sync_fn, param_layer         = inst.cfg._sync_fx_compat, 1

        name = self._resolve_grid_item(slot, _disp, kind, offset_attr)
        if name is None:
            return
        chain = getattr(inst.cfg, chain_attr)
        if name in chain:
            setattr(inst.cfg, edit_slot_attr, chain.index(name))
            sync_fn()
        else:
            toggle_fn(name)   # adds it, selects it
        self._param_layer   = param_layer
        self._param_idx     = 0
        self._editing_param = False
        _disp.go_to_params_screen()
        inst.osd.show(f"PARAMS: {name.replace('.glsl','').upper()}")

    def _active_slot_for_tab(self, tab, inst):
        """Return the slot number of the currently loaded item, or None."""
        cfg = inst.cfg
        if tab == 0:
            cur = cfg.current_shader
            return next((k for k, v in cfg.shader_slots.items() if v == cur), None)
        if tab == 2:
            cur = cfg.current_clip
            return next((k for k, v in cfg.clip_slots.items() if v == cur), None)
        return None

    def _slot_display_name(self, tab, slot, inst):
        cfg = inst.cfg
        if tab == 0:
            n = cfg.shader_slots.get(slot) or ""
            return n.replace(".glsl", "").upper() or f"SLOT {slot}"
        if tab == 2:
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
        elif tab == 2:
            if inst.sampler.slot(slot):
                inst.sampler.trigger()
            else:
                inst.osd.show(f"SLOT {slot}: EMPTY")
        elif tab == 3:
            inst.load_preset_slot(slot)

    def _push_staged(self, _disp):
        """Push all staged (pending) grid selections to the live output."""
        inst = self.inst
        pushed = False
        for tab in (0, 2, 3):   # SHADER, SAMPLER, LIVE
            slot = _disp._grid_pending[tab]
            if slot is not None:
                self._load_slot(tab, slot, inst)
                _disp._grid_pending[tab] = None
                pushed = True
        if not pushed:
            inst.osd.show("NOTHING STAGED")

    def _dispatch_perform(self, name):
        """Handle keys on the params sub-screen (second screen of each tab).

        ENTER toggles edit mode on the highlighted parameter. Outside edit
        mode, +/Bksp scroll the list and 1-9 jump to a row by grid position;
        inside edit mode, +/Bksp step that parameter's value instead.
        Tab key (NUM/slash/asterisk/minus/dot)  exit back to grid (handled
               in _dispatch before this method is reached)
        """
        inst  = self.inst
        _disp = getattr(inst, "display", None)

        if name == "ENTER":
            self._editing_param = not self._editing_param
            return

        if self._editing_param:
            if name == "+":
                self._step_param(+PARAM_STEP)
            elif name == "BKSP":
                self._step_param(-PARAM_STEP)
            return

        if name == "+":
            self._scroll_param(+1)
        elif name == "BKSP":
            self._scroll_param(-1)
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
        elif name == "REC":
            inst.record_toggle()

    def _current_row_keys(self):
        """Ordered row keys for whatever the active params layer shows —
        single source of truth shared with control/display.py rendering."""
        if self._param_layer == 1:
            return self.inst.shader.fx_row_keys()
        if self._param_layer == 2:
            return list(self._colour_slots())
        if self._param_layer == 3:
            return list(self._blend_slots())
        if self._param_layer == 4:
            return list(self._trail_slots())
        if self._param_layer == 5:
            return list(self._speed_slots())
        return self.inst.shader.shader_row_keys()

    def _scroll_param(self, direction):
        """+/Bksp outside edit mode: move the selection up/down the list."""
        keys = self._current_row_keys()
        if not keys:
            return
        self._param_idx = max(0, min(len(keys) - 1, self._param_idx + direction))

    def _select_param_by_number(self, n):
        """Select param n (1-based) within the current layer, show OSD."""
        inst = self.inst
        if self._param_layer == 1:   # FX params (+ this layer's blend mode/amount)
            row_keys = inst.shader.fx_row_keys()
            if not row_keys:
                return
            self._param_idx = min(n - 1, len(row_keys) - 1)
            key = row_keys[self._param_idx]
            lbls = inst.shader.fx_param_labels()
            label = ("BLEND" if key == "__blend_mode__" else
                     "BLD AMT" if key == "__blend_amt__" else
                     lbls.get(key, key.upper()).upper())
            inst.osd.show(f"FX: {label}")
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
        else:                          # SHDR: generative params (+ blend mode/amount if slot > 0)
            row_keys = inst.shader.shader_row_keys()
            if not row_keys:
                return
            self._param_idx = min(n - 1, len(row_keys) - 1)
            key = row_keys[self._param_idx]
            label = ("BLEND" if key == "__blend_mode__" else
                     "BLD AMT" if key == "__blend_amt__" else
                     inst.shader.param_labels().get(key, key.upper()).upper())
            inst.osd.show(f"PARAM: {label}")

    def _avail_layers(self):
        """Param layers valid for the current mode."""
        if self.inst.mode == "SHADER":
            return [0, 1, 2, 3, 4, 5]
        return [1, 2, 3, 4, 5]

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
        if self._param_layer == 0:        # ── SHDR: generative params + this slot's blend mode/amount
            row_keys = inst.shader.shader_row_keys()
            if not row_keys:
                return
            key = row_keys[self._param_idx % len(row_keys)]
            if key == "__blend_mode__":
                inst.shader.cycle_shader_layer_blend_mode(1 if delta > 0 else -1)
                inst.osd.show(f"BLEND: {cfg.shader_layer_blend.get('mode','normal').upper()}")
                return
            if key == "__blend_amt__":
                cur = cfg.shader_layer_blend.get("amt", 1.0)
                new = clamp01(cur + delta)
                if new == cur:
                    return
                inst.shader.set_shader_layer_blend_amount(new)
                inst.osd.show(f"BLD AMT: {new:.2f}")
                return
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
        elif self._param_layer == 1:      # ── FX: own params + this layer's blend mode/amount
            row_keys = inst.shader.fx_row_keys()
            if not row_keys:
                return
            key = row_keys[self._param_idx % len(row_keys)]
            if key == "__blend_mode__":
                inst.shader.cycle_fx_blend_mode(1 if delta > 0 else -1)
                inst.osd.show(f"BLEND: {cfg.fx_blend.get('mode','normal').upper()}")
                return
            if key == "__blend_amt__":
                cur = cfg.fx_blend.get("amt", 1.0)
                new = clamp01(cur + delta)
                if new == cur:
                    return
                inst.shader.set_fx_blend_amount(new)
                inst.osd.show(f"BLD AMT: {new:.2f}")
                return
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
