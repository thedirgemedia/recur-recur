# recur-recur — Operator Manual

*Accurate as of 2026-06-12. Generated from the actual code paths — where the
code and older docs disagreed, this manual follows the code.*

---

## Overview

recur-recur is a live video instrument for the Raspberry Pi 5, inspired by
r_e_c_u_r. A single mpv process owns the HDMI output at all times and is
driven over a JSON IPC socket (`/tmp/recur-mpv.sock`). GLSL shaders are
applied live through mpv's `glsl-shaders` hook system; temporal effects
(trail, overlay) are lavfi video-filter chains.

Three instrument modes determine the video source and what the controls do:

| Mode | Source | Typical use |
|---|---|---|
| **SAMPLER** | video clip | clip playback + FX shaders + overlay/trail |
| **SHADER** | generative GLSL (clip keeps playing hidden behind it) | synth visuals, optionally blended with clip/camera |
| **LIVE** | Pi Camera (CSI) or USB camera | camera input + FX shaders + overlay/trail |

Status and menus appear on the 3.5" SPI display. The HDMI output never shows
UI — it is always pure video.

---

## Hardware

| Component | Notes |
|---|---|
| Raspberry Pi 5 | mpv renders via DRM on `/dev/dri/card1` |
| HDMI output | 1280×720 default; 720×576@25 for composite |
| Waveshare 3.5" ILI9486 SPI display | SPI0.0, DC=GPIO24, RST=GPIO25 — status + menu |
| 19-key USB numpad (SIGMACHIP) | primary performance controller, hot-replug safe |
| MCP3008 ADC on SPI0.1 | 4 knobs → shader params p1–p4 |
| Tact buttons BCM 5 / 6 / 13 / 19 | mode / trigger / record / play-mode |
| USB MIDI controller | optional, hot-plugged (3 s rescan) |
| Pi Camera (CSI) or USB V4L2 camera | LIVE mode |

---

## Starting and stopping

Normally runs as a systemd service (appliance mode):

```bash
sudo systemctl restart recur     # restart the instrument
sudo systemctl stop recur        # stop it
journalctl -u recur -f           # live logs
```

Manual start for debugging:

```bash
cd ~/recur-recur
python3 main.py [--output hdmi|composite] [--mode SAMPLER|SHADER|LIVE]
                [--clips-dir clips/] [--shaders-dir shaders/]
                [--resolution 1280x720] [--no-midi] [--no-gpio] [-v]
```

Clips directory accepts `.mp4 .mov .mkv .avi .webm` (local clips scanned at
startup; removable drives re-scanned each time the BROWSER menu page is opened).
Diagnostics: mpv errors in `/tmp/mpv.err`, camera errors in `/tmp/rpicam.err`.

---

## Numpad layout

```
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
```

### Keys active in every mode

| Key | Action |
|---|---|
| **Num** | Toggle the menu (while the menu is open, no key reaches the video) |
| **Enter** | Cycle instrument mode (SAMPLER → SHADER → LIVE → …; LIVE skipped if disabled in SETTINGS) |
| **000** or **.** | Toggle the temporal trail (echo time delay) |
| **Bksp** | Cycle param layer for this mode: SHADER mode = SHDR → FX → GLOBAL; SAMPLER/LIVE = FX → GLOBAL. Top-bar chip shows `SHDR` / `FX` / `GLOBAL` |
| **1** | Cycle the selected parameter (see *Parameter editing*) |
| **2** / **3** | Selected parameter − / + |
| **0** | In/out points: 1st press = IN, 2nd = OUT, 3rd = clear |

### SAMPLER and LIVE modes

| Key | Action |
|---|---|
| **4–9** | Trigger the clip assigned to that slot (assign in BROWSER menu) |
| **+** / **−** | Next / previous FX shader applied over the video |
| **/** | Toggle V-overlay (self-blend echo) |
| **\*** | Cycle overlay blend mode (difference → addition → multiply → screen → negate) |

### SHADER mode

| Key | Action |
|---|---|
| **4–9** | Load the generative shader assigned to that slot (assign in SHADERS menu) |
| **+** / **−** | Cycle FX shader *stacked on top of* the generative (generative stays loaded) |
| **/** | Toggle shader blend (composite generative with clip or camera) |
| **\*** | Cycle shader blend mode (mix, screen, add, multiply, overlay, hardlight, softlight, dodge, burn, lighten, darken, difference, exclusion, **displace**) |

> Note the pattern: **/** always *toggles* an effect, **\*** always *cycles its
> mode* — in both mode families.

---

## Parameter editing

**Bksp** cycles the parameter layers available in the current mode. The active
layer shows in the SPI top bar (`SHDR` / `FX` / `BLEND` / `GLOBAL` chip) and as
the bars below the header. Key **1** cycles the selection within a layer;
**2/3** step (or cycle, for the BLEND mode/source slots); knobs and MIDI feed
the same params.

- **SHADER mode:** SHDR → FX → BLEND → GLOBAL
- **SAMPLER / LIVE mode:** FX → BLEND → GLOBAL  (no generative shader → SHDR skipped)

### Layer SHDR — generative shader params *(SHADER mode only)*

p1–p4 = the generative shader's parameters (labels from its source). Loading a
generative resets them to its authored defaults.

### Layer FX — the active FX shader's own params

Shows the four parameters of the **currently selected FX** (feedback, bitcrush,
glitch, grain, vhs …), with labels read from the shader source — e.g. glitch =
`SLICE INTENSITY / UPDATE RATE / CHANNEL CORRUPT / BLOCK DENSITY`. Kept separate
from the generative's p1–p4, so the FX is tunable even when stacked on a
generative in SHADER mode. Cycling the FX (`+`/`-`) reloads its defaults.
Persists in `prefs.json` (`fx_params`).

### Layer BLEND — compositing controls

The full editing surface for the active blend (what `*` quick-cycles). **2/3**
cycles the mode or steps the amount:

| Mode | Slots |
|---|---|
| **SHADER** | `MODE` (shader↔video blend, 14 modes incl. displace), `BLD AMT`, `SRC` (clip/live) |
| **SAMPLER / LIVE** | `OVL MODE` (5 self-blend modes), `OVL OPC` |

### Layer GLOBAL — master adjustments (all modes)

Key **1** cycles `HUE` → `SAT` → `TRL DEC`; **2/3** step:

| Param | Range | Meaning |
|---|---|---|
| `HUE` | 0–360° | global hue rotation (0 = no shift); wraps. GLSL colour pass |
| `SAT` | 0–2 | global saturation (0 = greyscale, 1 = normal, 2 = vivid) |
| `TRL DEC` | 0.80–0.99 | trail persistence (0.80 short ghost → 0.99 long tail) |

HUE/SAT apply to the final picture in every mode via a GLSL pass appended last
(no cost at neutral hue 0°, sat 1.0). Persist in `prefs.json`.

---

## SPI display

**Top bar (status view):**
- Row 1: mode name (colour-coded), `TRAIL` chip (green when on),
  param-layer chip (`SHDR` / `FX` / `GLOBAL`), current clip name.
- Row 2, four columns: generative shader | FX shader | active blend mode | sampler play mode.
- Below: the four parameter bars of the active layer (selected bar is orange);
  the SHDR layer adds a `BLD AMT` bar when shader-blending.
- SAMPLER mode only: clip timeline at the bottom — white tick = playhead,
  green tick = IN point, amber tick = OUT point, tinted region = active loop.

There is **no touch input** — all control is numpad / MIDI / GPIO.

---

## Menu system

Press **Num** to open/close. Four pages, cycled with **7** (prev) and **9**
(next; **/** also pages forward).

| Key | Action in menu |
|---|---|
| **8 / 2** | selection up / down |
| **4 / 6** | adjust value (SETTINGS, MIDI pages) |
| **5** | primary action: load (BROWSER/SHADERS), activate (SETTINGS), edit CC (MIDI) |
| **Enter** | BROWSER/SHADERS: start slot-assign; elsewhere same as 5 |
| **Num** | exit menu |

### BROWSER page
Lists clips from internal `clips/` (plus any video files on drives already
mounted under `/media/` or `/mnt/`, re-scanned each time you open the page).
To pull files off a USB stick, use the **IMPORT page** — drives are mounted on
demand there and copied into internal storage. `▶` marks the currently playing
clip. The right column shows the assigned slot key (4–9) for clips that have
one; for clips from a mounted removable drive it shows a short drive label.
- **5** — load + trigger the highlighted clip immediately.
- **Enter, then a key 4–9** — assign the highlighted clip to that performance
  slot (any other key cancels). A clip can hold only one slot; assigning moves it.
- **Bksp, then Bksp again** — delete the highlighted clip file. The first press
  arms it (`BKSP again = DELETE FILE`); any other key cancels. Only internal
  `clips/` files can be deleted (removable drives are read-only); the file is
  removed from disk and cleared from any slot it held.

### SHADERS page
Same as BROWSER but for generative shaders, feeding the SHADER-mode 4–9 keys.

### SETTINGS page

| Row | Description |
|---|---|
| MODE | instrument mode (4/6 or 5 cycles) |
| LIVE MODE | ON/OFF — OFF removes LIVE from the Enter-key cycle |
| PLAY | sampler playback mode (see *Playback modes*) |
| CAM RES | camera capture: 320×180 / 640×360 / 1280×720 (applies on next LIVE entry) |
| OVERLAY / OVL MODE | V-overlay toggle and blend mode |
| TRAIL / TRAIL TYPE / TRAIL MODE | trail toggle, blend type (MODE/OPACITY), and blend mode |
| BLEND / BLEND MODE / BLEND AMT / BLEND SRC | shader blend toggle, mode, mix, source (clip/live) |
| FX / GEN | cycle the active FX / generative shader |
| P1–P4 | shader params (±0.05 per press, labels from shader source) |
| SAVE PREFS | write current state to `prefs.json` now |
| SYSTEM | quit the application (a Pi poweroff would need root; the service can't escalate) |

### MIDI page
Per-target CC overrides. Defaults shown in brackets `[64]`; user overrides
shown as `CC 64` (highlighted).
- **4 / 6** — step the override ±5.
- **Bksp** — reset the highlighted target to its built-in default.
- **5** — numeric entry: type digits (3 digits auto-commit, clamped to 127),
  **Enter** confirms (empty Enter also resets to default), **Bksp** deletes
  a digit, any navigation key cancels.

### IMPORT page (USB → internal)
Copy video files off a USB drive into internal `clips/`. The Pi is headless, so
drives are mounted **on demand, read-only** by a small root helper (install once
with `sudo ./tools/install-usb-import.sh`, then restart). Without it the page
shows `RUN install-usb-import.sh`.
- Opening the page lists removable USB partitions (the SD/boot card is never
  shown). **5** mounts the highlighted drive and lists its video files.
- In the file list, **5** copies the highlighted file into `clips/` (a `✓`
  marks files already imported; same-named files are skipped). Imported clips
  are rescanned immediately, so they appear in BROWSER right away.
- **Enter** (or **Bksp**) ejects the drive and returns to the drive list.
- The drive is always unmounted when you leave the page or close the menu.

---

## Workflows

### Loop a section of a clip
1. SAMPLER mode, trigger the clip (4–9) or load it from BROWSER.
2. Press **0** at the loop start → green IN tick.
3. Press **0** again at the loop end → amber OUT tick. The clip now loops IN→OUT.
4. Press **0** a third time to clear both points.

### Build a performance set
1. **Num** → BROWSER. Highlight a clip, **Enter**, then press the slot key (4–9).
2. Repeat for up to six clips; **9** to the SHADERS page and assign generative
   shaders to slots the same way.
3. **Num** to exit. 4–9 now fire clips in SAMPLER/LIVE and shaders in SHADER mode.
4. SETTINGS → SAVE PREFS to keep the assignments across restarts.

### Auto-playing set (playlist)
1. Assign clips to slots (above).
2. SETTINGS → PLAY → `playlist`. Clips now chain automatically in slot order
   (4→5→…→9→4), advancing at each clip's end or OUT point.

### FX over video
1. SAMPLER or LIVE mode. **+ / −** cycles FX shaders (vhs, glitch, bitcrush…).
2. Key **1** to select p1–p4, **2/3** to tweak — labels on the SPI display
   match the effect (e.g. `CHROMA SHIFT` for vhs).

### Generative + FX stack
1. Enter SHADER mode (**Enter**). Load a generative shader (4–9 or SHADERS menu).
2. **+ / −** stacks an FX shader on top; p1–p4 keep tuning the *generative* layer.
3. Re-entering SHADER mode clears the stack (generative alone).

### Blend a generative shader with video
1. In SHADER mode press **/** — blend on. The shader composites with the clip
   (or camera: SETTINGS → BLEND SRC → `live`).
2. **\*** cycles the blend formula; `BLD AMT` (FX layer, or p5) sets the mix.

### Echo trail
1. Press **.** (or **000**) in any mode to toggle the trail on/off.
2. SETTINGS → **TRAIL TYPE** selects how the echo is rendered:
   - **MODE** — one continuous ghost blended on the luma plane using lagfun
     decay. TRAIL MODE and `TRL DEC` control the blend formula and persistence.
     `difference` is clean; the brightening/darkening modes (`screen`,
     `addition`, `multiply`, `overlay`) are tamed toward the original on luma
     (`trail_mode_opacity`, default 0.5) so they no longer wash out to white.
   - **OPACITY** — a weighted average of the live frame plus **five**
     progressively-delayed past echoes (mix). The echoes fall behind the
     motion (no pre-echo); the live frame is the sharpest and older echoes
     fade. Static areas stay clean — only motion ghosts — so it never washes
     out. The tail spans ~1.7× the base delay (≈3.3 s by default).
3. SETTINGS → **TRAIL MODE** (only active in MODE type) picks the luma blend:
   `screen` brightens ghosts, `difference` shows motion, etc.
4. `TRL DEC` (FX layer) controls fade persistence in MODE type (0.80–0.99).
5. The base delay (`trail_delay_s`, default 2 s) is set in `prefs.json`. The
   OPACITY layer weights are `trail_step_weights` (default
   `[1.0, 0.9, 0.8, 0.7, 0.6, 0.5]`, live first then oldest→ five echoes);
   raise the echo weights for a stronger trail.

### Map a MIDI controller
1. Plug in — connects automatically within 3 s.
2. Defaults work for most controllers (mod wheel = p1, etc. — see *MIDI reference*).
3. To rebind: **Num** → MIDI page → highlight target → **5** → type the CC
   number → **Enter**. User overrides win over built-ins.

### Keep your setup
Everything important (slots, effect states, modes, MIDI overrides, params) is
written to `prefs.json` on clean shutdown, or on demand with SETTINGS → SAVE
PREFS. Prefer SAVE PREFS after big changes — a power-cut skips the auto-save.

---

## Effects reference

### Trail — echo time delay (`000` / `.`)
Works in all three modes. Two types selectable via SETTINGS → TRAIL TYPE:

**MODE** — `split → tpad(delay) → lagfun(decay) → blend` on the luma plane
only (chroma passed through — no colour shifts). One continuous fading ghost.
Blend modes: screen, difference, multiply, overlay, addition. Decay 0.80–0.99
via `TRL DEC`; delay via `trail_delay_s` in `prefs.json`.

**OPACITY** — `split=6 → 5×tpad(step×1…5) → mix=inputs=6:weights=…`.
A weighted average of the live frame plus five progressively-delayed past
echoes, spaced `delay/3` apart (tail ≈1.7× the delay window). `mix` normalises
by the weight sum, so brightness is preserved and identical static regions
stay sharp — only moving content ghosts (no wash-out, no pre-echo).
Layer weights tunable as `trail_step_weights` in `prefs.json` (live first,
then five echoes); TRAIL MODE and `TRL DEC` have no effect in this type.

**In SHADER mode the trail is unavailable.** The lavfi trail runs in the vf
chain *before* the generative shader, which renders over it, so it never shows.
A GLSL trail would need cross-frame feedback (this frame reading last frame's
accumulator); the Pi 5 V3D / libplacebo renderer does not persist feedback
textures between frames (verified with controlled render tests), so it cannot
be done on this hardware. Toggling the trail in SHADER mode shows
`TRAIL N/A IN SHADER` and does nothing. The trail is a SAMPLER / LIVE feature.

### V-overlay — self-blend (`/` in SAMPLER/LIVE)
`split → blend` of the current frame with itself on the luma plane (chroma
passes through clean). A stylising blend — e.g. `screen` brightens, `multiply`
darkens — with **no time delay** (temporal echoes are the trail's job now).
`OVL OPC` (0–1) is the blend opacity. Modes: difference, addition, multiply,
screen, negate (`difference` self-blends to black). Blocked in SHADER mode
(the shader pipeline owns the picture).

### Shader blend (`/` in SHADER)
Two-pass GLSL composite: the generative output is saved (`//!SAVE gen_out`)
and a second hook composites it with the video, scaled by `BLD AMT`.
The full W3C/Photoshop separable set: `mix` (normal), `screen`, `addition`,
`multiply`, `overlay`, `hardlight`, `softlight`, `dodge`, `burn`, `lighten`,
`darken`, `difference`, `exclusion`. Plus **`displace`** — a Resolume-style
refraction where the shader's R/G channels warp the video like textured glass
(`BLD AMT` scales the warp, ±~95 px). The punchy ones (hardlight, overlay,
vivid dodge/burn, displace) read far stronger than the gentle screen/multiply.

---

## Playback modes (SETTINGS → PLAY)

| Mode | Behaviour |
|---|---|
| **loop** | loops forever; with IN/OUT set, loops that region |
| **oneshot** | plays once, freezes on the last frame (or pauses at the OUT point); retrigger with a slot key |
| **playlist** | chains slot-assigned clips in key order (4→5→…→9→4) at each clip's end or OUT point |
| **random** | loads a random clip from the clips directory at each clip's end |
| **fixed** | identical to `loop` (kept for r_e_c_u_r compatibility) |
| **randstart** | at each clip end, seeks to a random point in the first 80% and continues |

---

## Shaders reference

### FX shaders (over video; `+`/`−`)

| Shader | P1 | P2 | P3 | P4 |
|---|---|---|---|---|
| **bitcrush** | block size | colour depth | gap width | mix original |
| **feedback** | echo amount | spread | blend mode | trail depth |
| **glitch** | slice intensity | update rate | channel corrupt | block density |
| **grain** | scanline depth | noise amplitude | luma crush | speed |
| **hue_cycle** | speed | threshold | saturation | intensity |
| **vhs** | chroma shift | scanline depth | noise | tracking jitter |
| **passthrough** | — (excluded from +/− cycling) | | | |

**hue_cycle** is two-pass temporal: still pixels accumulate hue rotation,
changed pixels reset. Best on slow footage.

### Generative shaders (SHADER mode)

| Shader | P1 | P2 | P3 | P4 |
|---|---|---|---|---|
| **plasma** | speed | scale | warp | palette |
| **waves** | frequency | speed | count | hue |
| **tunnel** | speed | segments | twist | hue |
| **voronoi** | cell density | speed | edge sharpness | palette |
| **kaleidoscope** | sides | spin | zoom | colour shift |

New `.glsl` files in `shaders/` are picked up at startup; add generative ones
to `generative_shaders` in `config.py`. Param labels come from the
`/* comment */` after each `#define PARAM_N`.

---

## GPIO

| BCM pin | Function |
|---|---|
| 5 | cycle instrument mode |
| 6 | trigger (seek to IN and play) |
| 13 | toggle ffmpeg recording of the HDMI output → `recordings/recur-*.mkv` (needs the CAP_SYS_ADMIN service unit — rerun `install-service.sh` after updating) |
| 19 | cycle sampler playback mode |

Knobs: MCP3008 on SPI0.1 channels 0–3 → p1–p4, polled at 20 Hz with a 1.5%
deadband. (SPI0.0 belongs to the display — never share it.)

---

## MIDI reference

Auto-connects to the first non-passthrough port; hot-plug rescan every 3 s.

**Params (0–127 → 0.0–1.0)** — multiple CCs per param so common controllers
work unconfigured:

| Param | CCs |
|---|---|
| p1 | 1, 21, 48, 74 |
| p2 | 2, 22, 49, 71 |
| p3 | 3, 23, 50, 91 |
| p4 | 4, 24, 51, 93 |

**Action CCs** (toggles fire on value > 63):

| CC | Action |
|---|---|
| 64 | toggle overlay |
| 65 | cycle overlay mode (value > 63 = forward, ≤ 63 = back) |
| 66 | toggle shader blend |
| 67 | cycle shader blend mode |
| 68 / 69 | FX next / prev (stacks in SHADER mode) |
| 80 / 82 / 83 | mode SAMPLER / SHADER / LIVE |
| 81 | toggle trail |

All targets, plus `BLD AMT` / `OVL OPC` / `TRL DEC` (no default CC), can be
rebound from the MIDI menu page.

**Notes:** 120/122/123 set mode SAMPLER/SHADER/LIVE; any other note triggers
clip slot `note % 10` (only 4–9 are real slots).
**Program change** loads preset `NN.json` from `presets/`.

---

## State files

| File | Contents | Written |
|---|---|---|
| `prefs.json` | slots, effect states/modes, current clip/shader/fx, params, play mode, camera res, MIDI overrides, `trail_delay_s`, `trail_blend_type`, `trail_step_weights`, `trail_mode_opacity` | clean shutdown + SAVE PREFS |
| `presets/NN.json` | shader + fx + params snapshot | via Python API only; loaded by MIDI program change |
| `/tmp/recur_s*.glsl` | live shader temp files (unique path per recompile — mpv caches by path) | automatic |

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Black HDMI output | `journalctl -u recur -n 50`; `/tmp/mpv.err`; both HDMI DRM connectors need `hdmi_force_hotplug=1` in `/boot/firmware/config.txt` |
| Numpad dead | unplug/replug — the controller rescans every 2 s; check `journalctl` for "numpad connected" |
| Shader doesn't change | mpv caches by path — the engine writes unique temp paths; if stuck, restart the service |
| Recording produces no file | `/tmp/ffmpeg-rec.err`; the service unit needs `CAP_SYS_ADMIN` (rerun `install-service.sh`) |
| Camera won't start in LIVE | `/tmp/rpicam.err`; CSI camera must be detected by `rpicam-vid`; USB cams must be plain V4L2 capture nodes |
| Console text over video | vtcon1 must be unbound (the service does this in `ExecStartPre`) |

---

## Known issues

- **Local clips scanned at startup only** — new files added to `clips/` need a
  restart. USB/removable drives are re-scanned when the BROWSER page is opened.
- **CAM RES** applies on the next entry into LIVE mode.
- **`fixed` play mode is an alias of `loop`** — no distinct behaviour.
- **Speed/reverse API exists but is unbound** — `set_speed`/`reverse` in the
  sampler have no key, MIDI, or GPIO binding.
- **Recording is untested end-to-end** — the kmsgrab pipeline and service
  capability are in place but haven't been verified with a real capture yet;
  check `/tmp/ffmpeg-rec.err` on first use.
