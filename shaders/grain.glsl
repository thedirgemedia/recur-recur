//!DESC grain — fine scanline noise (analog moire texture)
//!HOOK MAIN
//!BIND HOOKED

#define PARAM_1 0.5    /* scanline depth  */
#define PARAM_2 0.5    /* noise amplitude */
#define PARAM_3 0.5    /* luma crush      */
#define PARAM_4 0.5    /* speed           */

float rand(vec2 c) {
    return fract(sin(dot(c, vec2(12.9898, 78.233))) * 43758.5453);
}

vec4 hook() {
    vec2 uv  = HOOKED_pos;
    float time = float(frame) / 60.0 * (0.5 + PARAM_4);

    vec3 col = textureLod(HOOKED_raw, uv, 0.0).rgb;

    // High-frequency sine at 1.5× pixel density produces a fine moire/grain
    // texture rather than distinct stripes — the original VHS scanline formula.
    float scan = sin(uv.y * HOOKED_size.y * 1.5) * 0.5 + 0.5;
    col *= 1.0 - (scan * PARAM_1 * 0.8);

    float n = rand(uv + vec2(time, 0.0)) * (PARAM_2 * 0.5);
    col += n - (PARAM_2 * 0.25);

    // Slight luma crush — pulls blacks down for a dirtier analog feel
    col = mix(col, col * col, PARAM_3 * 0.4);

    return vec4(clamp(col, 0.0, 1.0), 1.0);
}
