#!/usr/bin/env python3
"""
Menu — recur-style navigable UI shown on the 3.5" SPI display.

Modelled on the original r_e_c_u_r operate UI (cyberboy666/r_e_c_u_r), adapted
to recur-recur's modes and engines. Four pages cycled with 7 / 9:

  BROWSER  — clips list; 5 stages a pick (loads on menu close); ENTER+4–9 assigns
             the highlighted clip to that performance slot
  SHADERS  — generative shaders list; same bindings as BROWSER
  SETTINGS — editable options as a plain scrolling list grouped under
             headers (PLAYBACK / VIDEO / MIX / SHADERS / SYSTEM). Each row's
             group is declared on the _Item itself and a header is drawn
             wherever the group changes, so `self.sel` still indexes the
             ITEM list — headers are never selectable. ENTER opens the
             highlighted row for editing (+/BKSP then step its value, ENTER
             closes); rows flagged action=True fire on ENTER instead.
  MIDI     — per-target CC overrides; 4/6 step ±5, 5 = numeric entry,
             BKSP = reset to built-in default

Navigation (logical key names, mapped in keyboard.py for both NumLock states):
  + / BKSP   move selection up / down the list (BKSP is page-specific on
             BROWSER/PRESETS/MIDI — see handle())
  8 / 2      move selection up / down
  4 / 6      adjust selected value (left / right)
  5 / ENTER  select / activate (the "■" action)
  7 / 9      previous / next page (loops)
  .          back out one level / close the menu (handled in keyboard.py)

While the menu is active, NONE of these keys reach the perform handlers, so the
HDMI video output is never changed by a keypress in menu mode.
"""

import logging
import os
import socket
import threading
import time

from control.midi import MIDI_TARGETS, MIDI_TARGET_LABELS, MIDI_DEFAULTS


def _local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "no network"

log = logging.getLogger("menu")

PAGES = ("BROWSER", "SHADERS", "PRESETS", "SETTINGS", "MIDI", "IMPORT")


def _drive_label(path):
    """Return a short drive label for a path under /media or /mnt.

    /media/user/LABEL/...   →  LABEL (up to 6 chars)
    /mnt/LABEL/...          →  LABEL
    anything else           →  USB
    """
    parts = path.split(os.sep)   # ['', 'media', 'user', 'LABEL', ...]
    if len(parts) >= 4 and parts[1] == "media":
        return parts[3][:6]
    if len(parts) >= 3 and parts[1] == "mnt":
        return parts[2][:6]
    return "USB"



class Menu:
    def __init__(self, inst):
        self.inst   = inst
        self.active = False
        self.page   = 0          # index into PAGES
        self.sel    = 0          # selection row within the current page
        self._midi_editing   = False   # True while numeric CC entry is active
        self._midi_input_buf = ""      # digits typed so far
        self._settings_editing = False # True while +/BKSP change the highlighted
                                       # SETTINGS value instead of scrolling
        self._assigning      = False   # True while waiting for a slot key (BROWSER/SHADERS)
        self._confirm_delete = False   # True after one BKSP in BROWSER/PRESETS (arm delete)
        # Staged selections applied to the live output when the menu closes
        # (the menu never changes the HDMI output while it is open).
        self._pending_clip_idx = None
        self._pending_shader   = None
        # IMPORT page (USB → internal) state.
        self._usb_drives      = []    # cached removable-drive list (drive-list view)
        self._usb_dev         = None  # device currently mounted (None = drive list)
        self._usb_mp          = None  # its mountpoint
        self._usb_files       = []    # video files found on the mounted drive
        self._usb_status      = ""    # transient status line ("MOUNTING…", "COPIED", …)
        self._usb_import_busy = False # True while a copy/transcode thread is running
        self._usb_cancel      = threading.Event()  # set to abort in-progress import
        self._lfo_taps        = []    # monotonic times of recent TAP presses

    def open_page(self, page_name):
        """Open a menu page by name, running its leave/enter hooks.

        Both routes in — 7/9 cycling and the SETTINGS grid jump — go through
        here. They used to each switch pages themselves, and IMPORT's drive
        scan was wired into the cycling path only, so reaching IMPORT from the
        grid showed an empty drive list.
        """
        if page_name not in PAGES:
            return
        prev = PAGES[self.page] if self.active else None
        if prev == "IMPORT":
            self._usb_leave()          # release any mounted drive
        self.page   = PAGES.index(page_name)
        self.sel    = 0
        self.active = True
        self._cancel_edits()
        if page_name == "BROWSER":
            threading.Thread(
                target=self.inst.sampler.rescan_clips, daemon=True).start()
        elif page_name == "IMPORT":
            self._usb_enter()          # list removable drives

    # ───────────────────────────────────────────────────────── lifecycle
    def toggle(self):
        self.active = not self.active
        if self.active:
            # Opening: land on SETTINGS. Menu keys only ever drive the menu
            # (they never reach the perform handlers), but settings/params apply
            # live so their effect is visible while editing.
            self.page = PAGES.index("SETTINGS")
            self.sel  = 0
            self._pending_clip_idx = None
            self._pending_shader   = None
            threading.Thread(
                target=self.inst.sampler.rescan_clips, daemon=True).start()
        else:
            # Closing: release any mounted USB drive, then load any clip/shader
            # picked on the BROWSER/SHADERS pages (deferred so browsing never
            # yanks the live output).
            self._usb_leave()
            self.inst.apply_menu_selection(clip_idx=self._pending_clip_idx,
                                           gen_shader=self._pending_shader)
        self._cancel_edits()
        log.info("menu -> %s", "ON" if self.active else "OFF")

    # ───────────────────────────────────────────────────────── input
    def handle(self, name):
        """Route a logical key while the menu is active.

        Navigation: + = scroll up,  BKSP = scroll down (or, on BROWSER/PRESETS/
        MIDI, that page's delete/clear action).
        """
        try:
            # Any key other than BKSP cancels an armed BROWSER delete.
            if name != "BKSP":
                self._confirm_delete = False

            # Slot-assign: 4–9 assign the slot (7 and 9 are valid slots here),
            # any other key cancels.  Must be checked before page navigation so
            # pressing 7 or 9 during assignment does not skip pages.
            if self._assigning:
                self._handle_assign(name)
                return

            # MIDI numeric CC entry — 7 and 9 type digits, not navigate.
            if self._midi_editing:
                self._handle_midi_edit(name)
                return

            # SETTINGS value edit — +/BKSP step the highlighted row's value
            # rather than scrolling the list (checked before page navigation
            # so 7/9 can't page out mid-edit).
            if self._settings_editing:
                self._handle_settings_edit(name)
                return

            # Page navigation (only reached when no edit mode is active).
            # 7 / 9 cycle through all pages in each direction (wrapping).
            if name in ("7", "9"):
                step = -1 if name == "7" else +1
                self.open_page(PAGES[(self.page + step) % len(PAGES)])
                return

            # + = scroll up
            if name == "+":
                self._move(-1)
                return

            if name in ("8", "2"):
                self._move(-1 if name == "8" else +1)
            elif name in ("4", "6"):
                self._adjust(-1 if name == "4" else +1)
            elif name == "5":
                self._action_primary()
            elif name == "ENTER":
                self._action_enter()
            elif name == "BKSP":
                page = PAGES[self.page]
                if page == "MIDI":
                    self._midi_clear()
                elif page in ("BROWSER", "PRESETS"):
                    # First BKSP arms delete, second executes it.
                    if self._confirm_delete:
                        self._confirm_delete = False
                        if page == "BROWSER":
                            self._browser_delete()
                        else:
                            self._preset_delete()
                    else:
                        self._confirm_delete = True
                else:
                    self._move(+1)   # SETTINGS / SHADERS / IMPORT: scroll down
        except Exception as e:
            log.warning("menu handle %r: %s", name, e)

    def _cancel_edits(self):
        self._assigning        = False
        self._midi_editing     = False
        self._midi_input_buf   = ""
        self._settings_editing = False
        self._confirm_delete   = False

    def _rows(self):
        """Number of selectable rows on the current page."""
        page = PAGES[self.page]
        if page == "BROWSER":
            return max(1, len(self._browser_list()))
        if page == "SHADERS":
            return max(1, len(self._shader_list()))
        if page == "PRESETS":
            return max(1, len(self._presets_list()))
        if page == "MIDI":
            return len(MIDI_TARGETS)
        if page == "IMPORT":
            lst = self._usb_files if self._usb_dev else self._usb_drives
            return max(1, len(lst))
        # SETTINGS: param count is dynamic (depends on loaded shader), so compute live.
        return len(self._settings())

    def _move(self, d):
        """Move selection by d, clamped — no wrap-around."""
        self._cancel_edits()
        n = self._rows()
        self.sel = max(0, min(n - 1, self.sel + d))

    def _adjust(self, d):
        """4/6: adjust the selected value. 6 (d>0) on PRESETS saves a new preset."""
        page = PAGES[self.page]
        if page == "SETTINGS":
            items = self._settings()
            if 0 <= self.sel < len(items):
                items[self.sel].adjust(d)
        elif page == "MIDI":
            self._midi_adjust(d)
        elif page == "PRESETS" and d > 0:
            self._preset_save()
        # BROWSER / SHADERS: slot assignment is via ENTER + slot key, not 4/6

    def _action_primary(self):
        """5: load in BROWSER/SHADERS/PRESETS; select in SETTINGS; edit CC in MIDI."""
        page = PAGES[self.page]
        if page == "BROWSER":
            self._browser_load()
        elif page == "SHADERS":
            self._shader_browser_load()
        elif page == "PRESETS":
            self._preset_load()
        elif page == "SETTINGS":
            items = self._settings()
            if 0 <= self.sel < len(items):
                items[self.sel].select()
        elif page == "MIDI":
            self._midi_begin_edit()
        elif page == "IMPORT":
            self._usb_action()

    def _action_enter(self):
        """ENTER: enter slot-assign mode for BROWSER/SHADERS/PRESETS; eject
        IMPORT; on SETTINGS fire an action row or open value-edit mode."""
        page = PAGES[self.page]
        if page in ("BROWSER", "SHADERS", "PRESETS"):
            self._assigning = True
        elif page == "IMPORT":
            self._usb_eject()
        elif page == "SETTINGS":
            items = self._settings()
            if 0 <= self.sel < len(items):
                it = items[self.sel]
                if it.action:
                    it.select()               # SAVE PREFS / RESTART / QUIT
                else:
                    self._settings_editing = True
        else:
            self._action_primary()

    def _handle_settings_edit(self, name):
        """Process a keypress while a SETTINGS row is open for editing.

        +/BKSP (and 4/6) step the value; ENTER/5 close edit mode. Any other
        key also closes it, so no keypress is silently swallowed mid-edit.
        """
        if name == "+":
            self._adjust(+1)
        elif name == "BKSP":
            self._adjust(-1)
        elif name in ("4", "6"):
            self._adjust(-1 if name == "4" else +1)
        else:
            self._settings_editing = False

    def _handle_assign(self, name):
        """While in assign mode, press 4–9 to assign item to that performance slot."""
        page = PAGES[self.page]
        cfg  = self.inst.cfg
        if name in ("4", "5", "6", "7", "8", "9"):
            slot = int(name)
            if page == "BROWSER":
                clips = self.inst.sampler.clips
                if 0 <= self.sel < len(clips):
                    path = clips[self.sel]
                    for k in cfg.clip_slots:
                        if cfg.clip_slots[k] == path:
                            cfg.clip_slots[k] = None
                    cfg.clip_slots[slot] = path
                    log.info("clip slot %d → %s", slot, os.path.basename(path))
            elif page == "SHADERS":
                lst = self._shader_list()
                if 0 <= self.sel < len(lst):
                    shader = lst[self.sel]
                    for k in cfg.shader_slots:
                        if cfg.shader_slots[k] == shader:
                            cfg.shader_slots[k] = None
                    cfg.shader_slots[slot] = shader
                    log.info("shader slot %d → %s", slot, shader)
            elif page == "PRESETS":
                presets = self._presets_list()
                if 0 <= self.sel < len(presets):
                    preset = presets[self.sel]
                    for k in cfg.preset_slots:
                        if cfg.preset_slots[k] == preset:
                            cfg.preset_slots[k] = None
                    cfg.preset_slots[slot] = preset
                    log.info("preset slot %d → %s", slot, preset)
        # All keys exit assign mode (whether a valid slot was pressed or not)
        self._assigning = False

    # ───────────────────────────────────────────────────────── BROWSER
    def _browser_list(self):
        """Return basenames of all scanned clips."""
        return [os.path.basename(c) for c in self.inst.sampler.clips]

    def _browser_load(self):
        """Stage the selected clip; it loads to the live output on menu close."""
        clips = self.inst.sampler.clips
        if not clips or self.sel >= len(clips):
            return
        self._pending_clip_idx = self.sel
        log.info("staged clip %d → %s", self.sel,
                 os.path.basename(clips[self.sel]))

    def _browser_delete(self):
        """Delete the highlighted clip file from internal storage (BKSP×2).
        Only internal clips/ files are deletable — removable drives are mounted
        read-only and other paths are refused."""
        clips = self.inst.sampler.clips
        if not clips or self.sel >= len(clips):
            return
        path = clips[self.sel]
        clips_dir = os.path.abspath(self.inst.cfg.clips_dir)
        if os.path.abspath(path).startswith(clips_dir + os.sep):
            name = os.path.basename(path)
            try:
                os.remove(path)
                log.info("deleted clip %s", path)
                # forget it everywhere it was referenced
                for k, v in list(self.inst.cfg.clip_slots.items()):
                    if v == path:
                        self.inst.cfg.clip_slots[k] = None
                if self.inst.cfg.current_clip == path:
                    self.inst.cfg.current_clip = None
                if self._pending_clip_idx == self.sel:
                    self._pending_clip_idx = None
                self.inst.sampler.rescan_clips()
                self.sel = max(0, min(self.sel,
                                      len(self.inst.sampler.clips) - 1))
                self.inst.osd.show(f"DELETED {name[:18]}")
            except OSError as e:
                log.warning("delete %s failed: %s", path, e)
                self.inst.osd.show("DELETE FAILED")
        else:
            log.info("refusing to delete non-internal clip %s", path)
            self.inst.osd.show("CAN'T DELETE (not internal)")

    # ───────────────────────────────────────────────────────── SHADERS browser
    def _shader_list(self):
        """Return list of generative shader basenames."""
        return self.inst.shader.list_shaders(kind="generative")

    def _shader_browser_load(self):
        """Stage the selected shader; it loads to the live output on menu close."""
        lst = self._shader_list()
        if not lst or self.sel >= len(lst):
            return
        name = lst[self.sel]
        self._pending_shader = name
        self.inst.cfg.current_shader = name   # so the ▶ marker tracks the pick
        log.info("staged shader → %s", name)

    # ───────────────────────────────────────────────────────── PRESETS
    def _presets_list(self):
        """Sorted list of .json preset filenames from the presets directory."""
        d = self.inst.cfg.presets_dir
        if not os.path.isdir(d):
            return []
        return sorted(f for f in os.listdir(d) if f.endswith(".json"))

    def _preset_next_name(self):
        """Return the next available auto-increment name: P01.json, P02.json…"""
        existing = {f.upper() for f in self._presets_list()}
        for n in range(1, 100):
            name = f"P{n:02d}.json"
            if name.upper() not in existing:
                return name
        return "PRESET.json"

    def _preset_load(self):
        presets = self._presets_list()
        if not presets or self.sel >= len(presets):
            return
        name = presets[self.sel]
        data = self.inst.cfg.load_preset(name)
        if data:
            self.inst.apply_preset(data)
            self.inst.osd.show(f"PRESET: {name.replace('.json', '')}")
            log.info("preset loaded: %s", name)

    def _preset_save(self):
        name = self._preset_next_name()
        ok   = self.inst.cfg.save_preset(name)
        if ok:
            self.inst.osd.show(f"SAVED: {name.replace('.json', '')}")
            presets = self._presets_list()
            if name in presets:
                self.sel = presets.index(name)
        else:
            self.inst.osd.show("SAVE FAILED")

    def _preset_delete(self):
        presets = self._presets_list()
        if not presets or self.sel >= len(presets):
            return
        name = presets[self.sel]
        path = os.path.join(self.inst.cfg.presets_dir, name)
        cfg = self.inst.cfg
        for k in list(cfg.preset_slots):
            if cfg.preset_slots[k] == name:
                cfg.preset_slots[k] = None
        try:
            os.remove(path)
            self.inst.osd.show(f"DELETED {name[:18]}")
            self.sel = max(0, min(self.sel, len(self._presets_list()) - 1))
            log.info("preset deleted: %s", name)
        except OSError as e:
            log.warning("preset delete %s: %s", name, e)
            self.inst.osd.show("DELETE FAILED")

    # ───────────────────────────────────────────────────────── IMPORT (USB → internal)
    def _usb_enter(self):
        """Entering the IMPORT page: show the removable-drive list."""
        self._usb_dev = None
        self._usb_mp  = None
        self._usb_files = []
        self._usb_status = ""
        self.sel = 0
        self._usb_refresh_drives()

    def _usb_refresh_drives(self):
        mgr = getattr(self.inst, "usb", None)
        if not mgr or not mgr.available():
            self._usb_drives = []
            self._usb_status = "no mount permission (install pmount or use service)"
            return
        self._usb_drives = mgr.list_drives()
        if not self._usb_drives:
            self._usb_status = "no USB drives"

    def _usb_leave(self):
        """Leaving the IMPORT page (or closing the menu): release any drive."""
        self._usb_cancel.set()
        mgr = getattr(self.inst, "usb", None)
        if mgr:
            mgr.unmount_all()
        self._usb_dev = None
        self._usb_mp = None
        self._usb_files = []
        self._usb_status = ""

    def _usb_action(self):
        """5: in the drive list, mount the selected drive and list its videos;
        in the file list, copy the selected video to internal storage."""
        mgr = getattr(self.inst, "usb", None)
        if not mgr or not mgr.available():
            self._usb_status = "no mount permission (install pmount or use service)"
            return
        if self._usb_dev is None:
            if not self._usb_drives or self.sel >= len(self._usb_drives):
                return
            drive = self._usb_drives[self.sel]
            self._usb_status = "MOUNTING…"
            threading.Thread(target=self._usb_do_mount, args=(drive,),
                             daemon=True).start()
        else:
            if not self._usb_files or self.sel >= len(self._usb_files):
                return
            src = self._usb_files[self.sel]
            if self._usb_import_busy:
                self._usb_status = "import in progress"
                return
            self._usb_import_busy = True
            self._usb_cancel.clear()
            self._usb_status = "IMPORTING…"
            threading.Thread(target=self._usb_do_copy, args=(src,),
                             daemon=True).start()

    def _usb_do_mount(self, drive):
        mgr = self.inst.usb
        mp = mgr.mount(drive["dev"], drive.get("fstype"))
        if mp:
            self._usb_dev   = drive["dev"]
            self._usb_mp    = mp
            self._usb_files = mgr.scan_videos(mp)
            self.sel = 0
            self._usb_status = (f"{len(self._usb_files)} videos"
                                if self._usb_files else "no videos on drive")
        else:
            self._usb_status = "MOUNT FAILED"

    def _usb_do_copy(self, src):
        try:
            mgr = self.inst.usb

            def _progress(frac):
                pct = int(frac * 100)
                self._usb_status = f"CONVERTING… {pct}%"

            _, status = mgr.copy_to_internal(src, progress=_progress,
                                             cancel=self._usb_cancel)
            self._usb_status = {"copied":  "IMPORTED  ✓",
                                "exists":  "already imported",
                                "too_big": "TOO BIG — 4K MAX",
                                "error":   "IMPORT FAILED"}.get(status, status)
            if status == "copied":
                # make the new clip immediately playable / assignable
                threading.Thread(target=self.inst.sampler.rescan_clips,
                                 daemon=True).start()
        finally:
            self._usb_import_busy = False

    def _usb_eject(self):
        """ENTER (or '.') on the IMPORT page: unmount and go back to the drive
        list. Not BKSP — that scrolls the list, alongside +."""
        self._usb_cancel.set()
        mgr = getattr(self.inst, "usb", None)
        if self._usb_dev and mgr:
            mgr.unmount(self._usb_dev)
        self._usb_dev = None
        self._usb_mp = None
        self._usb_files = []
        self.sel = 0
        self._usb_status = "ejected"
        self._usb_refresh_drives()

    # ───────────────────────────────────────────────────────── MIDI
    def _midi_adjust(self, d):
        """4/6: step the user CC override by ±5, clamped 0–127.

        If no override exists, start from the built-in default (or 0/127).
        Use 5/ENTER to clear the override and return to the default.
        """
        if not (0 <= self.sel < len(MIDI_TARGETS)):
            return
        target   = MIDI_TARGETS[self.sel]
        user_map = self.inst.cfg.midi_target_cc
        cur = user_map.get(target)
        if cur is None:
            cur = MIDI_DEFAULTS.get(target) or (0 if d > 0 else 127)
        new = max(0, min(127, cur + d * 5))
        user_map[target] = new
        self.inst.midi.invalidate_cc_map()
        log.info("midi %s → CC %d", target, new)

    def _midi_clear(self):
        """Remove the user override for the selected target (use built-in default)."""
        if not (0 <= self.sel < len(MIDI_TARGETS)):
            return
        target = MIDI_TARGETS[self.sel]
        self.inst.cfg.midi_target_cc.pop(target, None)
        self.inst.midi.invalidate_cc_map()
        log.info("midi %s → default", target)

    def _midi_begin_edit(self):
        """5/ENTER: start numeric CC entry for the selected target."""
        if not (0 <= self.sel < len(MIDI_TARGETS)):
            return
        self._midi_editing   = True
        self._midi_input_buf = ""

    def _handle_midi_edit(self, name):
        """Process a keypress while numeric CC entry is active.

        Digits (0-9) build the value; 3 digits auto-commit if ≤ 127.
        ENTER confirms; empty ENTER clears the override (restores default).
        BKSP deletes the last digit.  Any other key cancels without saving.
        """
        if name.isdigit():
            self._midi_input_buf += name
            if len(self._midi_input_buf) == 3:
                val = int(self._midi_input_buf)
                if val <= 127:
                    self._midi_commit(val)
                else:
                    self._midi_commit(127)   # clamp, user sees result
        elif name in ("ENTER", "5"):
            buf = self._midi_input_buf
            if buf:
                self._midi_commit(max(0, min(127, int(buf))))
            else:
                self._midi_clear()           # empty ENTER = reset to default
                self._midi_editing   = False
                self._midi_input_buf = ""
        elif name == "BKSP":
            self._midi_input_buf = self._midi_input_buf[:-1]
        else:
            # Any navigation key cancels without saving
            self._midi_editing   = False
            self._midi_input_buf = ""

    def _midi_commit(self, val):
        if not (0 <= self.sel < len(MIDI_TARGETS)):
            return
        target = MIDI_TARGETS[self.sel]
        self.inst.cfg.midi_target_cc[target] = val
        self.inst.midi.invalidate_cc_map()
        self._midi_editing   = False
        self._midi_input_buf = ""
        log.info("midi %s → CC %d", target, val)

    # ───────────────────────────────────────────────────────── SETTINGS
    def _settings(self):
        inst, cfg = self.inst, self.inst.cfg

        def cyc(seq, cur, d):
            seq = list(seq)
            i = seq.index(cur) if cur in seq else 0
            return seq[(i + d) % len(seq)]

        items = []

        # ── PLAYBACK ──────────────────────────────────────────────────────
        items.append(_Item(
            "MODE", lambda: inst.mode,
            adjust=lambda d: inst.cycle_mode(d),
            select=lambda: inst.cycle_mode(+1), group="PLAYBACK"))
        items.append(_Item(
            "LIVE MODE",
            lambda: "ON" if getattr(cfg, 'live_mode_enabled', True) else "OFF",
            adjust=lambda d: setattr(cfg, 'live_mode_enabled', not cfg.live_mode_enabled),
            select=lambda: setattr(cfg, 'live_mode_enabled', not cfg.live_mode_enabled),
            group="PLAYBACK"))

        # sampler playback mode
        from engine.sampler import MODES as SMODES
        items.append(_Item(
            "PLAY", lambda: inst.sampler.mode,
            adjust=lambda d: inst.sampler.set_mode(cyc(SMODES, inst.sampler.mode, d)),
            select=lambda: inst.sampler.cycle_mode(), group="PLAYBACK"))

        # camera capture resolution — lower = less lag
        def _cam_res_label():
            return f"{cfg.camera_width}x{cfg.camera_height}"
        def _cam_res_cycle(d):
            presets = cfg.CAMERA_RESOLUTIONS
            cur = (cfg.camera_width, cfg.camera_height)
            i   = presets.index(cur) if cur in presets else 1
            cfg.camera_width, cfg.camera_height = presets[(i + d) % len(presets)]
        # ── VIDEO ─────────────────────────────────────────────────────────
        items.append(_Item(
            "CAM RES", _cam_res_label,
            adjust=lambda d: _cam_res_cycle(d),
            select=lambda: _cam_res_cycle(+1), group="VIDEO"))

        # video scaling for mismatched aspect ratios
        items.append(_Item(
            "VID SCALE", lambda: getattr(cfg, 'video_scale_mode', 'fit').upper(),
            adjust=lambda d: inst.video_scale_cycle(d),
            select=lambda: inst.video_scale_cycle(+1), group="VIDEO"))

        # ── MIX ───────────────────────────────────────────────────────────
        # overlay on/off (mode/opacity live on the BLEND param layer)
        items.append(_Item(
            "OVERLAY", lambda: "ON" if cfg.overlay_on else "OFF",
            adjust=lambda d: self._set_overlay(not cfg.overlay_on),
            select=lambda: self._set_overlay(not cfg.overlay_on), group="MIX"))

        # shader blend on/off (mode/amount live on the BLEND param layer)
        items.append(_Item(
            "BLEND", lambda: "ON" if cfg.shader_blend else "OFF",
            adjust=lambda d: inst.shader_blend_toggle(),
            select=lambda: inst.shader_blend_toggle(), group="MIX"))

        # What the shader blends against: the clip, or the live camera. The only
        # other control is the BLEND param layer's SRC row, and that layer is
        # unreachable (nothing assigns _param_layer = 3), which left the camera
        # unusable as a blend source. Cycling the source hot-swaps it (and
        # starts the camera), whereas shader_blend_toggle() deliberately won't —
        # enabling blend must never launch the camera by surprise (main.py).
        items.append(_Item(
            "BLEND SRC", lambda: cfg.shader_blend_source.upper(),
            adjust=lambda d: inst.shader_blend_source_cycle(),
            select=lambda: inst.shader_blend_source_cycle(),
            group="MIX"))

        # temporal echo on/off. The only control for it: the TRAIL param layer
        # is unreachable, and the 000 key it was bound to never fired.
        # trail_toggle() refuses in SHADER mode (no cross-frame GPU feedback on
        # the Pi 5 V3D driver), so the label says so rather than reading OFF.
        def _trail_label():
            if inst.mode == "SHADER":
                return "N/A"
            return "ON" if getattr(cfg, 'trail_on', False) else "OFF"
        items.append(_Item(
            "TRAIL", _trail_label,
            adjust=lambda d: inst.trail_toggle(),
            select=lambda: inst.trail_toggle(), group="MIX"))

        # shaders (params live on Bksp SHDR/FX layers; trail on Bksp TRAIL layer)
        def _fx_label():
            chain = getattr(cfg, "fx_chain", [])
            if not chain:
                return "—"
            slot  = getattr(cfg, "fx_edit_slot", 0)
            names = [f.replace(".glsl","").upper() for f in chain]
            tag   = f"[{slot+1}/{len(chain)}] " if len(chain) > 1 else ""
            return tag + (names[slot] if slot < len(names) else names[0])
        items.append(_Item(
            "FX", _fx_label,
            adjust=lambda d: inst.shader.cycle(d, kind="fx"),
            select=lambda: inst.shader.cycle(+1, kind="fx"), group="SHADERS"))
        def _gen_label():
            chain = getattr(cfg, "shader_chain", [])
            if not chain:
                return "—"
            slot  = getattr(cfg, "shader_edit_slot", 0)
            names = [s.replace(".glsl","").upper() for s in chain]
            tag   = f"[{slot+1}/{len(chain)}] " if len(chain) > 1 else ""
            return tag + (names[slot] if slot < len(names) else names[0])
        items.append(_Item(
            "GEN", _gen_label,
            adjust=lambda d: inst.shader.cycle(d, kind="generative"),
            select=lambda: inst.shader.cycle(+1, kind="generative"), group="SHADERS"))

        # ── LFO ───────────────────────────────────────────────────────────
        # The LFOs run on the GPU (engine/shader.py substitutes a recur_lfo()
        # call for a modulated param's literal), so every row here costs one
        # shader rewrite and nothing per frame. A param is bound by setting
        # "lfo_<key>" in its chain slot's params dict — no UI for that yet.
        def _lfo_touch():
            """Rebuild the shaders so a changed LFO takes effect."""
            inst.shader.reapply()

        def _lfo_tap():
            now  = time.monotonic()
            # Drop stale taps so an old session doesn't drag the average down.
            taps = [t for t in self._lfo_taps if now - t < 3.0] + [now]
            self._lfo_taps = taps[-4:]
            if len(self._lfo_taps) >= 2:
                spans = [b - a for a, b in zip(self._lfo_taps, self._lfo_taps[1:])]
                avg   = sum(spans) / len(spans)
                if avg > 0.05:
                    cfg.lfo_bpm = max(20.0, min(300.0, round(60.0 / avg, 1)))
                    _lfo_touch()
            inst.osd.show(f"BPM {cfg.lfo_bpm:.1f}  ({len(self._lfo_taps)} taps)")

        def _bpm_step(d):
            cfg.lfo_bpm = max(20.0, min(300.0, round(cfg.lfo_bpm + d, 1)))
            _lfo_touch()

        items.append(_Item(
            "BPM", lambda: f"{cfg.lfo_bpm:.1f}",
            adjust=_bpm_step, select=lambda: _bpm_step(+1), group="LFO"))
        items.append(_Item(
            "TAP", lambda: "tap ENTER x4",
            adjust=lambda d: _lfo_tap(), select=_lfo_tap,
            group="LFO", action=True))

        def _lfo_get(i):
            return cfg.lfos[i] if i < len(cfg.lfos) else {}

        def _shape_lbl(i):
            l = _lfo_get(i)
            return cfg.LFO_SHAPES[int(l.get("shape", 0)) % len(cfg.LFO_SHAPES)]

        def _shape_step(i, d):
            l = _lfo_get(i)
            l["shape"] = (int(l.get("shape", 0)) + (1 if d > 0 else -1)) % len(cfg.LFO_SHAPES)
            _lfo_touch()

        def _rate_lbl(i):
            l = _lfo_get(i)
            if l.get("bpm_sync"):
                beat = float(l.get("beat", 1.0))
                lbl  = (cfg.LFO_BEAT_LABELS[cfg.LFO_BEATS.index(beat)]
                        if beat in cfg.LFO_BEATS else f"{beat:g}")
                return f"{lbl} beat"
            return f"{float(l.get('period', 4.0)):.2f}s"

        def _rate_step(i, d):
            l = _lfo_get(i)
            if l.get("bpm_sync"):
                beats = list(cfg.LFO_BEATS)
                cur   = float(l.get("beat", 1.0))
                j     = beats.index(cur) if cur in beats else 3
                l["beat"] = beats[max(0, min(len(beats) - 1, j + (1 if d > 0 else -1)))]
            else:
                # Geometric steps: 0.1s and 30s are both useful, and a linear
                # nudge would take forever to cross that range.
                cur = float(l.get("period", 4.0))
                l["period"] = max(0.05, min(60.0, round(cur * (1.15 if d > 0 else 1 / 1.15), 3)))
            _lfo_touch()

        def _amt_step(i, key, d):
            l = _lfo_get(i)
            l[key] = max(0.0, min(1.0, round(float(l.get(key, 0.0)) + 0.05 * (1 if d > 0 else -1), 3)))
            _lfo_touch()

        def _sync_toggle(i):
            l = _lfo_get(i)
            l["bpm_sync"] = not l.get("bpm_sync", False)
            _lfo_touch()

        for i in range(len(cfg.lfos)):
            grp = f"LFO {i + 1}"
            items.append(_Item(
                "SHAPE", (lambda i=i: _shape_lbl(i)),
                adjust=(lambda d, i=i: _shape_step(i, d)),
                select=(lambda i=i: _shape_step(i, +1)), group=grp))
            items.append(_Item(
                "RATE", (lambda i=i: _rate_lbl(i)),
                adjust=(lambda d, i=i: _rate_step(i, d)),
                select=(lambda i=i: _rate_step(i, +1)), group=grp))
            items.append(_Item(
                "AMP", (lambda i=i: f"{float(_lfo_get(i).get('amp', 0.5)):.2f}"),
                adjust=(lambda d, i=i: _amt_step(i, "amp", d)),
                select=(lambda i=i: _amt_step(i, "amp", +1)), group=grp))
            items.append(_Item(
                "OFFSET", (lambda i=i: f"{float(_lfo_get(i).get('offset', 0.0)):.2f}"),
                adjust=(lambda d, i=i: _amt_step(i, "offset", d)),
                select=(lambda i=i: _amt_step(i, "offset", +1)), group=grp))
            items.append(_Item(
                "SYNC", (lambda i=i: "BPM" if _lfo_get(i).get("bpm_sync") else "SEC"),
                adjust=(lambda d, i=i: _sync_toggle(i)),
                select=(lambda i=i: _sync_toggle(i)), group=grp))

        # ── SYSTEM ────────────────────────────────────────────────────────
        # action=True rows fire on ENTER instead of opening value-edit mode.
        def _do_save():
            inst.cfg.save_prefs(sampler_mode=inst.sampler.mode)
            inst.osd.show("PREFS SAVED")
        items.append(_Item(
            "SAVE PREFS", lambda: "ENTER ■",
            adjust=lambda d: None,
            select=_do_save, group="SYSTEM", action=True))
        def _do_restart():
            inst.osd.show("RESTARTING…")
            inst.restart()
        items.append(_Item(
            "RESTART", lambda: "restart app ↺",
            adjust=lambda d: None,
            select=_do_restart, group="SYSTEM", action=True))
        # Quits the application (prefs auto-save in teardown).  A true Pi
        # poweroff would need root; the service user can't escalate.
        items.append(_Item(
            "QUIT", lambda: "quit app ■",
            adjust=lambda d: None,
            select=lambda: inst._shutdown(), group="SYSTEM", action=True))

        return items

    def _set_overlay(self, state):
        # overlay_toggle() itself blocks SHADER mode; no need to filter here
        if self.inst.cfg.overlay_on != state:
            self.inst.overlay_toggle()

    # ───────────────────────────────────────────────────────── rendering
    def render(self, img, draw, font_lg, font_md, font_sm, W, H, palette):
        """Draw the active menu page onto the PIL image."""
        C_BG, C_HL, C_LABEL, C_VALUE, C_DIM, C_ACCENT, C_EDIT = palette
        page = PAGES[self.page]

        # title bar — dark bg with bright green text, green border
        _hdr_bg = tuple(c // 8 for c in C_ACCENT)   # very dark tint of accent
        draw.rectangle([0, 0, W, 40], fill=_hdr_bg)
        draw.line([0, 40, W, 40], fill=C_ACCENT, width=1)
        draw.text((10, 6), f"MENU · {page}", font=font_md, fill=C_HL)
        draw.text((W - 10, 12), f"{self.page+1}/{len(PAGES)}",
                  font=font_sm, fill=C_LABEL, anchor="rm")

        if page == "BROWSER":
            if self._confirm_delete:
                draw.text((10, 44), "BKSP again = DELETE FILE   other = cancel",
                          font=font_sm, fill=C_HL)
            elif self._assigning:
                draw.text((10, 44), "press 4–9 to assign slot   other = cancel",
                          font=font_sm, fill=C_HL)
            else:
                draw.text((10, 44), "ENTER assign   5 pick   BKSP delete",
                          font=font_sm, fill=C_DIM)
            cfg            = self.inst.cfg
            clips_full     = self.inst.sampler.clips
            removable      = getattr(self.inst.sampler, 'removable_paths', set())
            slots          = getattr(cfg, 'clip_slots', {})
            path_to_slot   = {v: k for k, v in slots.items() if v}
            playing_path   = cfg.current_clip or ""
            rows = []
            for c in clips_full:
                name = os.path.basename(c)
                sn   = path_to_slot.get(c)
                is_playing = (c == playing_path)
                label = ("▶ " if is_playing else "  ") + name[:26]
                if sn is not None:
                    val = str(sn)
                elif c in removable:
                    val = _drive_label(c)
                else:
                    val = "–"
                rows.append((label, val))
            if not rows:
                rows = [("  (none)", "")]
            self._render_kv(draw, font_sm, W, H, palette, rows, y0=66)

        elif page == "SHADERS":
            if self._assigning:
                draw.text((10, 44), "press 4–9 to assign slot   other = cancel",
                          font=font_sm, fill=C_HL)
            else:
                draw.text((10, 44), "ENTER assign slot   5 pick   8/2 scroll",
                          font=font_sm, fill=C_DIM)
            cfg          = self.inst.cfg
            slots        = getattr(cfg, 'shader_slots', {})
            name_to_slot = {v: k for k, v in slots.items() if v}
            cur_shader   = cfg.current_shader or ""
            lst          = self._shader_list()
            rows = []
            for name in lst:
                sn    = name_to_slot.get(name)
                label = ("▶ " if name == cur_shader else "  ") + name.replace('.glsl', '')[:26]
                rows.append((label, str(sn) if sn is not None else "–"))
            if not rows:
                rows = [("  (none)", "")]
            self._render_kv(draw, font_sm, W, H, palette, rows, y0=66)

        elif page == "PRESETS":
            if self._confirm_delete:
                draw.text((10, 44), "BKSP again = DELETE   other = cancel",
                          font=font_sm, fill=C_HL)
            elif self._assigning:
                draw.text((10, 44), "press 4–9 to assign slot   other = cancel",
                          font=font_sm, fill=C_HL)
            else:
                draw.text((10, 44), "5 load  6 save new  ENTER assign slot  BKSP del",
                          font=font_sm, fill=C_DIM)
            cfg     = self.inst.cfg
            presets = self._presets_list()
            name_to_slot = {v: k for k, v in cfg.preset_slots.items() if v}
            rows = []
            for p in presets:
                sn = name_to_slot.get(p)
                rows.append(("  " + p.replace(".json", ""),
                             str(sn) if sn is not None else "–"))
            if not rows:
                rows = [("  (none)", "")]
            self._render_kv(draw, font_sm, W, H, palette, rows, y0=66)

        elif page == "MIDI":
            if self._midi_editing:
                draw.text((10, 44), "type 0–127   ENTER confirm   BKSP del",
                          font=font_sm, fill=C_HL)
            else:
                draw.text((10, 44), "5 edit CC   4/6 ±5   BKSP reset",
                          font=font_sm, fill=C_DIM)
            cfg      = self.inst.cfg
            user_map = getattr(cfg, 'midi_target_cc', {})
            rows_midi = []
            for target in MIDI_TARGETS:
                lbl      = MIDI_TARGET_LABELS.get(target, target)
                user_cc  = user_map.get(target)
                def_cc   = MIDI_DEFAULTS.get(target)
                if user_cc is not None:
                    val_str  = f"CC {user_cc}"
                    override = True
                elif def_cc is not None:
                    val_str  = f"[{def_cc}]"
                    override = False
                else:
                    val_str  = "---"
                    override = False
                rows_midi.append((lbl, val_str, override))
            line_h = 24
            y0     = 66
            avail  = (H - 22 - y0) // line_h
            top    = max(0, min(self.sel - avail // 2,
                                max(0, len(rows_midi) - avail)))
            for vi in range(min(avail, len(rows_midi) - top)):
                i   = top + vi
                y   = y0 + vi * line_h
                lbl, val_str, override = rows_midi[i]
                if i == self.sel:
                    draw.rectangle([4, y, W - 4, y + line_h - 2], fill=C_HL)
                lc  = C_BG if i == self.sel else C_LABEL
                vc  = C_BG if i == self.sel else (C_ACCENT if override else C_DIM)
                mid = y + line_h // 2
                draw.text((10,     mid), lbl[:18],    font=font_sm, fill=lc, anchor="lm")
                if i == self.sel and self._midi_editing:
                    entry_str = (self._midi_input_buf or "") + "_"
                    draw.text((W - 12, mid), entry_str, font=font_sm, fill=C_VALUE, anchor="rm")
                else:
                    draw.text((W - 12, mid), val_str[:12], font=font_sm, fill=vc, anchor="rm")

        elif page == "IMPORT":
            mgr = getattr(self.inst, "usb", None)
            if self._usb_dev:
                draw.text((10, 44), "5 copy to internal   ENTER eject   8/2 scroll",
                          font=font_sm, fill=C_DIM)
                rows = []
                for f in self._usb_files:
                    mark = "✓" if (mgr and mgr.is_internal(f)) else ""
                    rows.append((os.path.basename(f)[:30], mark))
                if not rows:
                    rows = [("  (no videos on drive)", "")]
            else:
                draw.text((10, 44), "5 mount & browse   8/2 scroll",
                          font=font_sm, fill=C_DIM)
                rows = [(f"  {d['label']}", f"{d['fstype']} {d['size']}")
                        for d in self._usb_drives]
                if not rows:
                    rows = [("  (no USB drives — plug one in)", "")]
            # list stops short so the status has its own line at the bottom
            self._render_kv(draw, font_sm, W, H - 24, palette, rows, y0=66)
            if self._usb_status:
                draw.text((W // 2, H - 32), self._usb_status[:36],
                          font=font_sm, fill=C_HL, anchor="mm")

        else:  # SETTINGS
            if self._settings_editing:
                draw.text((10, 44), "+/BKSP change value    ENTER done",
                          font=font_sm, fill=C_EDIT)
            else:
                draw.text((10, 44), f"ip  {_local_ip()}", font=font_sm, fill=C_DIM)
            self._render_settings_list(draw, font_sm, W, H, palette, y0=62)

        # footer hint
        draw.line([0, H - 22, W, H - 22], fill=C_DIM, width=1)
        if page == "SETTINGS":
            hint = ("+/BKSP change value   ENTER done"
                    if self._settings_editing else
                    "+/BKSP scroll   ENTER edit/do   7/9 page")
        else:
            hint = "+/BKSP scroll   4/6 adjust   5 ok   ENTER action   7/9 page"
        draw.text((W // 2, H - 11), hint, font=font_sm, fill=C_DIM, anchor="mm")

    def _render_settings_list(self, draw, font, W, H, palette, y0=62):
        """SETTINGS as a plain scrolling list, grouped under headers.

        `self.sel` indexes the ITEM list (headers aren't selectable), so the
        rows are built once here and the selection's visual row looked up to
        window the view around it.
        """
        C_BG, C_HL, C_LABEL, C_VALUE, C_DIM, C_ACCENT, C_EDIT = palette

        items = self._settings()
        rows  = []          # (kind, label, value, item_idx)
        group = None
        for i, it in enumerate(items):
            if it.group != group:
                rows.append(("hdr", it.group, "", -1))
                group = it.group
            rows.append(("item", it.label, str(it.value()), i))

        sel_row = next((r for r, (k, _l, _v, i) in enumerate(rows)
                        if k == "item" and i == self.sel), 0)

        line_h = 18
        avail  = max(1, (H - 22 - y0) // line_h)
        top    = max(0, min(sel_row - avail // 2, max(0, len(rows) - avail)))

        for vi in range(min(avail, len(rows) - top)):
            r    = top + vi
            y    = y0 + vi * line_h
            kind, label, value, idx = rows[r]
            mid  = y + line_h // 2

            if kind == "hdr":
                draw.text((8, mid), label[:20], font=font, fill=C_ACCENT, anchor="lm")
                ly = y + line_h - 2
                draw.line([8, ly, W - 8, ly], fill=C_DIM, width=1)
                continue

            selected = (idx == self.sel)
            editing  = selected and self._settings_editing
            if selected:
                draw.rectangle([4, y, W - 4, y + line_h - 2],
                               fill=C_EDIT if editing else C_HL)
                lc = vc = C_BG
            else:
                lc, vc = C_LABEL, C_VALUE
            # Arrows on the edited row show +/BKSP will now change the value.
            val = f"‹ {value[:16]} ›" if editing else value[:20]
            draw.text((18,     mid), label[:16], font=font, fill=lc, anchor="lm")
            draw.text((W - 12, mid), val,        font=font, fill=vc, anchor="rm")

    def _render_list(self, draw, font, W, H, palette, rows, y0=48):
        C_BG, C_HL, C_LABEL, C_VALUE, C_DIM, C_ACCENT, C_EDIT = palette
        line_h = 24
        avail  = (H - 22 - y0) // line_h
        top    = max(0, min(self.sel - avail // 2, max(0, len(rows) - avail)))
        for vi in range(min(avail, len(rows) - top)):
            i   = top + vi
            y   = y0 + vi * line_h
            label, marked = rows[i]
            if i == self.sel:
                draw.rectangle([4, y, W - 4, y + line_h - 2], fill=C_HL)
            col = C_BG if i == self.sel else (C_ACCENT if marked else C_LABEL)
            prefix = "▶ " if marked else "  "
            mid = y + line_h // 2
            draw.text((10, mid), f"{prefix}{str(label)[:40]}", font=font, fill=col, anchor="lm")

    def _render_kv(self, draw, font, W, H, palette, rows, y0=66):
        C_BG, C_HL, C_LABEL, C_VALUE, C_DIM, C_ACCENT, C_EDIT = palette
        line_h = 24
        avail  = (H - 22 - y0) // line_h
        top    = max(0, min(self.sel - avail // 2, max(0, len(rows) - avail)))
        for vi in range(min(avail, len(rows) - top)):
            i   = top + vi
            y   = y0 + vi * line_h
            label, value = rows[i]
            if i == self.sel:
                draw.rectangle([4, y, W - 4, y + line_h - 2], fill=C_HL)
            lc = C_BG if i == self.sel else C_LABEL
            vc = C_BG if i == self.sel else C_ACCENT
            mid = y + line_h // 2        # vertical centre of the row
            draw.text((10,      mid), str(label)[:18], font=font, fill=lc, anchor="lm")
            draw.text((W - 12,  mid), str(value)[:22], font=font, fill=vc, anchor="rm")


class _Item:
    """A single editable settings row.

    `group` is the header the row is filed under on the SETTINGS page — rows
    are rendered in list order and a header is drawn each time the group
    changes, so grouping lives here next to the row rather than as a separate
    table that has to be kept in step.

    `action` marks a row that *does* something rather than holding a value
    (SAVE PREFS / RESTART / QUIT): ENTER fires it straight away instead of
    entering value-edit mode.
    """
    __slots__ = ("label", "group", "action", "_value", "_adjust", "_select")

    def __init__(self, label, value, adjust, select, group="", action=False):
        self.label   = label
        self.group   = group
        self.action  = action
        self._value  = value
        self._adjust = adjust
        self._select = select

    def value(self):
        try:
            return self._value()
        except Exception:
            return "?"

    def adjust(self, d):
        try:
            self._adjust(d)
        except Exception as e:
            log.warning("adjust %s: %s", self.label, e)

    def select(self):
        try:
            self._select()
        except Exception as e:
            log.warning("select %s: %s", self.label, e)
