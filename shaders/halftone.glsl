//!DESC halftone — RGB halftone screen, per-channel size and angle
//!HOOK MAIN
//!BIND HOOKED

#define PARAM_1 0.25   /* R size  */
#define PARAM_2 0.083  /* R angle */
#define PARAM_3 0.25   /* G size  */
#define PARAM_4 0.417  /* G angle */
#define PARAM_5 0.25   /* B size  */
#define PARAM_6 0.0    /* B angle */
#define PARAM_7 1.0    /* mix     */

vec2 rot2(vec2 p, float a) {
    float c = cos(a), s = sin(a);
    return vec2(c * p.x - s * p.y, s * p.x + c * p.y);
}

// One colour channel's dot screen. Rotate into the screen's own frame, quantise
// to a cell, sample the source at that cell's centre, and draw a dot whose
// radius tracks the sampled value — so light areas get small dots, dark ones
// large. Each channel gets its own angle, which is what stops the three screens
// forming moire against each other.
float screen_dot(vec2 px, vec2 size, float cell, float ang, int ch) {
    vec2  p  = rot2(px, ang) / cell;
    vec2  cc = rot2((floor(p) + 0.5) * cell, -ang) / size;
    vec3  s  = textureLod(HOOKED_raw, clamp(cc, 0.0, 1.0), 0.0).rgb;
    float v  = ch == 0 ? s.r : (ch == 1 ? s.g : s.b);
    return step(length(fract(p) - 0.5), v * 0.5);
}

vec4 hook() {
    vec2 size = HOOKED_size;
    vec2 px   = HOOKED_pos * size;
    vec3 col  = textureLod(HOOKED_raw, HOOKED_pos, 0.0).rgb;

    // Cell size 2..28px, square-law so the fine end keeps usable resolution.
    // Angles span 0..PI; the defaults are the traditional printing screen
    // angles (R=15, G=75, B=0 degrees) that minimise the rosette pattern.
    float r_sz = mix(2.0, 28.0, PARAM_1 * PARAM_1), r_a = PARAM_2 * 3.14159;
    float g_sz = mix(2.0, 28.0, PARAM_3 * PARAM_3), g_a = PARAM_4 * 3.14159;
    float b_sz = mix(2.0, 28.0, PARAM_5 * PARAM_5), b_a = PARAM_6 * 3.14159;

    vec3 dots = vec3(screen_dot(px, size, r_sz, r_a, 0),
                     screen_dot(px, size, g_sz, g_a, 1),
                     screen_dot(px, size, b_sz, b_a, 2));
    return vec4(mix(col, dots, PARAM_7), 1.0);
}
