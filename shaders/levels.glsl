//!DESC levels — five-point tone curve
//!HOOK MAIN
//!BIND HOOKED

#define PARAM_1 0.0    /* blacks     */
#define PARAM_2 0.25   /* shadows    */
#define PARAM_3 0.5    /* midtones   */
#define PARAM_4 0.75   /* highlights */
#define PARAM_5 1.0    /* whites     */

// Five control points spaced evenly across the input range, joined by a
// Catmull-Rom spline and applied to each channel independently. Defaults form
// the identity ramp (0, .25, .5, .75, 1) so the shader is a no-op until a
// point is moved. Raising blacks lifts the shadows, lowering whites crushes
// the highlights, and the midtones point is the usual gamma-style bend.
float curve(float x) {
    float pts[5];
    pts[0] = PARAM_1; pts[1] = PARAM_2; pts[2] = PARAM_3;
    pts[3] = PARAM_4; pts[4] = PARAM_5;

    float t = clamp(x, 0.0, 1.0) * 4.0;   // 4 segments between 5 points
    int   i = min(int(t), 3);
    float u = t - float(i);

    // End segments have no outer neighbour: mirror one so the tangent stays
    // continuous instead of flattening at the ends.
    float p0 = i > 0 ? pts[i - 1] : 2.0 * pts[0] - pts[1];
    float p1 = pts[i];
    float p2 = pts[i + 1];
    float p3 = i < 3 ? pts[i + 2] : 2.0 * pts[i + 1] - pts[i];

    return clamp(0.5 * ((2.0 * p1)
                        + (-p0 + p2) * u
                        + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * u * u
                        + (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * u * u * u),
                 0.0, 1.0);
}

vec4 hook() {
    vec4 col = textureLod(HOOKED_raw, HOOKED_pos, 0.0);
    return vec4(curve(col.r), curve(col.g), curve(col.b), col.a);
}
