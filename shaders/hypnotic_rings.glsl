//!DESC hypnotic_rings — three orbiting ring-wave sources (generative)
//!HOOK MAIN
//!BIND HOOKED

// Ported from the original recur (cyberboy666/r_e_c_u_r) hypnotic_rings.frag.

#define PARAM_1 0.5    /* speed */
#define PARAM_2 0.5    /* freq  */
#define PARAM_3 0.5    /* orbit */
#define PARAM_4 0.5    /* hue   */
#define PARAM_5 0.5    /* zoom  */

vec3 hueShift(vec3 col, float h) {
    const vec3 k = vec3(0.57735);
    float c = cos(h), s = sin(h);
    return col * c + cross(k, col) * s + k * dot(k, col) * (1.0 - c);
}

float ring(vec2 p, vec2 src, float freq, float phase) {
    float r = length(p - src);
    return 0.5 + 0.5 * sign(sin(freq * r - phase));
}

vec4 hook() {
    vec2 aspect = HOOKED_size / HOOKED_size.y;
    vec2 uv = (HOOKED_pos - 0.5) * aspect * 2.0;
    uv *= mix(1.7, 0.3, PARAM_5);
    float t     = float(frame) / 60.0 * mix(0.1, 1.5, PARAM_1);
    float orbit = mix(0.1, 0.9, PARAM_3);
    float freq  = mix(4.0, 30.0, PARAM_2);

    vec2 s1 = orbit * vec2(cos(t), sin(t));
    vec2 s2 = orbit * vec2(sin(t), cos(t));
    vec2 s3 = vec2(0.0);

    vec3 col = vec3(0.0);
    col.r = ring(uv, s1, freq, t * 1.0 + 2.0);
    col.b = ring(uv, s2, freq, t * 1.2);
    col.g = ring(uv, s3, freq, t * 1.1 + 0.5);

    col = hueShift(col, PARAM_4 * 6.28318);
    return vec4(col, 1.0);
}
