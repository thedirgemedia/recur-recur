//!DESC mirror — split-axis mirror reflection
//!HOOK MAIN
//!BIND HOOKED

// Ported from the original recur (cyberboy666/r_e_c_u_r) mirror.frag.

#define PARAM_1 0.5    /* X split  */
#define PARAM_2 0.5    /* Y split  */
#define PARAM_3 1.0    /* X enable */
#define PARAM_4 0.0    /* Y enable */

vec4 hook() {
    vec2 pos = HOOKED_pos;
    if (PARAM_3 > 0.5 && pos.x > PARAM_1) pos.x = 1.0 - pos.x;
    if (PARAM_4 > 0.5 && pos.y < PARAM_2) pos.y = 1.0 - pos.y;
    return textureLod(HOOKED_raw, pos, 0.0);
}
