//!DESC hue_cycle — posterise hue into bands, cycle the palette over time
//!HOOK MAIN
//!BIND HOOKED

// PARAM_1  bands      0 = 2 bands (bold)   1 = 16 bands (fine)
// PARAM_2  speed      0 = frozen poster     1 = fast rotating palette (~2 s/rev)
// PARAM_3  saturation 0 = preserve          1 = push bands toward vivid
// PARAM_4  intensity  0 = original image    1 = full posterised/cycled output
#define PARAM_1 0.25   /* bands      */
#define PARAM_2 0.2    /* speed      */
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
    float time = float(frame) / 60.0;

    vec4 curr = HOOKED_texOff(vec2(0.0));
    vec3 hsv  = rgb2hsv(curr.rgb);

    // Posterise: snap hue to N evenly-spaced bands.
    // PARAM_1 → 2–16 integer bands.
    float n     = 2.0 + floor(PARAM_1 * 14.0);
    float band  = floor(hsv.x * n);          // which band (0..n-1)
    float bh    = band / n;                  // band's base hue (0..1)

    // Cycle: rotate each band's hue over time.  All bands spin together so
    // the N-band structure and inter-band spacing are preserved, but the
    // entire palette rotates through the hue wheel.
    float speed  = PARAM_2 * 0.5;            // 0..0.5 revolutions/second
    float new_hue = fract(bh + time * speed);

    // Optional saturation push — makes each band read as a clean vivid colour.
    float new_sat = clamp(hsv.y + PARAM_3 * (1.0 - hsv.y), 0.0, 1.0);

    vec3 cycled = hsv2rgb(vec3(new_hue, new_sat, hsv.z));
    return vec4(mix(curr.rgb, cycled, PARAM_4), curr.a);
}
