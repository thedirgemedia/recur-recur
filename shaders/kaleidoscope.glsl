//!DESC kaleidoscope — radial mirror pattern (generative)
//!HOOK MAIN
//!BIND HOOKED

#define PARAM_1 0.5    /* sides         */
#define PARAM_2 0.5    /* spin          */
#define PARAM_3 0.5    /* zoom          */
#define PARAM_4 0.5    /* color shift   */

vec3 hsv(float h, float s, float v) {
    vec3 k = vec3(5.0, 3.0, 1.0);
    vec3 p = abs(fract(h + k/6.0) * 6.0 - 3.0);
    return v * mix(vec3(1.0), clamp(p - 1.0, 0.0, 1.0), s);
}

vec4 hook() {
    vec2 uv = (HOOKED_pos - 0.5) * (HOOKED_size / HOOKED_size.y) * 2.0;
    float r = length(uv);
    float a = atan(uv.y, uv.x);
    float t = float(frame) / 60.0;

    float sides = mix(3.0, 12.0, PARAM_1);
    float spin = t * (PARAM_2 - 0.5) * 2.0;
    a = mod(a + spin, 6.28318 / sides);
    a = abs(a - (6.28318 / (2.0 * sides)));

    float zoom = mix(2.0, 0.4, PARAM_3);
    vec2 puv = vec2(cos(a), sin(a)) * r * zoom;
    float pat = sin(puv.x * 5.0 + t) + sin(puv.y * 5.0 - t);
    pat = pat * 0.25 + 0.5;

    return vec4(hsv(pat + PARAM_4 + t * 0.1, 0.85, 0.95), 1.0);
}
