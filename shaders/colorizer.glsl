//!DESC colorizer — luminance-band hue cycle colourise
//!HOOK MAIN
//!BIND HOOKED

// Ported from the original recur (cyberboy666/r_e_c_u_r) simple_colorizer.frag.

#define PARAM_1 0.3    /* speed  */
#define PARAM_2 0.3    /* bands  */
#define PARAM_3 0.5    /* spread */
#define PARAM_4 1.0    /* mix    */

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

vec4 hook() {
    vec4 src = textureLod(HOOKED_raw, HOOKED_pos, 0.0);
    float lum = dot(src.rgb, vec3(0.299, 0.587, 0.114));
    float t = float(frame) / 60.0 * mix(0.05, 1.0, PARAM_1);

    float levels = 2.0 + floor(PARAM_2 * 10.0);
    float band   = floor(lum * levels) / levels;
    float spread = mix(0.5, 4.0, PARAM_3);

    float hue = fract(band * spread + t);
    vec3 col  = hsv2rgb(vec3(hue, 0.75, 0.85));
    return vec4(mix(src.rgb, col, PARAM_4), src.a);
}
