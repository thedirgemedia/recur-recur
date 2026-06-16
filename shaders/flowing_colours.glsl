//!DESC flowing_colours — layered sine interference field (generative)
//!HOOK MAIN
//!BIND HOOKED

// Ported from the original recur (cyberboy666/r_e_c_u_r) flowing_colours.frag.

#define PARAM_1 0.5    /* speed  */
#define PARAM_2 0.5    /* detail */
#define PARAM_3 0.5    /* warp   */
#define PARAM_4 0.5    /* hue    */
#define PARAM_5 0.5    /* zoom   */

vec3 hueShift(vec3 col, float h) {
    const vec3 k = vec3(0.57735);
    float c = cos(h), s = sin(h);
    return col * c + cross(k, col) * s + k * dot(k, col) * (1.0 - c);
}

vec4 hook() {
    vec2 aspect = HOOKED_size / HOOKED_size.y;
    vec2 uv = (HOOKED_pos - 0.5) * aspect * 2.0;
    uv *= mix(1.7, 0.3, PARAM_5);
    float t   = float(frame) / 60.0 * mix(0.3, 3.0, PARAM_1);
    float det = mix(4.0, 24.0, PARAM_2);
    float w   = mix(0.3, 2.0, PARAM_3);

    float v = 0.0;
    v += sin(uv.x * cos(t / 15.0) * det)       + cos(uv.y * cos(t / 15.0) * det * 0.3) * w;
    v += sin(uv.y * sin(t / 10.0) * det * 1.5) + cos(uv.x * sin(t / 25.0) * det * 1.5) * w;
    v += sin(uv.x * sin(t / 5.0)  * det * 0.4) + sin(uv.y * sin(t / 35.0) * det * 2.5) * w;
    v *= sin(t / 10.0) * 0.8;

    vec3 col = vec3(v, v * 0.5 * sin(0.02 * t), sin(v + t / 3.0) * 0.75) * 0.5 + 0.5;
    col = hueShift(col, PARAM_4 * 6.28318);
    return vec4(clamp(col, 0.0, 1.0), 1.0);
}
