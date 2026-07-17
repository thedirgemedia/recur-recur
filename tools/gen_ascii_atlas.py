#!/usr/bin/env python3
"""gen_ascii_atlas.py — regenerate shaders/ascii.glsl with a baked glyph atlas.

recur-web builds this atlas at runtime: it renders each candidate character to a
2D canvas, measures its ink density, sorts light->dark, and uploads the strip as
a texture the shader indexes by luminance. A user shader has no canvas, but mpv
lets a shader carry a texture inline (//!TEXTURE), so we do the same measuring
here at build time and embed the result. Same charset, same 10x14 cells, same
density ordering, same NEAREST filtering — the effect matches rather than
approximates.

Run after changing CHARS or the fonts:

    python3 tools/gen_ascii_atlas.py

Needs Pillow, and DroidSansFallbackFull for the kana/kanji (fonts-droid-fallback).
"""

import os
import sys

from PIL import Image, ImageDraw, ImageFont

# Cell size. recur-web uses 10x14, which is what its canvas font renders at;
# blown up to 30-90px cells on screen that is visibly blocky, so we bake at 2x
# and keep the same 10:14 ratio (the shader reuses it as the cell aspect, and it
# is what keeps characters upright). The atlas is one strip N*CW wide, so this
# has to stay under the GL max texture width: 137 * 20 = 2740, against a 4096
# limit on the Pi 5's V3D. Going to 30x42 would blow past it.
CW, CH = 20, 28

# recur-web's charset, verbatim. Order here is irrelevant — everything is
# re-sorted by measured density below, exactly as the web build does.
CHARS = (
    " .·:;,'\"`^~-_|/\\!i1lrjftczso+<>=xkuvywbdpq0693&#%@WMQB"
    "ｦｧｨｩｪｫｬｭｮｯｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ"
    "一二三十口日目中人大小山川水火木金土月生死力電波光音色夢"
)

# The web asks for bold Courier New, falling back to MS Gothic / Hiragino for
# the CJK. Closest equivalents here: a bold mono for Latin, Droid's fallback
# (the only installed face with kana + kanji coverage) for the rest.
FONT_LATIN = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
FONT_CJK = "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "shaders", "ascii.glsl")


def fit_font(path, cell_h):
    """Largest font size whose ascent+descent still fits inside cell_h.

    Asking for `truetype(path, cell_h)` does NOT give a glyph cell_h tall — the
    em size is not the inked height. At size 14 DejaVu needs 17px and Droid 19,
    so glyphs drawn into a 14px cell overflow and get clipped, which is what
    made the mosaic look jumbled. Measure and step down instead.
    """
    size = cell_h
    while size > 1:
        font = ImageFont.truetype(path, size)
        ascent, descent = font.getmetrics()
        if ascent + descent <= cell_h:
            return font, ascent
        size -= 1
    return ImageFont.truetype(path, 1), 1


def render(ch, latin, cjk):
    """One character, rendered into a CW x CH cell as an 'L' image.

    Glyphs share a baseline (so relative heights stay meaningful — a comma must
    not be scaled up to fill the cell) and are centred horizontally in the cell.
    Wide glyphs, i.e. the double-width CJK, are squeezed to fit rather than
    clipped, as the web does with a canvas x-scale.
    """
    font, ascent = (latin if ord(ch) < 128 else cjk)
    w = font.getlength(ch)
    if w <= 0:
        return Image.new("L", (CW, CH), 0)

    # Draw at natural width first so a squeeze resamples a complete glyph
    # rather than a clipped one. Baseline placement is shared across glyphs.
    tmp_w = max(1, int(round(w)))
    tmp = Image.new("L", (tmp_w, CH), 0)
    ImageDraw.Draw(tmp).text((0, ascent), ch, font=font, fill=255, anchor="ls")

    # Keep a blank column each side: the atlas is one long strip sampled with
    # LINEAR, so a glyph flush against the cell edge bleeds into its neighbour.
    max_w = CW - 2
    if tmp_w > max_w:
        tmp = tmp.resize((max_w, CH), Image.LANCZOS)
        tmp_w = max_w
    x = (CW - tmp_w) // 2              # centre: the monospace advance is
                                       # narrower than the cell, and hard-left
                                       # read as uneven spacing
    cell = Image.new("L", (CW, CH), 0)
    cell.paste(tmp, (x, 0))
    return cell


def main():
    for path in (FONT_LATIN, FONT_CJK):
        if not os.path.exists(path):
            sys.exit("missing font: %s" % path)

    latin = fit_font(FONT_LATIN, CH)
    cjk = fit_font(FONT_CJK, CH)
    print("fitted: latin size=%d, cjk size=%d (cell %dx%d)"
          % (latin[0].size, cjk[0].size, CW, CH))

    chars = list(dict.fromkeys(CHARS))          # de-dupe, keep first occurrence
    cells = [(ch, render(ch, latin, cjk)) for ch in chars]
    # Sort light -> dark by total ink, so a luminance index maps straight onto
    # the ramp. This is the whole point of measuring rather than hand-ordering.
    cells.sort(key=lambda c: sum(c[1].getdata()))

    n = len(cells)
    atlas = Image.new("L", (CW * n, CH), 0)
    for i, (_, cell) in enumerate(cells):
        atlas.paste(cell, (i * CW, 0))

    hexdata = atlas.tobytes().hex()
    order = "".join(ch for ch, _ in cells)

    with open(OUT, "w") as f:
        f.write(TEMPLATE % {
            "n": n,
            "w": CW * n,
            "h": CH,
            "cw": float(CW),
            "ch": float(CH),
            "order": order.replace("\\", "\\\\"),
            "hex": hexdata,
            "bytes": len(atlas.tobytes()),
        })
    print("%s: %d glyphs, atlas %dx%d, %d bytes"
          % (os.path.relpath(OUT, ROOT), n, CW * n, CH, len(atlas.tobytes())))
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

// GENERATED by tools/gen_ascii_atlas.py — edit that, not this file.
//
// The atlas above is %(n)d glyphs in %(cw).0fx%(ch).0f cells, sorted light to dark by
// measured ink so a luminance index walks the ramp directly:
//   %(order)s

const float N = %(n)d.0, CW = %(cw).1f, CH = %(ch).1f;

float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }

vec4 hook() {
    vec2 size = HOOKED_size;

    // Cell size in pixels, held at the glyph's own aspect so characters stay
    // upright instead of stretching with the cell, and SNAPPED TO WHOLE PIXELS.
    // A fractional cell (9.43 x 13.20 at the default size) puts every cell on a
    // different sub-pixel offset, so each glyph resamples differently and the
    // mosaic reads as overlapping and jittering rather than as a grid.
    vec2 cell_sz = max(vec2(2.0),
                       floor(mix(4.0, 96.0, PARAM_1) * vec2(CW / CH, 1.0) + 0.5));
    vec2 cell_id = floor(HOOKED_pos * size / cell_sz);

    // One colour sample per cell, taken at its centre: it picks the character,
    // and it tints it. Nothing here is a fixed colour — each glyph carries the
    // colour of the image underneath it.
    vec2  cell_uv = (cell_id * cell_sz + cell_sz * 0.5) / size;
    vec3  col     = textureLod(HOOKED_raw, clamp(cell_uv, 0.0, 1.0), 0.0).rgb;
    float luma    = dot(col, vec3(0.299, 0.587, 0.114));

    // Walk the ramp by luminance; invert flips which end is dense. `variation`
    // blends toward a per-cell random glyph, so flat areas break up instead of
    // tiling one character.
    float luma_idx = PARAM_2 > 0.5 ? floor(luma * (N - 0.01))
                                   : floor((1.0 - luma) * (N - 0.01));
    float rand_idx = floor(hash(cell_id) * (N - 0.01));
    float idx      = clamp(mix(luma_idx, rand_idx, PARAM_4), 0.0, N - 1.0);

    // The atlas is declared as a plain `uniform sampler2D`: the _tex()/_raw
    // accessors mpv generates for bound video textures (HOOKED) do not exist
    // for shader-embedded ones, so sample it directly — same lookup as the web.
    // (Careful: never write the directive prefix inside a comment. mpv scans
    // for it anywhere in a line and will cut the shader in half right there.)
    vec2  pos   = fract(HOOKED_pos * size / cell_sz);
    float glyph = texture(ASCII_ATLAS, vec2((idx + pos.x) / N, pos.y)).r;

    vec3 ascii = col * glyph;
    vec3 orig  = textureLod(HOOKED_raw, HOOKED_pos, 0.0).rgb;
    return vec4(mix(ascii, orig, PARAM_3), 1.0);
}
'''


if __name__ == "__main__":
    main()
