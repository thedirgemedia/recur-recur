//!DESC wobble — sine ripple displacement
//!HOOK MAIN
//!BIND HOOKED

// Ported from the original recur (cyberboy666/r_e_c_u_r) wobble.frag.

#define PARAM_1 0.5    /* X amp  */
#define PARAM_2 0.5    /* X freq */
#define PARAM_3 0.5    /* Y amp  */
#define PARAM_4 0.5    /* Y freq */

vec4 hook() {
    vec2  pos   = HOOKED_pos;
    float t     = float(frame) / 60.0;
    float ampx  = PARAM_1 * 0.1;
    float freqx = mix(10.0, 120.0, PARAM_2);
    float ampy  = PARAM_3 * 0.1;
    float freqy = mix(10.0, 120.0, PARAM_4);

    pos.x += ampx * sin(pos.y * freqy + t);
    pos.y += ampy * sin(pos.x * freqx + t);
    pos = fract(pos);

    return textureLod(HOOKED_raw, pos, 0.0);
}
