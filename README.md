# recur-recur

A r_e_c_u_r-inspired live video instrument for the **Raspberry Pi 5**.

**→ See [MANUAL.md](MANUAL.md) for all controls, workflows, and operation.**

## Architecture

A single mpv process is the sole renderer — it owns the HDMI output (DRM on
`/dev/dri/card1`) at all times and is driven over a JSON IPC socket
(`/tmp/recur-mpv.sock`). GLSL shaders are hot-swapped via mpv's
`glsl-shaders` property; temporal effects (trail, V-overlay) are lavfi
video-filter chains. Status and the menu UI render on a 3.5" ILI9486 SPI
display via a direct spidev driver (`control/display.py`) — no fbtft.

```
main.py (RecurInstrument)
├── engine/sampler.py   mpv lifecycle + IPC, clips, play modes, vf chains
├── engine/shader.py    GLSL param substitution → unique tmp paths → mpv
├── engine/mixer.py     ffmpeg kmsgrab recording
└── control/            keyboard (numpad), menu, display, midi, gpio, osd
```

Three modes — **SAMPLER** (clips + FX), **SHADER** (generative GLSL,
optionally blended with clip/camera), **LIVE** (CSI/USB camera) — cycled with
the numpad **Enter** key. State persists in `prefs.json`.

## Install

```bash
cd ~/recur-recur
./setup.sh                 # packages + /boot/firmware/config.txt entries
./install-service.sh       # appliance mode: boots into the instrument
sudo reboot
```

Drop video files (`.mp4 .mov .mkv .avi .webm`) into `clips/`.

```bash
sudo systemctl restart recur   # apply changes
journalctl -u recur -f         # logs
```

For debugging, stop the service and run `python3 main.py -v` directly.

## Writing shaders

Shaders live in `shaders/` and use mpv's hook format with four live-tunable
parameters. The `/* comment */` after each define becomes the UI label:

```glsl
//!DESC my shader
//!HOOK MAIN
//!BIND HOOKED

#define PARAM_1 0.5    /* speed */
#define PARAM_2 0.5    /* scale */
#define PARAM_3 0.5    /* warp  */
#define PARAM_4 0.5    /* mix   */

vec4 hook() {
    vec4 c = HOOKED_texOff(vec2(0.0));
    return c * vec4(PARAM_1, PARAM_2, PARAM_3, 1.0);
}
```

FX shaders (process video) are picked up automatically. Generative shaders
(ignore the input, draw from scratch) must be added to `generative_shaders`
in `config.py` so they appear in SHADER mode instead of the FX list.

When a knob moves, the engine rewrites the `PARAM_N` values and writes the
shader to a **new unique temp path** before handing it to mpv — mpv 0.40
caches compiled shaders by path and will not recompile a reused path.
Changes are debounced 100 ms to avoid recompile storms.
