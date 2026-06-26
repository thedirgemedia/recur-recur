//!DESC motion blur — directional streak with tail fade
//!HOOK MAIN
//!BIND HOOKED

#define PARAM_1 0.3    /* strength      */
#define PARAM_2 0.0    /* direction     */
#define PARAM_3 0.5    /* tail fade     */
#define PARAM_4 0.0    /* mix original  */

vec4 hook() {
    vec2 uv   = HOOKED_pos;
    vec2 size = HOOKED_size;

    // Blur distance in pixels — square-law gives fine control at low end.
    float dist  = PARAM_1 * PARAM_1 * 80.0;
    // Direction: 0..1 wraps a full circle. 0=right, 0.25=down, 0.5=left, 0.75=up.
    float angle = PARAM_2 * 6.28318;
    // Blur vector in UV space — samples trail opposite to the motion direction.
    vec2  bv    = dist * vec2(cos(angle), sin(angle)) / size;

    // Per-step weight decay: PARAM_3=0 → uniform (classic motion blur),
    // PARAM_3=1 → strong exponential falloff (comet tail / wind streak).
    float d  = mix(1.0, 0.35, PARAM_3);
    float w0 = 1.0;
    float w1 = w0 * d;
    float w2 = w1 * d;
    float w3 = w2 * d;
    float w4 = w3 * d;
    float w5 = w4 * d;
    float w6 = w5 * d;
    float w7 = w6 * d;
    float wt = w0 + w1 + w2 + w3 + w4 + w5 + w6 + w7;

    vec3 col = vec3(0.0);
    col += textureLod(HOOKED_raw, uv - bv * (0.0/7.0), 0.0).rgb * w0;
    col += textureLod(HOOKED_raw, uv - bv * (1.0/7.0), 0.0).rgb * w1;
    col += textureLod(HOOKED_raw, uv - bv * (2.0/7.0), 0.0).rgb * w2;
    col += textureLod(HOOKED_raw, uv - bv * (3.0/7.0), 0.0).rgb * w3;
    col += textureLod(HOOKED_raw, uv - bv * (4.0/7.0), 0.0).rgb * w4;
    col += textureLod(HOOKED_raw, uv - bv * (5.0/7.0), 0.0).rgb * w5;
    col += textureLod(HOOKED_raw, uv - bv * (6.0/7.0), 0.0).rgb * w6;
    col += textureLod(HOOKED_raw, uv - bv * (7.0/7.0), 0.0).rgb * w7;
    col /= wt;

    vec3 orig = textureLod(HOOKED_raw, uv, 0.0).rgb;
    return vec4(mix(col, orig, PARAM_4), 1.0);
}
