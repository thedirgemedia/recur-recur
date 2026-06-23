//!DESC feedback — video echo trails
//!HOOK MAIN
//!BIND HOOKED

#define PARAM_1 0.5    /* echo amount */
#define PARAM_2 0.5    /* spread      */
#define PARAM_3 0.2    /* blend mode  */
#define PARAM_4 0.3    /* trail depth */

// Blend modes for the echo layer against source.
// PARAM_3 selects in seven equal zones across [0, 1]:
//   0.00 – 0.14  addition    bright halo, can wash out (original behaviour)
//   0.14 – 0.29  screen      soft-clips to white instead of blowing out
//   0.29 – 0.43  difference  psychedelic inverted trails
//   0.43 – 0.57  multiply    dark ghost trails, darkens with echo
//   0.57 – 0.71  overlay     contrast-boosting trails
//   0.71 – 0.86  subtract    dark erasure trails, frames eat each other
//   0.86 – 1.00  phoenix     1-|a-b|, bright where similar, dark where different
vec3 echo_blend(vec3 src, vec3 echo) {
    int m = int(PARAM_3 * 6.999);
    if (m == 1) return 1.0 - (1.0 - src) * (1.0 - echo);            // screen
    if (m == 2) return abs(src - echo);                               // difference
    if (m == 3) return clamp(src * echo * 2.0, 0.0, 1.0);            // multiply
    if (m == 4) {                                                      // overlay
        vec3 lo = 2.0 * src * echo;
        vec3 hi = 1.0 - 2.0 * (1.0 - src) * (1.0 - echo);
        return mix(lo, hi, step(vec3(0.5), src));
    }
    if (m == 5) return max(src - echo, vec3(0.0));                    // subtract
    if (m == 6) return 1.0 - abs(src - echo);                        // phoenix
    return clamp(src + echo, 0.0, 1.0);                               // addition (default)
}

vec4 hook() {
    vec2  uv  = HOOKED_pos;
    vec4  src = textureLod(HOOKED_raw, uv, 0.0);
    float sp  = PARAM_2 * 0.07;
    float d   = sp * 0.7071;     // sp / sqrt(2) — diagonal distance

    // Ring 1: 8 samples on a circle of radius sp (cross + diagonals)
    vec4 r1 = vec4(0.0);
    r1 += textureLod(HOOKED_raw, uv + vec2( sp,  0.0), 0.0);
    r1 += textureLod(HOOKED_raw, uv + vec2(-sp,  0.0), 0.0);
    r1 += textureLod(HOOKED_raw, uv + vec2(0.0,   sp), 0.0);
    r1 += textureLod(HOOKED_raw, uv + vec2(0.0,  -sp), 0.0);
    r1 += textureLod(HOOKED_raw, uv + vec2( d,    d),  0.0);
    r1 += textureLod(HOOKED_raw, uv + vec2(-d,    d),  0.0);
    r1 += textureLod(HOOKED_raw, uv + vec2( d,   -d),  0.0);
    r1 += textureLod(HOOKED_raw, uv + vec2(-d,   -d),  0.0);
    r1 *= 0.125;

    // Ring 2: 8 samples at 2× spread, rotated 22.5° so it interleaves ring 1
    float sp2 = sp * 2.0;
    float d2  = sp2 * 0.7071;
    float c22 = 0.9239;   // cos(22.5°)
    float s22 = 0.3827;   // sin(22.5°)
    vec4 r2 = vec4(0.0);
    r2 += textureLod(HOOKED_raw, uv + sp2 * vec2( c22,  s22), 0.0);
    r2 += textureLod(HOOKED_raw, uv + sp2 * vec2(-s22,  c22), 0.0);
    r2 += textureLod(HOOKED_raw, uv + sp2 * vec2(-c22,  s22), 0.0);
    r2 += textureLod(HOOKED_raw, uv + sp2 * vec2(-c22, -s22), 0.0);
    r2 += textureLod(HOOKED_raw, uv + sp2 * vec2(-s22, -c22), 0.0);
    r2 += textureLod(HOOKED_raw, uv + sp2 * vec2( s22, -c22), 0.0);
    r2 += textureLod(HOOKED_raw, uv + sp2 * vec2( c22, -s22), 0.0);
    r2 += textureLod(HOOKED_raw, uv + sp2 * vec2( s22,  c22), 0.0);
    r2 *= 0.125;

    // Ring 3: 8 samples at 3× spread — contributes only when PARAM_4 > 0.6
    float sp3 = sp * 3.0;
    vec4 r3 = vec4(0.0);
    float r3w = clamp((PARAM_4 - 0.6) / 0.4, 0.0, 1.0);
    if (r3w > 0.0) {
        r3 += textureLod(HOOKED_raw, uv + vec2( sp3,  0.0), 0.0);
        r3 += textureLod(HOOKED_raw, uv + vec2(-sp3,  0.0), 0.0);
        r3 += textureLod(HOOKED_raw, uv + vec2(0.0,   sp3), 0.0);
        r3 += textureLod(HOOKED_raw, uv + vec2(0.0,  -sp3), 0.0);
        r3 += textureLod(HOOKED_raw, uv + vec2( d2,   d2),  0.0);
        r3 += textureLod(HOOKED_raw, uv + vec2(-d2,   d2),  0.0);
        r3 += textureLod(HOOKED_raw, uv + vec2( d2,  -d2),  0.0);
        r3 += textureLod(HOOKED_raw, uv + vec2(-d2,  -d2),  0.0);
        r3 *= 0.125;
    }

    // Accumulate rings — ring 2 fades in from PARAM_4=0→0.5, ring 3 from 0.6→1.0
    float r2w = clamp(PARAM_4 / 0.5, 0.0, 1.0);
    // Normalise so total weight is always 1.0 (PARAM_1 controls overall amount)
    float total = 1.0 + r2w + r3w;
    vec4  echo  = (r1 + r2 * r2w + r3 * r3w) / total;

    vec3 blended = echo_blend(src.rgb, echo.rgb * PARAM_1);
    return vec4(blended, src.a);
}
