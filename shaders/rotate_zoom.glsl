//!DESC rotate_zoom — spinning rotate + zoom warp
//!HOOK MAIN
//!BIND HOOKED

// Ported from the original recur (cyberboy666/r_e_c_u_r) rotate_fine.frag.
//
// SQUARE_SRC / SQ_SCALE_X / SQ_SCALE_Y: when this FX is stacked on a
// generative shader, the engine renders the generative pass into a
// square buffer sized to the frame's diagonal (so the spin has margin
// to sample from in every direction instead of going out of bounds
// into black at any rotation angle) and substitutes SQUARE_SRC=1 plus
// the real SQ_SCALE_X/SQ_SCALE_Y here so the final sample lands in the
// right spot in that square buffer.

#define PARAM_1 0.5    /* spin     */
#define PARAM_2 0.5    /* centre X */
#define PARAM_3 0.5    /* centre Y */
#define PARAM_4 0.5    /* zoom     */

#define SQUARE_SRC 0
#define SQ_SCALE_X 1.0
#define SQ_SCALE_Y 1.0

vec4 hook() {
    vec2  centre = vec2(0.45 + 0.1 * PARAM_2, 0.45 + 0.1 * PARAM_3);
    float zoom   = mix(0.2, 3.0, PARAM_4);
    vec2  pos    = (HOOKED_pos - centre) / zoom + centre;

    float r    = distance(centre, pos);
    float a    = atan(pos.x - centre.x, pos.y - centre.y);
    float spin = (PARAM_1 - 0.5) * 2.0 * (float(frame) / 60.0);

    pos.x = r * cos(a + spin) + 0.5;
    pos.y = r * sin(a + spin) + 0.5;
    pos.x = 1.0 - pos.x;

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
