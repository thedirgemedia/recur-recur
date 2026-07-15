//!DESC feedback — video echo with zoom, rotate and spread
//!HOOK MAIN
//!BIND HOOKED

#define PARAM_1 0.92   /* decay      — 0=instant clear, 1=infinite hold */
#define PARAM_2 0.15   /* spread     — blur radius                       */
#define PARAM_3 0.5    /* zoom       — 0.5=none, <0.5=out, >0.5=in      */
#define PARAM_4 0.5    /* rotate     — 0.5=none                          */
#define PARAM_5 0.0    /* blend-mode — 0=echo, 1=bright, 2=screen,      */
                       /*              3=difference, 4=invert            */

vec4 hook() {
    vec2 uv = HOOKED_pos;

    // Apply zoom + rotate transform to the sampling UV (creates spiralling trails)
    vec2 c  = uv - 0.5;
    float zm = 1.0 - (PARAM_3 - 0.5) * 0.06;
    float an = (PARAM_4 - 0.5) * 0.04;
    float cs = cos(an), sn = sin(an);
    c = vec2(c.x * cs - c.y * sn, c.x * sn + c.y * cs) * zm;
    vec2 tuv = c + 0.5;

    // 8-sample radial blur around the transformed UV
    float sp = PARAM_2 * 0.04;
    float d  = sp * 0.7071;
    vec4 r = vec4(0.0);
    r += textureLod(HOOKED_raw, tuv + vec2( sp, 0.0), 0.0);
    r += textureLod(HOOKED_raw, tuv + vec2(-sp, 0.0), 0.0);
    r += textureLod(HOOKED_raw, tuv + vec2(0.0,  sp), 0.0);
    r += textureLod(HOOKED_raw, tuv + vec2(0.0, -sp), 0.0);
    r += textureLod(HOOKED_raw, tuv + vec2( d,   d),  0.0);
    r += textureLod(HOOKED_raw, tuv + vec2(-d,   d),  0.0);
    r += textureLod(HOOKED_raw, tuv + vec2( d,  -d),  0.0);
    r += textureLod(HOOKED_raw, tuv + vec2(-d,  -d),  0.0);
    r *= 0.125;

    vec3 echo = mix(textureLod(HOOKED_raw, tuv, 0.0).rgb, r.rgb, PARAM_2) * PARAM_1;
    vec3 src  = textureLod(HOOKED_raw, uv, 0.0).rgb;

    int bm = int(PARAM_5 * 4.99);
    vec3 col = echo;
    if      (bm == 1) col = clamp(echo * 1.5, 0.0, 1.0);
    else if (bm == 2) col = 1.0 - (1.0 - echo) * (1.0 - echo);
    else if (bm == 3) col = abs(echo * 2.0 - 1.0);
    else if (bm == 4) col = 1.0 - echo;

    return vec4(col, 1.0);
}
