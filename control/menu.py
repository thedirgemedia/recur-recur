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
             0 = reset to built-in default

Navigation (logical key names, mapped in keyboard.py for both NumLock states).
These are the SAME on every page — no page redefines one, because a pad has no
key legends to read and a key that changes meaning per page can only be learned
by trial:
  + / BKSP   move selection up / down the list ("+" and "−")
  4 / 6      adjust selected value (left / right)
  5 / ENTER  select / activate (the "■" action)
  7 / 9      previous / next page (loops)
  0         delete / reset the highlighted row (BROWSER, MIDI)
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

# Order matters: the SETTINGS grid maps these to numpad keys positionally
# (7 8 9 / 4 5 6 / 1 2 3), and both control/display.py and control/keyboard.py
# import this tuple rather than keeping their own copy.
#
# Presets are NOT here: all three stores are grid screens on their own tabs
# (SHADER → SHADER PRESETS, FX → FX PRESETS, LIVE → PRESETS), reached with the
# tab key. They were menu list-pages until the grids replaced them.
PAGES = ("BROWSER", "SHADERS", "SETTINGS", "MIDI", "IMPORT", "INPUT")


# INPUT page — the actions a pad key can be bound to. `name` is exactly the
# logical key string control/keyboard._dispatch switches on, so binding a pad
# key to one of these makes it behave like that numpad key everywhere. Phase 1
# reproduces the existing controls; direct toggles come in Phase 2. The final
# "unbind" entry (name None) clears a key.
INPUT_ACTIONS = [
    ("SHADER TAB",   "NUM"),
    ("FX TAB",       "/"),
    ("SAMPLER TAB",  "*"),
    ("LIVE TAB",     "-"),
    ("SLOT 1", "1"), ("SLOT 2", "2"), ("SLOT 3", "3"),
    ("SLOT 4", "4"), ("SLOT 5", "5"), ("SLOT 6", "6"),
    ("SLOT 7", "7"), ("SLOT 8", "8"), ("SLOT 9", "9"),
    ("SCROLL UP  +",   "+"),
    ("SCROLL DOWN  −", "BKSP"),
    ("CONFIRM",        "ENTER"),
    ("BACK",           "."),
    ("STAGED / DELETE", "0"),
    ("SETTINGS TAB",   "SET"),
    ("— unbind —",     None),
]

# Controls the walk asks for but does NOT require. A 17-key numpad has no key
# to spare, so every mandatory step must fit on one; a 24-key pad has room for
# a dedicated SETTINGS key and is better for having one, because without it
# SETTINGS shares the BACK key and you can only reach it from the top level.
# Optional steps self-skip if nobody answers, and never count as a gap in
# FIX MISSING — an unbound optional control is a choice, not a fault.
OPTIONAL_ACTIONS = {"SET"}
# Labels describe what the control DOES, not which numpad key it came from:
# a macropad has blank keycaps, so "BKSP" or "( . )" names nothing the user can
# see. The internal action names are unchanged — they are the values stored in
# prefs.json keymaps, and renaming them would invalidate every calibrated pad.

# The calibration wizard walks these in order, asking for a key per action —
# the inverse of EDIT KEYS (key first, then pick its action). Action-first is
# the right way round for a first-run walk: it terminates, it covers every
# control exactly once, and it can't leave you with a half-playable pad because
# you forgot which keys you'd already done. EDIT KEYS stays key-first, which is
# the right way round for fixing one key later.
WIZARD_STEPS = [(lbl, nm) for lbl, nm in INPUT_ACTIONS if nm is not None]

# The controls without which the menus cannot be driven AT ALL. Scrolling has
# exactly one key each way, so those groups have exactly one member; CONFIRM
# still accepts either 5 or ENTER (see Menu._action_primary / _action_enter).
# A map that satisfies none of a group is a lockout, not a gap: you can open
# the menu and then not move through it, so FIX MISSING is unreachable and the
# only way back in is the panic hold, a power-cycle or editing prefs.json.
# That is what makes this worth checking at boot rather than leaving to the
# user to notice. Ordered worst-first for the message on the offer screen.
ESSENTIAL_GROUPS = (
    ("SCROLL DOWN", ("BKSP",)),
    ("SCROLL UP",   ("+",)),
    ("CONFIRM",     ("ENTER", "5")),
    ("BACK",        (".",)),
)

WIZARD_OFFER_TIMEOUT = 60.0   # dismiss an unanswered offer (re-offered on replug)
WIZARD_STEP_TIMEOUT  = 45.0   # abandon a walk nobody is answering
WIZARD_OPTIONAL_TIMEOUT = 12.0  # decline an optional step by waiting it out


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
        self._confirm_delete = False   # True after one BKSP in BROWSER (arm delete)
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
        # INPUT page (USB keyboard config).
        self._input_view   = "MENU"   # "MENU" (rows) | "DEVICES" | "LEARN" | "WIZARD"
        self._input_devs   = []       # cached keyboard list for the picker
        self._input_learn  = False    # True while ARMED to capture the next key-down
        self._learn_code   = None     # captured raw keycode awaiting an action pick
        self._learn_status = ""       # transient result line ("SLOT 7 ← key 96")
        self._learn_timer  = None     # arm-timeout, so an unused arm self-clears
        self._learn_dev    = None     # vid:pid to capture from (None = any device)
        self._learn_dev_seen = None   # vid:pid the last learned key came FROM
        # Calibration wizard (WIZARD view) — see _wizard_* below.
        self._wiz_phase    = ""       # "OFFER" | "WALK" | "DONE"
        self._wiz_pad      = None     # {'vidpid','name'} being calibrated
        self._wiz_step     = 0        # index into _wiz_steps
        self._wiz_bound    = 0        # keys actually bound this run
        self._wiz_steps    = WIZARD_STEPS   # the walk (all controls, or just gaps)
        self._wiz_title    = "CALIBRATE"
        self._wiz_promote  = True     # make the pad primary when the walk finishes
        self._wiz_reason   = ""       # why the offer appeared, shown on it

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
        self._reset_input()
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
        self._reset_input()
        log.info("menu -> %s", "ON" if self.active else "OFF")

    # ───────────────────────────────────────────────────────── input
    def handle(self, name):
        """Route a logical key while the menu is active.

        Navigation is the SAME on every page: + scrolls up, BKSP (the '−' of
        the pair) scrolls down. Nothing else scrolls, and no page takes those
        keys over for its own action — BROWSER's delete and MIDI's reset live
        on 0 for exactly that reason. A pad has no key legends to read, so a
        key that means different things on different pages can only be learned
        by trial.
        """
        try:
            # Any key other than 0 cancels an armed BROWSER delete.
            if name != "0":
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

            # INPUT sub-views (DEVICES picker / LEARN) own their keys entirely —
            # checked before page navigation so 7/9 can't page out mid-config.
            if PAGES[self.page] == "INPUT" and self._input_view != "MENU":
                self._handle_input_sub(name)
                return

            # Page navigation (only reached when no edit mode is active).
            # 7 / 9 cycle through all pages in each direction (wrapping).
            if name in ("7", "9"):
                step = -1 if name == "7" else +1
                self.open_page(PAGES[(self.page + step) % len(PAGES)])
                return

            # +/BKSP = scroll up/down. Unconditional, on every page.
            if name == "+":
                self._move(-1)
                return
            if name == "BKSP":
                self._move(+1)
                return

            if name in ("4", "6"):
                self._adjust(-1 if name == "4" else +1)
            elif name == "5":
                self._action_primary()
            elif name == "ENTER":
                self._action_enter()
            elif name == "0":
                # The destructive row action, on the one key no page uses for
                # anything else. Kept off the scroll keys so that scrolling is
                # never the thing that deletes a clip.
                page = PAGES[self.page]
                if page == "MIDI":
                    self._midi_clear()
                elif page == "BROWSER":
                    # First press arms delete, second executes it.
                    if self._confirm_delete:
                        self._confirm_delete = False
                        self._browser_delete()
                    else:
                        self._confirm_delete = True
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
        if page == "MIDI":
            return len(MIDI_TARGETS)
        if page == "IMPORT":
            lst = self._usb_files if self._usb_dev else self._usb_drives
            return max(1, len(lst))
        if page == "INPUT":
            if self._input_view == "DEVICES":
                return max(1, len(self._input_devs))
            if self._input_view == "LEARN":
                return len(INPUT_ACTIONS)
            if self._input_view == "WIZARD":
                return 1          # no list — the walk owns the screen
            return len(self._input_menu_items())
        # SETTINGS: param count is dynamic (depends on loaded shader), so compute live.
        return len(self._settings())

    def _move(self, d):
        """Move selection by d, clamped — no wrap-around."""
        self._cancel_edits()
        n = self._rows()
        self.sel = max(0, min(n - 1, self.sel + d))

    def _adjust(self, d):
        """4/6: adjust the selected value."""
        page = PAGES[self.page]
        if page == "SETTINGS":
            items = self._settings()
            if 0 <= self.sel < len(items):
                items[self.sel].adjust(d)
        elif page == "MIDI":
            self._midi_adjust(d)
        elif page == "INPUT" and self._input_view == "MENU":
            self._input_adjust(d)
        # BROWSER / SHADERS: slot assignment is via ENTER + slot key, not 4/6

    def _action_primary(self):
        """5: load in BROWSER/SHADERS; select in SETTINGS; edit CC in MIDI."""
        page = PAGES[self.page]
        if page == "BROWSER":
            self._browser_load()
        elif page == "SHADERS":
            self._shader_browser_load()
        elif page == "SETTINGS":
            items = self._settings()
            if 0 <= self.sel < len(items):
                items[self.sel].select()
        elif page == "MIDI":
            self._midi_begin_edit()
        elif page == "IMPORT":
            self._usb_action()
        elif page == "INPUT":
            self._input_activate_row()

    def _action_enter(self):
        """ENTER: enter slot-assign mode for BROWSER/SHADERS; eject
        IMPORT; on SETTINGS fire an action row or open value-edit mode."""
        page = PAGES[self.page]
        if page in ("BROWSER", "SHADERS"):
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

        # video scaling for mismatched aspect ratios (global). Per-clip
        # rotation / zoom / speed / trail live on the clip's own settings
        # screen — long-press a clip on the SAMPLER grid.
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
            inst.osd.show("RESTART → DEFAULT")
            inst.restart(resume=False)
        items.append(_Item(
            "RESTART", lambda: "→ default ↺",
            adjust=lambda d: None,
            select=_do_restart, group="SYSTEM", action=True))
        def _do_restart_same():
            inst.osd.show("RESTART → SAME STATE")
            inst.restart(resume=True)
        items.append(_Item(
            "RESTART SAME", lambda: "keep state ↺",
            adjust=lambda d: None,
            select=_do_restart_same, group="SYSTEM", action=True))
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

    # ───────────────────────────────────────────────────────── INPUT page
    # A USB keyboard config surface: pick the primary controller, set the pad's
    # used grid, and press-to-learn each key to an action. The pad is a
    # programmable macropad emitting arbitrary codes, so binding is by capturing
    # a real key press (learn_key, fed raw by control/keyboard._handle_event)
    # rather than assuming numpad keycodes. Three sub-views on self._input_view:
    # MENU (the rows below) → DEVICES (primary picker) / LEARN (key→action).

    def _reset_input(self):
        """Drop any INPUT sub-view back to the row list and disarm learning.
        Called on every page open/close so learn mode never leaks."""
        self._input_view  = "MENU"
        self._input_learn = False
        self._learn_code  = None
        self._learn_dev   = None
        self._wiz_phase   = ""
        self._wiz_pad     = None
        self._wiz_step    = 0
        self._wiz_steps   = WIZARD_STEPS
        self._wiz_title   = "CALIBRATE"
        self._wiz_promote = True
        self._wiz_reason  = ""
        self._cancel_learn_timer()

    def _cancel_learn_timer(self):
        t = self._learn_timer
        self._learn_timer = None
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass

    def _save_prefs(self):
        """Persist immediately after an INPUT change (device / keymap / grid) so
        a learned mapping survives a restart without a manual SAVE PREFS."""
        try:
            self.inst.cfg.save_prefs(sampler_mode=self.inst.sampler.mode)
        except Exception as e:
            log.warning("input save_prefs: %s", e)

    def _missing_actions(self):
        """Controls with NO key bound to them, in WIZARD_STEPS order.

        A keymap is a code→action dict, so nothing stops a rebind from leaving
        a control with no key at all — and a missing BKSP or '.' is crippling
        precisely because it's the key you'd navigate with to go fix it. Only
        meaningful once a keymap exists: with none, the built-in numpad map is
        in force and nothing is missing.
        """
        km = self.inst.cfg.keymap
        if not km:
            return []
        bound = set(km.values())
        return [(lbl, nm) for lbl, nm in WIZARD_STEPS
                if nm not in bound and nm not in OPTIONAL_ACTIONS]

    def _duplicate_actions(self):
        """action name -> how many keys hold it, for actions on more than one.

        EDIT KEYS is key-first, so binding a key can leave an action on two
        keys without anything saying so. The wizard now prevents this, but a
        map made before that (or edited deliberately) can still carry one.
        """
        km = self.inst.cfg.keymap
        counts = {}
        for v in km.values():
            counts[v] = counts.get(v, 0) + 1
        return {a: c for a, c in counts.items() if c > 1}

    def _unnavigable(self):
        """Labels of the ESSENTIAL_GROUPS with nothing bound — i.e. the ways in
        which this keymap is a lockout rather than merely incomplete.

        Empty with no keymap at all: the built-in numpad DEFAULT_MAP is in force
        then (see control/keyboard._resolve) and satisfies every group.
        """
        km = self.inst.cfg.keymap
        if not km:
            return []
        bound = set(km.values())
        return [lbl for lbl, alts in ESSENTIAL_GROUPS
                if not any(a in bound for a in alts)]

    def _input_menu_items(self):
        """(id, label, value) for the INPUT row list."""
        cfg  = self.inst.cfg
        gaps = self._missing_actions()
        return [
            ("device", "PRIMARY DEVICE", cfg.input_primary or "auto (any)"),
            ("cols",   "COLS",           str(cfg.pad_cols)),
            ("rows",   "ROWS",           str(cfg.pad_rows)),
            ("wizard", "CALIBRATE PAD",  f"{len(WIZARD_STEPS)} steps ■"),
            ("gaps",   "FIX MISSING",    (f"{len(gaps)} unbound ■" if gaps
                                          else "all bound")),
            ("edit",   "EDIT KEYS",      f"{len(cfg.keymap)} mapped ■"),
            ("clear",  "CLEAR MAP",      "reset ■"),
        ]

    def _wizard_target(self):
        """Which device a walk should capture from: a recognized pad if one is
        attached, else the current primary (so the walk also works for an
        ordinary keyboard you want to re-map wholesale), else the device the
        existing keymap was learned on."""
        cfg = self.inst.cfg
        pad = self.inst.kb.detect_pad()
        if pad is not None:
            return pad
        for vp in (cfg.input_primary, cfg.keymap_dev):
            if vp:
                match = next((d for d in self.inst.kb.list_keyboards()
                              if d["vidpid"] == vp), None)
                if match:
                    return match
        return None

    def _input_adjust(self, d):
        """4/6 on the INPUT row list: step COLS / ROWS (1..12)."""
        items = self._input_menu_items()
        if not (0 <= self.sel < len(items)):
            return
        rid = items[self.sel][0]
        cfg = self.inst.cfg
        if rid == "cols":
            cfg.pad_cols = max(1, min(12, cfg.pad_cols + d))
            self._save_prefs()
        elif rid == "rows":
            cfg.pad_rows = max(1, min(12, cfg.pad_rows + d))
            self._save_prefs()

    def _input_activate_row(self):
        """5 / ENTER on the INPUT row list."""
        items = self._input_menu_items()
        if not (0 <= self.sel < len(items)):
            return
        rid = items[self.sel][0]
        if rid == "device":
            self._input_devs = [{"vidpid": "", "name": "AUTO (any keyboard)"}]
            self._input_devs += self.inst.kb.list_keyboards()
            self._input_view = "DEVICES"
            self.sel = 0
        elif rid == "wizard":
            pad = self._wizard_target()
            if pad is None:
                self.inst.osd.show("NO PAD DETECTED — set PRIMARY first")
                return
            self.start_calibration(pad, offer=False)
        elif rid == "gaps":
            # A short wizard over only the unbound controls. Press-only, so it
            # works even when the missing control is the one you'd need to
            # scroll the EDIT KEYS list with.
            gaps = self._missing_actions()
            if not gaps:
                self.inst.osd.show("EVERY CONTROL HAS A KEY")
                return
            pad = self._wizard_target()
            if pad is None:
                self.inst.osd.show("NO PAD DETECTED — set PRIMARY first")
                return
            self.start_calibration(pad, offer=False, steps=gaps,
                                   title="FIX MISSING", promote=False)
        elif rid == "edit":
            self._input_view = "LEARN"
            self.sel = 0
            self._learn_status = ""
            self._arm_learn()
        elif rid == "clear":
            # Also drop the ownership record, so an attached pad is offered the
            # calibration walk again on the next boot.
            self.inst.cfg.keymap     = {}
            self.inst.cfg.keymap_dev = ""
            self._save_prefs()
            self.inst.osd.show("KEYMAP CLEARED")
        # cols/rows are adjusted with 4/6, not activated

    # ── DEVICES picker ──────────────────────────────────────────────────────
    def _input_pick_device(self):
        devs = self._input_devs
        if 0 <= self.sel < len(devs):
            vp = devs[self.sel]["vidpid"]
            self.inst.cfg.input_primary = vp
            self._save_prefs()
            self.inst.osd.show(f"PRIMARY: {devs[self.sel]['name'][:18]}")
        self._input_view = "MENU"
        self.sel = 0

    # ── LEARN (press-to-learn) ──────────────────────────────────────────────
    def _arm_learn(self, dev=None, timeout=8.0):
        """Arm capture of the next physical key-down.

        `dev` restricts capture to one vid:pid (the wizard, so a second
        keyboard can still drive the menu); None captures from any keyboard.
        A timeout self-clears an unused arm — the only escape when capture is
        unrestricted, since then every key-down is consumed for binding (see
        control/keyboard._handle_event).
        """
        self._input_learn = True
        self._learn_code  = None
        self._learn_dev   = dev
        self._cancel_learn_timer()
        self._learn_timer = threading.Timer(timeout, self._learn_timeout)
        self._learn_timer.daemon = True
        self._learn_timer.start()

    def _learn_timeout(self):
        # Fired off-thread; only act if still armed on an INPUT capture view.
        if not (self.active and self._input_learn):
            return
        if self._input_view == "WIZARD":
            if self._wiz_phase == "OFFER":
                self._wizard_close("")              # nobody answered — just go
            elif self._wiz_optional():
                # An optional step is how a pad with no spare key says "not
                # this one" — waiting is the skip, since from the pad itself
                # every key-down would bind rather than pass.
                self._wizard_advance(skip=True)
            else:
                self._wizard_abort("CALIBRATION: timed out")
        elif self._input_view == "LEARN":
            self._reset_input()
            try:
                self.inst.osd.show("LEARN: timed out")
            except Exception:
                pass

    def learn_key(self, code, vidpid=None):
        """Called by the keyboard thread with the raw keycode of the pressed
        pad key while armed (and the device it came from). In the wizard the
        key binds straight to the step being asked for; in EDIT KEYS it
        switches to picking an action."""
        self._cancel_learn_timer()
        if self._input_view == "WIZARD":
            self._wizard_key(code)
            return
        self._learn_dev_seen = vidpid
        self._learn_code  = code
        self._input_learn = False        # now pick an action for this key
        self.sel          = 0
        self._learn_status = f"key {code}: pick action"

    def _input_bind_action(self):
        """ENTER on the action list: bind (or unbind) the captured key, then
        re-arm for the next key."""
        if self._learn_code is None:
            return
        label, name = INPUT_ACTIONS[self.sel]
        km  = self.inst.cfg.keymap
        key = str(self._learn_code)
        if name is None:
            km.pop(key, None)
            self._learn_status = f"unbound key {self._learn_code}"
        else:
            km[key] = name
            self._learn_status = f"{label} ← key {self._learn_code}"
            # The map now belongs to whichever device taught this key — so a
            # pad tweaked here is not re-offered the wizard, and a key learned
            # from a different keyboard moves ownership honestly.
            if self._learn_dev_seen:
                self.inst.cfg.keymap_dev = self._learn_dev_seen
        self._save_prefs()
        self._learn_code = None
        self._arm_learn()               # ready for the next key

    # ── WIZARD (first-run pad calibration) ──────────────────────────────────
    # An unmapped macropad is a chicken-and-egg problem: it can't drive the
    # instrument, and it can't be configured by its own keys either, so on a rig
    # with nothing else plugged in there is no way in. The wizard is the way in
    # — the one interaction an unmapped pad CAN perform is "a key went down", so
    # that alone accepts the offer and answers every step of the walk. It ends
    # by making the pad primary, which is only safe to do once it's mapped (a
    # primary with an empty keymap is exactly the lockout this avoids).

    def maybe_offer_calibration(self):
        """Offer the walk when a recognized pad is attached and the stored
        keymap wasn't learned on THAT pad. Called at boot (main.run) and again
        whenever a pad is hotplugged (control/keyboard._rescan) — so a missed
        or timed-out offer is always one unplug/replug away, which matters on a
        rig where the unmapped pad is the ONLY input device.

        Keyed on cfg.keymap_dev, not on "is the keymap empty": a couple of keys
        learned from some other keyboard must not count as a configured pad,
        and swapping in a different pad should offer to calibrate the new one.
        Once the walk completes, keymap_dev is the pad, so it never fires again
        (input prefs load on every boot, so this survives power-cuts).

        ALSO offered — regardless of keymap_dev — when the map is a lockout by
        _unnavigable()'s definition. A pad you calibrated yourself and then
        broke in EDIT KEYS is 'calibrated' as far as keymap_dev is concerned, so
        without this the one recovery that needs no working keys would never
        appear for the case most likely to need it. This is why the offer is
        answerable by any key-down: it is the whole recovery path, and a
        power-cycle is a gesture that survives any keymap.
        """
        try:
            cfg = self.inst.cfg
            # Never hijack a menu the user is already using (and never restart
            # an offer/walk that's already on screen — a pad presents two nodes,
            # so a single replug calls this twice).
            if self.active:
                return
            stranded = self._unnavigable()
            pad = self.inst.kb.detect_pad()
            if pad is None and stranded:
                # A lockout is a property of the map, not of the pad, so fall
                # back to whatever device that map was learned on / drives play.
                pad = self._wizard_target()
            if pad is None:
                return
            calibrated = bool(cfg.keymap) and cfg.keymap_dev == pad["vidpid"]
            if calibrated and not stranded:
                return
            if stranded:
                reason = "NO KEY FOR: " + ", ".join(stranded)
                log.warning("keymap is a lockout on %s %s (%s) — offering "
                            "calibration", pad["vidpid"], pad["name"],
                            ", ".join(stranded))
            else:
                reason = "NOT MAPPED YET"
                log.info("pad detected with no keymap: %s %s — offering "
                         "calibration", pad["vidpid"], pad["name"])
            self.start_calibration(pad, offer=True, reason=reason)
        except Exception as e:
            log.warning("calibration offer: %s", e)

    def start_calibration(self, pad, offer=False, steps=None,
                          title="CALIBRATE", promote=True,
                          reason="NOT MAPPED YET"):
        """Open the INPUT page on the wizard. offer=True asks first (the boot
        path); False starts the walk immediately (the CALIBRATE PAD row).

        `steps` defaults to every control; FIX MISSING passes just the unbound
        ones and promote=False, since repairing a gap shouldn't quietly change
        which device is primary. `reason` is the line the OFFER screen shows —
        an unmapped pad and a pad whose map locked you out need the same walk
        but are not the same situation, and only one of them is your own doing."""
        self._wiz_pad     = pad
        self._wiz_step    = 0
        self._wiz_bound   = 0
        self._wiz_steps   = steps if steps else WIZARD_STEPS
        self._wiz_title   = title
        self._wiz_promote = promote
        self._wiz_reason  = reason
        self._learn_status = ""
        self.page   = PAGES.index("INPUT")
        self.sel    = 0
        self._pending_clip_idx = None
        self._pending_shader   = None
        self._cancel_edits()
        self.active      = True
        self._input_view = "WIZARD"
        if offer:
            self._wiz_phase = "OFFER"
            self._arm_learn(dev=pad["vidpid"], timeout=WIZARD_OFFER_TIMEOUT)
        else:
            self._wizard_begin()

    def _wizard_begin(self):
        self._wiz_phase = "WALK"
        self._wiz_step  = 0
        self._wiz_bound = 0
        self._learn_status = ""
        self._arm_learn(dev=self._wiz_pad["vidpid"],
                        timeout=(WIZARD_OPTIONAL_TIMEOUT if self._wiz_optional()
                                 else WIZARD_STEP_TIMEOUT))

    def _wizard_key(self, code):
        """A key-down from the pad being calibrated (routed by learn_key)."""
        if self._wiz_phase == "OFFER":
            self._wizard_begin()
            return
        if self._wiz_phase != "WALK":
            return
        label, name = self._wiz_steps[self._wiz_step]
        km   = self.inst.cfg.keymap
        key  = str(code)
        prev = km.get(key)
        km[key] = name
        # ONE KEY PER ACTION. The walk asks for each action exactly once, so a
        # key that held this action before is by definition superseded — drop
        # it. Without this a stray binding is permanent: it survives every
        # subsequent walk, because each walk only ever ADDS the key you press.
        # That is how one action ended up on two keys and stayed there.
        stale = [k for k, v in km.items() if v == name and k != key]
        for k in stale:
            km.pop(k, None)
        # Reusing a key you already answered with silently drops the earlier
        # action, so say which one — the walk can't detect the intent, but the
        # user can see it and fix it with EDIT KEYS afterwards.
        self._learn_status = (f"{label} ← key {code}" if prev in (None, name)
                              else f"{label} ← key {code}  (took it from {prev})")
        if stale:
            self._learn_status += f"  (freed {', '.join(sorted(stale))})"
        self._wiz_bound += 1
        self._save_prefs()
        self._wizard_advance()

    def _wiz_optional(self, step=None):
        """Is the step being asked for one of OPTIONAL_ACTIONS?"""
        i = self._wiz_step if step is None else step
        if not (0 <= i < len(self._wiz_steps)):
            return False
        return self._wiz_steps[i][1] in OPTIONAL_ACTIONS

    def _wizard_advance(self, skip=False):
        if skip:
            self._learn_status = f"skipped {self._wiz_steps[self._wiz_step][0]}"
        self._wiz_step += 1
        if self._wiz_step >= len(self._wiz_steps):
            self._wizard_done()
        else:
            # Optional steps wait a shorter time, because waiting them out is
            # the normal way to decline one rather than an error.
            self._arm_learn(dev=self._wiz_pad["vidpid"],
                            timeout=(WIZARD_OPTIONAL_TIMEOUT
                                     if self._wiz_optional()
                                     else WIZARD_STEP_TIMEOUT))

    def _wizard_done(self):
        """Walk complete: the pad is mapped, so make it the primary device —
        now that it can actually drive everything, restricting play to it is
        what the user wanted all along."""
        cfg = self.inst.cfg
        self._input_learn = False
        self._learn_dev   = None
        self._cancel_learn_timer()
        if self._wiz_pad:
            cfg.keymap_dev = self._wiz_pad["vidpid"]
            if self._wiz_promote:
                cfg.input_primary = self._wiz_pad["vidpid"]
        self._wiz_phase = "DONE"
        self._save_prefs()
        log.info("pad calibrated: %s — %d keys mapped, now primary",
                 (self._wiz_pad or {}).get("name", "?"), len(cfg.keymap))

    def _wizard_abort(self, msg="CALIBRATION CANCELLED"):
        """Leave the walk early, KEEPING whatever was bound so far (a partial
        map is still useful, and EDIT KEYS can finish it). The primary is left
        alone — promoting a half-mapped pad is the lockout we're avoiding."""
        bound = self._wiz_bound
        self._wizard_close(f"{msg} ({bound} bound)" if bound else msg)

    def _wizard_close(self, msg=""):
        """Drop the wizard and close the menu (it opened itself, so leaving it
        open on the row list would be a surprise)."""
        self._reset_input()
        self.sel = 0
        if msg:
            try:
                self.inst.osd.show(msg)
            except Exception:
                pass
        if self.active:
            self.toggle()

    def _handle_wizard(self, name):
        """Keys reaching the wizard from a NON-pad keyboard (the pad's own
        key-downs are consumed by learn_key). This is the second-keyboard
        escape hatch: skip a step, or stop the walk."""
        if self._wiz_phase == "OFFER":
            if name in ("5", "ENTER"):
                self._wizard_begin()
            elif name in ("BKSP", "7", "9", "0"):
                self._wizard_close("")
            return
        if self._wiz_phase == "DONE":
            self._wizard_close("")
            return
        if name == "BKSP":
            self._wizard_advance(skip=True)
        elif name in ("5", "ENTER"):
            self._wizard_abort()

    def _input_back(self):
        """'.' while in an INPUT sub-view (see control/keyboard._dot_press).
        From picking an action → back to armed; otherwise → the row list."""
        if self._input_view == "WIZARD":
            if self._wiz_phase == "WALK":
                self._wizard_abort()
            else:
                self._wizard_close("")
            return
        if self._input_view == "LEARN" and self._learn_code is not None:
            self._learn_code = None
            self._arm_learn()
            return
        self._reset_input()
        self.sel = 0

    def _handle_input_sub(self, name):
        """Keys while a DEVICES / LEARN sub-view is open (routed from handle()).
        7/9 do NOT page here — the sub-view owns every key."""
        view = self._input_view
        if view == "WIZARD":
            self._handle_wizard(name)
            return
        if view == "DEVICES":
            if name == "+":
                self._move(-1)
            elif name == "BKSP":
                self._move(+1)
            elif name in ("5", "ENTER"):
                self._input_pick_device()
            return
        if view == "LEARN":
            # Armed: keydowns are consumed by the keyboard thread (learn_key),
            # so a key reaching here is a key_up or a non-mapping key — ignore.
            if self._learn_code is None:
                return
            if name == "+":
                self._move(-1)
            elif name == "BKSP":
                self._move(+1)
            elif name in ("5", "ENTER"):
                self._input_bind_action()
            return

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
                draw.text((10, 44), "0 again = DELETE FILE   other = cancel",
                          font=font_sm, fill=C_HL)
            elif self._assigning:
                draw.text((10, 44), "press 4–9 to assign slot   other = cancel",
                          font=font_sm, fill=C_HL)
            else:
                draw.text((10, 44), "ENTER assign   5 pick   0 delete",
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
                draw.text((10, 44), "ENTER assign slot   5 pick",
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

        elif page == "MIDI":
            if self._midi_editing:
                draw.text((10, 44), "type 0–127   ENTER confirm   − del",
                          font=font_sm, fill=C_HL)
            else:
                draw.text((10, 44), "5 edit CC   4/6 ±5   0 reset",
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
                draw.text((10, 44), "5 copy to internal   ENTER eject",
                          font=font_sm, fill=C_DIM)
                rows = []
                for f in self._usb_files:
                    mark = "✓" if (mgr and mgr.is_internal(f)) else ""
                    rows.append((os.path.basename(f)[:30], mark))
                if not rows:
                    rows = [("  (no videos on drive)", "")]
            else:
                draw.text((10, 44), "5 mount & browse",
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

        elif page == "INPUT":
            cfg  = self.inst.cfg
            view = self._input_view
            if view == "WIZARD":
                self._render_wizard(draw, font_lg, font_md, font_sm,
                                    W, H, palette)
            elif view == "DEVICES":
                draw.text((10, 44), "5/ENTER select   . back",
                          font=font_sm, fill=C_DIM)
                rows = []
                for dv in self._input_devs:
                    mark = "▸ " if dv["vidpid"] == cfg.input_primary else "  "
                    rows.append((mark + dv["name"][:22], dv["vidpid"] or "auto"))
                if not rows:
                    rows = [("  (no keyboards)", "")]
                self._render_kv(draw, font_sm, W, H, palette, rows, y0=66)
            elif view == "LEARN":
                if self._learn_code is None:
                    draw.text((10, 44), "PRESS A KEY ON THE PAD   (wait = exit)",
                              font=font_sm, fill=C_HL)
                else:
                    draw.text((10, 44),
                              f"key {self._learn_code}: pick action  ENTER bind",
                              font=font_sm, fill=C_EDIT)
                cur  = (cfg.keymap.get(str(self._learn_code))
                        if self._learn_code is not None else None)
                # Flag controls with NO key, and controls sitting on MORE than
                # one, so both kinds of fault are visible at the moment you're
                # choosing what to bind.
                gaps = {nm for _l, nm in self._missing_actions()}
                dups = self._duplicate_actions()
                def _flag(nm):
                    if nm in gaps:
                        return "   · no key"
                    if nm in dups:
                        return f"   · {dups[nm]} keys"
                    return ""
                rows = [(lbl + _flag(nm), nm == cur) for lbl, nm in INPUT_ACTIONS]
                self._render_list(draw, font_sm, W, H - 24, palette, rows, y0=64)
                if self._learn_status:
                    draw.text((W // 2, H - 32), self._learn_status[:40],
                              font=font_sm, fill=C_ACCENT, anchor="mm")
            else:  # MENU
                draw.text((10, 44), "5/ENTER open   4/6 cols·rows   . back",
                          font=font_sm, fill=C_DIM)
                rows = [(lbl, val) for _id, lbl, val in self._input_menu_items()]
                self._render_kv(draw, font_sm, W, H, palette, rows, y0=66)

        else:  # SETTINGS
            if self._settings_editing:
                draw.text((10, 44), "+/− change value    ENTER done",
                          font=font_sm, fill=C_EDIT)
            else:
                draw.text((10, 44), f"ip  {_local_ip()}", font=font_sm, fill=C_DIM)
            self._render_settings_list(draw, font_sm, W, H, palette, y0=62)

        # footer hint
        draw.line([0, H - 22, W, H - 22], fill=C_DIM, width=1)
        draw.text((W // 2, H - 11), self._footer_hint(page), font=font_sm,
                  fill=C_DIM, anchor="mm")

    def _footer_hint(self, page):
        """The footer key legend, per page and per sub-mode.

        It used to be one generic string on every page, which was wrong more
        often than right: BKSP scrolls on most pages but DELETES on BROWSER and
        RESETS on MIDI, and 4/6 adjust nothing at all on BROWSER, SHADERS and
        IMPORT. The top-of-page line says what the page DOES; this says how to
        move around it, so the two don't repeat each other.
        """
        # Sub-modes first — while one is active it owns the keyboard, so the
        # page's normal legend would be a lie.
        if self._assigning:
            return "press 4–9 = slot   any other key cancels"
        if self._confirm_delete:
            return "0 again = DELETE   any other key cancels"
        if self._midi_editing:
            return "type 0–127   ENTER confirm   − delete digit"
        if self._settings_editing:
            return "+/− or 4/6 change value   ENTER done"

        if page == "INPUT":
            view = self._input_view
            if view == "WIZARD":
                return {"OFFER": "any pad key start   ENTER start   . skip",
                        "WALK":  "press the pad key   − skip   . stop",
                        "DONE":  "ENTER close"}.get(self._wiz_phase, "")
            if view == "DEVICES":
                return "+/− scroll   5/ENTER select   . back"
            if view == "LEARN":
                return ("press a key on the pad   (wait = exit)"
                        if self._learn_code is None else
                        "+/− scroll   ENTER bind   . back")
            return "+/− scroll   4/6 cols·rows   7/9 page"

        # +/− scroll on every page without exception, so the only per-page
        # difference left is what 0 does.
        if page == "BROWSER":
            return "+/− scroll   5 load   0 = delete   7/9 page   . close"
        if page == "MIDI":
            return "+/− scroll   4/6 ±5   0 = reset   7/9 page   . close"
        if page == "IMPORT":
            return ("+/− scroll   5 copy   7/9 page   . eject"
                    if self._usb_dev else
                    "+/− scroll   5 mount   7/9 page   . close")
        if page == "SETTINGS":
            return "+/− scroll   4/6 ±   ENTER edit   7/9 page   . close"
        return "+/− scroll   5 load   7/9 page   . close"

    def _render_wizard(self, draw, font_lg, font_md, font_sm, W, H, palette):
        """The calibration walk owns the whole screen — one instruction, large,
        readable from playing distance. No list: at any moment there is exactly
        one thing to do, and the pad can only do one thing (press a key)."""
        C_BG, C_HL, C_LABEL, C_VALUE, C_DIM, C_ACCENT, C_EDIT = palette
        cx   = W // 2
        name = (self._wiz_pad or {}).get("name", "pad")

        if self._wiz_phase == "OFFER":
            draw.text((cx, 62),  "PAD DETECTED", font=font_md, fill=C_ACCENT,
                      anchor="mm")
            draw.text((cx, 92),  name[:30], font=font_sm, fill=C_VALUE,
                      anchor="mm")
            # A lockout reason names the missing controls, so it can outgrow the
            # panel — the recovery matters more than the detail, so it wraps to
            # the second line rather than being cut.
            _reason = self._wiz_reason or "NOT MAPPED YET"
            _lock   = _reason.startswith("NO KEY FOR")
            draw.text((cx, 132 if _lock else 140), _reason[:42], font=font_sm,
                      fill=C_EDIT if _lock else C_DIM, anchor="mm")
            if len(_reason) > 42:
                draw.text((cx, 150), _reason[42:84], font=font_sm, fill=C_EDIT,
                          anchor="mm")
            draw.text((cx, 178), "PRESS ANY KEY ON THE PAD",
                      font=font_md, fill=C_HL, anchor="mm")
            draw.text((cx, 206), "to teach it the controls",
                      font=font_sm, fill=C_DIM, anchor="mm")
            draw.text((cx, 246), f"{len(self._wiz_steps)} keys, one prompt each",
                      font=font_sm, fill=C_DIM, anchor="mm")
            return

        if self._wiz_phase == "DONE":
            gaps = len(self._missing_actions())
            if self._wiz_promote:
                draw.text((cx, 76), "PAD CALIBRATED", font=font_md,
                          fill=C_ACCENT, anchor="mm")
                draw.text((cx, 116), f"{len(self.inst.cfg.keymap)} keys mapped",
                          font=font_md, fill=C_HL, anchor="mm")
                draw.text((cx, 158), "PRIMARY DEVICE:", font=font_sm,
                          fill=C_DIM, anchor="mm")
                draw.text((cx, 180), name[:30], font=font_sm, fill=C_VALUE,
                          anchor="mm")
                draw.text((cx, 224), "the pad now drives the instrument",
                          font=font_sm, fill=C_DIM, anchor="mm")
            else:
                draw.text((cx, 88), "KEYS BOUND", font=font_md, fill=C_ACCENT,
                          anchor="mm")
                draw.text((cx, 132), f"{self._wiz_bound} fixed",
                          font=font_md, fill=C_HL, anchor="mm")
                draw.text((cx, 186),
                          "every control has a key" if not gaps else
                          f"{gaps} still with no key",
                          font=font_sm, fill=C_DIM if not gaps else C_EDIT,
                          anchor="mm")
            draw.text((cx, 246), "fix single keys with EDIT KEYS",
                      font=font_sm, fill=C_DIM, anchor="mm")
            return

        # WALK — progress, then the one action being asked for.
        total = len(self._wiz_steps)
        step  = min(self._wiz_step, total - 1)
        label = self._wiz_steps[step][0]
        draw.text((10, 44), f"{self._wiz_title} {name[:16]}", font=font_sm,
                  fill=C_DIM)
        draw.text((W - 10, 44), f"{step + 1} / {total}", font=font_sm,
                  fill=C_DIM, anchor="rt")

        bx0, bx1, by = 10, W - 10, 68
        draw.rectangle([bx0, by, bx1, by + 6], outline=C_DIM, width=1)
        fill_w = int((bx1 - bx0 - 2) * step / max(1, total))
        if fill_w > 0:
            draw.rectangle([bx0 + 1, by + 1, bx0 + 1 + fill_w, by + 5],
                           fill=C_ACCENT)

        optional = self._wiz_optional()
        draw.text((cx, 118), "PRESS THE KEY FOR" if not optional else
                  "OPTIONAL — PRESS A SPARE KEY FOR",
                  font=font_sm, fill=C_DIM if not optional else C_ACCENT,
                  anchor="mm")
        draw.text((cx, 158), label[:18], font=font_lg, fill=C_HL, anchor="mm")
        if self._learn_status:
            draw.text((cx, 214), self._learn_status[:44], font=font_sm,
                      fill=C_ACCENT, anchor="mm")
        draw.text((cx, 246),
                  "no spare key? wait — it'll skip" if optional else
                  "a key you've already used gets reassigned",
                  font=font_sm, fill=C_DIM, anchor="mm")

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
