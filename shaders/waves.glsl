//!DESC waves — overlapping radial/linear sine fields (generative)
//!HOOK MAIN
//!BIND HOOKED

#define PARAM_1 0.5    /* frequency */
#define PARAM_2 0.5    /* speed     */
#define PARAM_3 0.5    /* count     */
#define PARAM_4 0.5    /* hue       */

vec3 hsv(float h, float s, float v) {
    vec3 k = vec3(5.0, 3.0, 1.0);
    vec3 p = abs(fract(h + k/6.0) * 6.0 - 3.0);
    return v * mix(vec3(1.0), clamp(p - 1.0, 0.0, 1.0), s);
}

vec4 hook() {
    vec2 uv = (HOOKED_pos - 0.5) * (HOOKED_size / HOOKED_size.y) * 2.0;
    float t = float(frame) / 60.0 * mix(0.2, 3.0, PARAM_2);
    float freq = mix(2.0, 20.0, PARAM_1);
    int n = int(mix(2.0, 8.0, PARAM_3));
    float sum = 0.0;
    for (int i = 0; i < 8; ++i) {
        if (i >= n) break;
        float a = float(i) * 1.21;
        vec2 dir = vec2(cos(a), sin(a));
        sum += sin(dot(uv, dir) * freq + t + float(i));
    }
    sum /= float(n);
    sum = sum * 0.5 + 0.5;
    return vec4(hsv(sum + PARAM_4, 0.8, 0.9), 1.0);
}
