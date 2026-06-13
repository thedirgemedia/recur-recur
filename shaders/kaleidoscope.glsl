//!DESC kaleidoscope — radial mirror pattern (generative)
//!HOOK MAIN
//!BIND HOOKED

#define PARAM_1 0.5    /* sides         */
#define PARAM_2 0.5    /* spin          */
#define PARAM_3 0.5    /* zoom          */
#define PARAM_4 0.5    /* palette       */

vec3 palette(float t, float sel) {
    vec3 a = vec3(0.5), b = vec3(0.5);
    vec3 c = vec3(1.0);
    vec3 d = mix(vec3(0.0, 0.33, 0.67), vec3(0.3, 0.2, 0.2), sel);
    return a + b * cos(6.28318 * (c * t + d));
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

    return vec4(palette(pat + t * 0.1, PARAM_4), 1.0);
}
