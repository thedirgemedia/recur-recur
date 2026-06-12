//!DESC vhs — scanlines, chroma shift, noise, tracking jitter
//!HOOK MAIN
//!BIND HOOKED

#define PARAM_1 0.5    /* chroma shift   */
#define PARAM_2 0.5    /* scanline depth */
#define PARAM_3 0.5    /* noise          */
#define PARAM_4 0.5    /* tracking jitter*/

float rand(vec2 c) {
    return fract(sin(dot(c, vec2(12.9898, 78.233))) * 43758.5453);
}

vec4 hook() {
    vec2 uv = HOOKED_pos;
    float time = float(frame) / 60.0;
    float row = floor(uv.y * HOOKED_size.y * 0.8);
    float jitter = (rand(vec2(time * 2.0, row)) - 0.5) * (PARAM_4 * 0.04);
    uv.x += jitter;

    float ch = PARAM_1 * 0.015;
    float r = textureLod(HOOKED_raw, uv + vec2(ch, 0.0), 0.0).r;
    float g = textureLod(HOOKED_raw, uv, 0.0).g;
    float b = textureLod(HOOKED_raw, uv - vec2(ch, 0.0), 0.0).b;
    vec3 col = vec3(r, g, b);

    float scan = sin(uv.y * HOOKED_size.y * 0.5) * 0.5 + 0.5;
    col *= 1.0 - (scan * PARAM_2 * 0.7);

    float n = rand(uv + vec2(time, 0.0)) * (PARAM_3 * 0.4);
    col += n - (PARAM_3 * 0.2);

    float luma = dot(col, vec3(0.299, 0.587, 0.114));
    col = mix(col, vec3(luma), 0.15);
    col *= vec3(1.05, 1.0, 0.95);
    return vec4(col, 1.0);
}
