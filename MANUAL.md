# recur-recur — Operator Manual

*Accurate as of 2026-07-29. Generated from the actual code paths — where the
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

> **The `000` key is not usable.** It has no keycode of its own — it emits
> three rapid `KEY_KP0` presses, arriving as three plain `0`s — so nothing
> binds it. Earlier revisions of this manual described it as a global trail
> toggle; that toggle is the SETTINGS menu's **TRAIL** row (and MIDI CC 81).

The SPI display is organised as five **tabs** — SHADER, FX, SAMPLER, LIVE,
SETTINGS — and the top-row keys are permanently bound to them, in *every*
context (even while a menu page is open):

| Key | Tab |
|---|---|
| **Num** | SHADER |
| **/** | FX |
| **\*** | SAMPLER |
| **-** | LIVE |
| **.** | back out one level. At the top level it does nothing |

Pressing Num/\//\*/- for the tab you're **already on** cycles that tab's
sub-screens (a 3×3 slot **grid** first, then a **params** screen for tabs
that have one, then a **preset grid** on SHADER and FX). Pressing a
*different* tab's key jumps straight to that tab's grid and closes any open
menu page.

| Tab | Sub-screens, in cycling order |
|---|---|
| **SHADER** | generative grid → params → **SHADER PRESETS** |
| **FX** | FX grid → params → **FX PRESETS** |
| **SAMPLER** | clip grid → params |
| **LIVE** | **PRESETS** (whole state) |
| **SETTINGS** | menu-page grid → BROWSER → MIDI |

**.** is **back**: it goes up one level, cancelling an in-progress menu
sub-action (assign / CC-edit / confirm-delete / USB browse) if one's active,
else closing an open menu page, else exiting param edit mode, else dropping a
params screen back to its grid.

**.** steps out one level per press and stops when there's nothing left to
step out of — it never *goes* anywhere, so its meaning never depends on where
you happen to be. Pressing it at the top level does nothing.

### The SETTINGS key

SETTINGS is a destination, not a level, so it lives on its own key rather than
on **.**. Bind one to **SETTINGS TAB** — the calibration walk asks for it as an
optional last step, or add it later in EDIT KEYS.

- It is a **toggle**: press it from any tab to show SETTINGS, press it again to
  return **to the tab you came from**. Both directions land on that tab's grid,
  so one press always puts you somewhere predictable.
- It works from anywhere, including over an open menu page.

> **A device with no SETTINGS key bound cannot reach the SETTINGS tab** — and
> so cannot reach BROWSER, MIDI, IMPORT or INPUT either, since those are its
> sub-screens and grid cells. A 17-key numpad has exactly 18 keys for 18 other
> controls and none to spare, so it needs **.** to keep doubling as SETTINGS;
> that is what the numpad's own keymap preset is for. If you do strand
> yourself, the **panic hold** (any three keys for two seconds) still opens
> the calibration walk.

> **Note:** these keys only change what the SPI display is showing — pressing
> the SAMPLER tab key does **not** by itself put the instrument in SAMPLER
> mode. **Enter on that tab's grid screen is what commits it** (with nothing
> staged), and it is the only numpad route into SHADER / SAMPLER / LIVE. Mode
> also changes via the GPIO mode button, MIDI (CC 80/82/83 or notes
> 120/122/123), the SETTINGS menu's `MODE` row, or loading a preset.
>
> Enter only does this on the **grid** screen. On a tab's params screen it
> toggles param edit mode instead — see *Known issues*.

### The 3×3 grid (first screen of SHADER / FX / SAMPLER / LIVE / SETTINGS)

Grid cells sit at keys **7 8 9 / 4 5 6 / 1 2 3**, matching their on-screen
position (7 = top-left … 3 = bottom-right):

| Key | Action |
|---|---|
| **1–9** | select that grid cell (see per-tab behaviour below) |
| **+** | previous page (SHADER / FX grids and all three preset grids paginate 9 at a time) |
| **−** | next page (same screens) |
| **0** | toggle **STAGED** mode (SAMPLER grid only) — footer pill shows amber `STAGED` (clip picks wait for **Enter**) vs. green `LIVE` (clip picks apply immediately). SHADER and FX grid taps always apply immediately regardless of this setting. Turning STAGED back off discards anything pending |
| **Enter** | push all staged picks to the live output — **or, when nothing is staged, commit this tab's mode** to the instrument (SHADER / SAMPLER / LIVE; FX and SETTINGS aren't modes and just say so) |

Per-tab grid behaviour — **SHADER and FX use tap/hold**, distinguished by how
long the key is held (see *Numpad layout* → *Tap vs. hold*); the other three
grids are tap-only and unchanged:
- **SHADER** — lists generative shaders and doubles as the multi-shader
  stack editor (see *Parameter editing* → SHADER below). **Tap** toggles the
  shader in/out of the stack (up to 4 at once) — adding or removing never
  changes the screen, so you can build up a stack with repeated taps without
  being bounced into params. **Hold** opens that shader's params screen,
  adding it to the stack first if it wasn't already there (without removing
  anything else already stacked).
- **FX** — lists FX shaders and doubles as the multi-FX chain editor (see
  *Parameter editing* → FX below). **Tap** toggles the FX in/out of the chain
  (up to 4 at once) — adding or removing never changes the screen, so you can
  build up a chain with repeated taps without being bounced into params.
  **Hold** opens that FX's params screen, adding it to the chain first if it
  wasn't already there (without removing anything else already chained).
- **SAMPLER** — the 6 clip slots (keys 4–9; assigned in the BROWSER menu
  page). **Tap** triggers the clip. **Hold** loads the clip and opens its
  per-clip **CLIP settings** screen (rotation / zoom / speed / brightness /
  contrast / trail — see *Parameter editing* → *CLIP settings*); tapping the
  currently-playing clip's key again opens the same screen.
- **LIVE** — the whole-state **preset store** (see *Preset grids*). **Tap**
  loads a preset immediately — presets are never staged. **Hold** an empty
  cell to save the current state into it, or a filled one for its options.
- **SETTINGS** — five cells (BROWSER / SHADERS / SETTINGS / MIDI / IMPORT)
  that jump straight into the matching menu page (see *Menu system*).

### Tap vs. hold (SHADER, FX, SAMPLER, and preset grids)

Release a grid key within ~0.4s and it's a **tap**; keep it held past that
and it fires as a **hold** instead — the tap action never also fires
afterwards. On the SHADER and FX grids, tap toggles stack membership
immediately (STAGED mode has no effect there, only on SAMPLER clip picks —
see the table above) and hold activates the item first if it wasn't already
(adds the shader / FX to its stack) then jumps to its params screen. On the
SAMPLER grid, tap triggers the clip and hold loads it and opens its CLIP
settings screen. On the three **preset grids**, tap loads and hold either
saves into an empty cell or opens a filled cell's options. Holding to
configure something is a workshop action, not a performance change. The
SETTINGS grid doesn't use hold — those keys always dispatch immediately,
regardless of how long you hold them. No top-row key (**.** included) uses
hold.

> **Note:** because the SAMPLER grid now uses hold, a clip **triggers on key
> release** (like the SHADER/FX grids), not on press.

Hold is also used *inside* a params screen: on the SHADER/FX/CLIP params
screens, holding an **LFO cell (1/2/3)** opens that LFO's settings (a tap still
just assigns the LFO) — see *Parameter editing* → *LFO settings screen*.

### Record

Recording is not triggered from the numpad at all — see *Workflows* → *Record*
and the GPIO table for the one recorder that is actually wired up (BCM 13,
HDMI output → `.mkv`).

---

## Parameter editing

Each tab that has tunable parameters shows a second **params** screen — reach
it via hold (SHADER/FX/SAMPLER), by tapping the playing clip's key again
(SAMPLER → CLIP settings), or by pressing that tab's own key again while its
grid is showing. Params screens are **scrollable lists** (not capped at 9
rows) with two interaction modes:

| Key | Outside edit mode | Inside edit mode |
|---|---|---|
| **+** | scroll selection up the list | increase the selected parameter |
| **−** | scroll selection down the list | decrease the selected parameter |
| **1 / 2 / 3** (tap) | assign **LFO 1/2/3** to the highlighted param (tap again to clear) | *(no effect)* |
| **1 / 2 / 3** (hold) | open that **LFO's settings** screen — SHADER/FX/CLIP screens only (see below) | *(no effect)* |
| **4** | **MIDI-assign** the highlighted param (see below) | *(no effect)* |
| **9** | **DEFAULT** — reset this screen's target to its authored defaults (see below) | *(no effect)* |
| **Enter** | enter edit mode on the highlighted parameter | exit edit mode |
| **0** | toggle STAGED / LIVE | toggle STAGED / LIVE |

The selected row is **cyan**; entering edit mode turns the header and that
row **amber**, so it's visually obvious whether +/− will move the
selection or change a value.

**LFO / MIDI on CLIP settings** — the CLIP settings screen (see below) accepts
LFO (1/2/3) and MIDI (4) assignment on its **ZOOM** and **SPEED** rows only;
the other CLIP rows report *no LFO/MIDI here* if you try.

**MIDI-assign (key 4)** — on the SHADER and FX params screens (the layers with
real shader params) and on the CLIP settings ZOOM/SPEED rows. Press **4** on
the highlighted param, then either **move a MIDI knob** to learn its CC, or
**type a CC number** and press **Enter**. Any other key cancels; pressing 4
again on a param already bound to that CC clears it. The grid cell shows
`CC nn` when the selected param has a binding, `MIDI` otherwise. Bindings
persist in `prefs.json` as `midi_target_cc` (shared with the MIDI settings
page, which also lists `zoom` / `speed`).

**DEFAULT (key 9)** — the top-right action cell resets the current screen's
target to its authored/built-in defaults, **leaving LFO and MIDI assignments in
place**. On SHADER it reloads the edited slot's p-params, on FX the edited FX's
f-params, on CLIP the clip's `CLIP_DEFAULTS` (orientation / playback / colour /
trail), and on an LFO settings screen (below) that one LFO. The OSD confirms
with `DEFAULT: <name>`.

### LFO settings screen

The three LFOs (assigned with keys 1/2/3) each carry their own shape, depth and
rate. Edit one by **holding** its cell (1/2/3) on any SHADER, FX or CLIP params
screen — that opens a five-row editor for that single LFO, driven like any
params list (scroll with +/−, **Enter** to edit, +/− to change, key **9**
= DEFAULT):

| Row | Meaning |
|---|---|
| `SHAPE` | SINE / TRI / SAW / SQUARE / S&H |
| `MIN` | 0–100 — the value the LFO falls to |
| `MAX` | 0–100 — the value it rises to (MIN/MAX set the engine's `offset` and `amp`) |
| `SPEED` | period in seconds, **+** = faster; when `SYNC` = BPM this is a musical division (1/8…16) locked to the tempo instead |
| `SYNC` | SEC (free-running seconds) or BPM (locked to `lfo_bpm`) |

Editing an LFO re-bakes the shader preamble immediately, so every param bound to
it moves together. Persisted in `prefs.json` as `lfos` (and `lfo_bpm`).

LFOs run on the GPU off mpv's frame counter, clocked to the display's actual
refresh rate (discovered at runtime), so a **SPEED** of 4.00s means four real
seconds and the motion is as smooth as the output refresh.

### SHADER params — the edited stack slot's own p1–pN, plus blend mode/amount (slots 1+)

Up to 4 generative shaders can be stacked simultaneously (see the SHADER tab
above). Each stack slot keeps independent params. The bottom slot (0) never
composites — a generative shader synthesizes its picture from scratch, so
there's nothing meaningful beneath it — so its params list is just its own
p1–pN. Slots above the bottom also carry their own blend mode/amount — how
that layer's output composites with the folded result of the layers below
it — adding two extra rows:

| Row | Meaning |
|---|---|
| `BLEND` | this layer's blend mode against the stack below it — same palette as the FX chain's per-layer blend |
| `BLD AMT` | that blend's strength, 0–1 |

**`NORMAL` is the default for every layer**, so a freshly-stacked shader
looks unchanged until you pick a different blend mode. The params screen
shows whichever slot is currently selected for editing, tagged
`[slot/total]` when more than one shader is stacked. Labels are read from
the shader source; loading a shader into a slot resets its p-params to the
authored defaults, but leaves that slot's blend mode/amount untouched (the
blend belongs to the *position* in the stack, not to whichever shader
currently occupies it). Knobs and MIDI (CC1–4 family) feed the same p-param
values. Scrolling means shaders with more than 9 params (currently only
**starfield**, 15) are fully reachable — see *Shaders reference*. Persists
in `prefs.json` / presets as `shader_chain` / `shader_params_chain` /
`shader_blend_chain`.

### CLIP settings — per-clip orientation, playback, colour, and trail

**Hold** a clip on the SAMPLER grid (or tap the currently-playing clip's key
again) to open its CLIP settings. Every clip remembers its own settings —
they're keyed by clip path, applied automatically each time the clip loads,
and persist in `prefs.json` under `clip_settings`. The rows, edited like any
other params list (scroll with +/−, **Enter** to edit, +/− to change):

| Row | Range | Meaning |
|---|---|---|
| `ROTATE` | 0 / 90 / 180 / 270° | rotation applied **on top of** the clip's own orientation — 0° already plays a portrait phone clip upright (its metadata rotation is honoured automatically) |
| `ZOOM` | 1.00–4.00× | scale the picture up to fill the screen when its aspect ratio differs from the display (the overflow is cropped) |
| `SPEED` | 0.10–4.00× | playback speed |
| `DIR` | FWD / REV | play backwards |
| `BRIGHT` | −100…+100 | brightness (0 = neutral) |
| `CONTRAST` | −100…+100 | contrast (0 = neutral) |
| `TRAIL` | ON / OFF | temporal echo trail on/off |
| `TRL STEP` | 1–5 | number of echoes |
| `TRL TIME` | 0.25–8.00s | delay to the furthest echo |
| `TRL MODE` | screen / difference / … | how each echo blends (see `TRAIL_MODES`) |
| `TRL BLEND` | 0.00–1.00 | per-echo blend opacity — lower it so brightening modes (screen/addition) don't accumulate to white |

**ZOOM** and **SPEED** additionally take an **LFO** (keys 1/2/3) or a **MIDI**
CC (key 4), assigned per clip — see *Parameter editing* above. Because zoom
and speed aren't GPU shader params, a small CPU loop evaluates the LFO and
drives them; the modulation runs whenever that clip is the live source.

A trail is a **motion echo** — it's only visible on footage that moves. It
uses mpv's frame-delay filter (not GPU feedback), so unlike a generative
shader's trail it works in SAMPLER and LIVE.

> **Note:** the SAMPLER tab's *other* params sub-screen (reached by pressing
> the SAMPLER tab key again on the grid, without holding a clip) still shows
> the read-only clip/FX/timeline status summary. CLIP settings are the
> hold-to-open, per-clip editor described here.

### FX params — the selected FX chain slot's own f1–f4, plus its blend mode/amount

Up to 4 FX shaders can be chained simultaneously (see the FX tab above).
Each chain slot keeps independent params *and* its own blend mode/amount —
how that layer's effect composites with whatever is below it in the stack
(the previous layer, the generative, or the raw video/camera). The params
list is: this shader's own f1–f4/f5, then two extra rows:

| Row | Meaning |
|---|---|
| `BLEND` | this layer's blend mode against the layer below — the same rich palette as shader-blend (mix, screen, multiply, overlay, …, displace, hue, luminosity, color) plus `NORMAL`, a pure pass-through |
| `BLD AMT` | that blend's strength, 0–1 |

**`NORMAL` is the default for every layer**, so a freshly-added FX looks
exactly like a plain effect (no compositing) until you pick a different
blend mode. The params screen shows whichever slot is currently selected,
tagged `[slot/total]` when more than one FX is chained. Cycling to a
different FX in a slot reloads its f-params to their authored defaults but
leaves that slot's blend mode/amount untouched (the blend belongs to the
*position* in the stack, not to whichever shader currently occupies it).
Persists in `prefs.json` / presets as `fx_params_chain` / `fx_blend_chain`.

The very bottom of the stack (FX chain slot 0) only actually blends when
there's a real clip or camera signal under it — if the sampler hasn't loaded
anything yet (still on the blank keep-alive source), that layer's blend is
forced to plain pass-through instead of mixing with a meaningless blank
frame. In SHADER mode there's always something valid underneath (the
generative's own output), so this only matters in SAMPLER/LIVE.

### Not currently reachable from the numpad

Global hue/saturation and the shader↔video and overlay blend
**mode/amount/source** details are still fully implemented in
`engine/shader.py` / `main.py` and loaded/saved with presets and `prefs.json`
— but no numpad key, grid cell, or SPI screen currently exposes them (the old
The old Bksp-cycled COLOUR/BLEND layers were dropped when the tab/grid interface
replaced the old scheme). See *Known issues*. Some remain reachable via MIDI
CC (`blend_amt`, `ovl_opacity`, `trl_decay` are user-assignable targets) or
via the SETTINGS menu's on/off toggles and FX/GEN cycle rows.

The trail's **on/off, echo count, delay, blend mode and per-echo opacity** are
no longer stranded — they're editable **per clip** on the CLIP settings screen
(above). The SETTINGS menu's **TRAIL** row still flips `trail_on` for the current
source, but the per-clip screen is the real control now.

Note: a generative shader's own **palette param (p4)** is *not* in this list
— it's an ordinary shader parameter, so if the shader's source defines a
`PARAM_4`, it shows up on the SHADER params screen exactly like p1–p3 (see
below). It's unrelated to the FX chain's per-layer `BLEND` row above.

---

## Preset grids

There are three preset stores. All three are **3×3 grids on their own tab**,
reached by pressing that tab's key until you land on them — not menu pages.
All three look and behave identically:

| Store | Where | Saves | Files |
|---|---|---|---|
| **PRESETS** | LIVE tab | the whole instrument — mode, generative stack, FX stack, blends, colour | `presets/P*.json` |
| **SHADER PRESETS** | SHADER tab, 3rd screen | the generative stack only: every layer, each layer's p1–p10, the blends between them, and how the stack sits over incoming video. No FX, no clip, no mode | `presets/shaders/S*.json` |
| **FX PRESETS** | FX tab, 3rd screen | the FX stack only: every FX, each one's f1–f5, and the blends between them. No generative shader, no clip, no mode | `presets/fx/F*.json` |

A preset's **grid position is its identity** — there is no naming step,
because there is no text entry on a numpad. The cell at key 7 on page 1 is
`P01`, key 8 is `P02`, … key 3 is `P09`; page 2 continues at `P10`. Gaps are
fine: you can have `P01` and `P07` with nothing between them.

| Key | Action |
|---|---|
| **1–9**, tap | load that preset (an empty cell says so — hold it instead) |
| **1–9**, hold on an **empty** cell | save the current state into that cell |
| **1–9**, hold on a **filled** cell | open its options: **OVERWRITE WITH CURRENT** / **DELETE** |
| **+** / **−** | previous / next page of 9 |

Saving and placing are the same act, which is why these grids need no
separate assign mode. The header shows how many of the visible nine are
filled and which page you're on.

**Options screen** — `+`/`−` choose the action, **Enter** fires it, **.**
backs out to the grid without doing anything.

**Independence.** The two stack stores never touch each other or the rest of
the instrument: load a shader preset and your FX chain is untouched; load an
FX preset and your generative stack is untouched. Recall them in either order
to combine any look with any treatment. An FX preset applies over whatever is
on screen in **any** mode — SHADER, SAMPLER or LIVE — because FX are
post-stage filters. A shader preset loaded while you're in SAMPLER or LIVE is
stored and appears the next time you enter SHADER mode.

**Global shortcut.** Hold **0** then a key 4–9 (any mode), and MIDI notes,
still fire whole-state presets. These aren't looking at a screen, so they
always address **page one**: key 7 = `P01`, key 3 = `P09`, matching where
those keys sit on the LIVE grid.

---

## SPI display

**Tab bar** (top 42 px) — always visible: SHADER / FX / SAMPLER / LIVE /
SETTINGS, the active tab highlighted with a bright top accent bar. When the
active tab has more than one sub-screen, small dots under its name show which
one you're on.

**Grid screens** — a 3×3 button grid (see *Numpad layout*): green outline +
fill for the active/loaded item, amber outline + tint for a staged (pending)
pick, dim outline for an empty slot.

**Params screens** — horizontal sliders (one per parameter, the selected one
highlighted **cyan**) above a compact 3×3 selector grid mirroring the same
layout; both scroll together, windowed around the selection, for lists longer
than fit on screen. The header and selected row turn **amber** while in edit
mode (see *Parameter editing*).

**Footer** (bottom 22 px, every screen) — a pill on the left reading `LIVE`
(green) or `STAGED` (amber, with an `ENTER → PUSH` hint on the right); the
mode name is shown on the right when live.

**Menu overlay** — when a menu page (BROWSER/SHADERS/SETTINGS/MIDI/IMPORT)
is open it replaces the whole screen (tab bar and footer included)
until you back out of it — see *Menu system*.

There is **no touch input** — all control is numpad / MIDI / GPIO.

---

## Menu system

Menu pages are reached through the **SETTINGS tab** (the key you bound to
**SETTINGS TAB**): its grid has six cells — BROWSER (key **7**), SHADERS (**8**),
SETTINGS (**9**), MIDI (**4**), IMPORT (**5**), INPUT (**6**) — and pressing
one jumps straight into that page. Presets are *not* here any more; they are grids on
the SHADER, FX and LIVE tabs (see *Preset grids*). To back out of a
page, press **.** — this cancels any in-progress sub-action first (assign /
CC-edit / confirm-delete / USB browse) if one's active, otherwise closes the
page back to the SETTINGS grid. Pressing any *other* tab key also closes the
page and jumps to that tab, and pressing the **SETTINGS key** again returns
you to the tab you came from.

| Key | Action in menu |
|---|---|
| **+ / −** | scroll up / down. The **only** scroll keys, and they scroll on **every** page with no exceptions. On SETTINGS they step the value instead while a row is in edit mode |
| **4 / 6** | adjust value (SETTINGS, MIDI pages; INPUT cols/rows) |
| **5** | primary action: load (BROWSER/SHADERS), activate (SETTINGS), edit CC (MIDI), mount/copy (IMPORT), open row/pick (INPUT) |
| **Enter** | SETTINGS: open the highlighted row for editing (or fire an action row); BROWSER/SHADERS: start slot-assign; IMPORT: eject; elsewhere same as 5 |
| **7 / 9** | previous / next page (wraps) |
| **0** | delete / reset the highlighted row: BROWSER arms then confirms a delete, MIDI resets that CC to its built-in default. Does nothing on other pages |

> **On key names.** A macropad has blank keycaps, so this manual names the
> controls by what they *do*. **+** and **−** are the scroll pair — on a
> labelled numpad those are the `+` and `Backspace` keys in the right-hand
> column, but on a pad they are whichever two keys you taught during
> calibration. The names in the calibration walk match this table.

Scrolling used to also work on `8`/`2`, and **−** used to be taken over by
BROWSER and MIDI for their delete/reset. Both are gone: one pair of keys
scrolls, everywhere, and destructive row actions live on **0**.

The footer line on every menu page states that page's actual keys.

While a menu page is open, every setting you change (overlay/blend toggles,
FX/GEN cycling, params, MIDI CC bindings) applies to the live output
immediately — only a BROWSER/SHADERS *pick* is deferred until you leave the
page, so browsing never yanks the current clip or shader out from under you.

### BROWSER page
Lists clips from internal `clips/` (plus any video files on drives already
mounted under `/media/` or `/mnt/`, re-scanned each time you open the page).
To pull files off a USB stick, use the **IMPORT page** — drives are mounted on
demand there and copied into internal storage. `▶` marks the currently playing
clip. The right column shows the assigned slot key (4–9) for clips that have
one; for clips from a mounted removable drive it shows a short drive label.
- **5** — stage the highlighted clip; it loads to the live output when you leave the page.
- **Enter, then a key 4–9** — assign the highlighted clip to that performance
  slot (any other key cancels). A clip can hold only one slot; assigning moves it.
- **0, then 0 again** — delete the highlighted clip file. The first press
  arms it (`0 again = DELETE FILE`); any other key cancels. Only internal
  `clips/` files can be deleted (removable drives are read-only); the file is
  removed from disk and cleared from any slot it held.

### SHADERS page
Lists generative shaders and lets you assign one to a performance slot
(4–9), same interaction as BROWSER. **Note:** unlike clip slots,
the SHADER tab's own grid does not currently read this assignment — it just
paginates every generative shader alphabetically, 9 per page — so slot
assignments made here are saved but have no effect on what a numpad key
loads. Use **5** to stage/load a shader here instead if you want a
deterministic pick.

### SETTINGS page

The SETTINGS page header shows the Pi's current IP address (useful for SSH
when the display is the only UI available). Rows are a plain scrolling list,
grouped under headers — **+**/**−** move the selection up/down (headers
are skipped), **Enter** opens the highlighted row for editing.

Editing works like the params screens: **Enter** on a value row turns the row
amber and puts it in **edit mode**, where **+**/**−** step the value
(`‹ value ›` arrows show this is live); **Enter** again closes edit mode and
returns +/− to scrolling. The three SYSTEM rows are *actions*, not values —
**Enter** fires them immediately rather than opening edit mode. (**4**/**6**
still adjust the highlighted value directly, in or out of edit mode.)

| Group | Row | Description |
|---|---|---|
| PLAYBACK | MODE | instrument mode — SAMPLER/SHADER/LIVE |
| PLAYBACK | LIVE MODE | ON/OFF — OFF removes LIVE from the mode cycle |
| PLAYBACK | PLAY | sampler playback mode (see *Playback modes*) |
| VIDEO | CAM RES | camera capture: 320×180 / 640×360 / 1280×720 (applies on next LIVE entry) |
| VIDEO | VID SCALE | video scaling mode for mismatched aspect ratios |
| MIX | OVERLAY | V-overlay on/off toggle |
| MIX | BLEND | shader blend on/off toggle |
| SHADERS | FX | cycle the FX chain's currently-edited slot to the next/previous FX shader |
| SHADERS | GEN | cycle the active generative shader |
| SYSTEM | SAVE PREFS | *(action)* write current state to `prefs.json` now |
| SYSTEM | RESTART | *(action)* restart into a **clean default state** (prefs are saved on the way out but not reloaded) |
| SYSTEM | RESTART SAME | *(action)* restart and **resume the current state** (re-execs with `--resume`, reloading the just-saved `prefs.json`) |
| SYSTEM | QUIT | *(action)* quit the application (a Pi poweroff would need root; the service can't escalate) |

Note: this page has on/off toggles for OVERLAY and BLEND, and cycle actions
for FX/GEN, but no rows for their mode/amount/source/palette/hue/sat details
— see *Parameter editing* → "Not currently reachable from the numpad".

### MIDI page
Per-target CC overrides. Defaults shown in brackets `[64]`; user overrides
shown as `CC 64` (highlighted).
- **4 / 6** — step the override ±5.
- **0** — reset the highlighted target to its built-in default.
- **5** — numeric entry: type digits (3 digits auto-commit, clamped to 127),
  **Enter** confirms (empty Enter also resets to default), **−** deletes
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
- **Enter** (or **.**) ejects the drive and returns to the drive list. **−**
  scrolls the file list here, it does not eject.
- The drive is always unmounted when you leave the page or close the menu.

### INPUT page (USB keyboard config)
Choose which attached keyboard drives the instrument and, for a programmable
macropad, teach each of its keys an action. recur reads **every** attached USB
keyboard at once, so you can configure a still-unmapped pad using a spare
keyboard plugged in alongside it.

**First run — the calibration walk.** When a recognized macropad is plugged in
and hasn't been taught this rig's controls yet, recur says so on the SPI display
at boot: `PAD DETECTED / PRESS ANY KEY ON THE PAD`. Pressing any pad key starts
a guided walk that asks for one control at a time — `PRESS THE KEY FOR:
SHADER TAB`, then `FX TAB`, and so on through all 19 — and you answer each by
pressing the key you want it on. You choose the physical layout; the walk just
makes sure nothing is missed. At the end the pad is mapped **and made the
primary device**, so only it drives the live output.

- **The last step, SETTINGS TAB, is optional** and says so on screen
  (`OPTIONAL — PRESS A SPARE KEY FOR`). **Bind it if you possibly can** — it is
  the only way to reach the SETTINGS tab, and therefore the only way to reach
  BROWSER, MIDI, IMPORT and INPUT (see *The SETTINGS key*). If you genuinely
  have no key to spare — a 17-key numpad has none — just **wait ~12 s and it
  skips itself**, because from the pad alone every key you press would bind
  rather than pass. Skipping it is a choice, not a fault: **FIX MISSING**
  never counts it as a gap. If you skip it and then need a menu, the **panic
  hold** (any three keys for two seconds) reopens this walk.
- The walk asks *action → key*; **EDIT KEYS** (below) asks *key → action*.
  Use the walk for a new pad, EDIT KEYS to fix one key afterwards.
- Pressing a key you've already used reassigns it — the screen says which
  action it was taken from, so you can put that one somewhere else.
- **One key per action.** The walk asks for each control exactly once, so
  answering a prompt also **frees any other key that held that control**
  (`freed 6` on the status line). This is what makes a re-walk a real repair:
  a stray binding cannot outlive it. Earlier builds only ever *added* the key
  you pressed, so a stray survived every subsequent walk.
- **Only one key at a time counts.** A key pressed while another is still held
  is treated as part of a chord, not as an answer, and is ignored — so a
  fumbled press or a panic hold can't answer three prompts at once.
- With a second keyboard plugged in, **−** skips a step and **.** stops the
  walk. Bindings made so far are kept; the pad is *not* promoted to primary
  from a stopped walk, so you can't strand yourself on a half-mapped pad.
- If nobody answers, the offer clears itself after ~60 s and a walk in progress
  after ~45 s per step.
- **CALIBRATE PAD** on the INPUT page runs the same walk on demand.
- Once a pad is calibrated the offer stops appearing: recur records which device
  the keymap was learned on. **CLEAR MAP** brings it back — as does breaking the
  map badly enough to lock yourself out, below.

### Getting back in when the keys stop working

Two safety nets, because a keymap is *keycode → action*: rebinding a key can
leave a control with **no key at all**, and if that control is how you scroll or
confirm, you can no longer reach the page that would fix it.

- **The boot offer re-appears on a lockout.** At startup recur checks that
  something is bound for each of *scroll down, scroll up, confirm* and *back*
  (**5** counts as **Enter** for confirm; scrolling has one key each way, so
  those two have no alternative). If any of the four has nothing at all, the
  calibration offer appears even though the pad is "already calibrated", and
  names what's unreachable. **So a power-cycle is always a way back in** — the
  offer is answered by pressing *any* key, which is the one thing a pad with a
  broken map can still do.
- **The panic hold.** Hold **any three keys at once for two seconds** on the
  same device and the calibration offer appears immediately, from anywhere —
  mid-performance, mid-menu, even while the wizard itself is on screen. It is
  read from raw keycodes before the keymap is consulted, so it can't be unbound
  and works on a device that has never been mapped. Three keys at once can't
  happen while playing, which is why it needs no key of its own.

  It only *offers*: press any key to start the walk, or ignore it and it clears
  itself after ~60 s. It does nothing while the calibration wizard is already
  open — you're already where it would take you.

A gap that isn't a lockout — say **STAGED / DELETE** with no key, while
scrolling and confirming still work — is not worth interrupting you for, so
neither net fires. Use **FIX MISSING** for
those; the INPUT page shows a count.

Failing all of that, unplugging the pad drops the primary-device filter, so any
ordinary USB keyboard can drive the menu with the built-in numpad map.
- Set each pad key to a **single, unmodified** keystroke in the pad's own
  configuration software. A key that sends a combo (Ctrl+Shift+X) or a macro
  will bind whichever keystroke lands first, which may not be the one you want.

The page opens on a short row list:
- **PRIMARY DEVICE** — **5/Enter** opens a picker of every detected keyboard
  (shown by name + `vid:pid`). Pick one and only *that* device drives the live
  output during play — other keyboards are ignored, so a mouse-keyboard or a
  laptop keyboard left plugged in can't disturb a performance. The first entry,
  **AUTO (any keyboard)**, is the default: with no primary set, any keyboard
  drives play (handy before you've configured anything). If the chosen primary
  isn't plugged in, recur falls back to accepting any keyboard rather than
  going deaf — so unplugging the pad can never lock you out of the menu.
- **COLS / ROWS** — **4/6** set the pad's used grid size (default 6 × 4). This
  is just for the on-screen layout; you don't set orientation separately —
  press-to-learn binds whatever key you press, so you hold the pad the way
  you'll use it and press keys in reading order.
- **CALIBRATE PAD** — **5/Enter** runs the full 19-step walk described above.
- **FIX MISSING** — shows how many controls have **no key at all** and, on
  **5/Enter**, runs a short press-only walk over just those. A keymap is
  keycode → action, so rebinding a key can silently leave a control stranded —
  and a missing `−` or `.` is the worst case, because it's the key you'd
  need to navigate to the fix. This is press-only for that reason: it never
  requires scrolling. It does *not* change the primary device.
- **EDIT KEYS** — **5/Enter** starts **press-to-learn** (below). In the action
  list, a control with no key is flagged `· no key` and one sitting on more
  than one key is flagged `· 2 keys`, so both kinds of fault are visible while
  you're choosing what to bind.
- **CLEAR MAP** — **5/Enter** wipes all learned bindings, falling back to the
  built-in numpad map (so a plain USB numpad works again with no config). It
  also forgets which pad was calibrated, so the boot offer returns.

**Press-to-learn.** After EDIT KEYS the screen prompts `PRESS A KEY ON THE PAD`.
Press a physical key → its action list appears; scroll with **+/−** and press
**Enter** to bind that key to the highlighted action (tab select, grid slot 1–9,
`+`/`−`, `Enter`, `.` back, `0` staged/delete, or **— unbind —**). It then re-arms
for the next key, so you can walk the whole pad: *press key → pick action →
press key → pick action*. **.** steps back (from picking → back to armed; from
armed → out to the row list). An armed prompt with no key pressed times out
after ~8 s and returns to the row list. Every bind saves to `prefs.json`
immediately, so a learned layout survives a restart.

> The keymap and chosen device live in `prefs.json` (`keymap`, `keymap_dev`,
> `input_primary`, `pad_cols`, `pad_rows`). An empty `keymap` means the
> built-in numpad map is used, so nothing is required for a standard numpad.
> Unlike every other setting, these are **loaded on every boot** — a normal
> boot otherwise starts from defaults (see *Boot state*), but your controller
> and its layout are properties of the rig, not of the session, so they
> persist without needing RESTART SAME.

---

## Workflows

### Build a performance set
1. Press the **SETTINGS key** → tap the **BROWSER** cell. Highlight a clip,
   press **Enter**, then press the slot key (4–9) to assign it.
2. Repeat for up to six clips. (SHADERS also lets you assign a generative
   shader to a slot, but nothing currently reads that assignment back — see
   the SHADERS page note above; use its own **5** to stage/load a shader by
   hand instead.)
3. Press **.** to back out to the SETTINGS grid, or another tab key to leave
   the menu entirely. The SAMPLER tab's grid now fires clips at keys 4–9.
4. Build a look, then press **-** (LIVE tab) and **hold** an empty cell to
   save it as a preset there. Repeat for each look you want on hand; tapping
   a cell recalls it. For looks you want to recombine, save the generative
   stack and the FX treatment separately instead — SHADER tab and FX tab,
   third screen each (see *Preset grids*).
5. SETTINGS page → **SAVE PREFS** to write the assignments to `prefs.json`.
   Note a plain restart still boots to defaults — use SETTINGS → **RESTART
   SAME** (or `--resume`) to bring the saved assignments back. Presets are
   separate files and survive a restart regardless.

### Auto-playing set (playlist)
1. Assign clips to slots (above).
2. SETTINGS page → **PLAY** row → `playlist`. Clips now chain automatically
   in slot order (4→5→…→9→4), advancing at each clip's end or OUT point.

### Chain multiple FX over the video
1. Press **/** (FX tab). Tap an FX cell to add it to the chain (up to 4 at
   once) — its cell gets a `[n]` badge showing its position; you stay on the
   grid, so you can keep tapping to build up the whole chain before touching
   any params. Tap it again to remove it.
2. To tune a chained FX's params (or check/adjust a *different* member
   without disturbing the others), **hold** its key — that opens its params
   screen without adding/removing anything.
3. On an FX's params screen: scroll with **+ / −**, jump to a row with
   **1–9**, press **Enter** to edit the highlighted row, then **+ / −** to
   step its value, **Enter** again to stop editing. The last two rows —
   `BLEND` and `BLD AMT` — set how this layer composites with whatever's
   below it (default `NORMAL` = plain pass-through, no compositing).
4. The chain applies in SAMPLER, LIVE, *and* on top of a loaded generative
   shader in SHADER mode — it isn't mode-specific.

### Layer FX with blend modes (Photoshop-style stacking)
1. Chain 2+ FX as above. Each one defaults to `NORMAL` blend (just its own
   effect, no interaction with the layers below).
2. Hold a chained FX's key to open its params, scroll to `BLEND`, press
   **Enter**, then **+ / −** to pick a mode (screen, multiply, overlay,
   difference, displace, hue/luminosity/color, …). Scroll to `BLD AMT` to
   dial the strength back from full (1.0) toward invisible (0.0).
3. Chain-slot 0 (the bottom of the FX stack) only blends when there's a real
   clip or camera loaded beneath it — otherwise it's forced to plain
   pass-through so it never mixes with the blank keep-alive picture.

### Stack generative shaders
1. Press **Num** (SHADER tab). Tap a shader cell to add it to the stack (up
   to 4 at once) — its cell gets a `[n]` badge showing its position; you
   stay on the grid, so you can keep tapping to build up the whole stack
   before touching any params. Tap it again to remove it.
2. To tune a stacked shader's params (or check/adjust a *different* member
   without disturbing the others), **hold** its key — that opens its params
   screen, adding it to the stack first if it wasn't already there (without
   removing anything else already stacked).
3. On a shader's params screen: scroll with **+ / −**, jump to a row with
   **1–9**, **Enter** to edit the highlighted row, then **+ / −** to step
   its value, **Enter** again to stop editing. Slots above the bottom (1+)
   also get `BLEND` / `BLD AMT` rows — see *Parameter editing* → SHADER
   above.
4. Chain FX on top of the whole stack via the FX tab (above) — the pipeline
   is generative stack → (blend-with-video, if on) → FX chain → colour.

### Record

Two independent recorders exist in the codebase, with two different outputs:

- **HDMI output → `.mkv`** (`engine/mixer.py`, kmsgrab-based): wired to the
  **REC** GPIO button (BCM 13 — see *GPIO*), which is live today.
- **Live camera → clip `.mp4`** (`engine/recorder.py`, `inst.record_toggle()`,
  the mpv-stream-record + ffmpeg-remux path the SAMPLER/LIVE tab's `REC`/`SAV`
  status chip is built to show): currently has **no live trigger** — the
  keyboard, GPIO, MIDI, and menu layers never call `record_toggle()`. See
  *Known issues*.

### Echo trail
Open a clip's **CLIP settings** (hold it on the SAMPLER grid) and set
**TRAIL** on, then dial **TRL STEP** (echo count), **TRL TIME** (delay),
**TRL MODE** (blend) and **TRL BLEND** (per-echo opacity). It's per clip, so
each clip keeps its own trail. The SETTINGS menu's **TRAIL** row still toggles the
current source's trail globally, but the CLIP screen is the real control now. The
global hue/saturation and blend mode/amount/source details remain without a
live surface besides MIDI CC (`blend_amt`, `ovl_opacity`, `trl_decay`) — see
*Known issues*.

### Map a MIDI controller
1. Plug in — connects automatically within 3 s.
2. Defaults work for most controllers (mod wheel = p1, etc. — see *MIDI reference*).
3. To rebind: **SETTINGS key** → **MIDI** cell → highlight target → **5**
   → type the CC number → **Enter**. User overrides win over built-ins.

### Keep your setup
Everything important (slots, effect states, modes, MIDI overrides, LFOs,
params) is written to `prefs.json` on clean shutdown, or on demand with SETTINGS
page → **SAVE PREFS**. Prefer SAVE PREFS after big changes — a power-cut skips
the auto-save. Saving only *stores* the state: boot always comes up on defaults,
so reload a saved session with SETTINGS → **RESTART SAME** (see *State files* →
*Boot is a clean default state*).

---

## Effects reference

### Trail — echo time delay
Edited **per clip** on the CLIP settings screen (hold a clip): **TRL STEP**
(echo count 1–5), **TRL TIME** (delay to the furthest echo), **TRL MODE**
(blend mode) and **TRL BLEND** (per-echo opacity). The SETTINGS menu's
**TRAIL** row still toggles the current source's trail globally. Two underlying types exist in
`prefs.json`/presets (`trail_blend_type`); the CLIP screen drives the **MODE**
type — the OPACITY type is engine-only, not exposed live:

**MODE** (used by the CLIP screen) — `split → N×tpad(step×1…N) → chained
blend` of N progressively-delayed echoes onto the live frame. Each echo blends
at **TRL BLEND** (`trail_mode_opacity`, 0–1) so brightening modes don't
accumulate to white. Blend modes: screen, difference, multiply, overlay,
addition, subtract, lighten, darken, phoenix, negation, divide.

**OPACITY** — `split → N×tpad(step×1…N) → mix=inputs=N+1:weights=…`.
A weighted average of the live frame plus N progressively-delayed past echoes
(N = `trail_echo_count`, 1–5), spaced `delay/N` apart (tail ≈1.7× the delay
window). `mix` normalises by the weight sum, so brightness is preserved and
identical static regions stay sharp — only moving content ghosts (no
wash-out, no pre-echo). Not selectable from the CLIP screen (which forces
MODE); switch `trail_blend_type` in `prefs.json` to use it.

**In pure SHADER mode the trail isn't visible.** The lavfi trail runs in the
vf chain *before* the generative shader, which renders over it, so it never
shows.
A GLSL trail would need cross-frame feedback (this frame reading last frame's
accumulator); the Pi 5 V3D / libplacebo renderer does not persist feedback
textures between frames (verified with controlled render tests), so it cannot
be done on this hardware. `trail_toggle()` refuses in SHADER mode
(`TRAIL N/A IN SHADER`); a per-clip trail can still be set there but stays
hidden behind the shader. The trail is effectively a SAMPLER / LIVE feature.

### V-overlay — self-blend (SAMPLER/LIVE)
`split → blend` of the current frame with itself on the luma plane (chroma
passes through clean). A stylising blend — e.g. `screen` brightens, `multiply`
darkens — with **no time delay** (temporal echoes are the trail's job now).
`OVL OPC` (0–1) is the blend opacity. Modes: difference, addition, multiply,
screen, negate, subtract, divide, lighten, darken, hardlight, softlight, dodge,
burn, phoenix, negation, vividlight, linearlight, pinlight, hardmix,
grainmerge, grainextract (`difference` self-blends to black).
Blocked in SHADER mode (the shader pipeline owns the picture). Toggle via the
SETTINGS menu's OVERLAY row or MIDI (`overlay_toggle`/`overlay_cycle`) — see
*Known issues*.

### Shader blend (SHADER mode)
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
stronger than the gentle screen/multiply. Toggle via the SETTINGS menu's
BLEND row or MIDI (`shader_blend_toggle`/`shader_blend_cycle`); the blend
mode/amount/source are not currently reachable from the numpad — see
*Known issues*.

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

`engine/sampler.py`'s `set_in()`/`set_out()` back the IN/OUT points referenced
above and in the clip timeline, but nothing in the current numpad, menu, MIDI,
or GPIO control paths calls them — see *Known issues*. Until that's wired up,
IN/OUT stay at the clip's full length.

---

## Shaders reference

### FX shaders (over video; `+`/`−`)

| Shader | P1 | P2 | P3 | P4 |
|---|---|---|---|---|
| **ascii** | char size | invert | mix original | variation |
| **bitcrush** | block size | colour depth | gap width | mix original |
| **colorizer** | speed | bands | spread | mix |
| **feedback** | echo amount | spread | blend mode (7 modes) | trail depth |
| **glitch** | slice intensity | update rate | channel corrupt | block density |
| **grain** | scanline depth | noise amplitude | luma crush | speed |
| **halftone** | R size | R angle | G size | G angle |
| **hsv_shift** | hue | saturation | value | amount |
| **hue_cycle** | speed | threshold | saturation | intensity |
| **invert** | R invert | G invert | B invert | amount |
| **kaleido_warp** | sectors | spin | centre X | centre Y |
| **levels** | blacks | shadows | midtones | highlights |
| **mirror** | axes | rotation | centre X | centre Y |
| **posterize** | levels | mix | contrast | tint hue |
| **rotate_zoom** | spin | centre X | centre Y | zoom |
| **vhs** | chroma shift | scanline depth | noise | tracking jitter |
| **wobble** | X amplitude | X frequency | Y amplitude | Y frequency |
| **zoom** | zoom | centre X | centre Y | pulse |
| **passthrough** | — (excluded from +/− cycling) | | | |

**halftone** and **levels** carry more than four params (7 and 5); the extra
rows sit below P4 on the FX params screen and are reached by scrolling, exactly
like the multi-param generative shaders.

**ascii** renders the video as a mosaic of characters, each tinted by the colour
underneath it. **char size** sweeps the cell from fine to chunky; **invert**
flips which end of the ramp is dense; **mix original** dissolves back toward the
untouched video; **variation** breaks flat areas up with per-cell random glyphs.
The glyph set is baked from a 332-glyph atlas — full ASCII + full hiragana +
full katakana + a light→dense kanji spread — generated by
`tools/gen_ascii_atlas.py` (edit that, never `shaders/ascii.glsl`). Bind an
**LFO** to **char size** and the size glides continuously; a static size snaps
to whole pixels so a still image stays crisp.

**hue_cycle** is two-pass temporal: still pixels accumulate hue rotation,
changed pixels reset. Best on slow footage.

**kaleido_warp**, **mirror**, and **rotate_zoom** rotate/spin their sample
point and use square-buffer rendering when stacked on a generative shader —
the generative pass is rendered into a diagonal-sized square buffer first so
no corner clips to black at any rotation angle.

### Generative shaders (SHADER mode)

P4 (palette) selects the IQ cosine palette for shaders that use that system
(plasma, waves, tunnel, voronoi, starfield emitters). Shaders with their own
per-channel colouring (flowing_colours, hypnotic_rings, squarewaves,
zoom_clouds, kaleidoscope) use P4 as a hue rotation instead. It's edited the
same way as p1–p3: on the SHADER tab's params screen (labels/params are
parsed straight from the shader source, so anything with a `PARAM_4` define
just shows up there — there's no separate `PAL` slot any more).

| Shader | P1 | P2 | P3 | P4 |
|---|---|---|---|---|
| **flowing_colours** | speed | detail | warp | hue |
| **gamma-ray** | density | brightness | size | colour |
| **hypnotic_rings** | speed | frequency | orbit | hue |
| **kaleidoscope** | sides | spin | zoom | colour shift |
| **plasma** | speed | scale | warp | palette |
| **squarewaves** | amplitude | warp | frequency | hue |
| **tunnel** | speed | segments | twist | palette |
| **voronoi** | cell density | speed | edge sharpness | palette |
| **waves** | frequency | speed | count | palette |
| **zoom_clouds** | speed | detail | warp | hue |

**starfield** has two independent warp-speed emitters, each with full per-emitter
controls (15 params total). The SHADER params screen scrolls (see *Parameter
editing*), so all 15 are reachable: **1–9** still jumps straight to p1–p9,
and **+ / −** scroll on to p10–p15 (emitter 2's Y position, star count,
trail, palette, opacity, and the global zoom). Knobs and MIDI still only
reach p1–p4 either way.

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

Note: starfield's per-emitter palettes (P6, P13) are independent of each
other and of every other shader's p4 — there is no shared palette concept
any more (see the p4 note above).

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
| 68 / 69 | FX next / prev — cycles the currently-edited FX chain slot (same chain used in SAMPLER/LIVE and stacked on a generative in SHADER mode) |
| 80 / 82 / 83 | mode SAMPLER / SHADER / LIVE |
| 81 | toggle trail |

All targets, plus `ZOOM` / `SPEED` (per-clip zoom 1–4× and speed 0.1–4× of
the current clip) and `BLD AMT` / `OVL OPC` / `TRL DEC` (no default CC), can be
rebound from the MIDI menu page. `ZOOM`/`SPEED` can also be bound straight from
the CLIP settings screen (key **4** on the highlighted row).

**Notes:** 120/122/123 set mode SAMPLER/SHADER/LIVE; any other note triggers
clip slot `note % 10` (only 4–9 are real slots).
**Program change** loads preset `NN.json` from `presets/`.

---

## State files

| File | Contents | Written |
|---|---|---|
| `prefs.json` | clip/shader slots, effect states/modes, current clip/shader/fx, params, play mode, camera res, MIDI overrides, `lfos` / `lfo_bpm`, `trail_delay_s`, `trail_blend_type`, `trail_step_weights`, `trail_mode_opacity`, `trail_echo_count`; plus the input config, which is the only part reloaded on a normal boot | clean shutdown + SAVE PREFS; the INPUT page saves immediately |
| `presets/P*.json` | whole-instrument snapshot — mode, both stacks, blends, colour | hold an empty cell on the LIVE tab's grid |
| `presets/shaders/S*.json` | generative stack only (layers, params, blends) | hold an empty cell on the SHADER tab's preset grid |
| `presets/fx/F*.json` | FX stack only (layers, params, blends) | hold an empty cell on the FX tab's preset grid |
| `/tmp/recur_s*.glsl` | live shader temp files (unique path per recompile — mpv caches by path) | automatic |

**Boot is a clean default state.** `prefs.json` is written on shutdown/SAVE PREFS
but is **not** reloaded on boot — every field starts from its Config default (no
shader/FX chain, neutral colour, empty slots refilled by the clip/shader scan),
so the instrument always comes up the same way. Only SETTINGS → **RESTART SAME**
reloads it (the process re-execs with `--resume`); a plain reboot or **RESTART**
comes up on defaults.

**The one exception is the input config** — `input_primary`, `keymap`,
`keymap_dev`, `pad_cols`, `pad_rows` are read out of `prefs.json` on *every*
boot. Which keyboard drives the instrument and what its keys mean describe the
rig, not the session; if they reset with everything else, a calibrated macropad
would come up unmapped after every power-cut. Log line to confirm:
`[config] input prefs: primary=… , N keys mapped`.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Black HDMI output | `journalctl -u recur -n 50`; `/tmp/mpv.err`; both HDMI DRM connectors need `hdmi_force_hotplug=1` in `/boot/firmware/config.txt` |
| Numpad / pad dead | unplug/replug — the controller rescans every 2 s; check `journalctl` for `[kbd] input node:` (each keyboard it opened) and `[kbd] primary input:` (which one drives play). If the primary named there isn't the device in your hands, set it on the INPUT page |
| Pad presses do nothing but the menu still works | the pad has no keymap — INPUT → **CALIBRATE PAD**, or unplug/replug to get the boot offer |
| Can't scroll or confirm anywhere — the map is broken | hold **any 3 keys for 2 s** for the panic offer, or power-cycle (the boot offer re-appears on a lockout). `journalctl -u recur \| grep lockout` names the missing controls |
| One control does nothing, the rest are fine | a gap, not a lockout — INPUT → **FIX MISSING** walks just the unbound ones |
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
- **Recording is untested end-to-end** — the kmsgrab pipeline and service
  capability are in place but haven't been verified with a real capture yet;
  check `/tmp/ffmpeg-rec.err` on first use.

### Gaps introduced by the tab/grid interface (`recur-newgui`)

The numpad's interaction model was rewritten from a single always-live
perform surface into the tab/grid/params system this manual now describes.
A few things the old scheme (and its docstrings/comments) still describe
were not carried over to the new one — they remain fully implemented
elsewhere (engine code, `prefs.json`/preset fields, MIDI) but currently have
no live control path from the numpad:

- **Display tab is not instrument mode.** The top-row keys only pick which
  *display tab* is showing; **Enter** on that tab's **grid** screen is what
  commits the mode, and it is the only numpad route into SHADER / SAMPLER /
  LIVE. The trap is that Enter does this on the grid screen *only* — on a
  tab's params screen the same key toggles param edit mode, which on LIVE
  (no params) looks like a dead key. Mode also changes via the GPIO mode
  button (BCM 5), MIDI (CC 80/82/83, notes 120/122/123), the SETTINGS menu's
  `MODE` row, or loading a preset that specifies one.
- **COLOUR / BLEND "layers" are unreachable.** `KeyboardController` still has
  the stepping logic for hue, saturation, and shader/overlay blend
  mode/amount/source (`control/keyboard.py` `_step_param`, layers 2–3), but
  nothing ever selects those layers (no key sets `_param_layer` to 2 or 3) and
  no SPI screen renders them. Only the plain on/off toggles (SETTINGS menu's
  OVERLAY/BLEND rows) and a few MIDI CC targets (`blend_amt`, `ovl_opacity`,
  `trl_decay`) still work. **The old TRAIL layer (4) is likewise unreachable,
  but its controls are no longer stranded** — trail on/off, echo count, delay,
  blend mode and per-echo opacity are all on the per-clip CLIP settings screen
  (layer 5), reached by holding a clip.
- **IN/OUT clip points can't be set.** `SamplerEngine.set_in()`/`set_out()`
  are fully implemented and the clip timeline still draws them, but no key,
  menu action, MIDI CC, or GPIO button calls them.
- **Camera→clip recording (`inst.record_toggle()`) has no trigger.** The
  SAMPLER/LIVE tab's `REC`/`SAV` status chip is built to show it, but the
  keyboard/GPIO/MIDI/menu layers never call it — don't confuse it with the
  GPIO REC button (BCM 13), which drives the separate HDMI kmsgrab recorder.
- **The old hold-0-tap-`.` / hold-`/`-plus-slot combos are gone** — those
  specific bindings (from the pre-`recur-newgui` scheme) no longer exist.
  Hold-to-configure now exists again on the SHADER, FX, SAMPLER and preset
  grids (see *Numpad layout* → *Tap vs. hold*); it's a from-scratch mechanism
  (`control/keyboard.py` `_on_key_down`/`_on_key_up`/`_fire_hold`, a
  `threading.Timer` per keypress), not a revival of the old combos.
- **`cfg.shader_slots` (SHADERS menu page slot assignment) is vestigial.**
  The SHADER tab's grid doesn't consult it — it just paginates every
  generative shader alphabetically, 9 per page. Clip slots (`clip_slots`,
  SAMPLER tab) are now the only true per-key assignments — presets dropped
  theirs when the preset grids made a preset's position its identity
  (see *Preset grids*).
- **OSD toast messages are silent.** `control/osd.py`'s `show()` only logs
  at debug level — the many `inst.osd.show(...)` calls throughout the
  codebase (staged-pick confirmations, param names, etc.) produce no visible
  feedback; all status now has to come from the persistent tab/grid/params
  screens.
