//!DESC zoom-fx — zoom and pulse about a configurable point
//!HOOK MAIN
//!BIND HOOKED

#define PARAM_1 0.5    /* zoom     */
#define PARAM_2 0.5    /* centre X */
#define PARAM_3 0.5    /* centre Y */
#define PARAM_4 0.0    /* pulse    */

vec4 hook() {
    vec2  c     = vec2(PARAM_2, PARAM_3);
    float pulse = PARAM_4 * sin(float(frame) / 20.0) * 0.08;
    float zoom  = mix(0.25, 3.0, PARAM_1) + pulse;
    vec2  uv    = (HOOKED_pos - c) / zoom + c;
    return textureLod(HOOKED_raw, clamp(uv, 0.0, 1.0), 0.0);
}
