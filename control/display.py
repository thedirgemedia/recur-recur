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

SPI_BUS   = 0
SPI_DEV   = 0
SPI_SPEED = 8_000_000
GPIO_DC   = 24
GPIO_RST  = 25

# ── palette ──────────────────────────────────────────────────────────────────
C_BG        = (0x0d, 0x0d, 0x0d)
C_DIVIDER   = (0x2a, 0x2a, 0x2a)
C_LABEL     = (0x77, 0x77, 0x77)
C_VALUE     = (0xee, 0xee, 0xee)
C_BAR_TRACK = (0x22, 0x22, 0x22)
C_BAR_FILL  = (0x00, 0xbb, 0xff)
C_ON        = (0x00, 0xff, 0x88)
C_SEL       = (0xff, 0x88, 0x00)   # orange — selected parameter bar
C_HINT      = (0x44, 0x44, 0x44)

MODE_COLOURS = {
    "SAMPLER": (0xff, 0x99, 0x00),
    "SHADER":  (0xaa, 0x44, 0xff),
    "LIVE":    (0x00, 0xff, 0x55),
}

FONT_PATH = "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"

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


def _load_font(size):
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except (OSError, IOError):
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

class DisplayController:
    def __init__(self, inst):
        self.inst    = inst
        self._stop   = threading.Event()
        self._thread = None

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

        last_snapshot  = b""
        last_param_sig = None
        frame_n = 0
        # Sample 128 evenly-spaced bytes as a cheap pixel-change detector.
        # Pixel sampling alone can miss small bar-fill changes (e.g. p5 at low
        # values has a short bar; none of the 128 sample points may land in the
        # changed region).  A secondary param-signature tuple catches any cfg
        # or selection change that the pixel samples would miss.
        _STEP = (FB_W * FB_H * 2) // 128
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                try:
                    inst = self.inst
                    cfg  = inst.cfg
                    _kb  = getattr(inst, "kb", None)
                    _rec = getattr(inst, "recorder", None)
                    param_sig = (
                        tuple(cfg.params.get(f"p{n}", 0.5) for n in range(1, 11)),
                        cfg.fx_params.get("f1", 0.5), cfg.fx_params.get("f2", 0.5),
                        cfg.fx_params.get("f3", 0.5), cfg.fx_params.get("f4", 0.5),
                        round(getattr(cfg, "shader_blend_amount",   0.5),  3),
                        round(getattr(cfg, "overlay_blend_amount",  1.0) or 1.0, 2),
                        round(getattr(cfg, "trail_decay",           0.93), 3),
                        getattr(cfg, "overlay_on",    False),
                        getattr(cfg, "overlay_mode",  ""),
                        getattr(cfg, "shader_blend",  False),
                        getattr(cfg, "shader_blend_mode",   ""),
                        getattr(cfg, "shader_blend_source", ""),
                        getattr(cfg, "trail_on",      False),
                        getattr(_kb, "_param_idx",   0),
                        getattr(_kb, "_param_layer", 0),
                        # redraw when the active shader / fx / colour changes
                        getattr(cfg, "current_shader", None),
                        getattr(cfg, "current_fx",     None),
                        round(getattr(cfg, "color_hue", 0.0), 4),
                        round(getattr(cfg, "color_sat", 1.0), 3),
                        getattr(_rec, "status", ""),
                    )
                    img  = self._render(font_lg, font_md, font_sm)
                    data = _to_spi_bytes(img)
                    # The menu changes in small ways the 128-byte sample and the
                    # fixed param_sig don't capture (page swap, selection move, a
                    # single value flipping). While it's open, compare the FULL
                    # frame so every visible change is redrawn; the status view
                    # keeps the cheap sampled compare.
                    _menu = getattr(inst, "menu", None)
                    if _menu is not None and _menu.active:
                        snap = data
                    else:
                        snap = data[::_STEP]
                    if snap != last_snapshot or param_sig != last_param_sig:
                        disp.write_frame(data)
                        last_snapshot  = snap
                        last_param_sig = param_sig
                        frame_n += 1
                        log.debug("frame %d written (%.0f ms)",
                                  frame_n, (time.monotonic() - t0) * 1000)
                except Exception as e:
                    log.warning("render error: %s", e)
                self._stop.wait(1.0 / FPS)
        finally:
            disp.close()

    # ── frame builder ─────────────────────────────────────────────────────────

    def _render(self, font_lg, font_md, font_sm):
        inst = self.inst
        cfg  = inst.cfg
        mode = inst.mode

        img = Image.new("RGB", (FB_W, FB_H), C_BG)
        d   = ImageDraw.Draw(img)

        # when the navigable menu is active, draw it instead of the status view
        menu = getattr(inst, "menu", None)
        if menu is not None and menu.active:
            palette = (C_BG, (0xcc, 0x66, 0x00), C_LABEL, C_VALUE,
                       C_HINT, C_BAR_FILL)
            menu.render(img, d, font_lg, font_md, font_sm, FB_W, FB_H, palette)
            return img

        mc     = MODE_COLOURS.get(mode, (0xff, 0xff, 0xff))
        mc_dim = tuple(c // 5 for c in mc)

        # header background
        d.rectangle([0, 0, FB_W, 54], fill=mc_dim)

        # top half: mode label (left) + TRAIL indicator + clip name (right)
        d.text((10, 2), mode, font=font_md, fill=mc)

        trail_on = getattr(cfg, "trail_on", False)
        if trail_on:
            d.rectangle([116, 6, 162, 23], fill=C_ON)
            d.text((139, 14), "TRAIL", font=font_sm, fill=(0, 0, 0), anchor="mm")
        else:
            d.text((139, 14), "TRAIL", font=font_sm, fill=C_HINT, anchor="mm")

        # REC / SAV indicator
        _rec_status = getattr(getattr(inst, "recorder", None), "status", "")
        if _rec_status == "REC":
            d.rectangle([168, 6, 202, 23], fill=(0xcc, 0x00, 0x00))
            d.text((185, 14), "REC", font=font_sm, fill=(0xff, 0xff, 0xff), anchor="mm")
        elif _rec_status == "SAV":
            d.rectangle([168, 6, 202, 23], fill=(0xaa, 0x55, 0x00))
            d.text((185, 14), "SAV", font=font_sm, fill=(0xff, 0xff, 0xff), anchor="mm")

        # param-layer indicator (BKSP cycles): SHDR / FX / COLOUR / BLEND
        _hdr_layer = getattr(getattr(inst, "kb", None), "_param_layer", 0)
        _hdr_lbl   = {1: "FX", 2: "COLOUR", 3: "BLEND"}.get(_hdr_layer)
        _lx = 208   # shift right when REC/SAV chip is visible
        if _hdr_lbl:
            d.rectangle([_lx, 6, _lx + 76, 23], fill=C_SEL)
            d.text((_lx + 38, 14), _hdr_lbl, font=font_sm, fill=(0, 0, 0), anchor="mm")
        else:
            d.text((_lx + 32, 14), "SHDR", font=font_sm, fill=C_LABEL, anchor="mm")

        clip_name = os.path.basename(cfg.current_clip) if cfg.current_clip else "—"
        d.text((FB_W - 8, 12), clip_name[:20], font=font_sm, fill=C_LABEL, anchor="rm")

        # divider between top and bottom halves of header
        d.line([0, 27, FB_W, 27], fill=C_DIVIDER, width=1)

        # bottom half: shader name | FX name | blend mode | playback mode
        COL_W = FB_W // 4   # 120 px per column
        CY    = 41          # vertical centre of bottom half (28–54)

        shader_disp  = (cfg.current_shader or "—").replace(".glsl", "").upper()[:10]
        fx_disp      = (cfg.current_fx     or "—").replace(".glsl", "").upper()[:10]
        shader_col   = C_VALUE if mode == "SHADER" else (C_LABEL if cfg.current_shader else C_HINT)
        fx_col       = C_VALUE if getattr(cfg, 'shader_fx_stack', False) \
                               else (C_LABEL if cfg.current_fx else C_HINT)

        overlay_on   = getattr(cfg, "overlay_on",   False)
        shader_blend = getattr(cfg, "shader_blend", False)
        if overlay_on:
            blend_disp = cfg.overlay_mode.upper()[:10]
            blend_col  = C_ON
        elif shader_blend:
            blend_disp = cfg.shader_blend_mode.upper()[:10]
            blend_col  = C_ON
        else:
            blend_disp = "—"
            blend_col  = C_HINT

        play_disp = inst.sampler.mode.upper()[:10]

        for col in range(1, 4):
            x = COL_W * col
            d.line([x, 28, x, 54], fill=C_DIVIDER, width=1)

        d.text((COL_W * 0 + COL_W // 2, CY), shader_disp, font=font_sm, fill=shader_col, anchor="mm")
        d.text((COL_W * 1 + COL_W // 2, CY), fx_disp,     font=font_sm, fill=fx_col,     anchor="mm")
        d.text((COL_W * 2 + COL_W // 2, CY), blend_disp,  font=font_sm, fill=blend_col,  anchor="mm")
        d.text((COL_W * 3 + COL_W // 2, CY), play_disp,   font=font_sm, fill=C_LABEL,    anchor="mm")

        d.line([0, 55, FB_W, 55], fill=C_DIVIDER, width=1)

        BAR_X   = 90          # widened from 55 to fit real parameter names
        BAR_W   = 305         # reduced accordingly (total bar area stays ~395px)
        BAR_H   = 14
        BAR_GAP = 18

        _kb          = getattr(inst, "kb", None)
        _param_layer = getattr(_kb, "_param_layer", 0)
        _sel_idx     = getattr(_kb, "_param_idx",   0)

        if _param_layer == 0:          # SHDR — generative shader params (all at once)
            _plabels = inst.shader.param_labels()
            all_keys = sorted(_plabels.keys())
            bar_items = []
            for k in all_keys:
                raw_lbl = _plabels.get(k, k.upper())
                lbl     = raw_lbl.upper()[:7]
                v       = cfg.params.get(k, 0.5)
                ul      = raw_lbl.upper()
                if ul.endswith(' X') or ul.endswith(' Y') or ul in ('X', 'Y'):
                    disp_v = f"{(v - 0.5) * 200:+.0f}"
                elif ul.endswith('STARS') or ul == 'STARS':
                    disp_v = str(max(1, round(v * 500)))
                else:
                    disp_v = f"{v:.2f}"
                bar_items.append((lbl, v, disp_v, True))
        elif _param_layer == 1:        # FX — the active FX shader's own params
            _flabels = inst.shader.fx_param_labels()
            has_fx   = bool(getattr(cfg, 'current_fx', None))
            bar_items = [
                (_flabels.get(f"f{n}", f"F{n}").upper()[:7],
                 cfg.fx_params.get(f"f{n}", 0.5),
                 f"{cfg.fx_params.get(f'f{n}', 0.5):.2f}" if has_fx else "—",
                 has_fx)
                for n in range(1, 5)
            ]
        elif _param_layer == 2:        # COLOUR — hue / sat / trl dec
            hue     = getattr(cfg, 'color_hue', 0.0)
            sat     = getattr(cfg, 'color_sat', 1.0)
            sat_max = getattr(cfg, 'COLOR_SAT_MAX', 2.0)
            trl     = getattr(cfg, 'trail_decay', 0.93)
            bar_items = [
                ("HUE",     hue,             f"{hue*360:.0f}°",    True),
                ("SAT",     sat / sat_max,   f"{sat:.2f}",         True),
                ("TRL DEC", (trl-0.80)/0.19, f"{trl:.2f}",        True),
                ("—",       0.0,             "—",                  False),
            ]
        else:                          # BLEND — compositing (shader blend / overlay)
            def _idx_frac(seq, val):
                seq = list(seq)
                i = seq.index(val) if val in seq else 0
                return i / max(1, len(seq) - 1)
            if mode == "SHADER":
                modes = getattr(cfg, 'SHADER_BLEND_MODES', ())
                amt   = getattr(cfg, 'shader_blend_amount', 0.5)
                srcs  = getattr(cfg, 'SHADER_BLEND_SOURCES', ())
                bar_items = [
                    ("MODE",    _idx_frac(modes, cfg.shader_blend_mode),
                     cfg.shader_blend_mode.upper()[:9], True),
                    ("BLD AMT", amt, f"{amt*100:.0f}%", True),
                    ("SRC",     _idx_frac(srcs, cfg.shader_blend_source),
                     cfg.shader_blend_source.upper(), True),
                    ("—",       0.0, "—", False),
                ]
            else:
                modes = getattr(cfg, 'OVERLAY_MODES', ())
                opc   = getattr(cfg, 'overlay_blend_amount', 1.0)
                bar_items = [
                    ("OVL MODE", _idx_frac(modes, cfg.overlay_mode),
                     cfg.overlay_mode.upper()[:9], True),
                    ("OVL OPC",  opc, f"{opc:.2f}", True),
                    ("—", 0.0, "—", False),
                    ("—", 0.0, "—", False),
                ]

        # Tighten row spacing when more than 4 bars so they all fit on screen.
        bar_gap = 16 if len(bar_items) > 4 else BAR_GAP

        for i, (ltext, fill_val, disp_val, active) in enumerate(bar_items):
            by       = 64 + i * bar_gap
            selected = active and (i == _sel_idx)
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

        # ── SAMPLER: in/out playhead timeline (pinned to bottom) ─────────────
        if mode == "SAMPLER":
            s      = inst.sampler
            dur    = getattr(s, "duration",  0.0)
            in_pt  = getattr(s, "in_pt",     0.0)
            out_pt = getattr(s, "out_pt",    None)
            pos    = getattr(s, "time_pos",  0.0)

            TL_X = 10
            TL_Y = 280
            TL_W = FB_W - 20   # 460 px
            TL_H = 8

            # background track
            d.rectangle([TL_X, TL_Y, TL_X + TL_W, TL_Y + TL_H],
                        fill=C_BAR_TRACK)

            if dur and dur > 0:
                pos_c  = max(0.0, min(pos, dur))
                in_px  = int(in_pt / dur * TL_W)
                out_end = out_pt if out_pt is not None else dur
                out_px  = min(int(out_end / dur * TL_W), TL_W)
                pos_px  = int(pos_c / dur * TL_W)

                # tinted active zone (in → out)
                if out_px > in_px:
                    d.rectangle([TL_X + in_px, TL_Y,
                                 TL_X + out_px, TL_Y + TL_H],
                                fill=(0x00, 0x44, 0x33))

                # in-point tick — green, shown only when not at start
                if in_pt > 0.0:
                    d.rectangle([TL_X + in_px - 1, TL_Y - 3,
                                 TL_X + in_px + 1, TL_Y + TL_H + 3],
                                fill=C_ON)

                # out-point tick — amber, shown only when explicitly set
                if out_pt is not None:
                    d.rectangle([TL_X + out_px - 1, TL_Y - 3,
                                 TL_X + out_px + 1, TL_Y + TL_H + 3],
                                fill=(0xff, 0x88, 0x00))

                # playhead — white
                d.rectangle([TL_X + pos_px - 1, TL_Y - 3,
                             TL_X + pos_px + 1, TL_Y + TL_H + 3],
                            fill=C_VALUE)

                # labels: in (left) | pos/dur (centre) | out (right)
                in_lbl  = f"IN {in_pt:.1f}s"        if in_pt > 0.0      else "START"
                out_lbl = f"OUT {out_pt:.1f}s"       if out_pt is not None else "END"
                pos_lbl = f"{pos_c:.1f} / {dur:.1f}s"
                in_col  = C_ON                        if in_pt > 0.0      else C_HINT
                out_col = (0xff, 0x88, 0x00)          if out_pt is not None else C_HINT

                LY = TL_Y + TL_H + 4
                d.text((TL_X,              LY), in_lbl,  font=font_sm, fill=in_col)
                d.text((TL_X + TL_W // 2, LY), pos_lbl, font=font_sm,
                       fill=C_LABEL, anchor="mm")
                d.text((TL_X + TL_W,      LY), out_lbl, font=font_sm,
                       fill=out_col, anchor="rm")
            else:
                d.text((TL_X + TL_W // 2, TL_Y + 4), "no clip",
                       font=font_sm, fill=C_HINT, anchor="mm")

        return img
