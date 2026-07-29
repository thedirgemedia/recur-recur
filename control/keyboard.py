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

Panic gesture: hold any three keys at once for two seconds on one device and
the calibration offer appears, whatever the keymap says (see _track_panic).
This is the way back from a map that binds the navigation keys to nothing —
it reads raw keycodes, so it cannot itself be unbound.

The . key is BACK, and only that: it steps out one level per press — cancels
an in-progress menu sub-action, else closes an open menu page, else exits
param edit mode, else drops a params screen back to its grid — until you are
at the top level, where it does nothing. It is checked before the menu.active
branch in _dispatch, so it works over an open menu page too.

SETTINGS is a destination rather than a level, so it has its own key (bind
SETTINGS TAB in the calibration walk) and toggles: press to show it, press
again to return to the tab you were on. Note this means a device with no
SETTINGS key bound cannot reach the SETTINGS tab at all — and therefore not
BROWSER/MIDI/IMPORT/INPUT either, since those are its sub-screens and grid
cells. The panic gesture (three keys held) is the way back in if that happens.

Grid screens (keys 7 8 9 / 4 5 6 / 1 2 3, matching their on-screen position):
  SHADER grid — tap: load/unload the one active generative shader (tap the
                loaded one again to unload). Hold: open its params screen,
                loading it first if it wasn't already active.
  FX grid     — tap: toggle the FX in/out of the chain (up to 4 at once) —
                never changes screens, whether adding or removing. Hold:
                open that FX's params screen (adding it to the chain first
                if it wasn't already there, without removing anything else).
  SAMPLER grid — tap: load/trigger the clip in that slot. Hold: load it and
                open its per-clip CLIP settings screen (rotate / zoom / speed /
                dir / trail); tapping the already-playing clip opens the same
                screen. Each clip remembers its own settings.
  LIVE/SETTINGS grids — unchanged: tap triggers a preset or opens a menu page.
  0           — toggle STAGED (amber, picks wait for Enter) vs LIVE (green,
                picks apply immediately)
  Enter       — push staged picks if any are waiting; otherwise put the active
                tab's mode on the screen (SHADER/SAMPLER/LIVE tab + Enter
                switches the instrument to that mode — LIVE starts the camera).
                FX and SETTINGS aren't modes, so Enter there just says so.
  +/Bksp      — page the SHADER/FX grid (9 items per page)

Params screens (reached via hold, or by pressing an already-open tab's key
again):
  +           — move the selection UP the list (not in edit mode)
  Bksp        — move the selection DOWN the list
                (matches the menu list, and MANUAL.md. The view keeps the
                selection centred, so the rows slide the opposite way to the
                cursor — read the cursor when checking the direction.)
  7/8/9/4/5/6 — jump to parameter 1-6 by its on-screen grid position
  1/2/3       — assign LFO 1/2/3 to the highlighted parameter; the same key
                again clears it. So "7 2" = LFO 2 on parameter 1. These keys
                used to jump to params 7-9, which clamped to the last row on
                any shader with fewer (nearly all) — params 7+ scroll into
                reach with +/Bksp.
  Enter       — toggle edit mode on the highlighted parameter
  +/Bksp (in edit mode) — step that parameter's value up/down
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
import selectors
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

# NUMPAD_MAP resolved to integer evdev keycodes — the built-in DEFAULT used when
# cfg.keymap has no entry for a code, so a plain USB numpad works with zero
# config. cfg.keymap (set on the INPUT page) overrides this per-code.
DEFAULT_MAP = {}
if HAVE_EVDEV:
    for _codename, _logical in NUMPAD_MAP.items():
        _c = getattr(ecodes, _codename, None)
        if isinstance(_c, int):
            DEFAULT_MAP[_c] = _logical


def _dev_vidpid(dev):
    """'vvvv:pppp' (lowercase hex) for an evdev device — the stable identity we
    match a chosen primary against (a pad exposes several nodes under one)."""
    try:
        i = dev.info
        return f"{i.vendor:04x}:{i.product:04x}"
    except Exception:
        return "0000:0000"


def _is_keyboard(dev):
    """True for a USB key-sending node — the pad and any attached keyboards.

    Filters to BUS_USB so the non-USB CEC/power-button nodes never qualify, and
    requires an ENTER key so mice (buttons only, no ENTER) are excluded. A pad's
    two keyboard nodes both pass; we read all of them and dedupe by vid:pid."""
    try:
        if dev.info.bustype != ecodes.BUS_USB:
            return False
        keys = dev.capabilities().get(ecodes.EV_KEY, [])
    except Exception:
        return False
    return bool(keys) and (ecodes.KEY_ENTER in keys or ecodes.KEY_KPENTER in keys)


# USB ids of recognized macropads. A pad advertises a full HID keymap (so its
# real key-count can't distinguish it from a full keyboard), so we recognize it
# by identity: it becomes the auto-primary and is offered the boot calibration
# wizard. Extend as pads are added; the "sayo" name match catches re-badged
# SayoDevice units sold unbranded.
KNOWN_PADS = {"8089:0008"}


def _is_pad(dev):
    """True for a recognized macropad (by USB id or a SayoDevice name)."""
    return _dev_vidpid(dev) in KNOWN_PADS or "sayo" in (dev.name or "").lower()


PARAM_STEP = 0.05
SPEED_STEP = 0.1   # step size for sampler speed (0.1–4.0 range)

# How long a grid key must be held before it's treated as a hold rather than
# a tap (SHADER/FX grids only — see _HOLD_TABS).
HOLD_THRESHOLD = 0.4   # seconds

# Panic gesture: hold PANIC_KEYS keys at once for PANIC_HOLD seconds on any one
# device to force the calibration offer. Tracked on RAW keycodes ahead of
# _resolve(), so it is the one control that survives a keymap broken badly
# enough that nothing on screen can be navigated (see Menu._unnavigable). Three
# simultaneous keys can't happen while playing — keys are pressed one at a time
# — so it needs no dedicated key, and works on a device with no map at all.
PANIC_KEYS = 3
PANIC_HOLD = 2.0       # seconds

_HOLD_TABS = (0, 1, 2, 3)   # every tab whose grids have tap/hold semantics
                            # (LIVE joined when its grid became a preset store)
                         # (SAMPLER hold opens a clip's per-clip settings)

# Display tab → instrument mode, for the 000 key. FX (1) and SETTINGS (4) are
# absent: neither is a mode, they act on whatever mode is already running.
_TAB_MODES = {0: "SHADER", 2: "SAMPLER", 3: "LIVE"}
_SETTINGS_TAB = 4

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
#   5 CLIP    per-clip settings — rotate / zoom / speed / dir / trail
#             (SAMPLER tab; long-press a clip, or tap the already-playing one)
_PARAM_LAYERS = ("SHDR", "FX", "COLOUR", "BLEND", "TRAIL", "CLIP")
_BLEND_LABELS = {"mode": "MODE", "amt": "BLD AMT", "opc": "OVL OPC", "src": "SRC"}
_COLOUR_LABELS = {"hue": "HUE", "sat": "SAT", "trl_opc": "TRL OPC", "trl_decay": "TRL DEC"}
_TRAIL_LABELS  = {"on": "TRL ON", "type": "TYPE", "mode": "MODE",
                  "delay": "DELAY", "echos": "ECHOS", "opacity": "OPACITY"}
_CLIPSET_LABELS = {"rotate": "ROTATE", "zoom": "ZOOM", "speed": "SPEED",
                   "dir": "DIR", "bright": "BRIGHT", "contrast": "CONTRAST",
                   "trail_on": "TRAIL", "trail": "TRL STEP",
                   "trail_time": "TRL TIME", "trail_mode": "TRL MODE",
                   "trail_opc": "TRL BLEND"}
_LFO_LABELS = {"shape": "SHAPE", "min": "MIN", "max": "MAX",
               "speed": "SPEED", "sync": "SYNC"}
# Rows of the LFO settings screen (long-press an LFO cell to reach it).
_LFO_SLOTS = ("shape", "min", "max", "speed", "sync")


class KeyboardController:
    def __init__(self, inst):
        self.inst   = inst
        self._stop  = threading.Event()
        self._thread= None
        self._primary_logged  = None   # last vid:pid we logged as primary
        self._primary_present = False  # is the chosen primary attached right now?
                                       # (False ⇒ the source filter is dropped)

        self._param_layer   = 0
        self._param_idx     = 0
        self._editing_param = False   # True while Enter has "entered" the highlighted param

        # LFO settings screen: reached by long-pressing an LFO cell (key 1/2/3)
        # on any params screen. It edits ONE LFO (min/max/speed/shape/sync) and
        # is a self-contained sub-screen — flagged here rather than as a
        # _param_layer so it never leaks into a tab's normal params view.
        # _clear_param_edit() drops the flag on every navigation.
        self._lfo_screen    = False
        self._lfo_edit_idx  = 0       # which LFO (0-2) the screen edits

        # Preset options screen — opened by holding a FILLED cell on any of the
        # three preset grids. (store, index) while open, None otherwise. Like
        # _lfo_screen it borrows the params sub-screen slot rather than being a
        # _param_layer, so it never leaks into a tab's normal params view.
        self._preset_opts    = None
        self._preset_opt_idx = 0

        # MIDI-assign sub-mode on the params screen (key 4). While active, the
        # highlighted param is waiting for a CC: move a knob to learn one, or
        # type a CC number. Any other key cancels.
        self._midi_assign     = False
        self._midi_cc_buf     = ""     # digits typed for a manual CC number
        self._midi_assign_key = None   # param key being bound (frozen at begin)

        # Hold-vs-tap detection for the SHADER/FX grids (see _on_key_down).
        self._hold_timers = {}   # key name -> pending threading.Timer

        # Tab the dedicated SETTINGS key was pressed from, so pressing it again
        # goes back there rather than stranding you on SETTINGS.
        self._settings_origin = None

        # Panic gesture (see PANIC_KEYS). Raw keycodes currently held, per
        # device node, so the count is per-keyboard rather than across all of
        # them — holding one key on each of three keyboards isn't the gesture.
        self._down         = {}     # device path -> set of raw keycodes down
        self._panic_timer  = None
        self._panic_fired  = False  # latched until the keys are released, so a
                                    # 4th key can't re-trigger the same hold

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

    def list_keyboards(self):
        """One entry per physical keyboard (deduped by vid:pid) for the INPUT
        page's device picker: [{'vidpid': 'vvvv:pppp', 'name': str}, …].
        Opens each node briefly and closes it — safe to call off-thread."""
        if not HAVE_EVDEV:
            return []
        seen = {}
        for path in list_devices():
            try:
                d = InputDevice(path)
            except Exception:
                continue
            try:
                if _is_keyboard(d):
                    seen.setdefault(_dev_vidpid(d), d.name)
            finally:
                try:
                    d.close()
                except Exception:
                    pass
        return [{"vidpid": vp, "name": nm} for vp, nm in seen.items()]

    def detect_pad(self):
        """Return {'vidpid','name'} for an attached recognized macropad, else
        None. Used by the boot calibration offer (control/menu) — a pad is
        recognized by USB identity because it advertises a full HID keymap and
        so looks like any other keyboard to a capability scan."""
        if not HAVE_EVDEV:
            return None
        for path in list_devices():
            try:
                d = InputDevice(path)
            except Exception:
                continue
            try:
                if _is_keyboard(d) and _is_pad(d):
                    return {"vidpid": _dev_vidpid(d), "name": d.name or "pad"}
            finally:
                try:
                    d.close()
                except Exception:
                    pass
        return None

    # ------------------------------------------------------ keymap / dispatch
    def _resolve(self, code):
        """Integer evdev keycode → logical key name. cfg.keymap (set on the
        INPUT page) wins; otherwise the built-in numpad DEFAULT_MAP. Returns
        None for a code nothing binds."""
        km = self.inst.cfg.keymap
        if km:
            n = km.get(str(code))
            if n is not None:
                return n
        return DEFAULT_MAP.get(code)

    # ------------------------------------------------------------ panic gesture
    def _track_panic(self, dev, ev):
        """Watch raw key-downs for the PANIC_KEYS-keys-held gesture.

        Deliberately upstream of _resolve() and of the primary source filter:
        the situation this exists for is a keymap that resolves the keys you
        need to nothing, and a device that may not be primary. Observing only —
        never consumes the event.
        """
        down = self._down.setdefault(dev.path, set())
        if ev.value == 1:
            down.add(ev.code)
            if (len(down) >= PANIC_KEYS and self._panic_timer is None
                    and not self._panic_fired):
                t = threading.Timer(PANIC_HOLD, self._fire_panic,
                                    args=(_dev_vidpid(dev), dev.name))
                t.daemon = True
                self._panic_timer = t
                t.start()
        else:
            down.discard(ev.code)
            if len(down) < PANIC_KEYS:
                self._cancel_panic()
                self._panic_fired = False

    def _cancel_panic(self):
        t, self._panic_timer = self._panic_timer, None
        if t is not None:
            t.cancel()

    def _fire_panic(self, vidpid, name):
        """The hold completed: force the calibration offer for that device.

        offer=True rather than starting the walk outright — an offer is
        answered by any key and times out on its own, so a gesture triggered by
        accident costs a glance at the screen rather than an 18-step walk.
        """
        self._panic_timer = None
        self._panic_fired = True
        # Already in the wizard: there is nothing to recover from, and firing
        # here would reset a walk in progress back to OFFER while keeping its
        # partial bindings — which is how a stale duplicate outlives a re-walk.
        if getattr(self.inst.menu, "_input_view", "MENU") == "WIZARD":
            log.info("panic gesture ignored — wizard already open")
            return
        log.warning("panic gesture: %d keys held on %s %s — offering calibration",
                    PANIC_KEYS, vidpid, name)
        try:
            self.inst.osd.show("PANIC — CALIBRATE PAD?")
            self.inst.menu.start_calibration({"vidpid": vidpid,
                                              "name": name or "pad"},
                                             offer=True,
                                             reason="PANIC KEY HOLD")
        except Exception as e:
            log.warning("panic calibration: %s", e)

    def _handle_event(self, dev, ev):
        """Process one EV_KEY event from any keyboard node."""
        if ev.value == 2:        # OS auto-repeat while held — hold uses our timer
            return
        menu = self.inst.menu

        # Panic gesture first, on the raw code: it must fire from any state,
        # including one where LEARN is swallowing key-downs or the map resolves
        # to nothing. It only observes — the event still falls through to
        # whatever would normally handle it.
        self._track_panic(dev, ev)

        # INPUT-page LEARN: consume the raw key-down to bind it, never dispatch.
        # Works even on a code nothing maps yet (that's the point of learning).
        # menu._learn_dev narrows capture to one device: the calibration wizard
        # sets it to the pad being calibrated, so keys from a second keyboard
        # fall through to normal dispatch and can still skip/cancel the walk.
        # EDIT KEYS leaves it None — capture from whatever you press.
        if ev.value == 1 and getattr(menu, "_input_learn", False):
            want = getattr(menu, "_learn_dev", None)
            if want is None or _dev_vidpid(dev) == want:
                # ONE KEY AT A TIME. The walk asks for a single key, so a
                # key-down arriving while another is already held on that
                # device is part of a chord — a panic hold, a fumbled press —
                # and is never an answer to the prompt on screen. Binding it
                # silently corrupted maps: a panic gesture mid-walk answered
                # three consecutive prompts with the three keys being held.
                # Swallowed rather than dispatched: during capture no key
                # should reach the perform handlers either.
                if len(self._down.get(dev.path, ())) > 1:
                    return
                menu.learn_key(ev.code, _dev_vidpid(dev))
                return

        name = self._resolve(ev.code)
        if name is None:
            return

        # Source filter: during play only the chosen primary drives the
        # instrument, so other attached keyboards never touch the output. While
        # a menu page is open, ANY keyboard may navigate it — the bootstrap
        # valve that lets you configure an as-yet-unmapped pad from a spare
        # keyboard. An empty primary means auto (any keyboard drives play).
        #
        # If the chosen primary is not currently attached the filter is dropped
        # and any keyboard drives play: otherwise unplugging the pad (or booting
        # without it) would leave no key able to even open the menu and pick
        # another device — a lockout with no way out but editing prefs.json.
        if not menu.active:
            primary = self.inst.cfg.input_primary
            if primary and self._primary_present and _dev_vidpid(dev) != primary:
                return

        if ev.value == 1:
            self._on_key_down(name)
        elif ev.value == 0:
            self._on_key_up(name)

    # ------------------------------------------------------------- main loop
    def _loop(self):
        """Read every USB keyboard node at once via a selector, re-scanning on
        a ~2s cadence for hotplug. No exclusive grab, so many nodes coexist and
        the keys also still reach the console (unchanged from the old numpad)."""
        sel      = selectors.DefaultSelector()
        open_devs = {}          # path -> InputDevice
        last_scan = 0.0

        def _drop(path):
            # Unplugging never sends the key-ups, so drop this node's held-key
            # set too — otherwise phantom codes linger and the next two real
            # presses would look like a panic hold.
            self._down.pop(path, None)
            self._cancel_panic()
            d = open_devs.pop(path, None)
            if d is None:
                return
            try:
                sel.unregister(d)
            except Exception:
                pass
            try:
                d.close()
            except Exception:
                pass

        def _rescan():
            new_pad = False
            present = set(list_devices())
            for path in list(open_devs):
                if path not in present:
                    log.info("input node gone: %s", path)
                    _drop(path)
            for path in present:
                if path in open_devs:
                    continue
                try:
                    d = InputDevice(path)
                except Exception:
                    continue
                if _is_keyboard(d):
                    try:
                        sel.register(d, selectors.EVENT_READ)
                        open_devs[path] = d
                        new_pad = new_pad or _is_pad(d)
                        log.info("input node: %s (%s %s)",
                                 path, _dev_vidpid(d), d.name)
                    except Exception:
                        try:
                            d.close()
                        except Exception:
                            pass
                else:
                    try:
                        d.close()
                    except Exception:
                        pass

            # A pad just appeared: re-offer calibration if it still needs it.
            # This is the way back in after a missed/timed-out offer — on a rig
            # where the unmapped pad is the only input, replugging it is the
            # only gesture available. The menu decides whether to actually show
            # anything (already calibrated / menu busy → no-op).
            if new_pad:
                try:
                    self.inst.menu.maybe_offer_calibration()
                except Exception as e:
                    log.warning("calibration re-offer: %s", e)

        while not self._stop.is_set():
            now = time.monotonic()
            if not open_devs or now - last_scan > 2.0:
                _rescan()
                self._log_primary(open_devs)
                last_scan = now
            if not open_devs:
                time.sleep(1)
                continue
            for key, _mask in sel.select(timeout=1.0):
                if self._stop.is_set():
                    return
                d = key.fileobj
                try:
                    events = list(d.read())
                except BlockingIOError:
                    continue
                except OSError as e:
                    log.warning("input node read error %s (%s) — dropping",
                                d.path, e)
                    _drop(d.path)
                    continue
                for ev in events:
                    if ev.type == ecodes.EV_KEY:
                        self._handle_event(d, ev)

    def _log_primary(self, open_devs):
        """Track whether the chosen primary is attached (the source filter in
        _handle_event keys off it) and log the choice whenever it changes —
        the line to check after a restart."""
        primary = self.inst.cfg.input_primary
        if not primary:
            self._primary_present = False
            state = "auto"
        else:
            match = [d.name for d in open_devs.values()
                     if _dev_vidpid(d) == primary]
            self._primary_present = bool(match)
            state = (f"{primary} {match[0]}" if match else
                     f"{primary} (not attached — any keyboard drives play)")
        if state != self._primary_logged:
            self._primary_logged = state
            log.info("primary input: %s", state)

    # --------------------------------------------------------- hold vs tap
    def _on_key_down(self, name):
        """Grid-cell keys (1-9) on the SHADER/FX grids get a hold timer so a
        long-press can be distinguished from a tap (see module docstring).
        Every other key/context dispatches immediately, unchanged."""
        inst  = self.inst
        _disp = getattr(inst, "display", None)
        on_grid_hold = (name in ("1", "2", "3", "4", "5", "6", "7", "8", "9")
                        and not inst.menu.active
                        and _disp is not None
                        and _disp.is_grid_screen()
                        and _disp._active_tab in _HOLD_TABS)
        # Long-press an LFO cell (key 1/2/3) on a params screen → open that
        # LFO's settings screen. Tap still assigns the LFO to the highlighted
        # param (handled in _dispatch_perform).
        on_lfo_hold = (name in ("1", "2", "3")
                       and not inst.menu.active
                       and _disp is not None
                       and not _disp.is_grid_screen()
                       and not self._lfo_screen
                       and self._param_layer in (0, 1, 5))
        uses_hold = on_grid_hold or on_lfo_hold
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
        if _disp is None:
            return
        if _disp.is_grid_screen():
            self._grid_hold(name, _disp)
        elif (name in ("1", "2", "3") and not self._lfo_screen
              and self._param_layer in (0, 1, 5)):
            self._open_lfo_settings(int(name) - 1, _disp)

    def _settings_press(self):
        """A dedicated SETTINGS key: a TOGGLE, not another tab cycle.

        Pressing it from anywhere shows SETTINGS; pressing it again puts you
        back on the tab you left, which is the part '.' could never do — it has
        no memory of where you came from, so it stranded you on tab 4 (open it
        from SHADER and repeated presses ping-pong SETTINGS_GRID/BROWSER
        forever). Landing on the grid rather than resuming a sub-screen keeps
        one press = one predictable place.
        """
        _disp = getattr(self.inst, "display", None)
        if _disp is None:
            return
        self._clear_param_edit()
        menu = self.inst.menu
        if _disp._active_tab == _SETTINGS_TAB:
            back = (self._settings_origin
                    if self._settings_origin is not None else 0)
            self._settings_origin = None
            if menu.active:
                menu.active = False
                menu._cancel_edits()
            _disp.set_tab(back)
            _disp.go_to_grid_screen()
            return
        self._settings_origin = _disp._active_tab
        _disp.set_tab(_SETTINGS_TAB)
        _disp.go_to_grid_screen()

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
            # INPUT sub-view (DEVICES / LEARN / WIZARD): back out one level
            # rather than closing the whole menu. (While LEARN is ARMED the
            # keyboard loop consumes '.' to bind it, so this only fires when
            # picking — or, in the wizard, from a non-pad keyboard.)
            if getattr(menu, "_input_view", "MENU") != "MENU":
                menu._input_back()
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
            self._clear_param_edit()
            return

        # Back out of the preset options screen to the grid it was opened from.
        if self._preset_opts is not None:
            self._close_preset_opts(_disp)
            return

        if _disp and not _disp.is_grid_screen():
            _disp.go_to_grid_screen()
            return

        # Already at the top level — nothing left to back out of, so this does
        # nothing. '.' used to double as the SETTINGS tab key here, which made
        # its meaning depend on invisible state and gave it no way home: it has
        # no memory of the tab you came from, so opening SETTINGS with it left
        # you alternating SETTINGS_GRID/BROWSER with no route back. SETTINGS is
        # a destination, not a level, so it belongs on its own key (bind
        # SETTINGS TAB — see _settings_press).
        self._clear_param_edit()

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
        if name == "SET":
            self._settings_press()
            return
        if name == ".":
            self._dot_press()
            return
        if name in ("NUM", "/", "*", "-"):
            # Any tab-bar keypress leaves behind a fresh (non-editing) params
            # screen so you never land back on a stale edit-mode from a
            # different context.
            self._clear_param_edit()
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
            # + = previous page, BKSP = next page — matches the menu, where +
            # moves up/back through the list and BKSP moves down/forward. The
            # grid used to be reversed (+ = next), which read as opposite to the
            # menus on the same numpad.
            if name in ("+", "BKSP"):
                step = -1 if name == "+" else +1
                if _disp.preset_store() is not None:
                    _disp.preset_grid_page(step)
                elif tab == 1:
                    _disp.fx_grid_page(step)
                elif tab == 0:
                    _disp.shader_grid_page(step)
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

        # Preset grids: tap loads. Empty cells say so rather than doing
        # nothing silently — holding one is how you fill it.
        store = _disp.preset_store()
        if store is not None:
            self._preset_grid_tap(store, slot, _disp)
            return

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
            from control.menu import PAGES as _SETTINGS_PAGES
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

        # SAMPLER: tapping the already-playing clip opens its per-clip settings
        # (rotate / zoom / speed / dir / trail) — same screen a long-press opens.
        active_slot = self._active_slot_for_tab(tab, inst)
        if active_slot == slot and tab == 2:
            self._open_clip_settings(_disp)
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

    # ------------------------------------------------------------- preset grids
    def _preset_index(self, store, slot, _disp):
        """Slot key → absolute preset index on the current page, or None."""
        from control.display import _GRID_SLOTS as _GS
        try:
            pos = _GS.index(slot)
        except ValueError:
            return None
        return _disp.preset_offset(store) + pos

    def _preset_grid_tap(self, store, slot, _disp):
        from config import PRESET_STORES
        inst  = self.inst
        index = self._preset_index(store, slot, _disp)
        if index is None:
            return
        cfg = inst.cfg
        tag = PRESET_STORES[store]["tag"]
        if not cfg.preset_exists(store, index):
            inst.osd.show(f"{tag} {cfg.preset_name(store, index)[:-5]}: EMPTY"
                          "  (hold to save)")
            return
        data = cfg.load_preset_at(store, index)
        if not data:
            inst.osd.show("LOAD FAILED")
            return
        {"whole":  inst.apply_preset,
         "shader": inst.apply_shader_preset,
         "fx":     inst.apply_fx_preset}[store](data)
        inst.osd.show(f"{tag}: {cfg.preset_name(store, index)[:-5]}")

    def _preset_grid_hold(self, store, slot, _disp):
        from config import PRESET_STORES
        inst  = self.inst
        index = self._preset_index(store, slot, _disp)
        if index is None:
            return
        cfg = inst.cfg
        if cfg.preset_exists(store, index):
            self._open_preset_opts(store, index, _disp)
            return
        name = cfg.preset_name(store, index)[:-5]
        if cfg.save_preset_at(store, index):
            inst.osd.show(f"SAVED {PRESET_STORES[store]['tag']}: {name}")
        else:
            inst.osd.show("SAVE FAILED")

    def _open_preset_opts(self, store, index, _disp):
        """Open the options screen for a saved preset (hold a filled cell)."""
        self._clear_param_edit()
        self._preset_opts    = (store, index)
        self._preset_opt_idx = 0
        _disp.go_to_params_screen()

    def _close_preset_opts(self, _disp=None):
        self._preset_opts    = None
        self._preset_opt_idx = 0
        if _disp is not None:
            _disp.go_to_grid_screen()

    def _dispatch_preset_opts(self, name):
        """Keys on the preset options screen: +/Bksp choose, ENTER/5 fire."""
        from control.display import _PRESET_OPTS
        from config import PRESET_STORES
        inst  = self.inst
        _disp = getattr(inst, "display", None)
        if name == "+":
            self._preset_opt_idx = max(0, self._preset_opt_idx - 1)
            return
        if name == "BKSP":
            self._preset_opt_idx = min(len(_PRESET_OPTS) - 1,
                                       self._preset_opt_idx + 1)
            return
        if name not in ("ENTER", "5"):
            return
        store, index = self._preset_opts
        action = _PRESET_OPTS[self._preset_opt_idx][0]
        cfg    = inst.cfg
        label  = cfg.preset_name(store, index)[:-5]
        if action == "overwrite":
            ok = cfg.save_preset_at(store, index)
            inst.osd.show(f"{'SAVED' if ok else 'SAVE FAILED'} {label}")
        elif action == "delete":
            ok = cfg.delete_preset_at(store, index)
            inst.osd.show(f"{'DELETED' if ok else 'DELETE FAILED'} {label}")
        self._close_preset_opts(_disp)

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

    def _open_clip_settings(self, _disp):
        """Open the CLIP settings params screen for the currently-loaded clip
        (rotate / zoom / speed / dir / trail)."""
        self._param_layer = 5
        self._param_idx   = 0
        self._clear_param_edit()
        _disp.go_to_params_screen()
        self.inst.osd.show("CLIP SETTINGS")

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

        # Preset grids: hold an empty cell to save the current state into it,
        # or a filled one to open its options. Saving and placing are the same
        # act here, which is why these grids need no separate assign mode.
        store = _disp.preset_store()
        if store is not None:
            self._preset_grid_hold(store, slot, _disp)
            return

        # SAMPLER: hold a clip to load it and open its per-clip settings.
        if tab == 2:
            if not inst.sampler.slot(slot):
                inst.osd.show(f"SLOT {slot}: EMPTY")
                return
            inst.sampler.trigger()
            self._open_clip_settings(_disp)
            return

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
        self._clear_param_edit()
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
        mode, +/Bksp scroll the list, 1/2/3 assign LFO 1/2/3, and 4 starts
        MIDI-assign (learn a knob or type a CC); inside edit mode, +/Bksp step
        that parameter's value instead. While MIDI-assign is active it swallows
        every key until a CC is learned/typed or the assign is cancelled.
        Tab key (NUM/slash/asterisk/minus/dot)  exit back to grid (handled
               in _dispatch before this method is reached)
        """
        inst  = self.inst
        _disp = getattr(inst, "display", None)

        # Preset options screen has its own key map.
        if self._preset_opts is not None:
            self._dispatch_preset_opts(name)
            return

        # LFO settings screen has its own key map.
        if self._lfo_screen:
            self._dispatch_lfo(name)
            return

        # MIDI-assign sub-mode swallows every key until it resolves.
        if self._midi_assign:
            self._midi_assign_key_press(name)
            return

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
            self._scroll_param(-1)
        elif name == "BKSP":
            self._scroll_param(+1)
        elif name in ("1", "2", "3"):
            # Assign LFO 1/2/3 to the highlighted param. These used to jump to
            # params 7-9, which every shader with fewer than seven params
            # (nearly all of them) clamped to the last row — so they did
            # nothing. Params 7+ are still reachable by scrolling.
            self._assign_lfo(int(name) - 1)
        elif name == "4":
            # MIDI-assign the highlighted param (learn a knob or type a CC).
            # Replaces the old param-jump quick-access on keys 4-9, which was
            # redundant with +/Bksp scrolling.
            self._begin_midi_assign()
        elif name == "9":
            # DEFAULT button (top-right action cell): reset the current control
            # screen's target to its default values.
            self._reset_current_layer()
        elif name == "0":
            if _disp:
                staged = _disp.toggle_staged()
                if not staged:
                    _disp._grid_pending = [None, None, None, None, None]
                inst.osd.show("STAGED" if staged else "LIVE")
        elif name == "REC":
            inst.record_toggle()

    def _reset_current_layer(self):
        """DEFAULT button: restore the current control screen's target to its
        default values, leaving LFO/MIDI assignments in place.

        SHDR  → the edited generative shader slot's authored PARAM_N defaults.
        FX    → the edited FX shader's authored defaults.
        CLIP  → the current clip's settings reset to CLIP_DEFAULTS.
        (COLOUR/BLEND/TRAIL layers are not reachable from the numpad yet — the
        DEFAULT cell only ever renders on the three screens above.)"""
        inst = self.inst
        cfg  = inst.cfg
        layer = self._param_layer
        if layer == 0:                     # SHDR — generative shader params
            if not cfg.shader_chain:
                inst.osd.show("NO SHADER")
                return
            inst.shader._read_shader_defaults(cfg.shader_edit_slot)
            inst.shader.reapply()
            name = (cfg.current_shader or "").replace(".glsl", "").upper()
            inst.osd.show(f"DEFAULT: {name}" if name else "DEFAULTS RESET")
        elif layer == 1:                   # FX — fx shader params
            if not cfg.current_fx:
                inst.osd.show("NO FX")
                return
            inst.shader._read_fx_defaults(cfg.current_fx)
            inst.shader.reapply()
            inst.osd.show(f"DEFAULT: {cfg.current_fx.replace('.glsl','').upper()}")
        elif layer == 5:                   # CLIP — per-clip settings
            path = cfg.current_clip
            if not path:
                inst.osd.show("NO CLIP")
                return
            # Reset the value keys only; keep any lfo_/cc assignments beside them.
            cfg.clip_settings.setdefault(path, {}).update(cfg.CLIP_DEFAULTS)
            inst.sampler._apply_clip_settings(path)
            inst.sampler.refresh_trail()
            inst.sampler.refresh_overlay()
            import os
            inst.osd.show(f"DEFAULT: {os.path.basename(path).upper()}")
        else:
            inst.osd.show("NO DEFAULT HERE")

    # ── LFO settings screen ─────────────────────────────────────────────────
    def _open_lfo_settings(self, idx, _disp):
        """Open the settings screen for one LFO (long-press an LFO cell)."""
        self._clear_param_edit()          # also drops any stale _lfo_screen
        cfg = self.inst.cfg
        self._lfo_edit_idx = max(0, min(int(idx), len(cfg.lfos) - 1))
        self._lfo_screen   = True
        self._param_idx    = 0
        _disp.go_to_params_screen()
        self.inst.osd.show(f"LFO {self._lfo_edit_idx + 1} SETTINGS")

    def _dispatch_lfo(self, name):
        """Keys on the LFO settings screen. ENTER toggles edit mode; outside it
        +/Bksp scroll the rows and inside it they step the highlighted value;
        key 9 resets the LFO to defaults."""
        if name == "ENTER":
            self._editing_param = not self._editing_param
            return
        if self._editing_param:
            if name == "+":
                self._step_lfo(+1)
            elif name == "BKSP":
                self._step_lfo(-1)
            return
        if name == "+":
            self._scroll_param(-1)
        elif name == "BKSP":
            self._scroll_param(+1)
        elif name == "9":
            self._reset_lfo()

    def _lfo_slots(self):
        return _LFO_SLOTS

    def _step_lfo(self, delta):
        """Step the highlighted LFO setting. min = the value the LFO drops to,
        max = the value it rises to (both 0..1 shown as 0..100); together they
        set the engine's offset (=min) and amp (=max-min). speed changes the
        period (or the beat when synced); + = faster."""
        inst = self.inst
        cfg  = inst.cfg
        if not cfg.lfos:
            return
        L    = cfg.lfos[self._lfo_edit_idx % len(cfg.lfos)]
        slot = self._lfo_slots()[self._param_idx % len(self._lfo_slots())]
        d    = 1 if delta > 0 else -1
        n    = self._lfo_edit_idx + 1
        if slot == "shape":
            shapes = cfg.LFO_SHAPES
            L["shape"] = (int(float(L.get("shape", 0))) + d) % len(shapes)
            inst.osd.show(f"LFO {n} SHAPE: {shapes[int(L['shape'])]}")
        elif slot in ("min", "max"):
            cur_min = float(L.get("offset", 0.0))
            cur_max = cur_min + float(L.get("amp", 0.5))
            if slot == "min":
                new_min = round(max(0.0, min(cur_max, cur_min + d * 0.05)), 3)
                L["offset"] = new_min
                L["amp"]    = round(max(0.0, cur_max - new_min), 3)
                inst.osd.show(f"LFO {n} MIN: {int(round(new_min * 100))}")
            else:
                new_max = round(max(cur_min, min(1.0, cur_max + d * 0.05)), 3)
                L["amp"] = round(new_max - cur_min, 3)
                inst.osd.show(f"LFO {n} MAX: {int(round(new_max * 100))}")
        elif slot == "speed":
            if L.get("bpm_sync"):
                beats = list(cfg.LFO_BEATS)
                cur   = float(L.get("beat", 1.0))
                j     = beats.index(cur) if cur in beats else 3
                # + = faster = shorter musical division = lower index
                L["beat"] = beats[max(0, min(len(beats) - 1, j - d))]
                lbl = (cfg.LFO_BEAT_LABELS[beats.index(L["beat"])]
                       if L["beat"] in beats else f"{L['beat']:g}")
                inst.osd.show(f"LFO {n} SPEED: {lbl} beat")
            else:
                cur = float(L.get("period", 4.0))
                # + = faster = shorter period
                L["period"] = round(max(0.05, min(60.0,
                                    cur * (1 / 1.15 if d > 0 else 1.15))), 3)
                inst.osd.show(f"LFO {n} SPEED: {L['period']:.2f}s")
        elif slot == "sync":
            L["bpm_sync"] = not L.get("bpm_sync", False)
            inst.osd.show(f"LFO {n} SYNC: {'BPM' if L['bpm_sync'] else 'SEC'}")
        inst.shader.reapply()   # re-bake the GLSL LFO preamble (CPU LFOs read live)

    def _reset_lfo(self):
        """DEFAULT on the LFO screen: restore this LFO to a neutral default."""
        inst = self.inst
        cfg  = inst.cfg
        if not cfg.lfos:
            return
        cfg.lfos[self._lfo_edit_idx % len(cfg.lfos)].update(
            {"shape": 0, "amp": 0.5, "offset": 0.0,
             "period": 4.0, "bpm_sync": False, "beat": 1.0})
        inst.shader.reapply()
        inst.osd.show(f"LFO {self._lfo_edit_idx + 1} DEFAULT")

    def _current_row_keys(self):
        """Ordered row keys for whatever the active params layer shows —
        single source of truth shared with control/display.py rendering."""
        if self._lfo_screen:
            return list(self._lfo_slots())
        if self._param_layer == 1:
            return self.inst.shader.fx_row_keys()
        if self._param_layer == 2:
            return list(self._colour_slots())
        if self._param_layer == 3:
            return list(self._blend_slots())
        if self._param_layer == 4:
            return list(self._trail_slots())
        if self._param_layer == 5:
            return list(self._clipset_slots())
        return self.inst.shader.shader_row_keys()

    def _scroll_param(self, direction):
        """Move the selection down (+1) or up (-1) the list.

        Callers map + to -1 and Bksp to +1, matching the menu list, where +
        also moves the selection up. Note the params view keeps the selection
        centred, so the rows slide the opposite way to the cursor — read the
        cursor, not the content, when checking this.
        """
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
        elif self._param_layer == 5:   # CLIP (per-clip settings)
            slots = self._clipset_slots()
            self._param_idx = min(n - 1, len(slots) - 1)
            inst.osd.show(f"CLIP: {_CLIPSET_LABELS[slots[self._param_idx]]}")
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

    def _assign_lfo(self, idx):
        """Toggle LFO `idx` on the highlighted param — "tap 7 2": 7 picks the
        param, 2 names the LFO. Pressing the same LFO again clears it, so no
        separate un-assign key is needed.

        Only the SHDR (0) and FX (1) layers hold real shader params; the blend
        mode/amount rows and the other layers have no PARAM_N to substitute.
        """
        inst = self.inst
        cfg  = inst.cfg
        if self._param_layer == 5:
            self._assign_clip_lfo(idx)
            return
        if self._param_layer == 0:
            params, labels = cfg.params, inst.shader.param_labels()
        elif self._param_layer == 1:
            params, labels = cfg.fx_params, inst.shader.fx_param_labels()
        else:
            inst.osd.show("NO PARAMS HERE")
            return

        keys = self._current_row_keys()
        if not keys:
            return
        key = keys[self._param_idx % len(keys)]
        if key.startswith("__"):          # __blend_mode__ / __blend_amt__
            inst.osd.show("NO LFO ON BLEND")
            return

        label = labels.get(key, key.upper()).upper()
        mkey  = "lfo_" + key
        cur   = params.get(mkey)
        if cur is not None and int(cur) == idx:
            params.pop(mkey, None)
            inst.osd.show(f"{label}: LFO OFF")
        else:
            params[mkey] = idx
            inst.osd.show(f"{label} -> LFO {idx + 1}")
        inst.shader.reapply()

    def _assign_clip_lfo(self, idx):
        """CLIP layer: toggle an LFO on the highlighted zoom/speed row (per
        clip). Other rows aren't continuous CPU targets, so they refuse."""
        inst = self.inst
        cfg  = inst.cfg
        key  = self._clipset_slots()[self._param_idx % len(self._clipset_slots())]
        if key not in ("zoom", "speed"):
            inst.osd.show("LFO: ZOOM/SPEED ONLY")
            return
        path  = cfg.current_clip
        label = key.upper()
        cur   = cfg.clip_lfo(path, key)
        if cur is not None and cur == idx:
            cfg.clip_set_lfo(path, key, None)
            inst.osd.show(f"{label}: LFO OFF")
            # restore the clip's static value the LFO was overriding
            if key == "zoom":
                inst.sampler.apply_video_zoom()
            else:
                inst.sampler.set_speed_dir(cfg.clip_get(path, "speed"),
                                           cfg.clip_get(path, "reverse"))
        else:
            cfg.clip_set_lfo(path, key, idx)
            inst.osd.show(f"{label} -> LFO {idx + 1}")

    def _clear_param_edit(self):
        """Leave both params-screen sub-modes (value-edit and MIDI-assign),
        disarming any pending MIDI-learn. Called whenever navigation lands on
        or leaves a params screen so neither sub-mode leaks across contexts."""
        self._editing_param  = False
        self._lfo_screen     = False
        self._preset_opts    = None
        self._preset_opt_idx = 0
        if self._midi_assign:
            self._cancel_midi_assign()

    # ── MIDI assign (params-screen key 4) ───────────────────────────────────

    def _midi_param_labels(self):
        """(labels, ok) for the current layer — SHDR/FX hold shader params a CC
        can drive; the CLIP layer exposes zoom/speed (the two continuous CPU
        targets). Everything else has nothing a CC can scale."""
        inst = self.inst
        if self._param_layer == 0:
            return inst.shader.param_labels(), True
        if self._param_layer == 1:
            return inst.shader.fx_param_labels(), True
        if self._param_layer == 5:
            return {"zoom": "ZOOM", "speed": "SPEED"}, True
        return {}, False

    def _begin_midi_assign(self):
        """Start MIDI-assign for the highlighted param: arm learn AND accept a
        typed CC number, whichever the user does first."""
        inst = self.inst
        labels, ok = self._midi_param_labels()
        if not ok:
            inst.osd.show("NO MIDI HERE")
            return
        keys = self._current_row_keys()
        if not keys:
            return
        key = keys[self._param_idx % len(keys)]
        if key.startswith("__"):          # __blend_mode__ / __blend_amt__
            inst.osd.show("NO MIDI ON BLEND")
            return
        if key not in labels:             # e.g. CLIP rotate/dir/trail: not CC-able
            inst.osd.show("NO MIDI HERE")
            return

        self._midi_assign     = True
        self._midi_cc_buf     = ""
        self._midi_assign_key = key
        midi = getattr(inst, "midi", None)
        if midi is not None:
            midi.arm_learn(self._on_midi_learned)
        inst.osd.show("MIDI: move knob or type CC")

    def _midi_assign_key_press(self, name):
        """Handle a keypress while MIDI-assign is active."""
        if name.isdigit():
            self._midi_cc_buf += name
            self.inst.osd.show(f"MIDI CC: {self._midi_cc_buf}")
            if len(self._midi_cc_buf) == 3:   # 3 digits can't grow further
                self._commit_midi_cc()
        elif name == "ENTER":
            self._commit_midi_cc()
        elif name == "BKSP":
            self._midi_cc_buf = self._midi_cc_buf[:-1]
            self.inst.osd.show(f"MIDI CC: {self._midi_cc_buf or '_'}")
        else:
            self._cancel_midi_assign()

    def _on_midi_learned(self, cc):
        """arm_learn callback — runs on the MIDI thread when a knob moves."""
        if not self._midi_assign:
            return
        self._bind_midi_cc(self._midi_assign_key, int(cc))
        self._midi_assign = False
        self._midi_cc_buf = ""

    def _commit_midi_cc(self):
        """Finish MIDI-assign using whatever CC number was typed (empty=cancel)."""
        inst = self.inst
        buf  = self._midi_cc_buf
        self._midi_assign = False
        self._midi_cc_buf = ""
        midi = getattr(inst, "midi", None)
        if midi is not None:
            midi.cancel_learn()
        if not buf:
            inst.osd.show("MIDI: CANCELLED")
            return
        self._bind_midi_cc(self._midi_assign_key, max(0, min(127, int(buf))))

    def _cancel_midi_assign(self):
        self._midi_assign = False
        self._midi_cc_buf = ""
        midi = getattr(self.inst, "midi", None)
        if midi is not None:
            midi.cancel_learn()
        self.inst.osd.show("MIDI: CANCELLED")

    def _bind_midi_cc(self, key, cc):
        """Store key -> CC in cfg.midi_target_cc and refresh the reverse map.
        Pressing MIDI again on an already-bound CC clears it (no un-assign key)."""
        inst   = self.inst
        cfg    = inst.cfg
        labels, _ = self._midi_param_labels()
        label  = labels.get(key, key.upper()).upper()
        if cfg.midi_target_cc.get(key) == cc:
            cfg.midi_target_cc.pop(key, None)
            inst.osd.show(f"{label}: MIDI OFF")
        else:
            cfg.midi_target_cc[key] = cc
            inst.osd.show(f"{label} -> CC {cc}")
        midi = getattr(inst, "midi", None)
        if midi is not None:
            midi.invalidate_cc_map()

    def _step_video_blend_mode(self, d):
        """Slot 0's BLEND row: how the shader composites over the video layer.

        Uses the same palette as every other slot, so "normal" keeps its
        pass-through meaning — here that is "shader replaces the video", i.e.
        cfg.shader_blend off. Any real mode turns it on. Routed through
        shader_blend_toggle() so starting/tearing down the video source and the
        reapply stay in one place (it also shows its own OSD).
        """
        cfg   = self.inst.cfg
        inst  = self.inst
        modes = list(cfg.FX_LAYER_BLEND_MODES)      # ("normal",) + SHADER_BLEND_MODES
        cur   = cfg.shader_blend_mode if cfg.shader_blend else "normal"
        i     = modes.index(cur) if cur in modes else 0
        new   = modes[(i + d) % len(modes)]

        if new == "normal":
            if cfg.shader_blend:
                inst.shader_blend_toggle()          # OFF: blank the source, reapply
            return
        cfg.shader_blend_mode = new
        if not cfg.shader_blend:
            inst.shader_blend_toggle()              # ON: starts the source, reapply
        else:
            inst.shader.reapply()
            inst.osd.show(f"BLEND: {new.upper()}")

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

    def _clipset_slots(self):
        return ("rotate", "zoom", "speed", "dir", "bright", "contrast",
                "trail_on", "trail", "trail_time", "trail_mode", "trail_opc")

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
            # Slot 0's "layer below" is the video, not another shader — see
            # shader_row_keys(). Its blend rows drive cfg.shader_blend* instead.
            video_blend = cfg.shader_edit_slot == 0
            if key == "__blend_mode__":
                if video_blend:
                    self._step_video_blend_mode(1 if delta > 0 else -1)
                    return
                inst.shader.cycle_shader_layer_blend_mode(1 if delta > 0 else -1)
                inst.osd.show(f"BLEND: {cfg.shader_layer_blend.get('mode','normal').upper()}")
                return
            if key == "__blend_amt__":
                if video_blend:
                    # shows its own OSD, and reapplies only when blend is on
                    inst.shader_blend_adjust_amount(delta)
                    return
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
        else:                             # ── CLIP: per-clip rotate/zoom/speed/dir/trail
            slots = self._clipset_slots()
            slot  = slots[self._param_idx % len(slots)]
            path  = cfg.current_clip
            d     = 1 if delta > 0 else -1
            if not path:
                inst.osd.show("NO CLIP")
                return
            if slot == "rotate":
                steps = list(getattr(cfg, 'VIDEO_ROTATE_STEPS', (0, 90, 180, 270)))
                cur   = cfg.clip_get(path, "rotate")
                i     = steps.index(cur) if cur in steps else 0
                val   = steps[(i + d) % len(steps)]
                cfg.clip_set(path, "rotate", val)
                inst.sampler.refresh_overlay()   # rebuild vf with the new angle
                inst.osd.show(f"ROTATE: {val}°")
            elif slot == "zoom":
                zmax = getattr(cfg, 'VIDEO_ZOOM_MAX', 4.0)
                cur  = cfg.clip_get(path, "zoom")
                val  = round(max(1.0, min(zmax, cur + d * 0.05)), 2)
                if val == cur:
                    return
                cfg.clip_set(path, "zoom", val)
                inst.sampler.apply_video_zoom()
                inst.osd.show(f"ZOOM: {val:.2f}x")
            elif slot == "speed":
                cur = cfg.clip_get(path, "speed")
                val = round(max(0.1, min(4.0, cur + SPEED_STEP * d)), 2)
                if val == cur:
                    return
                cfg.clip_set(path, "speed", val)
                inst.sampler.set_speed_dir(val, cfg.clip_get(path, "reverse"))
                inst.osd.show(f"SPEED: {val:.2f}x")
            elif slot == "dir":
                rev = not cfg.clip_get(path, "reverse")
                cfg.clip_set(path, "reverse", rev)
                inst.sampler.set_speed_dir(cfg.clip_get(path, "speed"), rev)
                inst.osd.show("REVERSE" if rev else "FORWARD")
            elif slot in ("bright", "contrast"):
                cur = int(cfg.clip_get(path, slot))
                val = max(-100, min(100, cur + d * 5))
                if val == cur:
                    return
                cfg.clip_set(path, slot, val)
                inst.sampler.apply_video_eq()
                inst.osd.show(f"{slot.upper()}: {val:+d}")
            elif slot == "trail_on":                  # trail on/off toggle
                on = not bool(cfg.clip_get(path, "trail_on"))
                cfg.clip_set(path, "trail_on", on)
                inst.sampler.apply_clip_trail(path)
                inst.osd.show(f"TRAIL: {'ON' if on else 'OFF'}")
            elif slot == "trail":                     # echo STEPS (1..max)
                tmax = getattr(cfg, 'CLIP_TRAIL_MAX', 5)
                cur  = int(cfg.clip_get(path, "trail"))
                val  = max(1, min(tmax, cur + d))
                if val == cur:
                    return
                cfg.clip_set(path, "trail", val)
                inst.sampler.apply_clip_trail(path)
                inst.osd.show(f"TRL STEPS: {val}")
            elif slot == "trail_time":                # delay to furthest echo
                cur = cfg.clip_get(path, "trail_time")
                val = round(max(0.25, min(8.0, cur + d * 0.25)), 2)
                if val == cur:
                    return
                cfg.clip_set(path, "trail_time", val)
                inst.sampler.apply_clip_trail(path)
                inst.osd.show(f"TRL TIME: {val:.2f}s")
            elif slot == "trail_mode":                # echo blend mode
                modes = list(cfg.TRAIL_MODES)
                cur   = cfg.clip_get(path, "trail_mode")
                i     = modes.index(cur) if cur in modes else 0
                val   = modes[(i + d) % len(modes)]
                cfg.clip_set(path, "trail_mode", val)
                inst.sampler.apply_clip_trail(path)
                inst.osd.show(f"TRL MODE: {val.upper()}")
            elif slot == "trail_opc":                 # per-echo blend opacity
                cur = cfg.clip_get(path, "trail_opc")
                val = round(max(0.0, min(1.0, cur + d * 0.05)), 2)
                if val == cur:
                    return
                cfg.clip_set(path, "trail_opc", val)
                inst.sampler.apply_clip_trail(path)
                inst.osd.show(f"TRL BLEND: {val:.2f}")
