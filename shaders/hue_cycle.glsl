//!DESC hue_cycle — select pixels by hue and shift their colour
//!HOOK MAIN
//!BIND HOOKED

// PARAM_1  target hue   0=red  0.17=yellow  0.33=green  0.5=cyan  0.67=blue  0.83=magenta
// PARAM_2  tolerance    0=razor-thin selection   1=entire colour wheel selected
// PARAM_3  hue shift    0=no shift   0.5=opposite colour   1=full rotation (no change)
// PARAM_4  intensity    0=passthrough   1=full shift applied to selected pixels
#define PARAM_1 0.0    /* target hue */
#define PARAM_2 0.15   /* tolerance  */
#define PARAM_3 0.5    /* hue shift  */
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
    vec4 curr = HOOKED_texOff(vec2(0.0));
    vec3 hsv  = rgb2hsv(curr.rgb);

    // Circular hue distance — wraps correctly at 0/1 boundary
    float diff     = abs(hsv.x - PARAM_1);
    float hue_dist = min(diff, 1.0 - diff);

    // Selection mask: full weight inside inner radius, fades to zero at outer.
    // Saturation gate prevents near-grey/white/black pixels being selected
    // (they have no meaningful hue).
    float tol    = max(0.01, PARAM_2 * 0.5);
    float select = 1.0 - smoothstep(tol * 0.5, tol, hue_dist);
    select      *= smoothstep(0.05, 0.25, hsv.y);

    // Shift the hue of selected pixels, leave everything else unchanged
    vec3 shifted     = hsv;
    shifted.x        = fract(hsv.x + PARAM_3);
    vec3 shifted_rgb = hsv2rgb(shifted);

    return vec4(mix(curr.rgb, shifted_rgb, select * PARAM_4), curr.a);
}
