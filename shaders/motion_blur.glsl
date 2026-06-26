//!DESC motion blur — photographic linear motion blur
//!HOOK MAIN
//!BIND HOOKED

#define PARAM_1 0.4    /* length        */
#define PARAM_2 0.0    /* angle         */
#define PARAM_3 0.0    /* softness      */
#define PARAM_4 0.0    /* mix original  */

vec4 hook() {
    vec2 uv   = HOOKED_pos;
    vec2 size = HOOKED_size;

    // Total blur length in pixels — square-law gives fine control at low end.
    float dist  = PARAM_1 * PARAM_1 * 80.0;
    // Angle: 0..1 maps to 0..180°.  Blur is symmetric so 0° and 180° are
    // identical — the full range gives every possible axis direction.
    float angle = PARAM_2 * 3.14159;
    // Half-extent vector in UV space: samples spread ±(dist/2) px from centre.
    vec2  bv    = (dist * 0.5) * vec2(cos(angle), sin(angle)) / size;

    // 12 samples — 6 symmetric pairs, no centre sample (matches PS/Gimp
    // uniform averaging across the blur length).
    // PARAM_3 applies a cosine window: 0=flat/sharp, 1=feathered edges.
    // Weights are cos(|t|·π/2) pre-evaluated at each offset (constant-folded).
    float w1 = mix(1.0, 0.9914, PARAM_3);  // |t| = 1/12
    float w2 = mix(1.0, 0.9239, PARAM_3);  // |t| = 3/12
    float w3 = mix(1.0, 0.7934, PARAM_3);  // |t| = 5/12
    float w4 = mix(1.0, 0.6088, PARAM_3);  // |t| = 7/12
    float w5 = mix(1.0, 0.3827, PARAM_3);  // |t| = 9/12
    float w6 = mix(1.0, 0.1305, PARAM_3);  // |t| = 11/12
    float wt = 2.0 * (w1 + w2 + w3 + w4 + w5 + w6);

    vec3 col = vec3(0.0);
    col += (textureLod(HOOKED_raw, uv - bv * ( 1.0/12.0), 0.0).rgb
          + textureLod(HOOKED_raw, uv + bv * ( 1.0/12.0), 0.0).rgb) * w1;
    col += (textureLod(HOOKED_raw, uv - bv * ( 3.0/12.0), 0.0).rgb
          + textureLod(HOOKED_raw, uv + bv * ( 3.0/12.0), 0.0).rgb) * w2;
    col += (textureLod(HOOKED_raw, uv - bv * ( 5.0/12.0), 0.0).rgb
          + textureLod(HOOKED_raw, uv + bv * ( 5.0/12.0), 0.0).rgb) * w3;
    col += (textureLod(HOOKED_raw, uv - bv * ( 7.0/12.0), 0.0).rgb
          + textureLod(HOOKED_raw, uv + bv * ( 7.0/12.0), 0.0).rgb) * w4;
    col += (textureLod(HOOKED_raw, uv - bv * ( 9.0/12.0), 0.0).rgb
          + textureLod(HOOKED_raw, uv + bv * ( 9.0/12.0), 0.0).rgb) * w5;
    col += (textureLod(HOOKED_raw, uv - bv * (11.0/12.0), 0.0).rgb
          + textureLod(HOOKED_raw, uv + bv * (11.0/12.0), 0.0).rgb) * w6;
    col /= wt;

    vec3 orig = textureLod(HOOKED_raw, uv, 0.0).rgb;
    return vec4(mix(col, orig, PARAM_4), 1.0);
}
