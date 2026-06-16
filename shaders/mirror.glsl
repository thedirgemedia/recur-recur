//!DESC mirror — N-axis radial mirror fold
//!HOOK MAIN
//!BIND HOOKED

// Reflects the image across `axes` evenly-spaced lines through the centre.
// axes=1 is a single straight mirror line at the given rotation; higher
// values fold the image into a kaleidoscope-style wedge pattern.
//
// SQUARE_SRC / NATIVE_ASPECT / SQ_SCALE_X / SQ_SCALE_Y: when this FX is
// stacked on a generative shader, the engine renders the generative pass
// into a square buffer sized to the frame's diagonal (so the fold has
// margin to sample from in every direction instead of going out of
// bounds into black at any rotation angle) and substitutes SQUARE_SRC=1
// plus the real NATIVE_ASPECT/SQ_SCALE_X/SQ_SCALE_Y here so the final
// sample lands in the right spot in that square buffer.

#define PARAM_1 0.0    /* axes     */
#define PARAM_2 0.5    /* rotation */
#define PARAM_3 0.5    /* centre X */
#define PARAM_4 0.5    /* centre Y */

#define SQUARE_SRC 0
#define NATIVE_ASPECT 1.7777778
#define SQ_SCALE_X 1.0
#define SQ_SCALE_Y 1.0

vec4 hook() {
#if SQUARE_SRC
    vec2  aspect = vec2(NATIVE_ASPECT, 1.0);
#else
    vec2  aspect = HOOKED_size / HOOKED_size.y;
#endif
    vec2  centre = vec2(PARAM_3, PARAM_4);
    vec2  uv     = (HOOKED_pos - centre) * aspect;

    float r   = length(uv);
    float a   = atan(uv.y, uv.x);
    int   axes  = 1 + int(PARAM_1 * 7.0 + 0.5);
    float rot   = (PARAM_2 - 0.5) * 6.28318;
    float wedge = 3.14159265 / float(axes);

    a = mod(a + rot, wedge);
    a = abs(a - wedge * 0.5);

    vec2 pos = centre + (vec2(cos(a), sin(a)) * r) / aspect;

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
