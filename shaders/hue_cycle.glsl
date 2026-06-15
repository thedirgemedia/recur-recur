//!DESC hue_cycle — animated per-pixel hue rotation
//!HOOK MAIN
//!BIND HOOKED

// PARAM_1  speed      0 = very slow (~1 rev / 3 min)   1 = fast (~1 rev / 1.5 s)
// PARAM_2  spatial    0 = uniform shift   1 = radial rainbow spread from centre
// PARAM_3  saturation 0 = preserve original   1 = push toward fully vivid
// PARAM_4  intensity  0 = pass-through   1 = full hue-shifted output
#define PARAM_1 0.25   /* speed      */
#define PARAM_2 0.0    /* spatial    */
#define PARAM_3 0.5    /* saturation */
#define PARAM_4 1.0    /* intensity  */

vec3 rgb2hsv(vec3 c) {
    vec4 K = vec4(0.0, -1.0/3.0, 2.0/3.0, -1.0);
    vec4 p = mix(vec4(c.bg, K.wz), vec4(c.gb, K.xy), step(c.b, c.g));
    vec4 q = mix(vec4(p.xyw, c.r), vec4(c.r, p.yzx), step(p.x, c.r));
    float d = q.x - min(q.w, q.y);
    return vec3(abs(q.z + (q.w - q.y) / (6.0*d + 1e-10)),
                d / (q.x + 1e-10), q.x);
}

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

vec4 hook() {
    vec2  uv   = HOOKED_pos;
    float time = float(frame) / 60.0;

    // Square-law speed: fine control at slow end, fast at high end.
    // Range: ~0.006 rev/s (3 min/rev) → ~0.67 rev/s (1.5 s/rev)
    float speed = PARAM_1 * PARAM_1 * 0.67;

    // Global time phase + optional radial spatial offset so different
    // positions sit at different points in the hue wheel simultaneously.
    float spatial = length(uv - 0.5) * PARAM_2 * 2.0;
    float phase   = fract(time * speed + spatial);

    vec4 curr = HOOKED_texOff(vec2(0.0));
    vec3 hsv  = rgb2hsv(curr.rgb);

    hsv.x = fract(hsv.x + phase);
    hsv.y = clamp(hsv.y + PARAM_3 * (1.0 - hsv.y), 0.0, 1.0);

    vec3 shifted = hsv2rgb(hsv);
    return vec4(mix(curr.rgb, shifted, PARAM_4), curr.a);
}
