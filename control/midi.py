#!/usr/bin/env python3
"""
MidiController — USB MIDI input via python-rtmidi.

CC → shader params  (multi-mapping covers common controllers out of the box)
Note-on             → clip slots / triggers
Note-off            → release (gated mode)
Program-change      → load preset XX.json

CC layout
─────────
  p1  CC1 (mod wheel)   CC21  CC48  CC74
  p2  CC2               CC22  CC49  CC71
  p3  CC3               CC23  CC50  CC91
  p4  CC4               CC24  CC51  CC93

  Overlay toggle          CC64  (sustain pedal)
  Overlay cycle mode      CC65
  Shader blend toggle     CC66
  Shader blend cycle      CC67
  Shader cycle +1         CC68
  Shader cycle -1         CC69
  Mode → SAMPLER          CC80
  Trail toggle            CC81  (was FX mode — removed)
  Mode → SHADER           CC82
  Mode → LIVE             CC83

Notes
─────
  Any note: slot = note % 10 (only 4-9 are real slots), trigger on note-on.
  Notes 120/122/123: mode SAMPLER / SHADER / LIVE (velocity > 0 only).
  Note 121 (was FX) falls through to clip-slot trigger (slot 1).

Port selection
──────────────
  Skips ALSA "Midi Through" passthrough ports that are always present.
  Opens the first real hardware or software port found.
  A background thread polls every 3 s so hot-plugged devices are picked up
  without restarting the service.
"""

import logging
import threading
import time

log = logging.getLogger("midi")

try:
    import rtmidi
    HAVE_RTMIDI = True
except Exception:
    HAVE_RTMIDI = False


# ── CC mappings ───────────────────────────────────────────────────────────────

# CC numbers that control shader params — multiple assignments per param so
# mod wheel, Akai/Novation knobs, Ableton Push encoders, etc. all just work.
CC_PARAMS: dict[int, str] = {
    # p1
    1: "p1", 21: "p1", 48: "p1", 74: "p1",
    # p2
    2: "p2", 22: "p2", 49: "p2", 71: "p2",
    # p3
    3: "p3", 23: "p3", 50: "p3", 91: "p3",
    # p4
    4: "p4", 24: "p4", 51: "p4", 93: "p4",
}

# CC → action name (fired when value > 63 for toggles; always for cycles)
CC_ACTIONS: dict[int, str] = {
    64: "overlay_toggle",        # sustain pedal
    65: "overlay_cycle",         # portamento on/off
    66: "shader_blend_toggle",   # sostenuto
    67: "shader_blend_cycle",    # soft pedal
    68: "shader_next",           # general purpose 5
    69: "shader_prev",           # general purpose 6
    80: "mode_sampler",          # general purpose 1
    81: "trail_toggle",          # general purpose 2  (FX mode removed)
    82: "mode_shader",           # general purpose 3
    83: "mode_live",             # general purpose 4
}

# ── user-assignable targets ───────────────────────────────────────────────────

# Ordered list of every target the user can bind a CC to, shown in the MIDI
# settings page. "Continuous" targets (params) scale 0–127 → 0.0–1.0.
# "Toggle" targets fire on val > 63; "cycle" targets fire on any value.
MIDI_TARGETS: list[str] = [
    "p1", "p2", "p3", "p4",
    "blend_amt", "ovl_opacity", "trl_decay",
    "overlay_toggle", "overlay_cycle",
    "shader_blend_toggle", "shader_blend_cycle",
    "shader_next", "shader_prev",
    "trail_toggle",
    "mode_sampler", "mode_shader", "mode_live",
]

# Display label for each target (≤8 chars for the menu column)
MIDI_TARGET_LABELS: dict[str, str] = {
    "p1":                  "P1",
    "p2":                  "P2",
    "p3":                  "P3",
    "p4":                  "P4",
    "blend_amt":           "BLD AMT",
    "ovl_opacity":         "OVL OPC",
    "trl_decay":           "TRL DEC",
    "overlay_toggle":      "OVL TOG",
    "overlay_cycle":       "OVL CYC",
    "shader_blend_toggle": "BLD TOG",
    "shader_blend_cycle":  "BLD CYC",
    "shader_next":         "FX NEXT",
    "shader_prev":         "FX PREV",
    "trail_toggle":        "TRL TOG",
    "mode_sampler":        "MODE SAM",
    "mode_shader":         "MODE SHD",
    "mode_live":           "MODE LIV",
}

# Built-in default CC for each target (None = no default; user must assign one
# to make that target reachable at all).  Primary CC only — p1 also responds
# to 21/48/74 via CC_PARAMS but we show just the canonical one here.
MIDI_DEFAULTS: dict[str, int | None] = {
    "p1":                  1,
    "p2":                  2,
    "p3":                  3,
    "p4":                  4,
    "blend_amt":           None,
    "ovl_opacity":         None,
    "trl_decay":           None,
    "overlay_toggle":      64,
    "overlay_cycle":       65,
    "shader_blend_toggle": 66,
    "shader_blend_cycle":  67,
    "shader_next":         68,
    "shader_prev":         69,
    "trail_toggle":        81,
    "mode_sampler":        80,
    "mode_shader":         82,
    "mode_live":           83,
}

# Notes that switch modes directly (velocity > 0).
# Note 121 (was FX mode) is no longer mapped — falls through to clip-slot trigger.
NOTE_MODES: dict[int, str] = {
    120: "SAMPLER",
    122: "SHADER",
    123: "LIVE",
}

# Prefix strings that identify ALSA pass-through ports to skip
_SKIP_PREFIXES = ("midi through", "through port")


def _is_passthrough(name: str) -> bool:
    n = name.lower()
    return any(n.startswith(p) or p in n for p in _SKIP_PREFIXES)


def _best_port(midi_in) -> tuple[int, str] | None:
    """Return (index, name) of the first non-passthrough port, or None."""
    for i, name in enumerate(midi_in.get_ports()):
        if not _is_passthrough(name):
            return i, name
    return None


# ── controller ────────────────────────────────────────────────────────────────

class MidiController:
    def __init__(self, inst):
        self.inst     = inst
        self._midi    = None
        self._stop    = threading.Event()
        self._lock    = threading.Lock()
        self._port_name: str | None = None
        self._cc_reverse: dict[int, str] | None = None  # cc -> target, lazy

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def start(self):
        if not self.inst.cfg.use_midi:
            return
        if not HAVE_RTMIDI:
            log.warning("python-rtmidi not installed — MIDI disabled")
            return
        # Immediate attempt, then poll for hot-plug
        self._try_connect()
        t = threading.Thread(target=self._hotplug_loop, daemon=True,
                             name="midi-hotplug")
        t.start()

    def stop(self):
        self._stop.set()
        self._disconnect()

    # ── connection management ──────────────────────────────────────────────────

    def _try_connect(self):
        """Open the first real MIDI port. No-op if already connected."""
        with self._lock:
            if self._midi is not None:
                return
            probe = rtmidi.MidiIn()
            result = _best_port(probe)
            if result is None:
                probe.delete()
                return
            idx, name = result
            try:
                probe.open_port(idx)
                probe.ignore_types(sysex=True, timing=True, active_sense=True)
                probe.set_callback(self._on_midi)
                self._midi = probe
                self._port_name = name
                log.info("MIDI connected: '%s'", name)
            except Exception as e:
                log.warning("MIDI open failed: %s", e)
                probe.delete()

    def _disconnect(self):
        with self._lock:
            if self._midi:
                try:
                    self._midi.close_port()
                    self._midi.delete()
                except Exception:
                    pass
                self._midi = None
                self._port_name = None

    def _hotplug_loop(self):
        """Poll every 3 s; reconnect when a device appears or disappears."""
        while not self._stop.wait(3.0):
            with self._lock:
                midi = self._midi
                port = self._port_name

            if midi is not None:
                # Verify current port still exists
                probe = rtmidi.MidiIn()
                names = probe.get_ports()
                probe.delete()
                if port not in names:
                    log.info("MIDI device '%s' disconnected", port)
                    self._disconnect()
            else:
                self._try_connect()

    # ── MIDI callback ──────────────────────────────────────────────────────────

    def _on_midi(self, event, _data=None):
        msg, _ = event
        if len(msg) < 2:
            return
        status = msg[0] & 0xF0
        d1     = msg[1]
        d2     = msg[2] if len(msg) > 2 else 0

        if status == 0xB0:          # control change
            self._handle_cc(d1, d2)
        elif status == 0x90:        # note on (d2=velocity)
            if d2 > 0:
                self._handle_note_on(d1, d2)
            else:
                self._handle_note_off(d1)
        elif status == 0x80:        # note off
            self._handle_note_off(d1)
        elif status == 0xC0:        # program change
            self.inst.cfg.load_preset(f"{d1:02d}.json")

    def invalidate_cc_map(self):
        """Call after midi_target_cc is edited (MIDI settings menu) so the
        next CC message rebuilds the reverse lookup instead of using stale
        assignments."""
        self._cc_reverse = None

    def _handle_cc(self, cc: int, val: int):
        # User-defined assignments take full priority over built-in defaults.
        # A user CC fires its target and returns; built-ins are only reached
        # when no user assignment matches the incoming CC number. Reverse
        # lookup is cached (rebuilt lazily via invalidate_cc_map) so this is
        # O(1) instead of scanning the whole map on every CC message.
        if self._cc_reverse is None:
            user_map: dict = getattr(self.inst.cfg, 'midi_target_cc', {})
            self._cc_reverse = {cc_: target for target, cc_ in user_map.items()
                                 if cc_ is not None}
        target = self._cc_reverse.get(cc)
        if target is not None:
            self._dispatch_target(target, val)
            return

        # Built-in param knobs — scale 0–127 → 0.0–1.0
        if cc in CC_PARAMS:
            self.inst.shader.set_param(CC_PARAMS[cc], val / 127.0)
            return

        # Built-in action CCs
        action = CC_ACTIONS.get(cc)
        if action is not None:
            self._dispatch_target(action, val)

    def _dispatch_target(self, target: str, val: int):
        """Execute a named target with a raw CC value (0–127).

        Continuous targets (p1-p4, blend_amt, ovl_opacity, trl_decay) map
        0–127 linearly to their natural range.  Toggle targets fire on
        val > 63; cycle targets fire on any value.
        """
        inst = self.inst
        cfg  = inst.cfg

        # ── continuous params ────────────────────────────────────────────
        if target in ("p1", "p2", "p3", "p4"):
            inst.shader.set_param(target, val / 127.0)

        elif target == "blend_amt":
            cfg.shader_blend_amount = round(val / 127.0, 3)
            if cfg.shader_blend:
                inst.shader.reapply()

        elif target == "ovl_opacity":
            cfg.overlay_blend_amount = round(val / 127.0, 3)
            if getattr(cfg, 'overlay_on', False):
                inst.sampler.refresh_overlay()

        elif target == "trl_decay":
            cfg.trail_decay = round(0.80 + val / 127.0 * 0.19, 3)
            if getattr(cfg, 'trail_on', False):
                inst.sampler.refresh_trail()

        # ── toggles (fire on val > 63) ───────────────────────────────────
        elif target == "overlay_toggle" and val > 63:
            inst.overlay_toggle()

        elif target == "shader_blend_toggle" and val > 63:
            inst.shader_blend_toggle()

        elif target == "trail_toggle" and val > 63:
            inst.trail_toggle()

        elif target == "mode_sampler" and val > 63:
            inst.set_mode("SAMPLER")

        elif target == "mode_shader" and val > 63:
            inst.set_mode("SHADER")

        elif target == "mode_live" and val > 63:
            inst.set_mode("LIVE")

        # ── cycles (fire on any value) ───────────────────────────────────
        elif target == "overlay_cycle":
            inst.overlay_cycle_mode(+1 if val > 63 else -1)

        elif target == "shader_blend_cycle":
            inst.shader_blend_cycle()

        elif target == "shader_next" and val > 63:
            if inst.mode == "SHADER":
                inst.shader.apply_fx_overlay(+1)
            else:
                inst.shader.cycle(+1, kind="fx")
            chain = getattr(cfg, "fx_chain", [])
            chain_str = " > ".join(f.replace(".glsl","").upper() for f in chain) if chain else "—"
            inst.osd.show(f"FX: {chain_str}")

        elif target == "shader_prev" and val > 63:
            if inst.mode == "SHADER":
                inst.shader.apply_fx_overlay(-1)
            else:
                inst.shader.cycle(-1, kind="fx")
            chain = getattr(cfg, "fx_chain", [])
            chain_str = " > ".join(f.replace(".glsl","").upper() for f in chain) if chain else "—"
            inst.osd.show(f"FX: {chain_str}")

    def _handle_note_on(self, note: int, vel: int):
        inst = self.inst

        # High notes switch modes
        if note in NOTE_MODES:
            inst.set_mode(NOTE_MODES[note])
            return

        # All other notes trigger clip slots (note % 10; only 4-9 exist).
        # In LIVE mode there's no clip to trigger — the camera is the
        # source — so notes recall presets instead, same as keyboard.py's
        # plain-4-9-in-LIVE behaviour (avoids killing the camera feed).
        if inst.mode == "LIVE":
            inst.load_preset_slot(note % 10)
            return
        s = inst.sampler
        if s.slot(note % 10):
            s.trigger()

    def _handle_note_off(self, note: int):
        if note not in NOTE_MODES:
            self.inst.sampler.release()
