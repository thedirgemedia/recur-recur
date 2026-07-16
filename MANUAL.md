# recur-recur — Operator Manual

*Accurate as of 2026-07-16. Generated from the actual code paths — where the
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

The SPI display is organised as five **tabs** — SHADER, FX, SAMPLER, LIVE,
SETTINGS — and the top-row keys are permanently bound to them, in *every*
context (even while a menu page is open):

| Key | Tab |
|---|---|
| **Num** | SHADER |
| **/** | FX |
| **\*** | SAMPLER |
| **-** | LIVE |
| **.** | SETTINGS — *or* back out one level, depending on where you are (see below) |

Pressing Num/\//\*/- for the tab you're **already on** cycles that tab's
sub-screens (a 3×3 slot **grid** first, then a **params** screen for tabs
that have one). Pressing a *different* tab's key jumps straight to that
tab's grid and closes any open menu page.

**.** is the odd one out — a single press whose meaning depends on how deep
you currently are. At the **top level** (a grid screen, no menu open) it's
the SETTINGS tab key, exactly like the other four are for their tab.
**Anywhere deeper it goes up one level** instead: it cancels an in-progress
menu sub-action (assign / CC-edit / confirm-delete / USB browse) if one's
active, else closes an open menu page, else exits param edit mode, else
drops a params screen back to its grid.

> **Note:** these keys only change what the SPI display is showing — they do
> **not** switch the instrument's SAMPLER/SHADER/LIVE mode. Mode is switched
> by the GPIO mode button, MIDI (CC 80/82/83 or notes 120/122/123), or the
> SETTINGS menu's `MODE` row (see *Menu system*). See *Known issues* — there
> is currently no numpad key bound to mode switching.

### The 3×3 grid (first screen of SHADER / FX / SAMPLER / LIVE / SETTINGS)

Grid cells sit at keys **7 8 9 / 4 5 6 / 1 2 3**, matching their on-screen
position (7 = top-left … 3 = bottom-right):

| Key | Action |
|---|---|
| **1–9** | select that grid cell (see per-tab behaviour below) |
| **+** | next page (SHADER / FX grids only — they paginate 9 shaders at a time) |
| **Bksp** | previous page (SHADER / FX grids only) |
| **0** | toggle **STAGED** mode (SAMPLER grid only) — footer pill shows amber `STAGED` (clip picks wait for **Enter**) vs. green `LIVE` (clip picks apply immediately). SHADER and FX grid taps always apply immediately regardless of this setting. Turning STAGED back off discards anything pending |
| **Enter** | push all staged picks to the live output |
| **000** | toggle the temporal trail (works from any screen) |

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
  page). Tap triggers the clip; tapping the currently-playing clip's key
  again drills into a **SPEED** params screen (playback speed + reverse)
  instead of retriggering it.
- **LIVE** — the 6 preset slots (keys 4–9; assigned in the PRESETS menu
  page). Always loads immediately — presets are never staged.
- **SETTINGS** — six cells (BROWSER / SHADERS / PRESETS / SETTINGS / MIDI /
  IMPORT) that jump straight into the matching menu page (see *Menu system*).

### Tap vs. hold (SHADER and FX grids only)

Release a grid key within ~0.4s and it's a **tap**; keep it held past that
and it fires as a **hold** instead — the tap action never also fires
afterwards. On the SHADER and FX grids, tap always toggles stack membership
immediately — STAGED mode has no effect on either grid, only on SAMPLER clip
picks (see the table above). Holding always activates the item first if it
wasn't already (adds the shader / FX to its stack) and jumps to its params
screen; holding to configure something is a workshop action, not a
performance change. SAMPLER/LIVE/SETTINGS grids don't use hold at all —
pressing those keys always dispatches immediately, regardless of how long
you hold them. No top-row key (**.** included) uses hold.

### Record

Recording is not triggered from the numpad at all — see *Workflows* → *Record*
and the GPIO table for the one recorder that is actually wired up (BCM 13,
HDMI output → `.mkv`).

---

## Parameter editing

Each tab that has tunable parameters shows a second **params** screen — reach
it via hold (SHADER/FX), by drilling into SPEED (SAMPLER), or by pressing
that tab's own key again while its grid is showing. Params screens are
**scrollable lists** (not capped at 9 rows) with two interaction modes:

| Key | Outside edit mode | Inside edit mode |
|---|---|---|
| **+** | scroll selection up the list | increase the selected parameter |
| **Bksp** | scroll selection down the list | decrease the selected parameter |
| **1–9** | jump to a parameter by its on-screen grid position | *(no effect)* |
| **Enter** | enter edit mode on the highlighted parameter | exit edit mode |
| **0** | toggle STAGED / LIVE | toggle STAGED / LIVE |
| **000** | toggle the temporal trail | toggle the temporal trail |

The header and the selected row turn amber while in edit mode, so it's
visually obvious whether +/Bksp will move the selection or change a value.

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

### SAMPLER params — FX chain params, speed, and status

The SAMPLER tab's params screen shows the clip name, PLAY/OVL/TRL/REC status,
the active FX chain, and the f1–f4 bars of the FX chain slot currently
selected for editing (same data as the FX tab, read-only summary — not
scrollable/editable from here), plus the clip timeline (playhead / IN / OUT
ticks) at the bottom. Tapping the playing clip's key again on the grid
screen instead opens a dedicated **SPEED** screen (nudge ±0.1, range
0.1–4.0, plus a reverse toggle).

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

Global hue/saturation, the shader↔video and overlay blend
**mode/amount/source** details, and the trail's **type/mode/delay/echo-count**
are all still fully implemented in `engine/shader.py` / `main.py` and are
still loaded/saved with presets and `prefs.json` — but no numpad key, grid
cell, or SPI screen currently exposes them (the old Bksp-cycled
COLOUR/BLEND/TRAIL layers were dropped when the tab/grid interface replaced
the old scheme). See *Known issues*. Some of these remain reachable via MIDI
CC (`blend_amt`, `ovl_opacity`, `trl_decay` are user-assignable targets) or
via the SETTINGS menu's on/off toggles and FX/GEN cycle rows.

Note: a generative shader's own **palette param (p4)** is *not* in this list
— it's an ordinary shader parameter, so if the shader's source defines a
`PARAM_4`, it shows up on the SHADER params screen exactly like p1–p3 (see
below). It's unrelated to the FX chain's per-layer `BLEND` row above.

---

## SPI display

**Tab bar** (top 42 px) — always visible: SHADER / FX / SAMPLER / LIVE /
SETTINGS, the active tab highlighted with a bright top accent bar. When the
active tab has more than one sub-screen, small dots under its name show which
one you're on.

**Grid screens** — a 3×3 button grid (see *Numpad layout*): green outline +
fill for the active/loaded item, amber outline + tint for a staged (pending)
pick, dim outline for an empty slot.

**Params screens** — horizontal sliders (one per parameter, selected one
highlighted) above a compact 3×3 selector grid mirroring the same layout;
both scroll together, windowed around the selection, for lists longer than
fit on screen. The header and selected row turn amber while in edit mode
(see *Parameter editing*).

**Footer** (bottom 22 px, every screen) — a pill on the left reading `LIVE`
(green) or `STAGED` (amber, with an `ENTER → PUSH` hint on the right); the
mode name is shown on the right when live.

**Menu overlay** — when a menu page (BROWSER/SHADERS/PRESETS/SETTINGS/MIDI/
IMPORT) is open it replaces the whole screen (tab bar and footer included)
until you back out of it — see *Menu system*.

There is **no touch input** — all control is numpad / MIDI / GPIO.

---

## Menu system

Menu pages are reached through the **SETTINGS tab** (key **.** from any grid
screen): its grid has six cells — BROWSER, SHADERS, PRESETS, SETTINGS, MIDI,
IMPORT — and pressing one jumps straight into that page. To back out of a
page, press **.** again — this cancels any in-progress sub-action first
(assign / CC-edit / confirm-delete / USB browse) if one's active, otherwise
closes the page back to the SETTINGS grid; pressing any *other* tab key also
closes the page and jumps to that tab.

> **Note:** because **.** now backs out of an open page rather than
> advancing, it no longer cycles onward through SETTINGS' own BROWSER/MIDI
> sub-screens — pressing it on the SETTINGS grid opens BROWSER, and pressing
> it again returns to the grid. Both pages remain reachable from the
> SETTINGS grid cells (BROWSER = key 7, MIDI = key 5).

| Key | Action in menu |
|---|---|
| **8 / 2** | selection up / down |
| **4 / 6** | adjust value (SETTINGS, MIDI, PRESETS pages) |
| **5** | primary action: load (BROWSER/SHADERS/PRESETS), activate (SETTINGS), edit CC (MIDI), mount/copy (IMPORT) |
| **Enter** | BROWSER/SHADERS/PRESETS: start slot-assign; IMPORT: eject; elsewhere same as 5 |
| **7 / 9** | previous / next page (wraps) |
| **+** | scroll up (alternative to 8) |
| **Bksp** | scroll down (SETTINGS); reset CC (MIDI); eject (IMPORT); arm/confirm delete (BROWSER, PRESETS) |

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
- **Bksp, then Bksp again** — delete the highlighted clip file. The first press
  arms it (`BKSP again = DELETE FILE`); any other key cancels. Only internal
  `clips/` files can be deleted (removable drives are read-only); the file is
  removed from disk and cleared from any slot it held.

### SHADERS page
Lists generative shaders and lets you assign one to a performance slot
(4–9), same interaction as BROWSER. **Note:** unlike clip and preset slots,
the SHADER tab's own grid does not currently read this assignment — it just
paginates every generative shader alphabetically, 9 per page — so slot
assignments made here are saved but have no effect on what a numpad key
loads. Use **5** to stage/load a shader here instead if you want a
deterministic pick.

### PRESETS page
Lists saved presets (`presets/*.json` — shader + FX + params snapshots).
- **5** — load the highlighted preset onto the live output immediately (not staged).
- **6** — save the current live state as a new preset (auto-named `P01.json`, `P02.json`, …).
- **Enter, then a key 4–9** — assign the highlighted preset to that key (loaded from the LIVE tab's grid).
- **Bksp, then Bksp again** — delete the highlighted preset file (same arm/confirm pattern as BROWSER).

### SETTINGS page

The SETTINGS page header shows the Pi's current IP address (useful for SSH when the display is the only UI available). Rows are laid out as a slider grid with three action buttons underneath.

| Row | Description |
|---|---|
| MODE | instrument mode — SAMPLER/SHADER/LIVE (4/6 or 5 cycles) |
| LIVE MODE | ON/OFF — OFF removes LIVE from the mode cycle |
| PLAY | sampler playback mode (see *Playback modes*) |
| CAM RES | camera capture: 320×180 / 640×360 / 1280×720 (applies on next LIVE entry) |
| VID SCALE | video scaling mode for mismatched aspect ratios |
| OVERLAY | V-overlay on/off toggle |
| BLEND | shader blend on/off toggle |
| FX | cycle the FX chain's currently-edited slot to the next/previous FX shader |
| GEN | cycle the active generative shader |
| SAVE PREFS | write current state to `prefs.json` now |
| RESTART | restart the application |
| SYSTEM | quit the application (a Pi poweroff would need root; the service can't escalate) |

Note: this page has on/off toggles for OVERLAY and BLEND, and cycle actions
for FX/GEN, but no rows for their mode/amount/source/palette/hue/sat details
— see *Parameter editing* → "Not currently reachable from the numpad".

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

### Build a performance set
1. Press **.** (SETTINGS tab) → tap the **BROWSER** cell. Highlight a clip,
   press **Enter**, then press the slot key (4–9) to assign it.
2. Repeat for up to six clips; press **9** to page forward to **PRESETS** and
   assign saved look snapshots to slots the same way — these two (clips,
   presets) are true slot assignments. (SHADERS also lets you assign a
   generative shader to a slot, but nothing currently reads that assignment
   back — see the SHADERS page note above; use its own **5** to stage/load
   a shader by hand instead.)
3. Press **.** to back out to the SETTINGS grid, or another tab key to leave
   the menu entirely. The SAMPLER tab's grid now fires clips at keys 4–9 and
   the LIVE tab's grid fires presets at keys 4–9.
4. SETTINGS page → **SAVE PREFS** to keep the assignments across restarts.

### Auto-playing set (playlist)
1. Assign clips to slots (above).
2. SETTINGS page → **PLAY** row → `playlist`. Clips now chain automatically
   in slot order (4→5→…→9→4), advancing at each clip's end or OUT point.

### Chain multiple FX over the video
1. Press **-** (FX tab). Tap an FX cell to add it to the chain (up to 4 at
   once) — its cell gets a `[n]` badge showing its position; you stay on the
   grid, so you can keep tapping to build up the whole chain before touching
   any params. Tap it again to remove it.
2. To tune a chained FX's params (or check/adjust a *different* member
   without disturbing the others), **hold** its key — that opens its params
   screen without adding/removing anything.
3. On an FX's params screen: scroll with **+ / Bksp**, jump to a row with
   **1–9**, press **Enter** to edit the highlighted row, then **+ / Bksp** to
   step its value, **Enter** again to stop editing. The last two rows —
   `BLEND` and `BLD AMT` — set how this layer composites with whatever's
   below it (default `NORMAL` = plain pass-through, no compositing).
4. The chain applies in SAMPLER, LIVE, *and* on top of a loaded generative
   shader in SHADER mode — it isn't mode-specific.

### Layer FX with blend modes (Photoshop-style stacking)
1. Chain 2+ FX as above. Each one defaults to `NORMAL` blend (just its own
   effect, no interaction with the layers below).
2. Hold a chained FX's key to open its params, scroll to `BLEND`, press
   **Enter**, then **+ / Bksp** to pick a mode (screen, multiply, overlay,
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
3. On a shader's params screen: scroll with **+ / Bksp**, jump to a row with
   **1–9**, **Enter** to edit the highlighted row, then **+ / Bksp** to step
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
Press **000** (works from any screen) to toggle the trail on/off — this is
currently the *only* trail control reachable from the numpad. The trail's
type/mode/delay/echo-count and the global hue/saturation and blend
mode/amount/source details are still fully implemented (and still saved in
`prefs.json` and presets) but have no live control surface right now besides
MIDI CC (`blend_amt`, `ovl_opacity`, `trl_decay`) — see *Known issues*.

### Map a MIDI controller
1. Plug in — connects automatically within 3 s.
2. Defaults work for most controllers (mod wheel = p1, etc. — see *MIDI reference*).
3. To rebind: **.** (SETTINGS tab) → **MIDI** cell → highlight target → **5**
   → type the CC number → **Enter**. User overrides win over built-ins.

### Keep your setup
Everything important (slots, effect states, modes, MIDI overrides, params) is
written to `prefs.json` on clean shutdown, or on demand with SETTINGS page →
**SAVE PREFS**. Prefer SAVE PREFS after big changes — a power-cut skips the
auto-save.

---

## Effects reference

### Trail — echo time delay (`000`)
Works in all three modes; **000** is currently the only numpad control for it
(on/off only — see *Known issues*). Two underlying types exist in
`prefs.json`/presets (`trail_blend_type`), though neither the numpad nor the
SETTINGS menu currently expose a way to switch between them live:

**MODE** — `split → tpad(delay) → lagfun(decay) → blend` on the luma plane
only (chroma passed through — no colour shifts). One continuous fading ghost.
Blend modes: screen, difference, multiply, overlay, addition, subtract, lighten,
darken, phoenix, negation, divide. Decay (`trail_decay`, 0.80–0.99) and blend
mode (`trail_mode`) live in `prefs.json`/presets only right now.

**OPACITY** — `split → N×tpad(step×1…N) → mix=inputs=N+1:weights=…`.
A weighted average of the live frame plus N progressively-delayed past echoes
(N = `trail_echo_count`, 1–5), spaced `delay/N` apart (tail ≈1.7× the delay
window). `mix` normalises by the weight sum, so brightness is preserved and
identical static regions stay sharp — only moving content ghosts (no
wash-out, no pre-echo). Layer weights tunable as `trail_step_weights` in
`prefs.json` (live first, then N echoes); `trail_mode`/`trail_decay` have no
effect in this type.

**In SHADER mode the trail is unavailable.** The lavfi trail runs in the vf
chain *before* the generative shader, which renders over it, so it never shows.
A GLSL trail would need cross-frame feedback (this frame reading last frame's
accumulator); the Pi 5 V3D / libplacebo renderer does not persist feedback
textures between frames (verified with controlled render tests), so it cannot
be done on this hardware. Toggling the trail in SHADER mode shows
`TRAIL N/A IN SHADER` and does nothing. The trail is a SAMPLER / LIVE feature.

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
and **+ / Bksp** scroll on to p10–p15 (emitter 2's Y position, star count,
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

- **No numpad key switches the instrument's SAMPLER/SHADER/LIVE mode.** The
  top-row keys only pick which *display tab* is showing. Mode changes only
  via the GPIO mode button (BCM 5), MIDI (CC 80/82/83, notes 120/122/123),
  the SETTINGS menu's `MODE` row, or loading a preset that specifies one.
- **COLOUR / BLEND / TRAIL "layers" are unreachable.** `KeyboardController`
  still has all the stepping logic for hue, saturation, trail
  opacity/decay/delay/echo-count/type/mode, and shader/overlay blend
  mode/amount/source (`control/keyboard.py` `_step_param`, layers 2–4), but
  nothing ever selects those layers any more (no key sets `_param_layer` to
  2, 3, or 4) and no SPI screen renders them. Only the plain on/off toggles
  (SETTINGS menu's OVERLAY/BLEND rows, `000` for trail) and a few MIDI CC
  targets (`blend_amt`, `ovl_opacity`, `trl_decay`) still work.
- **IN/OUT clip points can't be set.** `SamplerEngine.set_in()`/`set_out()`
  are fully implemented and the clip timeline still draws them, but no key,
  menu action, MIDI CC, or GPIO button calls them.
- **Camera→clip recording (`inst.record_toggle()`) has no trigger.** The
  SAMPLER/LIVE tab's `REC`/`SAV` status chip is built to show it, but the
  keyboard/GPIO/MIDI/menu layers never call it — don't confuse it with the
  GPIO REC button (BCM 13), which drives the separate HDMI kmsgrab recorder.
- **The old hold-0-tap-`.` / hold-`/`-plus-slot combos are gone** — those
  specific bindings (from the pre-`recur-newgui` scheme) no longer exist.
  Hold-to-configure now exists again, but only on the SHADER and FX grids
  (see *Numpad layout* → *Tap vs. hold*); it's a from-scratch mechanism
  (`control/keyboard.py` `_on_key_down`/`_on_key_up`/`_fire_hold`, a
  `threading.Timer` per keypress), not a revival of the old combos.
- **`cfg.shader_slots` (SHADERS menu page slot assignment) is vestigial.**
  The SHADER tab's grid doesn't consult it — it just paginates every
  generative shader alphabetically, 9 per page. Only clip slots
  (`clip_slots`, SAMPLER tab) and preset slots (`preset_slots`, LIVE tab)
  are true persistent per-key assignments today.
- **OSD toast messages are silent.** `control/osd.py`'s `show()` only logs
  at debug level — the many `inst.osd.show(...)` calls throughout the
  codebase (staged-pick confirmations, param names, etc.) produce no visible
  feedback; all status now has to come from the persistent tab/grid/params
  screens.
