//!DESC posterize — quantised luminance bands with duotone tint
//!HOOK MAIN
//!BIND HOOKED

// Ported from the original recur (cyberboy666/r_e_c_u_r) gray_divisions.frag.

#define PARAM_1 0.3    /* levels   */
#define PARAM_2 1.0    /* mix      */
#define PARAM_3 0.5    /* contrast */
#define PARAM_4 0.5    /* tint hue */

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

vec4 hook() {
    vec4 src = textureLod(HOOKED_raw, HOOKED_pos, 0.0);
    float lum = dot(src.rgb, vec3(0.299, 0.587, 0.114));
    lum = clamp((lum - 0.5) * (1.0 + PARAM_3 * 3.0) + 0.5, 0.0, 1.0);

    float levels = 2.0 + floor(PARAM_1 * 14.0);
    float band   = floor(lum * levels) / levels;

    vec3 tint = hsv2rgb(vec3(PARAM_4, 0.6, band));
    return vec4(mix(src.rgb, tint, PARAM_2), src.a);
}
