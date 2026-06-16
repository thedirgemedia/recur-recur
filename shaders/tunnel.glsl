//!DESC tunnel — animated polar tunnel (generative)
//!HOOK MAIN
//!BIND HOOKED

#define PARAM_1 0.5    /* speed    */
#define PARAM_2 0.5    /* segments */
#define PARAM_3 0.5    /* twist    */
#define PARAM_4 0.5    /* palette  */
#define PARAM_5 0.5    /* zoom     */

vec3 palette(float t, float sel) {
    vec3 a = vec3(0.5), b = vec3(0.5);
    vec3 c = vec3(1.0);
    vec3 d = mix(vec3(0.0, 0.33, 0.67), vec3(0.3, 0.2, 0.2), sel);
    return a + b * cos(6.28318 * (c * t + d));
}

vec4 hook() {
    vec2 uv = (HOOKED_pos - 0.5) * (HOOKED_size / HOOKED_size.y) * 2.0;
    uv *= mix(1.7, 0.3, PARAM_5);
    float r = length(uv);
    float a = atan(uv.y, uv.x);
    float t = float(frame) / 60.0 * mix(0.3, 3.0, PARAM_1);
    float segs = mix(4.0, 24.0, PARAM_2);
    float twist = (PARAM_3 - 0.5) * 4.0;

    // polar coordinate texture
    float u = 0.6 / r + t;
    float v = a * segs / 6.28318 + twist * r + t * 0.3;
    float pattern = sin(u * 4.0) * sin(v * 4.0);
    float falloff = smoothstep(0.0, 0.3, r);

    vec3 col = palette(pattern * 0.5 + 0.5, PARAM_4);
    return vec4(col * falloff, 1.0);
}
