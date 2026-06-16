//!DESC kaleido_warp — radial mirror warp of incoming video
//!HOOK MAIN
//!BIND HOOKED

// Ported from the original recur (cyberboy666/r_e_c_u_r) kaleidoscope.frag.

#define PARAM_1 0.3    /* sectors  */
#define PARAM_2 0.5    /* spin     */
#define PARAM_3 0.5    /* centre X */
#define PARAM_4 0.5    /* centre Y */

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
    if (pos.x < 0.0 || pos.x > 1.0 || pos.y < 0.0 || pos.y > 1.0)
        return vec4(0.0, 0.0, 0.0, 1.0);
    return textureLod(HOOKED_raw, pos, 0.0);
}
