//!DESC glitch — block displacement and channel corruption
//!HOOK MAIN
//!BIND HOOKED

// Slice size:      0 = 2 huge slices   |  1 = 128 thin slices  (square-law)
// Slice variation: 0 = uniform height  |  1 = highly randomised heights
#define PARAM_1 0.5    /* slice intensity  */
#define PARAM_2 0.5    /* update rate      */
#define PARAM_3 0.5    /* channel corrupt  */
#define PARAM_4 0.5    /* slice count      */
#define PARAM_5 0.0    /* slice variation  */

float rand(vec2 c) {
    return fract(sin(dot(c, vec2(12.9898, 78.233))) * 43758.5453);
}

vec4 hook() {
    vec2  uv   = HOOKED_pos;
    float time = float(frame) / 60.0;

    // Slice count: square-law 2–128; higher = thinner slices.
    // At 0.5 ≈ 34 slices; lower half gives large chunky slices.
    float blocks = max(2.0, PARAM_4 * PARAM_4 * 126.0 + 2.0);
    float rate   = mix(2.0, 30.0, PARAM_2);
    float t      = floor(time * rate);
    float t_slow = floor(t * 0.1);   // variation structure shifts at 1/10 glitch rate

    // Determine which glitch slice this pixel belongs to.
    // PARAM_5 = 0: uniform height (classic quantise).
    // PARAM_5 > 0: some base slots randomly merge with the slot(s) above them,
    // creating a mix of thin and tall slices.  Both probability and max merge
    // distance scale with PARAM_5 so the effect builds intuitively.
    float slot = floor(uv.y * blocks);
    float rr   = rand(vec2(slot, t_slow));
    if (PARAM_5 > 0.001 && rr < PARAM_5) {
        float max_dist = 1.0 + floor(PARAM_5 * 3.0);   // 1–4 slots
        float dist     = 1.0 + floor(rr / PARAM_5 * max_dist);
        slot = max(0.0, slot - dist);
    }
    float row = slot;

    float shift = (rand(vec2(row, t)) - 0.5);
    if (rand(vec2(row * 1.7, t)) < PARAM_1) {
        uv.x += shift * PARAM_1 * 0.5;
    }
    vec3 col = textureLod(HOOKED_raw, uv, 0.0).rgb;
    if (rand(vec2(row, t + 5.0)) < PARAM_3) {
        float o = (rand(vec2(row, t + 9.0)) - 0.5) * 0.05;
        col.r = textureLod(HOOKED_raw, uv + vec2(o, 0.0), 0.0).r;
        col.b = textureLod(HOOKED_raw, uv - vec2(o, 0.0), 0.0).b;
    }
    return vec4(col, 1.0);
}
