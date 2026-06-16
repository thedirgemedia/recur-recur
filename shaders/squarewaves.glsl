//!DESC squarewaves — warped square-wave interference bands (generative)
//!HOOK MAIN
//!BIND HOOKED

// Ported from the original recur (cyberboy666/r_e_c_u_r) squarewaves.frag.

#define PARAM_1 0.5    /* amplitude */
#define PARAM_2 0.5    /* warp      */
#define PARAM_3 0.5    /* frequency */
#define PARAM_4 0.5    /* hue       */
#define PARAM_5 0.5    /* zoom      */

vec3 hueShift(vec3 col, float h) {
    const vec3 k = vec3(0.57735);
    float c = cos(h), s = sin(h);
    return col * c + cross(k, col) * s + k * dot(k, col) * (1.0 - c);
}

vec4 hook() {
    vec2 aspect = HOOKED_size / HOOKED_size.y;
    vec2 uv = (HOOKED_pos - 0.5) * aspect * 2.0;
    uv *= mix(1.7, 0.3, PARAM_5);
    float t    = float(frame) / 60.0;
    float freq = mix(5.0, 40.0, PARAM_3);
    float warp = mix(0.3, 2.5, PARAM_2);

    // abs()+epsilon avoids pow() of a negative base going NaN below the centreline.
    vec3 col = vec3(0.0);
    col.r = PARAM_1 * sign(sin(freq * uv.x - t + sin(freq * pow(abs(uv.y) + 0.001, warp) - t)));
    col.b = sign(cos(freq * uv.x * 0.6 - 2.0 * t + 5.0 * PARAM_1 * sin(freq * uv.y - 2.0 * t)));

    col = col * 0.5 + 0.5;
    col = hueShift(col, PARAM_4 * 6.28318);
    return vec4(clamp(col, 0.0, 1.0), 1.0);
}
