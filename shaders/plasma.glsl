//!DESC plasma — sinusoidal interference (generative)
//!HOOK MAIN
//!BIND HOOKED

#define PARAM_1 0.5    /* speed   */
#define PARAM_2 0.5    /* scale   */
#define PARAM_3 0.5    /* warp    */
#define PARAM_4 0.5    /* palette */

vec3 palette(float t, float sel) {
    vec3 a = vec3(0.5), b = vec3(0.5);
    vec3 c = vec3(1.0);
    vec3 d = mix(vec3(0.0, 0.33, 0.67), vec3(0.3, 0.2, 0.2), sel);
    return a + b * cos(6.28318 * (c * t + d));
}

vec4 hook() {
    vec2 aspect = HOOKED_size / HOOKED_size.y;
    vec2 uv = (HOOKED_pos - 0.5) * aspect * 2.0;
    float t = float(frame) / 60.0 * mix(0.1, 2.5, PARAM_1);
    float scale = mix(2.0, 14.0, PARAM_2);
    uv += PARAM_3 * vec2(sin(uv.y * 3.0 + t), cos(uv.x * 3.0 + t));
    float v = sin(uv.x * scale + t)
            + sin(uv.y * scale + t)
            + sin((uv.x + uv.y) * scale * 0.7 + t)
            + sin(length(uv) * scale + t);
    v *= 0.25;
    return vec4(palette(v + t * 0.05, PARAM_4), 1.0);
}
