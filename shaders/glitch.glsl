//!DESC glitch — block displacement and channel corruption
//!HOOK MAIN
//!BIND HOOKED

#define PARAM_1 0.5    /* slice intensity */
#define PARAM_2 0.5    /* update rate     */
#define PARAM_3 0.5    /* channel corrupt */
#define PARAM_4 0.5    /* block density   */

float rand(vec2 c) {
    return fract(sin(dot(c, vec2(12.9898, 78.233))) * 43758.5453);
}

vec4 hook() {
    vec2 uv = HOOKED_pos;
    float time = float(frame) / 60.0;
    float blocks = mix(8.0, 64.0, PARAM_4);
    float rate   = mix(2.0, 30.0, PARAM_2);
    float row    = floor(uv.y * blocks);
    float t      = floor(time * rate);
    float shift  = (rand(vec2(row, t)) - 0.5);

    if (rand(vec2(row * 1.7, t)) < PARAM_1) {
        uv.x += shift * PARAM_1 * 0.5;
    }
    vec3 col = textureLod(HOOKED_raw, uv, 0.0).rgb;
    if (rand(vec2(row, t + 5.0)) < PARAM_3) {
        float o = (rand(vec2(row, t + 9.0)) - 0.5) * 0.05;
        col.r = textureLod(HOOKED_raw, uv + vec2(o, 0.0), 0.0).r;
        col.b = textureLod(HOOKED_raw, uv - vec2(o, 0.0), 0.0).b;
    }
    return vec4(col, 1.0);
}
