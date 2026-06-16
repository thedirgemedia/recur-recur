//!DESC hsv_shift — hue / saturation / value offset
//!HOOK MAIN
//!BIND HOOKED

// Ported from the original recur (cyberboy666/r_e_c_u_r) hsv_control.frag.

#define PARAM_1 0.5    /* hue    */
#define PARAM_2 0.5    /* sat    */
#define PARAM_3 0.5    /* value  */
#define PARAM_4 1.0    /* amount */

vec3 rgb2hsv(vec3 c) {
    vec4 K = vec4(0.0, -1.0/3.0, 2.0/3.0, -1.0);
    vec4 p = mix(vec4(c.bg, K.wz), vec4(c.gb, K.xy), step(c.b, c.g));
    vec4 q = mix(vec4(p.xyw, c.r), vec4(c.r, p.yzx), step(p.x, c.r));
    float d = q.x - min(q.w, q.y);
    return vec3(abs(q.z + (q.w - q.y) / (6.0 * d + 1e-10)), d / (q.x + 1e-10), q.x);
}

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

vec4 hook() {
    vec4 src = textureLod(HOOKED_raw, HOOKED_pos, 0.0);
    vec3 hsv = rgb2hsv(src.rgb);
    hsv.x = fract(hsv.x + (PARAM_1 - 0.5));
    hsv.y = clamp(hsv.y + (PARAM_2 - 0.5), 0.0, 1.0);
    hsv.z = clamp(hsv.z + (PARAM_3 - 0.5), 0.0, 1.0);
    vec3 shifted = hsv2rgb(hsv);
    return vec4(mix(src.rgb, shifted, PARAM_4), src.a);
}
