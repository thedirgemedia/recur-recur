//!DESC zoom — radial zoom about a point
//!HOOK MAIN
//!BIND HOOKED

// Ported from the original recur (cyberboy666/r_e_c_u_r) zoom.frag.

#define PARAM_1 0.5    /* zoom     */
#define PARAM_2 0.5    /* centre X */
#define PARAM_3 0.5    /* centre Y */
#define PARAM_4 0.0    /* pulse    */

vec4 hook() {
    vec2  centre = vec2(PARAM_2, PARAM_3);
    float base   = mix(0.2, 3.0, PARAM_1);
    float pulse  = 1.0 + PARAM_4 * 0.3 * sin(float(frame) / 30.0);
    float zoom   = base * pulse;

    vec2 pos = (HOOKED_pos - centre) / zoom + centre;
    if (pos.x < 0.0 || pos.x > 1.0 || pos.y < 0.0 || pos.y > 1.0)
        return vec4(0.0, 0.0, 0.0, 1.0);
    return textureLod(HOOKED_raw, pos, 0.0);
}
