//!DESC rotate_zoom — spinning rotate + zoom warp
//!HOOK MAIN
//!BIND HOOKED

// Ported from the original recur (cyberboy666/r_e_c_u_r) rotate_fine.frag.

#define PARAM_1 0.5    /* spin     */
#define PARAM_2 0.5    /* centre X */
#define PARAM_3 0.5    /* centre Y */
#define PARAM_4 0.5    /* zoom     */

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

    if (pos.x < 0.0 || pos.x > 1.0 || pos.y < 0.0 || pos.y > 1.0)
        return vec4(0.0, 0.0, 0.0, 1.0);
    return textureLod(HOOKED_raw, pos, 0.0);
}
