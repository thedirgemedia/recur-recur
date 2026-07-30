#!/usr/bin/env python3
"""
DisplayController — status/menu on the 3.5" SPI display (ILI9486, 480x320).

Drives the waveshare35a ILI9486 directly over SPI0.0, bypassing fbtft entirely.
This sidesteps kernel 6.12's broken deferred-IO mechanism in the staging driver.

Prerequisites:
  - Remove (comment out) dtoverlay=waveshare35a from /boot/firmware/config.txt
  - dtparam=spi=on must remain (provides /dev/spidev0.0)
  - Optionally add spidev.bufsiz=131072 to /boot/firmware/cmdline.txt for speed
  - python3-spidev and lgpio must be installed (both present on Pi OS Bookworm)

Protocol (regwidth=16, buswidth=8 — matches fbtft write_reg16_bus8):
  command  → DC=low,  xfer [0x00, cmd]
  data     → DC=high, xfer [byte, byte, ...]
  pixels   → DC=high, writebytes2 big-endian RGB565
"""

import os
import threading
import time
import logging

log = logging.getLogger("display")

FB_W  = 480
FB_H  = 320
FPS   = 20

# The render loop skips render+convert entirely when the param signature is
# unchanged (see _render_loop). KEEPALIVE_S bounds how long anything the
# signature does NOT cover can stay stale on the panel: at least one full
# render+sample-compare happens this often no matter what. Measured costs that
# justify the gate: signature 0.006 ms, PIL render 4.17 ms, RGB565 convert
# 1.53 ms — the signature is ~950x cheaper than the frame it avoids.
KEEPALIVE_S = 0.5

# Dirty-row updates. A full 480x320x2 frame is 307200 bytes ≈ 307 ms of bus
# time at SPI_SPEED, so a full refresh caps out near 3 fps no matter what FPS
# says — writing only the rows that changed is what makes menu scrolling and
# LFO-driven params keep up. Most screens change a few rows, not the panel.
ROW_BYTES = FB_W * 2
BAND_MERGE_GAP = 8      # bridge gaps this small rather than open a new window
FULL_WRITE_FRAC = 0.75  # past this share of the panel, just write the frame

# Timeline geometry. Module-level because the render loop signatures the
# playhead at the same pixel resolution _draw_timeline draws it — if these
# disagree, the playhead either stutters or redraws pointlessly.
TL_X = 10
TL_W = FB_W - 20

# Fixed key order for the param-signature change detector below — avoids a
# sorted() + lambda call on cfg.params/fx_params every render tick when the
# key set (p1..p10, f1..f5) never actually changes.
_P_SIG_KEYS = tuple(f"p{n}" for n in range(1, 11))
_F_SIG_KEYS = tuple(f"f{n}" for n in range(1, 6))
# LFO bindings live in the same dicts under "lfo_" keys. They change the badge
# and the grid, so they belong in the signature — but their VALUE does not
# change per frame (the LFO runs on the GPU), which is what keeps the SPI
# display from redrawing continuously while a param is modulated.
_P_LFO_KEYS = tuple("lfo_" + k for k in _P_SIG_KEYS)
_F_LFO_KEYS = tuple("lfo_" + k for k in _F_SIG_KEYS)

SPI_BUS   = 0
SPI_DEV   = 0
SPI_SPEED = 8_000_000
GPIO_DC   = 24
GPIO_RST  = 25

# ── palette — terminal green, matching dirgemedia.com/recur-recur ─────────────
C_BG        = (0x00, 0x00, 0x00)   # pure black
C_DIVIDER   = (0x00, 0x2a, 0x00)   # dark green line
C_LABEL     = (0x00, 0xaa, 0x00)   # medium green  (#0a0)
C_VALUE     = (0x00, 0xff, 0x00)   # bright green  (#0f0)
C_BAR_TRACK = (0x00, 0x11, 0x00)   # very dark green
C_BAR_FILL  = (0x00, 0xaa, 0x00)   # medium green  (#0a0)
C_ON        = (0x00, 0xff, 0x00)   # bright green  (#0f0) — active / on states
C_SEL       = (0x00, 0xff, 0x00)   # bright green  (#0f0) — selected parameter
C_ROWSEL    = (0x00, 0xe0, 0xe0)   # cyan — the highlighted row (not editing);
                                   # distinct from green (labels) and amber (edit)
C_HINT      = (0x00, 0x44, 0x00)   # dim green     (#050 approx)

MODE_COLOURS = {
    "SAMPLER": (0x00, 0xff, 0x00),
    "SHADER":  (0xff, 0xff, 0x00),
    "LIVE":    (0x00, 0xff, 0x55),
}

# ── Tabs ──────────────────────────────────────────────────────────────────────
TAB_H  = 42                  # height of the tab bar in pixels
TABS   = ("SHADER", "FX", "SAMPLER", "LIVE", "SETTINGS")
TAB_W  = FB_W // 5           # 96 px each (5 tabs)

TAB_COL = {
    "SHADER":   (0xff, 0xff, 0x00),   # yellow
    "SAMPLER":  (0x00, 0xff, 0x00),   # bright green
    "LIVE":     (0x00, 0xff, 0x55),   # bright green
    "FX":       (0xff, 0x55, 0x00),   # orange-red
    "SETTINGS": (0x00, 0xaa, 0x00),   # medium green
}

C_STAGED = (0xff, 0x88, 0x00)         # amber — staged mode indicator

# Sub-screen definitions per tab (index 0 = main screen for that tab).
# Re-pressing the active tab key cycles through these.
# Names ending in _GRID render the 3×3 slot-grid view.
# Names that match a menu.PAGES entry activate the full-screen menu renderer.
_TAB_SCREENS = {
    0: ("SHADER_GRID",   "SHADER",  "SHADER_PRESET_GRID"),
    1: ("FX_GRID",       "FX",      "FX_PRESET_GRID"),
    2: ("SAMPLER_GRID",  "SAMPLER"),
    3: ("LIVE_GRID",     "LIVE"),
    4: ("SETTINGS_GRID", "BROWSER", "MIDI"),
}

# Grid screens that browse a preset store rather than a shader/clip list.
# Named *_GRID so they inherit grid semantics (1-9 select, hold, +/Bksp page)
# from is_grid_screen(); the store here is what makes them act on presets.
_PRESET_SCREENS = {
    "SHADER_PRESET_GRID": "shader",
    "FX_PRESET_GRID":     "fx",
    "LIVE_GRID":          "whole",
}

# Actions on the preset options screen (hold a filled preset cell).
_PRESET_OPTS = (("overwrite", "OVERWRITE WITH CURRENT"),
                ("delete",    "DELETE"))

# Grid slot display order: matches numpad spatial layout.
# Position 0 (top-left) = key 7, position 8 (bottom-right) = key 3.
_GRID_SLOTS = (7, 8, 9, 4, 5, 6, 1, 2, 3)

# Monospace font gives the Courier-New terminal look of the web version.
# Fall back through available options to the PIL default.
FONT_PATH = "/usr/share/fonts/truetype/freefont/FreeMonoBold.ttf"
FONT_PATH_FALLBACKS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
]

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    log.warning("PIL not available — display disabled")

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


# ── pixel conversion ─────────────────────────────────────────────────────────

def _to_spi_bytes(img):
    """Convert PIL RGB image to big-endian RGB565 for direct SPI output.

    bits[15:11]=R, bits[10:5]=G, bits[4:0]=B.
    """
    rgb = img.convert("RGB")
    if HAS_NUMPY:
        a  = np.frombuffer(rgb.tobytes(), dtype=np.uint8).reshape(-1, 3)
        r  = a[:, 0].astype(np.uint16)
        g  = a[:, 1].astype(np.uint16)
        b  = a[:, 2].astype(np.uint16)
        px = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)   # RGB565
        return px.astype(">u2").tobytes()
    else:
        raw = rgb.tobytes()
        out = bytearray(len(raw) // 3 * 2)
        j = 0
        for i in range(0, len(raw), 3):
            r, g, b = raw[i], raw[i + 1], raw[i + 2]
            v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)  # RGB565
            out[j]     = (v >> 8) & 0xFF
            out[j + 1] = v & 0xFF
            j += 2
        return bytes(out)


def _dirty_bands(cur, prev):
    """Row bands that differ between two RGB565 frames, as [(r0, r1), ...].

    [] means the frames are identical. Bands separated by fewer than
    BAND_MERGE_GAP unchanged rows are merged: opening a second window costs a
    CASET+RASET+RAMWR round trip, which outweighs pushing a few unchanged rows
    through a window that is already open.
    """
    if HAS_NUMPY:
        a = np.frombuffer(cur,  dtype=np.uint8).reshape(FB_H, ROW_BYTES)
        b = np.frombuffer(prev, dtype=np.uint8).reshape(FB_H, ROW_BYTES)
        changed = np.flatnonzero((a != b).any(axis=1)).tolist()
    else:
        # memoryview slices don't copy, so this stays cheap without numpy.
        mc, mp = memoryview(cur), memoryview(prev)
        changed = [i for i in range(FB_H)
                   if mc[i * ROW_BYTES:(i + 1) * ROW_BYTES]
                   != mp[i * ROW_BYTES:(i + 1) * ROW_BYTES]]
    if not changed:
        return []
    bands = []
    start = last = changed[0]
    for r in changed[1:]:
        if r - last > BAND_MERGE_GAP:
            bands.append((start, last))
            start = r
        last = r
    bands.append((start, last))
    return bands


def _load_font(size):
    for path in [FONT_PATH] + FONT_PATH_FALLBACKS:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


# ── ILI9486 direct SPI driver ─────────────────────────────────────────────────

class _ILI9486:
    """Direct SPI driver for the waveshare35a ILI9486 panel.

    Reproduced init sequence from the working waveshare35a.dtbo (extracted from
    the dtb binary; includes the 120 ms delay before DISPON that was needed to
    keep the panel out of sleep mode).
    """

    def __init__(self):
        import spidev
        import lgpio
        self._lgpio = lgpio
        self._h     = lgpio.gpiochip_open(0)
        lgpio.gpio_claim_output(self._h, GPIO_DC,  1)
        lgpio.gpio_claim_output(self._h, GPIO_RST, 1)

        self._spi = spidev.SpiDev()
        self._spi.open(SPI_BUS, SPI_DEV)
        self._spi.max_speed_hz = SPI_SPEED
        self._spi.mode = 0

        self._reset()
        self._init()

    # ── low-level helpers ─────────────────────────────────────────────────────

    def _dc(self, v):
        self._lgpio.gpio_write(self._h, GPIO_DC, v)

    def _cmd(self, cmd, *data):
        # 16-bit register framing (regwidth=16, buswidth=8): every command and
        # parameter byte is sent as a 16-bit word (0x00, value). This is how the
        # working fbtft waveshare35a driver (write_reg16_bus8) talks to the panel,
        # and is REQUIRED for multi-byte address params (CASET/RASET) to latch the
        # high byte — single-byte framing silently drops it.
        self._dc(0)
        self._spi.xfer([0x00, cmd])
        if data:
            self._dc(1)
            seq = []
            for d in data:
                seq += [0x00, d]
            self._spi.xfer(seq)

    def _reset(self):
        self._lgpio.gpio_write(self._h, GPIO_RST, 0)
        time.sleep(0.10)                               # extended for cold-boot stability
        self._lgpio.gpio_write(self._h, GPIO_RST, 1)
        time.sleep(0.20)                               # allow panel to stabilise

    # ── display initialisation ────────────────────────────────────────────────

    def _init(self):
        self._cmd(0xB0, 0x00)
        self._cmd(0x11)                                        # SLPOUT
        time.sleep(0.255)
        self._cmd(0x3A, 0x55)                                  # COLMOD: 16 bpp
        self._cmd(0x36, 0x28)                                  # MADCTL: 480×320
        self._cmd(0xC2, 0x44)
        self._cmd(0xC5, 0x00, 0x00, 0x00, 0x00)
        self._cmd(0xE0, 0x0f, 0x1f, 0x1c, 0x0c, 0x0f, 0x08,
                        0x48, 0x98, 0x37, 0x0a, 0x13, 0x04,
                        0x11, 0x0d, 0x00)
        self._cmd(0xE1, 0x0f, 0x32, 0x2e, 0x0b, 0x0d, 0x05,
                        0x47, 0x75, 0x37, 0x06, 0x10, 0x03,
                        0x24, 0x20, 0x00)
        self._cmd(0xE2, 0x0f, 0x32, 0x2e, 0x0b, 0x0d, 0x05,
                        0x47, 0x75, 0x37, 0x06, 0x10, 0x03,
                        0x24, 0x20, 0x00)
        self._cmd(0x36, 0x28)
        time.sleep(0.120)                                      # required before DISPON
        self._cmd(0x29)                                        # DISPON

    # ── frame write ───────────────────────────────────────────────────────────

    def _ramwr(self, data: bytes):
        self._dc(0)
        self._spi.xfer([0x00, 0x2C])              # RAMWR (16-bit framed command)
        self._dc(1)
        chunk = 4096                              # small chunks transmit reliably
        for i in range(0, len(data), chunk):
            self._spi.writebytes2(data[i:i + chunk])

    def write_frame(self, data: bytes):
        """Write a full 480×320 frame as big-endian RGB565 (307200 bytes).

        This panel's GRAM is 480 columns (CASET) × 320 rows (RASET), so the
        WIDTH axis is addressed by CASET and the HEIGHT axis by RASET."""
        self._cmd(0x2A, 0x00, 0x00, 0x01, 0xDF)  # CASET: 0–479 (width)
        self._cmd(0x2B, 0x00, 0x00, 0x01, 0x3F)  # RASET: 0–319 (height)
        self._ramwr(data)

    def write_rows(self, data: bytes, r0: int, r1: int):
        """Write logical rows (image rows = HEIGHT) r0–r1 inclusive.
        data must be (r1-r0+1)*480*2 bytes (RGB565).
        CASET = full width 0–479, RASET = row window r0–r1."""
        self._cmd(0x2A, 0x00, 0x00, 0x01, 0xDF)                   # CASET: 0–479 (width)
        self._cmd(0x2B, r0 >> 8, r0 & 0xFF, r1 >> 8, r1 & 0xFF)   # RASET: row window
        self._ramwr(data)

    def close(self):
        try:
            self._spi.close()
        except Exception:
            pass
        try:
            self._lgpio.gpiochip_close(self._h)
        except Exception:
            pass


# ── controller ────────────────────────────────────────────────────────────────

def _local_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "no network"


class DisplayController:
    def __init__(self, inst):
        self.inst        = inst
        self._stop       = threading.Event()
        self._thread     = None
        # Tab state — keyboard.py sets these via set_tab() / toggle_staged()
        self._active_tab  = 0          # 0=SHADER 1=FX 2=SAMPLER 3=LIVE 4=SETTINGS
        self._tab_screen  = [0, 0, 0, 0, 0]  # current sub-screen index per tab
        self._staged      = False      # False=live (immediate), True=staged (ENTER to push)
        self._grid_pending   = [None, None, None, None, None]  # staged slot per tab
        self._fx_grid_offset     = 0   # first index of the visible FX page
        self._shader_grid_offset = 0   # first index of the visible shader page
        # First slot index of the visible page, per preset store.
        self._preset_offset = {"whole": 0, "shader": 0, "fx": 0}
        self._cached_ip   = "…"
        self._ip_ts       = 0.0

    # ── public API ────────────────────────────────────────────────────────────

    def set_tab(self, idx):
        idx = max(0, min(len(TABS) - 1, idx))
        if idx == self._active_tab:
            menu = getattr(self.inst, "menu", None)
            # A menu page was opened directly from the grid (e.g. SETTINGS_GRID
            # cell pressed), so _tab_screen is still 0.  Pressing the same tab
            # key should close the menu and return to the grid — not cycle to the
            # next sub-screen (which would land on the wrong page).
            if menu and menu.active and self._tab_screen[idx] == 0:
                menu.active = False
                menu._cancel_edits()
                return
            # Otherwise cycle through this tab's sub-screens normally.
            screens = _TAB_SCREENS[idx]
            self._tab_screen[idx] = (self._tab_screen[idx] + 1) % len(screens)
        else:
            # Switching to a different tab — close any open menu first.
            self._deactivate_menu_screen()
            self._active_tab = idx
        self._activate_screen(self._active_tab, self._tab_screen[self._active_tab])

    def toggle_staged(self):
        self._staged = not self._staged
        return self._staged

    def is_grid_screen(self):
        """True when the current tab is showing its first (grid) sub-screen."""
        return _TAB_SCREENS[self._active_tab][
            self._tab_screen[self._active_tab]].endswith("_GRID")

    def go_to_grid_screen(self):
        """Jump back to the grid (first) sub-screen of the current tab."""
        self._tab_screen[self._active_tab] = 0
        self._activate_screen(self._active_tab, 0)

    def go_to_params_screen(self):
        """Jump to the params (second) sub-screen of the current tab."""
        self._tab_screen[self._active_tab] = 1
        self._activate_screen(self._active_tab, 1)

    def fx_grid_page(self, direction):
        """Advance the FX grid by ±9 items (clamp at boundaries)."""
        self._fx_grid_offset = max(0, self._fx_grid_offset + direction * 9)

    def shader_grid_page(self, direction):
        """Advance the shader grid by ±9 items (clamp at boundaries)."""
        self._shader_grid_offset = max(0, self._shader_grid_offset + direction * 9)

    def preset_store(self):
        """The preset store the current screen browses, or None if it isn't a
        preset grid. This is what tells the key handlers to act on presets."""
        return _PRESET_SCREENS.get(
            _TAB_SCREENS[self._active_tab][self._tab_screen[self._active_tab]])

    def preset_grid_page(self, direction):
        """Advance the current preset grid by ±9 slots (clamp at zero)."""
        store = self.preset_store()
        if store is None:
            return
        self._preset_offset[store] = max(
            0, self._preset_offset[store] + direction * 9)

    def preset_offset(self, store):
        return self._preset_offset.get(store, 0)

    def _activate_screen(self, tab, screen):
        """Open or close the menu renderer depending on the target sub-screen."""
        menu = getattr(self.inst, "menu", None)
        if menu is None:
            return
        screen_name = _TAB_SCREENS[tab][screen]
        # _GRID screens never open the deep menu.
        if screen_name.endswith("_GRID"):
            menu.active = False
            return
        # If the screen name matches a PAGES entry, activate the menu on that page.
        try:
            from control.menu import PAGES
            if screen_name in PAGES:
                menu.page   = list(PAGES).index(screen_name)
                menu.sel    = 0
                menu.active = True
                menu._cancel_edits()
                return
        except (ImportError, ValueError):
            pass
        menu.active = False

    def _deactivate_menu_screen(self):
        menu = getattr(self.inst, "menu", None)
        if menu is not None:
            menu.active = False
            menu._cancel_edits()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        if not HAS_PIL:
            return
        self._thread = threading.Thread(target=self._render_loop,
                                        daemon=True, name="display")
        self._thread.start()
        log.info("display started (direct SPI %d.%d, %dx%d @ %dfps)",
                 SPI_BUS, SPI_DEV, FB_W, FB_H, FPS)

    def stop(self):
        self._stop.set()

    # ── render loop ───────────────────────────────────────────────────────────

    def _timeline_sig(self, inst, _kb, _smp):
        """Signature terms for the SAMPLER playhead, or None when it isn't shown.

        The playhead moves every frame while a clip plays, so signaturing it
        unconditionally would defeat the render gate on every tab — including
        the four that never draw a timeline. So mirror _render's routing and
        signature it only while the one screen that draws it is actually up
        (_render_sampler_tab is the sole caller of _draw_timeline).
        """
        if _smp is None or TABS[self._active_tab] != "SAMPLER":
            return None
        screen = _TAB_SCREENS[self._active_tab][self._tab_screen[self._active_tab]]
        if (screen.endswith("_GRID")
                or getattr(_kb, "_preset_opts", None) is not None
                or getattr(_kb, "_lfo_screen", False)
                or getattr(_kb, "_param_layer", 0) == 5):   # CLIP settings
            return None
        dur = float(getattr(_smp, "duration", 0.0) or 0.0)
        pos = float(getattr(_smp, "time_pos", 0.0) or 0.0)
        # Quantise to what is actually visible — the playhead's pixel column and
        # the 0.1 s resolution of the position readout — so sub-pixel drift
        # doesn't trigger a redraw that would look identical.
        return (round(pos, 1), round(dur, 1),
                round(float(getattr(_smp, "in_pt", 0.0) or 0.0), 2),
                getattr(_smp, "out_pt", None),
                int(max(0.0, min(pos, dur)) / dur * TL_W) if dur > 0 else 0)

    def _render_loop(self):
        font_lg = _load_font(38)
        font_md = _load_font(20)
        font_sm = _load_font(15)

        try:
            disp = _ILI9486()
            log.info("ILI9486 initialised over SPI%d.%d", SPI_BUS, SPI_DEV)
        except Exception as e:
            log.error("SPI display init failed: %s", e)
            return

        last_data      = None
        last_param_sig = None
        last_render    = 0.0
        frame_n = 0
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                try:
                    inst = self.inst
                    cfg  = inst.cfg
                    _kb  = getattr(inst, "kb", None)
                    _rec = getattr(inst, "recorder", None)
                    _smp = getattr(inst, "sampler", None)
                    param_sig = (
                        tuple(cfg.params.get(k, 0.5) for k in _P_SIG_KEYS),
                        tuple(cfg.fx_params.get(k, 0.5) for k in _F_SIG_KEYS),
                        tuple(cfg.params.get(k) for k in _P_LFO_KEYS),
                        tuple(cfg.fx_params.get(k) for k in _F_LFO_KEYS),
                        round(getattr(cfg, "shader_blend_amount",   0.5),  3),
                        round(getattr(cfg, "overlay_blend_amount",  1.0) or 1.0, 2),
                        round(getattr(cfg, "trail_delay_s",         2.0),  2),
                        round(getattr(cfg, "trail_mode_opacity",    0.5),  3),
                        getattr(cfg, "trail_mode",       "screen"),
                        getattr(cfg, "trail_blend_type", "mode"),
                        getattr(cfg, "overlay_on",    False),
                        getattr(cfg, "overlay_mode",  ""),
                        getattr(cfg, "shader_blend",  False),
                        getattr(cfg, "shader_blend_mode",   ""),
                        getattr(cfg, "shader_blend_source", ""),
                        getattr(cfg, "trail_on",      False),
                        getattr(cfg, "current_clip",  None),
                        # Snapshots, not the live dicts: the keyboard/MIDI/menu
                        # threads write these while this thread walks them.
                        tuple(sorted(cfg.clip_snapshot(
                            getattr(cfg, "current_clip", None)).items())),
                        getattr(_kb, "_param_idx",   0),
                        getattr(_kb, "_param_layer", 0),
                        getattr(_kb, "_editing_param", False),
                        getattr(_kb, "_lfo_screen",   False),
                        getattr(_kb, "_lfo_edit_idx", 0),
                        tuple(tuple(sorted(l.items()))
                              for l in getattr(cfg, "lfos", [])),
                        round(getattr(cfg, "lfo_bpm", 120.0), 2),
                        getattr(_kb, "_midi_assign", False),
                        getattr(_kb, "_midi_cc_buf", ""),
                        tuple(sorted(cfg.midi_cc_snapshot().items())),
                        getattr(cfg, "current_shader", None),
                        getattr(cfg, "current_fx",     None),
                        tuple(getattr(cfg, "shader_chain", [])),
                        getattr(cfg, "shader_edit_slot", 0),
                        tuple((b.get("mode",""), round(b.get("amt",1.0),3))
                              for b in getattr(cfg, "shader_blend_chain", [])),
                        tuple(getattr(cfg, "fx_chain", [])),
                        getattr(cfg, "fx_edit_slot",   0),
                        tuple((b.get("mode",""), round(b.get("amt",1.0),3))
                              for b in getattr(cfg, "fx_blend_chain", [])),
                        round(getattr(cfg, "color_hue", 0.0), 4),
                        round(getattr(cfg, "color_sat", 1.0), 3),
                        getattr(_rec, "status", ""),
                        inst.mode,
                        self._active_tab,
                        tuple(self._tab_screen),
                        self._staged,
                        tuple(cfg.shader_slots.get(k) for k in _GRID_SLOTS),
                        tuple(cfg.clip_slots.get(k) for k in _GRID_SLOTS),
                        getattr(cfg, "preset_gen", 0),
                        tuple(sorted(self._preset_offset.items())),
                        getattr(_kb, "_preset_opts", None),
                        getattr(_kb, "_preset_opt_idx", 0),
                        tuple(self._grid_pending),
                        self._fx_grid_offset,
                        self._shader_grid_offset,
                        # Drawn by the tab screens but tracked by nothing above.
                        # They change on events rather than per frame, so they
                        # cost the gate nothing and save a keepalive-long wait
                        # before the panel catches up.
                        getattr(_smp, "mode", ""),
                        round(float(getattr(_smp, "speed", 1.0) or 1.0), 3),
                        getattr(_smp, "_active_source", None),
                        getattr(cfg, "camera_width",  0),
                        getattr(cfg, "camera_height", 0),
                        self._cached_ip,
                        self._timeline_sig(inst, _kb, _smp),
                    )
                    _menu = getattr(inst, "menu", None)
                    menu_active = _menu is not None and _menu.active
                    # Signature-first gate. Building param_sig costs ~0.006 ms;
                    # the render+convert it can skip costs ~5.7 ms, so testing
                    # first is ~950x cheaper than the frame it avoids — this is
                    # what keeps the display thread off the CPU in steady state.
                    # Two things still force a render: the menu, whose state is
                    # not in the signature at all (so the signature cannot say
                    # whether it changed), and the keepalive, which bounds how
                    # long anything unsignatured can stay stale. The sampled
                    # pixel compare below still gates the SPI write, so a
                    # keepalive render that changed nothing costs no bus time.
                    if (menu_active or param_sig != last_param_sig
                            or (t0 - last_render) >= KEEPALIVE_S):
                        last_render = t0
                        img  = self._render(font_lg, font_md, font_sm)
                        data = _to_spi_bytes(img)
                        # The row diff is an exact comparison, so it replaces
                        # the old sampled 128-byte detector outright — that
                        # sample could miss a short bar fill, which is why a
                        # param signature had to back it up on the write path.
                        # Here the signature only gates the RENDER; what
                        # reaches the panel is decided by the pixels alone.
                        if last_data is None:
                            disp.write_frame(data)      # first frame: no baseline
                            rows = FB_H
                        else:
                            bands = _dirty_bands(data, last_data)
                            rows  = sum(r1 - r0 + 1 for r0, r1 in bands)
                            if rows >= FB_H * FULL_WRITE_FRAC:
                                disp.write_frame(data)
                                rows = FB_H
                            else:
                                for r0, r1 in bands:
                                    disp.write_rows(
                                        data[r0 * ROW_BYTES:(r1 + 1) * ROW_BYTES],
                                        r0, r1)
                        # Always adopt the signature we just rendered, even if
                        # nothing reached the panel: a cfg change that isn't
                        # visible on this screen would otherwise leave the
                        # signature permanently mismatched and pin the loop at
                        # a full render every tick.
                        last_param_sig = param_sig
                        last_data      = data
                        if rows:
                            frame_n += 1
                            log.debug("frame %d written (%d rows, %.0f ms)",
                                      frame_n, rows, (time.monotonic() - t0) * 1000)
                except Exception as e:
                    log.warning("render error: %s", e)
                self._stop.wait(1.0 / FPS)
        finally:
            disp.close()

    # ── frame builder ─────────────────────────────────────────────────────────

    def _render(self, font_lg, font_md, font_sm):
        inst = self.inst
        cfg  = inst.cfg

        img = Image.new("RGB", (FB_W, FB_H), C_BG)
        d   = ImageDraw.Draw(img)

        # Navigable menu overlays the full screen when active.
        menu = getattr(inst, "menu", None)
        if menu is not None and menu.active:
            palette = (C_BG, C_ON, C_LABEL, C_VALUE, C_HINT, C_BAR_FILL, C_STAGED)
            menu.render(img, d, font_lg, font_md, font_sm, FB_W, FB_H, palette)
            return img

        self._draw_tabs(d, font_sm)

        tab         = TABS[self._active_tab]
        screen_name = _TAB_SCREENS[self._active_tab][self._tab_screen[self._active_tab]]

        _kb = getattr(inst, "kb", None)
        if screen_name.endswith("_GRID"):
            self._render_tab_grid(d, font_md, font_sm, inst, cfg, tab, screen_name)
        elif getattr(_kb, "_preset_opts", None) is not None:
            # Preset options screen — opened by holding a filled preset cell,
            # so like the LFO screen it is rendered before the tab's own params.
            self._render_preset_opts(d, font_md, font_sm, inst, cfg, _kb)
        elif getattr(_kb, "_lfo_screen", False):
            # LFO settings screen — reachable from any tab's params view, so it
            # is rendered here (before the tab-specific params screens).
            self._render_lfo_settings(d, font_sm, inst, cfg, _kb)
        elif tab == "SHADER":
            self._render_shader_tab(d, font_lg, font_md, font_sm, inst, cfg)
        elif tab == "SAMPLER":
            self._render_sampler_tab(d, font_lg, font_md, font_sm, inst, cfg)
        elif tab == "LIVE":
            self._render_live_tab(d, font_lg, font_md, font_sm, inst, cfg)
        elif tab == "FX":
            self._render_fx_params_tab(d, font_lg, font_md, font_sm, inst, cfg)
        else:
            self._render_settings_tab(d, font_lg, font_md, font_sm, inst, cfg)

        self._draw_footer(d, font_sm, inst)
        return img

    # ── Grid screen ──────────────────────────────────────────────────────────

    def _render_tab_grid(self, d, font_md, font_sm, inst, cfg, tab, screen_name):
        """Render a 3×3 grid screen. Dispatches on the SCREEN, not the tab —
        SHADER and FX each have two grid screens now (their item grid and their
        preset store)."""
        col = TAB_COL[tab]
        store = _PRESET_SCREENS.get(screen_name)
        if store is not None:
            self._render_preset_grid(d, font_md, font_sm, inst, cfg, col, store)
            return
        if tab == "SHADER":
            self._render_shader_grid(d, font_md, font_sm, inst, cfg, col)
        elif tab == "SAMPLER":
            self._render_sampler_grid(d, font_md, font_sm, inst, cfg, col)
        elif tab == "FX":
            self._render_fx_grid(d, font_md, font_sm, inst, cfg, col)
        else:
            self._render_settings_grid(d, font_md, font_sm, inst, cfg, col)

    def _draw_grid(self, d, font_md, font_sm, section, cells, section_col):
        """Draw a 3×3 button grid.

        cells: 9-element list, each dict with keys:
            label (str)   — displayed text (may be multi-word; auto-split at ' ')
            active (bool) — item is currently active/loaded
            empty  (bool) — slot is unassigned

        Grid position order matches numpad layout:
            pos 0  1  2   → keys 7 8 9  (top row on screen)
            pos 3  4  5   → keys 4 5 6
            pos 6  7  8   → keys 1 2 3  (bottom row)
        """
        # Section label
        sec_y = TAB_H + 4
        d.text((10, sec_y), section, font=font_sm, fill=section_col)
        d.line([0, TAB_H + 20, FB_W, TAB_H + 20], fill=C_DIVIDER, width=1)

        # Grid geometry
        GM  = 4    # outer margin
        GG  = 4    # gap between cells
        GY0 = TAB_H + 24
        GY1 = FB_H - 26
        gh  = GY1 - GY0
        cw  = (FB_W - 2 * GM - 2 * GG) // 3
        ch  = (gh - 2 * GG) // 3

        for pos, cell in enumerate(cells[:9]):
            row = pos // 3
            col = pos % 3
            x0  = GM  + col * (cw + GG)
            y0  = GY0 + row * (ch + GG)
            x1  = x0 + cw
            y1  = y0 + ch

            empty  = cell.get("empty",  False)
            active = cell.get("active", False)

            pending = cell.get("pending", False)
            if empty:
                bg     = C_BG
                border = C_DIVIDER
                tc     = C_HINT
            elif active:
                bg     = tuple(max(0, c // 5) for c in C_ON)  # dark green tint
                border = C_ON
                tc     = C_ON
            elif pending:
                bg     = (0x18, 0x08, 0x00)   # dark amber tint
                border = C_STAGED
                tc     = C_STAGED
            else:
                bg     = (0x00, 0x0a, 0x00)
                border = C_LABEL
                tc     = C_VALUE

            d.rectangle([x0, y0, x1, y1], fill=bg)
            d.rectangle([x0, y0, x1, y1], outline=border, width=2)

            label = cell.get("label", "")
            cx    = (x0 + x1) // 2
            cy    = (y0 + y1) // 2
            # Split long labels at space or hyphen to fit two lines
            parts = label.replace("-", "- ").split()
            if len(parts) <= 1 or len(label) <= 12:
                d.text((cx, cy), label[:14], font=font_sm, fill=tc, anchor="mm")
            else:
                half = (len(parts) + 1) // 2
                top  = " ".join(parts[:half])[:12]
                bot  = " ".join(parts[half:])[:12]
                d.text((cx, cy - 8), top, font=font_sm, fill=tc, anchor="mm")
                d.text((cx, cy + 8), bot, font=font_sm, fill=tc, anchor="mm")

    def _render_shader_grid(self, d, font_md, font_sm, inst, cfg, col):
        sh_list      = inst.shader.list_shaders(kind="generative")
        shader_chain = getattr(cfg, "shader_chain", [])
        offset  = min(self._shader_grid_offset, max(0, len(sh_list) - 1) // 9 * 9)
        self._shader_grid_offset = offset
        page_items = sh_list[offset:offset + 9]
        cells = []
        for name in page_items:
            label    = name.replace(".glsl", "").upper().replace("_", " ")
            in_stack = name in shader_chain
            # Show stack-position badge for stack members (mirrors FX grid)
            if in_stack:
                stack_pos = shader_chain.index(name) + 1
                label = f"[{stack_pos}]{label[:10]}"
            cells.append({"label": label, "active": in_stack})
        while len(cells) < 9:
            cells.append({"label": "", "empty": True})
        total_pages = max(1, (len(sh_list) + 8) // 9)
        cur_page    = offset // 9 + 1
        n_stack     = len(shader_chain)
        stack_info  = f" ({n_stack})" if n_stack else ""
        section     = f"GEN SHADER{stack_info}  {cur_page}/{total_pages}"
        self._draw_grid(d, font_md, font_sm, section, cells, col)

    def _render_sampler_grid(self, d, font_md, font_sm, inst, cfg, col):
        slots   = getattr(cfg, "clip_slots", {})
        cur     = cfg.current_clip or ""
        pending = self._grid_pending[1]
        cells   = []
        for slot in _GRID_SLOTS:
            path = slots.get(slot)
            if path:
                label = os.path.basename(path)
                label = os.path.splitext(label)[0].upper().replace("_", " ")
                cells.append({"label": label[:14], "active": path == cur,
                              "pending": slot == pending})
            else:
                cells.append({"label": f"[{slot}]", "empty": True})
        self._draw_grid(d, font_md, font_sm, "CLIP SLOTS", cells, col)

    def _render_preset_grid(self, d, font_md, font_sm, inst, cfg, col, store):
        """Render one page of a preset store. Shared by all three stores, so
        PRESETS / SHADER PRESETS / FX PRESETS are the same screen with a
        different source — an empty cell shows its key, and holding it saves."""
        from config import PRESET_STORES
        offset = self._preset_offset.get(store, 0)
        names  = cfg.preset_page(store, offset)
        cells  = []
        for pos, name in enumerate(names):
            if name:
                cells.append({"label": name.replace(".json", "").upper()[:14]})
            else:
                cells.append({"label": f"[{_GRID_SLOTS[pos]}]", "empty": True})
        total  = max(1, (max(cfg.preset_count(store), offset + 1) + 8) // 9)
        page   = offset // 9 + 1
        used   = sum(1 for n in names if n)
        label  = PRESET_STORES[store]["label"]
        section = f"{label} ({used})  {page}/{total}"
        self._draw_grid(d, font_md, font_sm, section, cells, col)

    def _render_fx_grid(self, d, font_md, font_sm, inst, cfg, col):
        fx_list  = inst.shader.list_shaders(kind="fx")
        fx_chain = getattr(cfg, "fx_chain", [])
        offset   = min(self._fx_grid_offset, max(0, len(fx_list) - 1) // 9 * 9)
        self._fx_grid_offset = offset
        page_items = fx_list[offset:offset + 9]
        cells = []
        for name in page_items:
            label  = name.replace(".glsl", "").upper().replace("_", " ")
            in_chain = name in fx_chain
            # Show slot number badge for chain members
            if in_chain:
                chain_pos = fx_chain.index(name) + 1
                label = f"[{chain_pos}]{label[:10]}"
            cells.append({"label": label, "active": in_chain})
        while len(cells) < 9:
            cells.append({"label": "", "empty": True})
        total_pages = max(1, (len(fx_list) + 8) // 9)
        cur_page    = offset // 9 + 1
        n_chain     = len(fx_chain)
        chain_info  = f" ({n_chain})" if n_chain else ""
        section     = f"FX SHADER{chain_info}  {cur_page}/{total_pages}"
        self._draw_grid(d, font_md, font_sm, section, cells, col)

    def _render_settings_grid(self, d, font_md, font_sm, inst, cfg, col):
        from control.menu import PAGES as _labels
        cells = []
        for i, slot in enumerate(_GRID_SLOTS):
            if i < len(_labels):
                cells.append({"label": _labels[i]})
            else:
                cells.append({"label": "", "empty": True})
        self._draw_grid(d, font_md, font_sm, "SETTINGS", cells, col)

    # ── Tab bar ───────────────────────────────────────────────────────────────

    def _draw_tabs(self, d, font_sm):
        for i, tab in enumerate(TABS):
            x0      = i * TAB_W
            x1      = x0 + TAB_W - 1
            col     = TAB_COL[tab]
            active  = (i == self._active_tab)
            screens = _TAB_SCREENS[i]
            screen  = self._tab_screen[i]
            multi   = len(screens) > 1

            if active:
                d.rectangle([x0, 0, x1, TAB_H - 1], fill=(0x00, 0x22, 0x00))
                d.rectangle([x0, 0, x1, 2], fill=C_VALUE)   # top accent bar
                text_col = C_VALUE
            else:
                text_col = C_LABEL

            cx = x0 + TAB_W // 2

            if active and multi:
                # Name shifted up to leave room for sub-screen dots at bottom.
                label = screens[screen].replace("_GRID", "")
                d.text((cx, TAB_H // 2 - 2), label, font=font_sm,
                       fill=text_col, anchor="mm")
                # Position dots (one per sub-screen).
                dot_w   = 5
                dot_gap = 3
                total_w = len(screens) * dot_w + (len(screens) - 1) * dot_gap
                dx0     = cx - total_w // 2
                for di in range(len(screens)):
                    dx      = dx0 + di * (dot_w + dot_gap)
                    dot_col = C_VALUE if di == screen else C_DIVIDER
                    d.rectangle([dx, TAB_H - 8, dx + dot_w, TAB_H - 4],
                                fill=dot_col)
            else:
                d.text((cx, TAB_H // 2 + 2), tab, font=font_sm,
                       fill=text_col, anchor="mm")

            if i > 0:
                d.line([x0, 0, x0, TAB_H - 1], fill=C_DIVIDER, width=1)

        d.line([0, TAB_H, FB_W, TAB_H], fill=C_DIVIDER, width=1)

    # ── Footer ────────────────────────────────────────────────────────────────

    def _draw_footer(self, d, font_sm, inst):
        fy = FB_H - 22
        d.line([0, fy, FB_W, fy], fill=C_DIVIDER, width=1)
        cy = fy + 11

        if self._staged:
            pill_col = C_STAGED
            lbl      = "STAGED"
            hint     = "ENTER → PUSH"
        else:
            pill_col = C_ON
            lbl      = "LIVE"
            hint     = inst.mode

        d.rectangle([5, fy + 3, 65, FB_H - 3], fill=pill_col)
        d.text((35, cy), lbl, font=font_sm, fill=C_BG, anchor="mm")
        d.text((FB_W - 6, cy), hint, font=font_sm, fill=C_HINT, anchor="rm")

    # ── Shared params screen (sliders above, 3×3 selector grid below) ──────────

    def _render_params_screen(self, d, font_sm, tab_col, name,
                               all_keys, get_lbl, get_val, fmt_val, sel_idx,
                               editing=False, lfo_of=None, cc_of=None,
                               midi_assign=None):
        """Scrollable params list: as many rows as fit are shown at once,
        windowed around sel_idx so lists longer than the visible area (e.g.
        an FX layer's own params + its BLEND/BLD AMT rows) stay reachable via
        +/Bksp scrolling. `editing` highlights the header/selection in amber
        to show Enter has "entered" the highlighted row for value-stepping."""
        # Header
        sec_y = TAB_H + 4
        d.text((10, sec_y), name[:20], font=font_sm, fill=tab_col)
        if all_keys and 0 <= sel_idx < len(all_keys):
            k = all_keys[sel_idx]
            hdr_col = C_STAGED if editing else C_ROWSEL
            hdr_txt = f"{get_lbl(k)[:8]}: {fmt_val(k, get_val(k))}"
            if editing:
                hdr_txt = "EDIT " + hdr_txt
            d.text((FB_W - 8, sec_y), hdr_txt,
                   font=font_sm, fill=hdr_col, anchor="rm")
        d.line([0, TAB_H + 20, FB_W, TAB_H + 20], fill=C_DIVIDER, width=1)

        # ── Slim horizontal sliders (windowed around the selection) ────────
        SY0     = TAB_H + 24          # = 66
        BAR_X   = 85
        BAR_W   = 295
        BAR_H   = 8
        bar_gap = 15
        # leave 94px for the grid + 4px gap above it
        GY0     = FB_H - 26 - 94     # = 200
        visible = max(1, min(len(all_keys), (GY0 - 4 - SY0) // bar_gap))
        top     = max(0, min(sel_idx - visible // 2, max(0, len(all_keys) - visible)))

        sel_c = C_STAGED if editing else C_ROWSEL
        for row, i in enumerate(range(top, min(top + visible, len(all_keys)))):
            k        = all_keys[i]
            v        = get_val(k)
            by       = SY0 + row * bar_gap
            selected = (i == sel_idx)
            lc = sel_c if selected else C_LABEL
            bc = sel_c if selected else C_BAR_FILL

            d.text((10, by + BAR_H // 2), get_lbl(k)[:8],
                   font=font_sm, fill=lc, anchor="lm")
            d.rectangle([BAR_X, by + 1, BAR_X + BAR_W, by + BAR_H + 1],
                        fill=C_BAR_TRACK)
            filled = max(1, int(BAR_W * v))
            d.rectangle([BAR_X, by + 1, BAR_X + filled, by + BAR_H + 1],
                        fill=bc)
            d.text((BAR_X + BAR_W + 6, by + BAR_H // 2),
                   fmt_val(k, v), font=font_sm, fill=lc, anchor="lm")
            # LFO badge: a modulated param's bar moves on its own, so say why.
            li = lfo_of(k) if lfo_of else None
            if li is not None:
                d.text((FB_W - 6, by + BAR_H // 2), f"L{int(li) + 1}",
                       font=font_sm, fill=C_STAGED, anchor="rm")

        # ── 3×3 selector grid (same window as the sliders above) ──────────
        GM  = 4
        GG  = 3
        GY1 = FB_H - 26              # = 294
        gh  = GY1 - GY0
        cw  = (FB_W - 2 * GM - 2 * GG) // 3
        ch  = (gh  - 2 * GG) // 3

        for pos in range(9):
            row  = pos // 3
            col_ = pos % 3
            x0 = GM  + col_ * (cw + GG)
            y0 = GY0 + row  * (ch + GG)
            x1 = x0 + cw
            y1 = y0 + ch

            # Key 9 (top-right): DEFAULT — reset this control screen's target to
            # its default values (generative-shader / FX authored defaults, or
            # the clip's CLIP_DEFAULTS). Handled by _reset_current_layer().
            if pos == 2:
                d.rectangle([x0, y0, x1, y1], fill=(0x00, 0x06, 0x00))
                d.rectangle([x0, y0, x1, y1], outline=C_ON, width=2)
                d.text(((x0 + x1) // 2, (y0 + y1) // 2), "DEFAULT",
                       font=font_sm, fill=C_ON, anchor="mm")
                continue

            # The action grid now holds just two controls for the highlighted
            # param — LFO assign on the bottom row (keys 1/2/3) and MIDI assign
            # on key 4 — replacing the old param-jump quick-access on keys 4-9,
            # which duplicated +/Bksp scrolling. Everything else is blank.
            if pos >= 6 and lfo_of is not None:
                li    = pos - 6
                cur   = (lfo_of(all_keys[sel_idx])
                         if 0 <= sel_idx < len(all_keys) else None)
                bound = cur is not None and int(cur) == li
                bg_c   = tuple(max(0, c // 4) for c in C_STAGED) if bound else (0x00, 0x06, 0x00)
                border = C_STAGED if bound else C_DIVIDER
                tc     = C_STAGED if bound else C_HINT
                d.rectangle([x0, y0, x1, y1], fill=bg_c)
                d.rectangle([x0, y0, x1, y1], outline=border, width=2)
                d.text(((x0 + x1) // 2, (y0 + y1) // 2), f"LFO {li + 1}",
                       font=font_sm, fill=tc, anchor="mm")
                continue

            if pos == 3 and cc_of is not None:   # key 4: MIDI assign
                if midi_assign and midi_assign[0]:
                    # Assign in progress: show it's waiting (knob or typed CC).
                    buf   = midi_assign[1]
                    label = f"CC {buf}" if buf else "LEARN"
                    hi    = True
                else:
                    cur   = (cc_of(all_keys[sel_idx])
                             if 0 <= sel_idx < len(all_keys) else None)
                    label = f"CC {int(cur)}" if cur is not None else "MIDI"
                    hi    = cur is not None
                bg_c   = tuple(max(0, c // 4) for c in C_STAGED) if hi else (0x00, 0x06, 0x00)
                border = C_STAGED if hi else C_DIVIDER
                tc     = C_STAGED if hi else C_HINT
                d.rectangle([x0, y0, x1, y1], fill=bg_c)
                d.rectangle([x0, y0, x1, y1], outline=border, width=2)
                d.text(((x0 + x1) // 2, (y0 + y1) // 2), label,
                       font=font_sm, fill=tc, anchor="mm")
                continue

            # Every other cell is blank (quick-access removed).
            d.rectangle([x0, y0, x1, y1], fill=C_BG)
            d.rectangle([x0, y0, x1, y1], outline=C_DIVIDER, width=2)

    # ── SHADER tab ────────────────────────────────────────────────────────────

    def _render_shader_tab(self, d, font_lg, font_md, font_sm, inst, cfg):
        plabels     = inst.shader.param_labels()
        all_keys    = inst.shader.shader_row_keys()
        sel_idx     = getattr(getattr(inst, "kb", None), "_param_idx", 0)
        blend_modes = list(getattr(cfg, "FX_LAYER_BLEND_MODES", ("normal",)))

        # Slot 0 blends against the video layer, not the slot below — its rows
        # read cfg.shader_blend* instead (see shader.shader_row_keys()), where
        # blend being off reads as "normal": the shader replaces the video.
        video_blend = getattr(cfg, "shader_edit_slot", 0) == 0

        def _blend_mode_name():
            if video_blend:
                return (getattr(cfg, "shader_blend_mode", "normal")
                        if getattr(cfg, "shader_blend", False) else "normal")
            return cfg.shader_layer_blend.get("mode", "normal")

        def _blend_amt():
            if video_blend:
                return getattr(cfg, "shader_blend_amount", 0.5)
            return cfg.shader_layer_blend.get("amt", 1.0)

        def get_lbl(k):
            if k == "__blend_mode__":
                return "BLEND"
            if k == "__blend_amt__":
                return "BLD AMT"
            return plabels.get(k, k.upper()).upper()

        def get_val(k):
            if k == "__blend_mode__":
                cur = _blend_mode_name()
                i   = blend_modes.index(cur) if cur in blend_modes else 0
                return i / max(1, len(blend_modes) - 1)
            if k == "__blend_amt__":
                return _blend_amt()
            return cfg.params.get(k, 0.5)

        def fmt_val(k, v):
            if k == "__blend_mode__":
                return _blend_mode_name().upper()[:9]
            if k == "__blend_amt__":
                return f"{v:.2f}"
            ul = get_lbl(k)
            if ul.endswith((' X', ' Y')) or ul in ('X', 'Y'):
                return f"{(v - 0.5) * 200:+.0f}"
            if ul.endswith('STARS') or ul == 'STARS':
                return str(max(1, round(v * 500)))
            return f"{v:.2f}"

        shader_chain = getattr(cfg, "shader_chain", [])
        slot = getattr(cfg, "shader_edit_slot", 0)
        n    = len(shader_chain)
        slot_tag = f" [{slot+1}/{n}]" if n > 1 else ""
        name    = ((cfg.current_shader or "—").replace(".glsl", "").upper()) + slot_tag
        editing = getattr(getattr(inst, "kb", None), "_editing_param", False)
        self._render_params_screen(d, font_sm, TAB_COL["SHADER"], name,
                                   all_keys, get_lbl, get_val, fmt_val, sel_idx,
                                   editing=editing,
                                   lfo_of=lambda k: cfg.params.get("lfo_" + k),
                                   cc_of=lambda k: cfg.midi_target_cc.get(k),
                                   midi_assign=(getattr(getattr(inst, "kb", None), "_midi_assign", False),
                                                getattr(getattr(inst, "kb", None), "_midi_cc_buf", "")))

    # ── SAMPLER tab ───────────────────────────────────────────────────────────

    def _render_clip_settings(self, d, font_sm, inst, cfg, _kb):
        """Per-clip settings screen (CLIP layer): rotate / zoom / speed / dir /
        bright / contrast / trail (steps, time, mode) as sliders, edited with
        +/Bksp/Enter like every other params list. zoom + speed also take LFO
        (keys 1/2/3) and MIDI (key 4). Values are normalised 0..1 for the bar."""
        path  = cfg.current_clip
        slots = ("rotate", "zoom", "speed", "dir", "bright", "contrast",
                 "trail_on", "trail", "trail_time", "trail_mode", "trail_opc")
        labels = {"rotate": "ROTATE", "zoom": "ZOOM", "speed": "SPEED",
                  "dir": "DIR", "bright": "BRIGHT", "contrast": "CONTRAST",
                  "trail_on": "TRAIL", "trail": "TRL STEP",
                  "trail_time": "TRL TIME", "trail_mode": "TRL MODE",
                  "trail_opc": "TRL BLEND"}
        zmax  = getattr(cfg, "VIDEO_ZOOM_MAX", 4.0)
        tmax  = getattr(cfg, "CLIP_TRAIL_MAX", 5)
        tmodes = list(getattr(cfg, "TRAIL_MODES", ("screen",)))

        # The "dir" row edits the stored "reverse" flag.
        _store_key = {"dir": "reverse"}

        def raw(k):
            kk = _store_key.get(k, k)
            return cfg.clip_get(path, kk) if path else cfg.CLIP_DEFAULTS[kk]

        def get_val(k):   # normalised 0..1 for the slider fill
            if k == "rotate":     return (raw(k) % 360) / 360.0
            if k == "zoom":       return (raw(k) - 1.0) / (zmax - 1.0) if zmax > 1.0 else 0.0
            if k == "speed":      return min(1.0, raw(k) / 4.0)
            if k == "dir":        return 1.0 if raw(k) else 0.0
            if k in ("bright", "contrast"): return (raw(k) + 100) / 200.0
            if k == "trail_on":   return 1.0 if raw(k) else 0.0
            if k == "trail":      return (raw(k) / tmax) if tmax else 0.0
            if k == "trail_time": return min(1.0, raw(k) / 8.0)
            if k == "trail_mode":
                v = raw(k)
                return (tmodes.index(v) / max(1, len(tmodes) - 1)) if v in tmodes else 0.0
            if k == "trail_opc":  return max(0.0, min(1.0, raw(k)))
            return 0.0

        def fmt_val(k, _v):
            if k == "rotate":     return f"{raw(k)}°"
            if k in ("zoom", "speed"): return f"{raw(k):.2f}x"
            if k == "dir":        return "REV" if raw(k) else "FWD"
            if k in ("bright", "contrast"): return f"{int(raw(k)):+d}"
            if k == "trail_on":   return "ON" if raw(k) else "OFF"
            if k == "trail":      return str(int(raw(k)))
            if k == "trail_time": return f"{raw(k):.2f}s"
            if k == "trail_mode": return str(raw(k)).upper()[:9]
            if k == "trail_opc":  return f"{raw(k):.2f}"
            return ""

        def lfo_of(k):
            return cfg.clip_lfo(path, k) if k in ("zoom", "speed") else None

        def cc_of(k):
            return cfg.midi_target_cc.get(k) if k in ("zoom", "speed") else None

        name = os.path.basename(path).upper() if path else "NO CLIP"
        self._render_params_screen(
            d, font_sm, TAB_COL["SAMPLER"], name,
            list(slots), lambda k: labels[k], get_val, fmt_val,
            getattr(_kb, "_param_idx", 0),
            editing=getattr(_kb, "_editing_param", False),
            lfo_of=lfo_of, cc_of=cc_of,
            midi_assign=(getattr(_kb, "_midi_assign", False),
                         getattr(_kb, "_midi_cc_buf", "")))

    def _render_preset_opts(self, d, font_md, font_sm, inst, cfg, _kb):
        """Options for one saved preset (hold a filled preset cell to reach it).

        Deliberately not the arm-then-confirm pattern the old menu used for
        delete: holding to configure a thing is already the gesture everywhere
        else, and it lands you here where the action is named rather than
        being a second press of a scroll key.
        """
        from config import PRESET_STORES
        store, index = _kb._preset_opts
        name  = cfg.preset_name(store, index).replace(".json", "")
        label = PRESET_STORES[store]["label"]
        key   = _GRID_SLOTS[index % 9]
        sel   = getattr(_kb, "_preset_opt_idx", 0)

        d.text((10, TAB_H + 6), label, font=font_sm, fill=C_STAGED)
        d.line([0, TAB_H + 22, FB_W, TAB_H + 22], fill=C_DIVIDER, width=1)
        d.text((FB_W // 2, TAB_H + 44), name, font=font_md, fill=C_ON,
               anchor="mm")
        d.text((FB_W // 2, TAB_H + 66), f"page {index // 9 + 1}  ·  key {key}",
               font=font_sm, fill=C_HINT, anchor="mm")

        rows = _PRESET_OPTS
        y0   = TAB_H + 92
        rh   = 30
        for i, (_, text) in enumerate(rows):
            y   = y0 + i * rh
            on  = (i == sel)
            if on:
                d.rectangle([20, y, FB_W - 20, y + rh - 6],
                            fill=(0x00, 0x22, 0x00), outline=C_ON, width=2)
            d.text((FB_W // 2, y + (rh - 6) // 2), text, font=font_sm,
                   fill=C_ON if on else C_LABEL, anchor="mm")

        d.text((FB_W // 2, FB_H - 14), "+/Bksp choose   ENTER do   . back",
               font=font_sm, fill=C_HINT, anchor="mm")

    def _render_lfo_settings(self, d, font_sm, inst, cfg, _kb):
        """LFO settings screen (long-press an LFO cell to reach it). Edits one
        LFO: SHAPE, MIN (the value it drops to), MAX (the value it rises to,
        both on a 0..100 scale) and SPEED (period, or beat when synced)."""
        idx  = getattr(_kb, "_lfo_edit_idx", 0)
        lfos = getattr(cfg, "lfos", [])
        L    = lfos[idx] if 0 <= idx < len(lfos) else {}
        slots  = ("shape", "min", "max", "speed", "sync")
        labels = {"shape": "SHAPE", "min": "MIN", "max": "MAX",
                  "speed": "SPEED", "sync": "SYNC"}
        shapes = list(getattr(cfg, "LFO_SHAPES", ("SINE",)))
        beats  = list(getattr(cfg, "LFO_BEATS", (1.0,)))
        blabels = list(getattr(cfg, "LFO_BEAT_LABELS", ("1",)))

        def _min(): return float(L.get("offset", 0.0))
        def _max(): return _min() + float(L.get("amp", 0.5))
        def _period(): return float(L.get("period", 4.0))

        def get_val(k):   # normalised 0..1 for the slider fill
            if k == "shape":
                s = int(float(L.get("shape", 0)))
                return (s / max(1, len(shapes) - 1))
            if k == "min":   return max(0.0, min(1.0, _min()))
            if k == "max":   return max(0.0, min(1.0, _max()))
            if k == "speed":
                if L.get("bpm_sync"):
                    cur = float(L.get("beat", 1.0))
                    j   = beats.index(cur) if cur in beats else 3
                    # shorter beat = faster → fuller bar
                    return 1.0 - j / max(1, len(beats) - 1)
                # shorter period = faster → fuller bar (range ~0.1..16s)
                return max(0.0, min(1.0, 1.0 - (_period() - 0.1) / 15.9))
            if k == "sync":  return 1.0 if L.get("bpm_sync") else 0.0
            return 0.0

        def fmt_val(k, _v):
            if k == "shape":
                return shapes[int(float(L.get("shape", 0))) % len(shapes)]
            if k == "min":   return str(int(round(_min() * 100)))
            if k == "max":   return str(int(round(_max() * 100)))
            if k == "speed":
                if L.get("bpm_sync"):
                    cur = float(L.get("beat", 1.0))
                    lbl = blabels[beats.index(cur)] if cur in beats else f"{cur:g}"
                    return f"{lbl} beat"
                return f"{_period():.2f}s"
            if k == "sync":  return "BPM" if L.get("bpm_sync") else "SEC"
            return ""

        name = f"LFO {idx + 1}"
        self._render_params_screen(
            d, font_sm, C_STAGED, name,
            list(slots), lambda k: labels[k], get_val, fmt_val,
            getattr(_kb, "_param_idx", 0),
            editing=getattr(_kb, "_editing_param", False))

    def _render_sampler_tab(self, d, font_lg, font_md, font_sm, inst, cfg):
        _kb = getattr(inst, "kb", None)
        if getattr(_kb, "_param_layer", 0) == 5:
            self._render_clip_settings(d, font_sm, inst, cfg, _kb)
            return

        Y0 = TAB_H + 4
        s  = inst.sampler

        clip_name = os.path.basename(cfg.current_clip) if cfg.current_clip else "NO CLIP"
        d.text((10, Y0 + 2), clip_name[:24], font=font_md, fill=TAB_COL["SAMPLER"])

        # Status chips: PLAY MODE / OVL / TRL / REC
        cx      = 10
        chips_y = Y0 + 26
        d.text((cx, chips_y), s.mode.upper(), font=font_sm, fill=C_LABEL)
        cx += len(s.mode) * 9 + 12

        ovl_on = getattr(cfg, "overlay_on", False)
        d.text((cx, chips_y), "OVL", font=font_sm, fill=C_ON if ovl_on else C_HINT)
        cx += 42

        trail_on = getattr(cfg, "trail_on", False)
        d.text((cx, chips_y), "TRL", font=font_sm, fill=C_ON if trail_on else C_HINT)

        _rec_status = getattr(getattr(inst, "recorder", None), "status", "")
        if _rec_status == "REC":
            d.text((FB_W - 8, chips_y), "REC", font=font_sm,
                   fill=(0xcc, 0x00, 0x00), anchor="rm")
        elif _rec_status == "SAV":
            d.text((FB_W - 8, chips_y), "SAV", font=font_sm,
                   fill=(0xaa, 0x55, 0x00), anchor="rm")

        fx_chain = getattr(cfg, "fx_chain", [])
        if fx_chain:
            fx_name = " > ".join(f.replace(".glsl","").upper() for f in fx_chain)[:20]
        else:
            fx_name = "—"
        fx_col  = C_VALUE if getattr(cfg, "shader_fx_stack", False) and fx_chain else C_LABEL
        d.text((10, Y0 + 44), f"FX  {fx_name}", font=font_sm, fill=fx_col)

        spd = getattr(s, "speed", 1.0)
        _kb = getattr(inst, "kb", None)
        spd_col = C_SEL if (getattr(_kb, "_param_layer", 0) == 5
                            and getattr(_kb, "_param_idx", 0) == 0) else C_VALUE
        d.text((FB_W - 8, Y0 + 44), f"SPD {spd:.2f}x",
               font=font_sm, fill=spd_col, anchor="rm")

        dy = Y0 + 62
        d.line([0, dy, FB_W, dy], fill=C_DIVIDER, width=1)

        _kb     = getattr(inst, "kb", None)
        sel_idx = getattr(_kb, "_param_idx", 0)
        flabels = inst.shader.fx_param_labels()
        has_fx  = bool(getattr(cfg, "current_fx", None))
        fx_items = [
            (flabels.get(k, k.upper()).upper()[:7],
             cfg.fx_params.get(k, 0.5),
             f"{cfg.fx_params.get(k, 0.5):.2f}" if has_fx else "—",
             has_fx)
            for k in sorted(flabels.keys(), key=lambda k: int(k[1:]))
        ]
        self._draw_bars(d, font_sm, fx_items, sel_idx, y0=dy + 4)

        self._draw_timeline(d, font_sm, s, y_base=270)

    # ── LIVE tab ──────────────────────────────────────────────────────────────

    def _render_live_tab(self, d, font_lg, font_md, font_sm, inst, cfg):
        Y0 = TAB_H + 4
        s  = inst.sampler

        # This tab is not the instrument's mode: the camera only starts when the
        # mode is LIVE, so outside LIVE mode it was never asked for and saying
        # "NO CAMERA" is a lie. Only claim that when the mode IS LIVE — then
        # play_camera() has run and genuinely failed to give us a camera.
        if getattr(s, "_active_source", None) == "camera":
            d.text((10, Y0 + 6), "LIVE", font=font_lg, fill=TAB_COL["LIVE"])
        elif getattr(inst, "mode", None) != "LIVE":
            d.text((10, Y0 + 6), "CAMERA IDLE", font=font_md, fill=C_LABEL)
            d.text((10, Y0 + 30), f"MODE: {getattr(inst, 'mode', '—')}",
                   font=font_sm, fill=C_HINT)
        else:
            d.text((10, Y0 + 6), "NO CAMERA", font=font_md, fill=C_HINT)

        _rec_status = getattr(getattr(inst, "recorder", None), "status", "")
        if _rec_status == "REC":
            d.rectangle([10, Y0 + 54, 68, Y0 + 72], fill=(0xcc, 0, 0))
            d.text((39, Y0 + 63), "REC", font=font_sm, fill=(0xff, 0xff, 0xff), anchor="mm")
        elif _rec_status == "SAV":
            d.rectangle([10, Y0 + 54, 68, Y0 + 72], fill=(0xaa, 0x55, 0))
            d.text((39, Y0 + 63), "SAV", font=font_sm, fill=(0xff, 0xff, 0xff), anchor="mm")

        cam_w = getattr(cfg, "camera_width",  0)
        cam_h = getattr(cfg, "camera_height", 0)
        if cam_w and cam_h:
            d.text((FB_W - 8, Y0 + 6), f"{cam_w}×{cam_h}",
                   font=font_sm, fill=C_LABEL, anchor="rm")

        dy = Y0 + 82
        d.line([0, dy, FB_W, dy], fill=C_DIVIDER, width=1)
        dy += 6

        trail_on = getattr(cfg, "trail_on",   False)
        ovl_on   = getattr(cfg, "overlay_on", False)
        d.text((10, dy),      f"TRAIL    {'ON' if trail_on else 'OFF'}",
               font=font_sm, fill=C_ON if trail_on else C_HINT)
        d.text((10, dy + 20), f"OVERLAY  {'ON' if ovl_on else 'OFF'}",
               font=font_sm, fill=C_ON if ovl_on else C_HINT)

    # ── FX params tab ────────────────────────────────────────────────────────

    def _render_fx_params_tab(self, d, font_lg, font_md, font_sm, inst, cfg):
        flabels  = inst.shader.fx_param_labels()
        has_fx   = bool(getattr(cfg, "current_fx", None))
        all_keys = inst.shader.fx_row_keys() if has_fx else []
        sel_idx  = getattr(getattr(inst, "kb", None), "_param_idx", 0)
        tab_col  = TAB_COL["FX"]
        fx_chain = getattr(cfg, "fx_chain", [])
        fx_col   = tab_col if getattr(cfg, "shader_fx_stack", False) and fx_chain else C_LABEL
        blend_modes = list(getattr(cfg, "FX_LAYER_BLEND_MODES", ("normal",)))

        def get_lbl(k):
            if k == "__blend_mode__":
                return "BLEND"
            if k == "__blend_amt__":
                return "BLD AMT"
            return flabels.get(k, k.upper()).upper()

        def get_val(k):
            if k == "__blend_mode__":
                cur = cfg.fx_blend.get("mode", "normal")
                i   = blend_modes.index(cur) if cur in blend_modes else 0
                return i / max(1, len(blend_modes) - 1)
            if k == "__blend_amt__":
                return cfg.fx_blend.get("amt", 1.0)
            return cfg.fx_params.get(k, 0.5)

        def fmt_val(k, v):
            if k == "__blend_mode__":
                return cfg.fx_blend.get("mode", "normal").upper()[:9]
            return f"{v:.2f}"

        slot = getattr(cfg, "fx_edit_slot", 0)
        n    = len(fx_chain)
        slot_tag = f" [{slot+1}/{n}]" if n > 1 else ""
        name    = ((cfg.current_fx or "—").replace(".glsl", "").upper()) + slot_tag
        editing = getattr(getattr(inst, "kb", None), "_editing_param", False)
        self._render_params_screen(d, font_sm, fx_col, name,
                                   all_keys, get_lbl, get_val, fmt_val, sel_idx,
                                   editing=editing,
                                   lfo_of=lambda k: cfg.fx_params.get("lfo_" + k),
                                   cc_of=lambda k: cfg.midi_target_cc.get(k),
                                   midi_assign=(getattr(getattr(inst, "kb", None), "_midi_assign", False),
                                                getattr(getattr(inst, "kb", None), "_midi_cc_buf", "")))

    # ── SETTINGS tab ──────────────────────────────────────────────────────────

    def _render_settings_tab(self, d, font_lg, font_md, font_sm, inst, cfg):
        Y0 = TAB_H + 4

        now = time.monotonic()
        if now - self._ip_ts > 30:
            self._cached_ip = _local_ip()
            self._ip_ts     = now

        clip_name   = os.path.basename(cfg.current_clip) if cfg.current_clip else "—"
        shader_name = (cfg.current_shader or "—").replace(".glsl", "").upper()

        rows = [
            ("IP",    self._cached_ip),
            ("MODE",  inst.mode),
            ("PLAY",  inst.sampler.mode.upper()),
            ("CLIP",  clip_name[:20]),
            ("SHDR",  shader_name[:20]),
        ]
        for i, (lbl, val) in enumerate(rows):
            y = Y0 + 4 + i * 20
            d.text((10,        y), lbl, font=font_sm, fill=C_LABEL)
            d.text((FB_W - 8,  y), val, font=font_sm, fill=C_VALUE, anchor="rm")

        dy = Y0 + 4 + len(rows) * 20 + 4
        d.line([0, dy, FB_W, dy], fill=C_DIVIDER, width=1)
        dy += 6
        d.text((10, dy), "NUM SHDR  /  SMPL  *  LIVE  -  HERE", font=font_sm, fill=C_HINT)

    # ── Shared bar renderer ────────────────────────────────────────────────────

    def _draw_bars(self, d, font_sm, bar_items, sel_idx, y0=86):
        BAR_X    = 90
        BAR_W    = 305
        BAR_H    = 14
        bar_gap  = 16 if len(bar_items) > 4 else 18
        max_bars = max(1, (FB_H - 24 - y0) // bar_gap)

        for i, (ltext, fill_val, disp_val, active) in enumerate(bar_items[:max_bars]):
            by       = y0 + i * bar_gap
            selected = active and (i == sel_idx)
            lcolour  = C_VALUE if selected else (C_LABEL if active else C_DIVIDER)
            bcolour  = C_SEL   if selected else (C_BAR_FILL if active else C_DIVIDER)
            d.text((10, by), ltext, font=font_sm, fill=lcolour)
            d.rectangle([BAR_X, by + 2, BAR_X + BAR_W, by + BAR_H + 2],
                        fill=C_BAR_TRACK)
            if active:
                filled = max(1, int(BAR_W * fill_val))
                d.rectangle([BAR_X, by + 2, BAR_X + filled, by + BAR_H + 2],
                            fill=bcolour)
            d.text((BAR_X + BAR_W + 8, by), disp_val, font=font_sm, fill=lcolour)

    # ── Playhead timeline ─────────────────────────────────────────────────────

    def _draw_timeline(self, d, font_sm, s, y_base=270):
        dur    = getattr(s, "duration", 0.0)
        in_pt  = getattr(s, "in_pt",    0.0)
        out_pt = getattr(s, "out_pt",   None)
        pos    = getattr(s, "time_pos", 0.0)

        TL_Y = y_base
        TL_H = 8

        d.rectangle([TL_X, TL_Y, TL_X + TL_W, TL_Y + TL_H], fill=C_BAR_TRACK)

        if not (dur and dur > 0):
            d.text((TL_X + TL_W // 2, TL_Y + 4), "no clip",
                   font=font_sm, fill=C_HINT, anchor="mm")
            return

        pos_c   = max(0.0, min(pos, dur))
        in_px   = int(in_pt / dur * TL_W)
        out_end = out_pt if out_pt is not None else dur
        out_px  = min(int(out_end / dur * TL_W), TL_W)
        pos_px  = int(pos_c / dur * TL_W)

        if out_px > in_px:
            d.rectangle([TL_X + in_px, TL_Y, TL_X + out_px, TL_Y + TL_H],
                        fill=(0x00, 0x44, 0x33))
        if in_pt > 0.0:
            d.rectangle([TL_X + in_px - 1, TL_Y - 3,
                         TL_X + in_px + 1, TL_Y + TL_H + 3], fill=C_ON)
        if out_pt is not None:
            d.rectangle([TL_X + out_px - 1, TL_Y - 3,
                         TL_X + out_px + 1, TL_Y + TL_H + 3],
                        fill=(0xff, 0x88, 0x00))
        d.rectangle([TL_X + pos_px - 1, TL_Y - 3,
                     TL_X + pos_px + 1, TL_Y + TL_H + 3], fill=C_VALUE)

        in_lbl  = f"IN {in_pt:.1f}s"   if in_pt > 0.0      else "START"
        out_lbl = f"OUT {out_pt:.1f}s" if out_pt is not None else "END"
        in_col  = C_ON                  if in_pt > 0.0      else C_HINT
        out_col = (0xff, 0x88, 0x00)    if out_pt is not None else C_HINT

        LY = TL_Y + TL_H + 4
        d.text((TL_X,              LY), in_lbl,                    font=font_sm, fill=in_col)
        d.text((TL_X + TL_W // 2, LY), f"{pos_c:.1f}/{dur:.1f}s", font=font_sm,
               fill=C_LABEL, anchor="mm")
        d.text((TL_X + TL_W,      LY), out_lbl,                   font=font_sm,
               fill=out_col, anchor="rm")
