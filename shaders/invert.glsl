//!DESC invert — selective per-channel colour invert
//!HOOK MAIN
//!BIND HOOKED

// Ported from the original recur (cyberboy666/r_e_c_u_r) invert_effect.frag.

#define PARAM_1 1.0    /* R invert */
#define PARAM_2 1.0    /* G invert */
#define PARAM_3 1.0    /* B invert */
#define PARAM_4 1.0    /* amount   */

vec4 hook() {
    vec4 src = textureLod(HOOKED_raw, HOOKED_pos, 0.0);
    vec3 inv = vec3(mix(src.r, 1.0 - src.r, PARAM_1),
                     mix(src.g, 1.0 - src.g, PARAM_2),
                     mix(src.b, 1.0 - src.b, PARAM_3));
    return vec4(mix(src.rgb, inv, PARAM_4), src.a);
}
