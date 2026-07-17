//!DESC gamma-ray — scintillator particle flicker (generative)
//!HOOK MAIN
//!BIND HOOKED

#define PARAM_1 0.15   /* density    */
#define PARAM_2 0.8    /* brightness */
#define PARAM_3 0.1    /* size       */
#define PARAM_4 0.0    /* colour     */

float hash(vec2 p)  { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
float hash3(vec3 p) { return fract(sin(dot(p, vec3(127.1, 311.7, 74.7))) * 43758.5453); }

vec4 hook() {
    vec2  uv = HOOKED_pos * HOOKED_size;
    float t  = float(frame);

    // Particle size in pixels: 1 = single pixel, up to 6.
    float sz   = max(1.0, floor(mix(1.0, 6.0, PARAM_3)));
    vec2  cell = floor(uv / sz);

    // Re-randomised every frame with no temporal coherence — the flicker *is*
    // the effect, so each cell rolls fresh rather than persisting.
    float r       = hash3(vec3(cell, t));
    float density = mix(0.0, 0.98, PARAM_1 * PARAM_1);
    float hit     = step(1.0 - density, r);

    float bright = mix(0.6, 1.0, hash(cell + t * 0.1)) * mix(0.5, 1.0, PARAM_2);

    // colour 0 = white; above that the hue sweeps the phosphor/scintillator
    // range (green ≈ 0.33, blue ≈ 0.5, red ≈ 0.67)
    float hue = PARAM_4 * 6.28318;
    float sat = step(0.01, PARAM_4);
    vec3  col = bright * mix(vec3(1.0),
                             vec3(0.5 + 0.5 * cos(hue),
                                  0.5 + 0.5 * cos(hue + 2.094),
                                  0.5 + 0.5 * cos(hue + 4.189)), sat);
    return vec4(col * hit, 1.0);
}
