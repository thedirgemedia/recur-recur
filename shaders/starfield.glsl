//!DESC starfield — warp star emitters (generative)
//!HOOK MAIN
//!BIND HOOKED

// ── Parameters (editable on the SHDR layer, key 1 cycles, 2/3 adjust) ────────
//
// Speed: 0.5 = frozen  |  > 0.5 = outward  |  < 0.5 = inward
// X:     0.5 = centre  |  0.0 = left edge  |  1.0 = right edge
// Y:     0.5 = centre  |  0.0 = top edge   |  1.0 = bottom edge
// Stars: 0.0 = 1 star  |  1.0 = 500 stars  (steps of 0.05 = 25 stars)
//
#define PARAM_1  0.65   /* em1 speed  */
#define PARAM_2  0.5    /* em1 X      */
#define PARAM_3  0.5    /* em1 Y      */
#define PARAM_4  0.35   /* em2 speed  */
#define PARAM_5  0.5    /* em2 X      */
#define PARAM_6  0.5    /* em2 Y      */
#define PARAM_7  0.5    /* trail      */
#define PARAM_8  0.5    /* palette    */
#define PARAM_9  0.05   /* em1 stars  */
#define PARAM_10 0.05   /* em2 stars  */

vec3 palette(float t, float sel) {
    vec3 a = vec3(0.5), b = vec3(0.5);
    vec3 c = vec3(1.0);
    vec3 d = mix(vec3(0.0, 0.33, 0.67), vec3(0.3, 0.2, 0.2), sel);
    return a + b * cos(6.28318 * (c * t + d));
}

float hash(float n) { return fract(sin(n) * 43758.5453); }

// Returns the additive colour contribution of one star emitter at pixel uv.
// origin:    emitter centre (aspect-corrected centred screen coords).
// speed:     advance rate/s (positive = outward, negative = inward, 0 = frozen).
// seed_off:  per-emitter seed offset so emitters have different star positions.
// hue_off:   palette phase offset so emitters have distinct hues.
// trail_len: trail as fraction of the star's radial distance (0 = no trail).
vec3 emitter(vec2 uv, vec2 origin, float speed, int n,
             float seed_off, float hue_off, float trail_len) {
    const float TWO_PI = 6.28318530;
    float fN    = float(n);
    bool  inward = speed < 0.0;

    vec2  ruv   = uv - origin;
    float theta = atan(ruv.y, ruv.x);

    // Map angle to nearest lane, then check 3 adjacent lanes to avoid
    // missing stars near a lane boundary.
    float lane_f = (theta / TWO_PI + 0.5) * fN;
    int   base   = int(floor(lane_f));

    vec3 col = vec3(0.0);

    for (int k = -1; k <= 1; k++) {
        int   li     = ((base + k) % n + n) % n;
        float lane_a = (float(li) / fN - 0.5) * TWO_PI;
        vec2  ldir   = vec2(cos(lane_a), sin(lane_a));

        float seed   = hash(float(li) * 17.3 + seed_off);
        float d      = fract(seed + float(frame) / 60.0 * speed);

        float max_r  = 2.4;
        float star_r = pow(d, 1.8) * max_r;
        vec2  spos   = origin + ldir * star_r;

        float star_sz = mix(0.003, 0.024, d);

        // Fade in at birth end so stars emerge gradually (no pop-in).
        float fade_in = inward ? smoothstep(1.0, 0.88, d)
                               : smoothstep(0.0, 0.12, d);

        float dist = length(uv - spos);
        float core = smoothstep(star_sz, 0.0, dist) * fade_in;

        // Trail extends opposite to direction of travel.
        float trail_b = 0.0;
        if (trail_len > 0.001 && star_r > 0.001) {
            vec2  tdir   = inward ? ldir : -ldir;
            vec2  to_uv  = uv - spos;
            float along  = dot(to_uv, tdir);
            float perp   = abs(dot(to_uv, vec2(-tdir.y, tdir.x)));
            float twidth = star_sz * 0.55;
            float tend   = star_r * trail_len;
            if (along > 0.0 && along < tend && perp < twidth) {
                trail_b = (1.0 - along / tend)
                        * smoothstep(twidth, 0.0, perp)
                        * fade_in * 0.7;
            }
        }

        float b = max(core, trail_b);
        if (b > 0.001)
            col += palette(d * 0.4 + hue_off, PARAM_8) * b;
    }

    return col;
}

vec4 hook() {
    vec2 asp = HOOKED_size / HOOKED_size.y;
    vec2 uv  = (HOOKED_pos - 0.5) * asp * 2.0;

    // Map 0–1 speed params: centre (0.5) = frozen, extremes = ±0.8/s
    float s1 = (PARAM_1 - 0.5) * 1.6;
    float s2 = (PARAM_4 - 0.5) * 1.6;

    // Map 0–1 position params to ±100 screen pixels from centre.
    // In aspect-corrected UV, 1 unit = HOOKED_size.y/2 pixels,
    // so 100px = 200/HOOKED_size.y UV units.
    float ps = 200.0 / HOOKED_size.y;
    vec2 o1 = vec2((PARAM_2 - 0.5) * ps, (PARAM_3 - 0.5) * ps);
    vec2 o2 = vec2((PARAM_5 - 0.5) * ps, (PARAM_6 - 0.5) * ps);

    float trail = mix(0.0, 0.65, PARAM_7);

    // Per-emitter star counts: PARAM 0–1 → 1–500 radial lanes.
    int n1 = max(1, int(PARAM_9  * 500.0 + 0.5));
    int n2 = max(1, int(PARAM_10 * 500.0 + 0.5));

    // Each emitter gets a distinct palette phase so they sit in different hues.
    vec3 col = emitter(uv, o1, s1, n1, 0.0,  0.0,  trail)
             + emitter(uv, o2, s2, n2, 7.3,  0.45, trail);

    return vec4(clamp(col, 0.0, 1.0), 1.0);
}
