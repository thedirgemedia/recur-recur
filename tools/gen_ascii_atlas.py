#!/usr/bin/env python3
"""gen_ascii_atlas.py — regenerate shaders/ascii.glsl with a baked glyph atlas.

recur-web builds this atlas at runtime: it renders each candidate character to a
2D canvas, measures its ink density, sorts light->dark, and uploads it as a
texture the shader indexes by luminance. A user shader has no canvas, but mpv
lets a shader carry a texture inline (//!TEXTURE), so we do the same measuring
here at build time and embed the result.

Differences from the first port (and why):

* 2D GRID atlas, not a 1D strip. A strip is N*CW wide and hits the 4096 GL
  texture limit on the Pi 5's V3D fast — which capped both glyph count and cell
  resolution. Laying glyphs out in a near-square grid escapes that, so we can
  afford many more characters at a much higher cell resolution.

* HIGHER-RES cells (36x48, was 20x28). The atlas is magnified with LINEAR up to
  ~96px cells on screen; at 28px that is a 3.4x blow-up and visibly pixelated.
  At 48px it is ~2x — far smoother. TrueType is vector, so the "high res" that
  matters is the atlas we bake, not the font file.

* FULLER charset — full printable ASCII, full hiragana, full katakana, and a
  spread of kanji from light (一) to dense (鬱). More glyphs = a finer luminance
  ramp and more per-cell variety under the `variation` param.

* Per-cell PADDING. In a grid, LINEAR sampling near a cell edge bleeds the
  neighbour above/below/beside it. A blank margin around every glyph keeps the
  seams clean.

Run after changing the charset, cell size, or fonts:

    python3 tools/gen_ascii_atlas.py

Needs Pillow, and DroidSansFallbackFull for the kana/kanji (fonts-droid-fallback).
Higher-quality output would come from a Noto Sans CJK / Source Han face if one is
ever installed — Droid's fallback is the only CJK face present here.
"""

import math
import os
import sys

from PIL import Image, ImageDraw, ImageFont

# Cell size in the atlas. Kept at ~10:14 (0.71) so characters stay upright when
# the shader reuses CW/CH as the on-screen cell aspect. CW is a multiple of 4 so
# every grid row (COLS*CW bytes) stays 4-aligned for the r8 texture upload.
CW, CH = 36, 48
# Blank margin baked around each glyph so LINEAR sampling doesn't bleed a
# neighbouring grid cell across the seam.
PAD = 3

FONT_LATIN = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
FONT_CJK = "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"

# ── Charset ────────────────────────────────────────────────────────────────
# Order here is irrelevant — everything is re-sorted by measured ink below,
# exactly as the web build does.
ASCII = "".join(chr(c) for c in range(0x20, 0x7F))               # space .. ~
HIRAGANA = "".join(chr(c) for c in range(0x3041, 0x3097))        # ぁ .. ゖ
KATAKANA = "".join(chr(c) for c in range(0x30A1, 0x30FB)) + "ー"  # ァ .. ヺ  ＋ 長音
# Kanji spread light -> dense so the ramp keeps climbing past the kana.
KANJI = (
    "一二三四五六七八九十百千万"
    "上下大小山川水火木金土日月"
    "口目田中人力女男子天空海雨風"
    "車電光音色花草木林森夢愛美"
    "生死鬼龍鑑鬱藝麗"
)
CHARS = ASCII + HIRAGANA + KATAKANA + KANJI

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "shaders", "ascii.glsl")


def fit_font(path, box_h):
    """Largest font size whose ascent+descent fit inside box_h.

    The em size is not the inked height (at 14px DejaVu needs 17), so asking for
    truetype(path, box_h) overflows the box and clips glyphs. Measure and step
    down instead. Returns (font, ascent) — ascent positions a shared baseline.
    """
    size = box_h
    while size > 1:
        font = ImageFont.truetype(path, size)
        ascent, descent = font.getmetrics()
        if ascent + descent <= box_h:
            return font, ascent
        size -= 1
    return ImageFont.truetype(path, 1), 1


def render(ch, latin, cjk):
    """One character, rendered into a CW x CH cell as an 'L' image.

    Glyphs share a baseline (relative heights stay meaningful — a comma must not
    be scaled up to fill the cell) placed PAD from the top; wide CJK glyphs are
    squeezed to the inner width rather than clipped, as the web does with a
    canvas x-scale. The PAD margin on every side keeps LINEAR sampling from
    bleeding neighbouring cells across a grid seam.
    """
    font, ascent = (latin if ord(ch) < 128 else cjk)
    w = font.getlength(ch)
    if w <= 0:
        return Image.new("L", (CW, CH), 0)

    inner_w = CW - 2 * PAD
    # Draw at natural width first so a squeeze resamples a complete glyph, not a
    # clipped one. Baseline is shared across glyphs (anchor="ls").
    tmp_w = max(1, int(round(w)))
    tmp = Image.new("L", (tmp_w, CH), 0)
    ImageDraw.Draw(tmp).text((0, PAD + ascent), ch, font=font, fill=255, anchor="ls")
    if tmp_w > inner_w:
        tmp = tmp.resize((inner_w, CH), Image.LANCZOS)   # X-only: height (baseline) preserved
        tmp_w = inner_w
    x = (CW - tmp_w) // 2                                 # centre in the cell
    cell = Image.new("L", (CW, CH), 0)
    cell.paste(tmp, (x, 0))
    return cell


def main():
    for path in (FONT_LATIN, FONT_CJK):
        if not os.path.exists(path):
            sys.exit("missing font: %s" % path)

    box_h = CH - 2 * PAD
    latin = fit_font(FONT_LATIN, box_h)
    cjk = fit_font(FONT_CJK, box_h)
    print("fitted: latin size=%d, cjk size=%d (cell %dx%d, pad %d)"
          % (latin[0].size, cjk[0].size, CW, CH, PAD))

    chars = list(dict.fromkeys(CHARS))          # de-dupe, keep first occurrence
    cells = [(ch, render(ch, latin, cjk)) for ch in chars]
    # Drop anything that rendered with no ink (e.g. an unsupported glyph): a
    # blank in the ramp would show as a hole. Space is kept explicitly as the
    # lightest rung.
    cells = [c for c in cells if c[0] == " " or sum(c[1].getdata()) > 0]
    # Sort light -> dark by total ink, so a luminance index maps straight onto
    # the ramp. This is the whole point of measuring rather than hand-ordering.
    cells.sort(key=lambda c: sum(c[1].getdata()))

    n = len(cells)
    # Near-square grid; COLS*CW stays 4-aligned because CW is a multiple of 4.
    cols = max(1, round(math.sqrt(n * CH / CW)))
    rows = math.ceil(n / cols)
    aw, ah = cols * CW, rows * CH

    atlas = Image.new("L", (aw, ah), 0)
    for i, (_, cell) in enumerate(cells):
        atlas.paste(cell, ((i % cols) * CW, (i // cols) * CH))

    hexdata = atlas.tobytes().hex()
    order = "".join(ch for ch, _ in cells)

    with open(OUT, "w") as f:
        f.write(TEMPLATE % {
            "n": n,
            "cols": cols,
            "rows": rows,
            "w": aw,
            "h": ah,
            "cw": float(CW),
            "ch": float(CH),
            "order": order.replace("\\", "\\\\"),
            "hex": hexdata,
            "bytes": len(atlas.tobytes()),
        })
    print("%s: %d glyphs, grid %dx%d, atlas %dx%d, %d bytes (%.2f MB)"
          % (os.path.relpath(OUT, ROOT), n, cols, rows, aw, ah,
             len(atlas.tobytes()), len(hexdata) / 1e6))
    print("light -> dark: %s" % order)


TEMPLATE = '''//!TEXTURE ASCII_ATLAS
//!SIZE %(w)d %(h)d
//!FORMAT r8
//!FILTER LINEAR
//!BORDER CLAMP
%(hex)s

//!DESC ascii — luminance-mapped character mosaic
//!HOOK MAIN
//!BIND HOOKED
//!BIND ASCII_ATLAS

#define PARAM_1 0.1   /* char size */
#define PARAM_2 0.0   /* invert    */
#define PARAM_3 0.0   /* mix       */
#define PARAM_4 0.2   /* variation */

// 1.0 = snap the cell to whole pixels (crisp on a still image, no sub-pixel
// shimmer). The engine rewrites this to 0.0 when an LFO drives char size, so a
// swept size varies continuously instead of stepping in whole pixels.
#define ASCII_SNAP 1.0

// GENERATED by tools/gen_ascii_atlas.py — edit that, not this file.
//
// The atlas above is %(n)d glyphs in a %(cols)dx%(rows)d grid of %(cw).0fx%(ch).0f cells,
// sorted light to dark by measured ink so a luminance index walks the ramp:
//   %(order)s

const float N = %(n)d.0, CW = %(cw).1f, CH = %(ch).1f;
const float COLS = %(cols)d.0, ROWS = %(rows)d.0;

float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }

vec4 hook() {
    vec2 size = HOOKED_size;

    // Cell size in pixels, at the glyph's own aspect so characters stay upright.
    // ASCII_SNAP rounds it to whole pixels (see the define above) — a still
    // image reads as a clean grid — but a size the engine flags as LFO-driven
    // is left continuous, so it glides between sizes instead of jumping.
    vec2 raw     = mix(4.0, 96.0, PARAM_1) * vec2(CW / CH, 1.0);
    vec2 cell_sz = max(vec2(2.0), mix(raw, floor(raw + 0.5), ASCII_SNAP));
    vec2 cell_id = floor(HOOKED_pos * size / cell_sz);

    // One colour sample per cell, taken at its centre: it picks the character
    // and tints it. Nothing here is a fixed colour — each glyph carries the
    // colour of the image underneath it.
    vec2  cell_uv = (cell_id * cell_sz + cell_sz * 0.5) / size;
    vec3  col     = textureLod(HOOKED_raw, clamp(cell_uv, 0.0, 1.0), 0.0).rgb;
    float luma    = dot(col, vec3(0.299, 0.587, 0.114));

    // Walk the ramp by luminance; invert flips which end is dense. `variation`
    // blends toward a per-cell random glyph so flat areas break up.
    float luma_idx = PARAM_2 > 0.5 ? floor(luma * (N - 0.01))
                                   : floor((1.0 - luma) * (N - 0.01));
    float rand_idx = floor(hash(cell_id) * (N - 0.01));
    float idx      = clamp(mix(luma_idx, rand_idx, PARAM_4), 0.0, N - 1.0);

    // 2D atlas lookup: idx -> (column,row) cell, plus the in-cell position.
    // The atlas is a plain `uniform sampler2D` — the _tex()/_raw accessors mpv
    // generates for bound video textures (HOOKED) do not exist for embedded
    // ones, so sample it directly. (Careful: never write the directive prefix
    // inside a comment — mpv scans for it anywhere in a line and cuts the
    // shader in half right there.)
    vec2  gpos  = fract(HOOKED_pos * size / cell_sz);
    float gcol  = mod(idx, COLS);
    float grow  = floor(idx / COLS);
    vec2  guv   = (vec2(gcol, grow) + gpos) / vec2(COLS, ROWS);
    float glyph = texture(ASCII_ATLAS, guv).r;

    vec3 ascii = col * glyph;
    vec3 orig  = textureLod(HOOKED_raw, HOOKED_pos, 0.0).rgb;
    return vec4(mix(ascii, orig, PARAM_3), 1.0);
}
'''


if __name__ == "__main__":
    main()
