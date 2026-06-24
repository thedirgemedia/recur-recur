# recur-recur — Operator Manual

*Accurate as of 2026-06-23. Generated from the actual code paths — where the
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
| **Bksp** | Cycle param layer for this mode: SHADER mode = SHDR → FX → COLOUR → BLEND → TRAIL; SAMPLER/LIVE = FX → COLOUR → BLEND → TRAIL. Top-bar chip shows `SHDR` / `FX` / `COLOUR` / `BLEND` / `TRAIL` |
| **1** | Cycle the selected parameter (see *Parameter editing*) |
| **2** / **3** | Selected parameter − / + |
| **0** | In/out points: 1st press = IN, 2nd = OUT, 3rd = clear |
| **Hold 0, tap .** | Record live camera to clip file (LIVE mode only; hold numpad 0 while tapping `.`; press again to stop and save) |

### SAMPLER and LIVE modes

| Key | Action |
|---|---|
| **4–9** | Trigger the clip assigned to that slot (assign in BROWSER menu) |
| **+** / **−** | Next / previous FX shader applied over the video |
| **/** | Toggle V-overlay (self-blend echo) |
| **\*** | Cycle overlay blend mode — many modes available: difference, addition, multiply, screen, negate, subtract, divide, lighten, darken, hardlight, softlight, dodge, burn, phoenix, negation, and more |

### SHADER mode

| Key | Action |
|---|---|
| **4–9** | Load the generative shader assigned to that slot (assign in SHADERS menu) |
| **Hold /** + **4–9** | Load + trigger the clip at that slot (assign in BROWSER menu), without leaving SHADER mode — change the blended video while the generative stays running |
| **+** / **−** | Cycle FX shader *stacked on top of* the generative (generative stays loaded) |
| **/** | Toggle shader blend (composite generative with clip or camera) |
| **\*** | Cycle shader blend mode — full set: mix, screen, addition, multiply, overlay, hardlight, softlight, dodge, burn, lighten, darken, difference, exclusion, **displace**, subtract, divide, negation, reflect, glow, phoenix, vividlight, linearlight, hardmix, **hue**, **luminosity**, **color** |

> Note the pattern: **/** always *toggles* an effect, **\*** always *cycles its
> mode* — in both mode families.

---

## Parameter editing

**Bksp** cycles the parameter layers available in the current mode. The active
layer shows in the SPI top bar (`SHDR` / `FX` / `COLOUR` / `BLEND` chip) and as
the bars below the header. Key **1** cycles the selection within a layer;
**2/3** step (or cycle, for the BLEND mode/source slots); knobs and MIDI feed
the same params.

- **SHADER mode:** SHDR → FX → COLOUR → BLEND → TRAIL
- **SAMPLER / LIVE mode:** FX → COLOUR → BLEND → TRAIL  (no generative shader → SHDR skipped)

### Layer SHDR — generative shader params *(SHADER mode only)*

p1–p3 = the generative shader's primary parameters (labels from its source).
Loading a generative resets them to its authored defaults. p4 (palette) lives
in the COLOUR layer (see below) so it is always accessible without cycling away
from the generative.

### Layer FX — the active FX shader's own params

Shows the four parameters of the **currently selected FX** (feedback, bitcrush,
glitch, grain, vhs …), with labels read from the shader source — e.g. glitch =
`SLICE INTENSITY / UPDATE RATE / CHANNEL CORRUPT / BLOCK DENSITY`. Kept separate
from the generative's p1–p4, so the FX is tunable even when stacked on a
generative in SHADER mode. Cycling the FX (`+`/`-`) reloads its defaults.
Persists in `prefs.json` (`fx_params`).

### Layer COLOUR — palette and colour adjustments (all modes)

All colour controls in one place. Key **1** cycles slots; **2/3** step:

| Param | Range | Meaning | Visible |
|---|---|---|---|
| `PAL` | 0–1 | IQ cosine palette selector for the generative | SHADER mode only |
| `HUE` | 0–360° | global hue rotation (0 = no shift); wraps. GLSL colour pass | always |
| `SAT` | 0–2 | global saturation (0 = greyscale, 1 = normal, 2 = vivid) | always |
| `TRL OPC` | 0–1 | trail blend opacity — how far MODE-type brightening blends are pulled back toward the original (prevents wash-out) | always |
| `TRL DEC` | 0.80–0.99 | trail persistence in MODE type (0.80 short ghost → 0.99 long tail) | always |

`PAL` is the generative shader's p4 parameter — it selects the IQ cosine
palette for plasma, waves, tunnel, voronoi and similar shaders, or acts as a
hue rotation for flowing_colours, hypnotic_rings, squarewaves, zoom_clouds.
Note: starfield's P4 is emitter-1 star count — tune starfield palettes via P6/P13
on the SHDR layer directly. `PAL` only appears in SHADER mode. HUE/SAT apply to the final picture in every mode via a
GLSL pass (no cost at neutral hue 0°, sat 1.0). All four values persist in
`prefs.json`.

### Layer BLEND — compositing controls

The full editing surface for the active blend (what `*` quick-cycles). **2/3**
cycles the mode or steps the amount:

| Mode | Slots |
|---|---|
| **SHADER** | `MODE` (shader↔video blend, 26 modes incl. displace, hue, luminosity, color), `BLD AMT`, `SRC` (clip/live) |
| **SAMPLER / LIVE** | `OVL MODE` (21 self-blend modes), `OVL OPC` |

In SHADER mode the `SRC` slot selects what the generative is composited
against: `clip` (the looping sampler clip) or `live` (the camera input).
Blend defaults to `clip`; enabling blend while the camera is not already
running will not auto-switch to live.

### Layer TRAIL — temporal echo controls (all modes)

All trail parameters in one place. Key **1** cycles slots; **2/3** step:

| Slot | Meaning |
|---|---|
| `TRL ON` | trail on/off toggle |
| `TYPE` | blend type: `MODE` (lagfun ghost) or `OPACITY` (weighted echo mix) |
| `MODE` | luma blend formula for MODE type (screen, difference, multiply…) |
| `DELAY` | time to the furthest echo (0.25–8 s) |
| `ECHOS` | number of delayed echoes in OPACITY type (1–5) |
| `OPACITY` | trail blend opacity for MODE type (same as `TRL OPC` on COLOUR layer) |

---

## SPI display

**Top bar (status view):**
- Row 1: mode name (colour-coded), `TRAIL` chip (green when on),
  `REC` chip (red while recording, amber while saving), param-layer chip
  (`SHDR` / `FX` / `COLOUR` / `BLEND` / `TRAIL`), current clip name.
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

The SETTINGS page header shows the Pi's current IP address (useful for SSH when the display is the only UI available).

| Row | Description |
|---|---|
| MODE | instrument mode (4/6 or 5 cycles) |
| LIVE MODE | ON/OFF — OFF removes LIVE from the Enter-key cycle |
| PLAY | sampler playback mode (see *Playback modes*) |
| CAM RES | camera capture: 320×180 / 640×360 / 1280×720 (applies on next LIVE entry) |
| OVERLAY | V-overlay on/off toggle (mode and opacity live on Bksp BLEND layer) |
| BLEND | shader blend on/off toggle (mode, amount, source live on Bksp BLEND layer) |
| FX / GEN | cycle the active FX / generative shader |
| SAVE PREFS | write current state to `prefs.json` now |
| RESTART | restart the application |
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
2. **+ / −** stacks an FX shader on top; p1–p3 keep tuning the *generative* layer.
3. Re-entering SHADER mode clears the stack (generative alone).

### Blend a generative shader with video
1. In SHADER mode press **/** — blend on. The shader composites with the clip
   (default) or the live camera (BLEND layer → `SRC` → `live`).
2. **\*** cycles the blend formula; `BLD AMT` (BLEND layer) sets the mix.
3. FX shaders can be stacked on top of a blend with **+ / −** — the pipeline
   is generative → blend → FX, all three live simultaneously.

### Record live camera to a clip
1. Enter LIVE mode (camera must be running).
2. **Hold numpad 0, tap `.`** — `REC` appears in the display top bar (red chip).
3. **Hold numpad 0, tap `.` again** — recording stops. The display shows `SAV`
   (amber) while ffmpeg remuxes in the background; chip clears when done.
4. The saved file appears in `clips/` as `rec_YYYYMMDD_HHMMSS.mp4` and is
   immediately available in the BROWSER for use in SAMPLER mode.

### Echo trail
1. Press **.** (or **000**) in any mode to toggle the trail on/off.
2. **Bksp** to the **TRAIL** layer to access all trail controls. Key **1** cycles slots, **2/3** adjust:
   - **TRL ON** — same toggle as the `.` key.
   - **TYPE** — selects how the echo is rendered:
     - **MODE** — one continuous ghost blended on the luma plane using lagfun decay. `TRAIL MODE` picks the blend formula; `TRL DEC` (COLOUR layer) sets persistence. Brightening modes (`screen`, `addition`, `multiply`, `overlay`) are tamed toward the original (`TRL OPC`) so they don't wash out.
     - **OPACITY** — a weighted average of the live frame plus 1–5 progressively-delayed echoes. Echoes fall behind motion (no pre-echo); static areas stay sharp. Never washes out.
   - **MODE** — luma blend formula (MODE type only): `screen` brightens, `difference` shows motion, `subtract` darkens, `phoenix` shows similarity. Full set: screen, difference, multiply, overlay, addition, subtract, lighten, darken, phoenix, negation, divide.
   - **DELAY** — time to the furthest echo (0.25–8 s).
   - **ECHOS** — number of echoes in OPACITY type (1–5, shown as hex `1`–`5`).
   - **OPACITY** — blend strength for MODE type (same as `TRL OPC` on COLOUR layer).
3. `TRL DEC` on the **COLOUR** layer controls lagfun fade persistence in MODE type (0.80–0.99; 0.80 = short ghost, 0.99 = long tail).
4. The OPACITY-type echo weights are `trail_step_weights` in `prefs.json` (default `[1.0, 0.9, 0.8, 0.7, 0.6, 0.5]`, live first); raise echo weights for a stronger trail.

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
Blend modes: screen, difference, multiply, overlay, addition, subtract, lighten,
darken, phoenix, negation, divide. Decay 0.80–0.99 via `TRL DEC`; delay via
`trail_delay_s` in `prefs.json`.

**OPACITY** — `split → N×tpad(step×1…N) → mix=inputs=N+1:weights=…`.
A weighted average of the live frame plus N progressively-delayed past echoes
(N = `trail_echo_count`, 1–5; set on the Bksp TRAIL layer), spaced
`delay/N` apart (tail ≈1.7× the delay window). `mix` normalises by the weight
sum, so brightness is preserved and identical static regions stay sharp —
only moving content ghosts (no wash-out, no pre-echo).
Layer weights tunable as `trail_step_weights` in `prefs.json` (live first,
then N echoes); TRAIL MODE and `TRL DEC` have no effect in this type.

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
screen, negate, subtract, divide, lighten, darken, hardlight, softlight, dodge,
burn, phoenix, negation, vividlight, linearlight, pinlight, hardmix,
grainmerge, grainextract (`difference` self-blends to black).
Blocked in SHADER mode (the shader pipeline owns the picture).

### Shader blend (`/` in SHADER)
Two-pass GLSL composite: the generative output is saved (`//!SAVE gen_out`)
and a second hook composites it with the video, scaled by `BLD AMT`.

**Separable modes:** `mix` (normal), `screen`, `addition`, `multiply`, `overlay`,
`hardlight`, `softlight`, `dodge`, `burn`, `lighten`, `darken`, `difference`,
`exclusion`, `subtract`, `divide`, `negation`, `reflect`, `glow`, `phoenix`,
`vividlight`, `linearlight`, `hardmix`.

**Non-separable (HSV) modes:** `hue` — takes the shader's hue, video's
saturation/value; `luminosity` — takes the shader's value (brightness), video's
hue/saturation; `color` — takes the shader's hue and saturation, video's value.

**`displace`** — a Resolume-style refraction where the shader's R/G channels warp
the video like textured glass (`BLD AMT` scales the warp, ±~95 px).

The punchy ones (hardlight, overlay, vividlight, dodge/burn, displace) read far
stronger than the gentle screen/multiply.

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
| **colorizer** | speed | bands | spread | mix |
| **feedback** | echo amount | spread | blend mode (7 modes) | trail depth |
| **glitch** | slice intensity | update rate | channel corrupt | block density |
| **grain** | scanline depth | noise amplitude | luma crush | speed |
| **hsv_shift** | hue | saturation | value | amount |
| **hue_cycle** | speed | threshold | saturation | intensity |
| **invert** | R invert | G invert | B invert | amount |
| **kaleido_warp** | sectors | spin | centre X | centre Y |
| **mirror** | axes | rotation | centre X | centre Y |
| **posterize** | levels | mix | contrast | tint hue |
| **rotate_zoom** | spin | centre X | centre Y | zoom |
| **vhs** | chroma shift | scanline depth | noise | tracking jitter |
| **wobble** | X amplitude | X frequency | Y amplitude | Y frequency |
| **zoom** | zoom | centre X | centre Y | pulse |
| **passthrough** | — (excluded from +/− cycling) | | | |

**hue_cycle** is two-pass temporal: still pixels accumulate hue rotation,
changed pixels reset. Best on slow footage.

**kaleido_warp**, **mirror**, and **rotate_zoom** rotate/spin their sample
point and use square-buffer rendering when stacked on a generative shader —
the generative pass is rendered into a diagonal-sized square buffer first so
no corner clips to black at any rotation angle.

### Generative shaders (SHADER mode)

P4 (palette) is edited in the **COLOUR layer** (`PAL` slot) for shaders that
use the IQ cosine palette system (plasma, waves, tunnel, voronoi, starfield
emitters). Shaders with their own per-channel colouring (flowing_colours,
hypnotic_rings, squarewaves, zoom_clouds, kaleidoscope) use P4 as a hue
rotation instead — still accessible via `PAL` in the COLOUR layer.

| Shader | P1 | P2 | P3 | P4 |
|---|---|---|---|---|
| **flowing_colours** | speed | detail | warp | hue |
| **hypnotic_rings** | speed | frequency | orbit | hue |
| **kaleidoscope** | sides | spin | zoom | colour shift |
| **plasma** | speed | scale | warp | palette |
| **squarewaves** | amplitude | warp | frequency | hue |
| **tunnel** | speed | segments | twist | palette |
| **voronoi** | cell density | speed | edge sharpness | palette |
| **waves** | frequency | speed | count | palette |
| **zoom_clouds** | speed | detail | warp | hue |

**starfield** has two independent warp-speed emitters, each with full per-emitter
controls (15 params total — edited on the SHDR layer with key **1** to scroll):

| Param | Emitter 1 | Emitter 2 |
|---|---|---|
| speed | P1 | P8 |
| X position | P2 | P9 |
| Y position | P3 | P10 |
| star count | P4 | P11 |
| trail | P5 | P12 |
| palette | P6 | P13 |
| opacity | P7 | P14 |
| (global) zoom | P15 | — |

Note: starfield's per-emitter palette (P6/P13) is independent — the `PAL`
slot in the COLOUR layer maps to P4 (emitter 1 star count), not palette.
Use the SHDR layer directly to tune starfield palettes.

**Adding your own shaders:** drop a `.glsl` file into `shaders/` and restart.
The app classifies it automatically by reading its `//!DESC` line — include
`(generative)` in the description for SHADER-mode shaders, omit it for FX.
Param labels come from the `/* comment */` after each `#define PARAM_N`.

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
| `prefs.json` | slots, effect states/modes, current clip/shader/fx, params, play mode, camera res, MIDI overrides, `trail_delay_s`, `trail_blend_type`, `trail_step_weights`, `trail_mode_opacity`, `trail_echo_count` | clean shutdown + SAVE PREFS |
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
