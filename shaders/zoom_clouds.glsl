//!DESC zoom_clouds — drifting cloud-like warp field (generative)
//!HOOK MAIN
//!BIND HOOKED

// Ported from the original recur (cyberboy666/r_e_c_u_r) zoom_clouds.frag.

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
    float speed = mix(0.2, 2.0, PARAM_1);
    float t   = float(frame) / 60.0 * speed;
    float mt  = t * speed;
    float det  = mix(1.0, 6.0, PARAM_2);
    float warp = mix(0.05, 0.5, PARAM_3);

    float denom = warp + abs(cos(mt / 5.0));
    float v = sin(0.7 * det * uv.x - cos(speed * mt)) + cos(0.3 * uv.x / denom)
            + sin(0.3 * uv.y - cos(mt / 5.0))          + cos(1.5 * uv.y / denom);
    v *= 0.25;

    vec3 col = vec3(v, 0.0, sin(mt)) * 0.5 + 0.5;
    col = hueShift(col, PARAM_4 * 6.28318);
    return vec4(clamp(col, 0.0, 1.0), 1.0);
}
