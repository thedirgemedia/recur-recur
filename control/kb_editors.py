"""Params / LFO / MIDI sub-screen editors for the numpad controller.

Split out of control/keyboard.py, which had grown into three separate things
wearing one coat: the evdev reader thread, the perform-mode dispatcher, and
these editors. This is the third — everything that runs while a params, LFO or
MIDI-assign screen is up.

It stays a MIXIN rather than a collaborator object on purpose: control/display.py
and control/menu.py read editor state (_param_idx, _param_layer, _lfo_screen,
_editing_param, _preset_opts, ...) straight off the KeyboardController instance,
including in the render loop's param signature. Moving that state onto another
object would break those readers for no gain, so the methods move and `self`
stays exactly what it was.
"""

import os

from engine.shader import clamp01

SPEED_STEP = 0.1   # step size for sampler speed (0.1–4.0 range)

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


class ParamEditorMixin:
    """Mixed into KeyboardController — `self` is that controller throughout."""

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
            cfg.clip_reset(path)
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
            cfg.midi_cc_pop(key)
            inst.osd.show(f"{label}: MIDI OFF")
        else:
            cfg.midi_cc_set(key, cc)
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
