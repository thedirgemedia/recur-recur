//!DESC voronoi — cellular pattern (generative)
//!HOOK MAIN
//!BIND HOOKED

#define PARAM_1 0.5    /* cell density */
#define PARAM_2 0.5    /* speed        */
#define PARAM_3 0.5    /* edge sharpness */
#define PARAM_4 0.5    /* palette      */

vec2 rand2(vec2 p) {
    p = vec2(dot(p, vec2(127.1, 311.7)),
             dot(p, vec2(269.5, 183.3)));
    return fract(sin(p) * 43758.5453);
}

vec3 palette(float t, float sel) {
    vec3 d = mix(vec3(0.0, 0.33, 0.67), vec3(0.4, 0.1, 0.6), sel);
    return 0.5 + 0.5 * cos(6.28318 * (t + d));
}

vec4 hook() {
    vec2 uv = HOOKED_pos * (HOOKED_size / HOOKED_size.y);
    float scale = mix(3.0, 18.0, PARAM_1);
    uv *= scale;
    float t = float(frame) / 60.0 * mix(0.1, 1.5, PARAM_2);
    vec2 i_uv = floor(uv);
    vec2 f_uv = fract(uv);
    float md = 8.0;
    float md2 = 8.0;
    vec2 mp = vec2(0.0);
    for (int y = -1; y <= 1; ++y) {
        for (int x = -1; x <= 1; ++x) {
            vec2 g = vec2(float(x), float(y));
            vec2 o = rand2(i_uv + g);
            o = 0.5 + 0.5 * sin(t + 6.28318 * o);
            vec2 r = g + o - f_uv;
            float d = dot(r, r);
            if (d < md) { md2 = md; md = d; mp = i_uv + g; }
            else if (d < md2) { md2 = d; }
        }
    }
    float edge = sqrt(md2) - sqrt(md);
    float edgef = smoothstep(0.0, mix(0.2, 0.02, PARAM_3), edge);
    float cell = dot(rand2(mp), vec2(0.5, 0.5));
    vec3 col = palette(cell + t * 0.1, PARAM_4) * edgef;
    return vec4(col, 1.0);
}
