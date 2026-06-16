//!DESC kaleido_warp — radial mirror warp of incoming video
//!HOOK MAIN
//!BIND HOOKED

// Ported from the original recur (cyberboy666/r_e_c_u_r) kaleidoscope.frag.
//
// SQUARE_SRC / SQ_SCALE_X / SQ_SCALE_Y: when this FX is stacked on a
// generative shader, the engine renders the generative pass into a
// square buffer sized to the frame's diagonal (so the warp has margin
// to sample from in every direction instead of going out of bounds
// into black at any rotation angle) and substitutes SQUARE_SRC=1 plus
// the real SQ_SCALE_X/SQ_SCALE_Y here so the final sample lands in the
// right spot in that square buffer.

#define PARAM_1 0.3    /* sectors  */
#define PARAM_2 0.5    /* spin     */
#define PARAM_3 0.5    /* centre X */
#define PARAM_4 0.5    /* centre Y */

#define SQUARE_SRC 0
#define SQ_SCALE_X 1.0
#define SQ_SCALE_Y 1.0

vec4 hook() {
    vec2 uv     = HOOKED_pos;
    vec2 centre = vec2(0.5) + (vec2(PARAM_3, PARAM_4) - 0.5) * 0.6;
    vec2 v      = uv - centre;
    float r     = length(v);
    float a     = atan(v.y, v.x);

    int   sectors = 1 + int(PARAM_1 * 20.0);
    float seg     = 6.28318 / float(sectors);
    a = mod(a, seg);
    a = abs(a - seg * 0.5);
    a += float(frame) / 60.0 * (PARAM_2 - 0.5) * 2.0;

    vec2 pos = centre + vec2(cos(a), sin(a)) * r;

#if SQUARE_SRC
    vec2 spos = vec2((pos.x - 0.5) * SQ_SCALE_X + 0.5,
                      (pos.y - 0.5) * SQ_SCALE_Y + 0.5);
#else
    vec2 spos = pos;
#endif
    if (spos.x < 0.0 || spos.x > 1.0 || spos.y < 0.0 || spos.y > 1.0)
        return vec4(0.0, 0.0, 0.0, 1.0);
    return textureLod(HOOKED_raw, spos, 0.0);
}
